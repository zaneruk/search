"""S3: детерминированные фонетические ключи и транслитерация RU <-> EN.

Два механизма:

1. `phonetic_keys(word)` / `skeleton_keys(word)` — общий фонетический ключ для обоих алфавитов:
   «кибер» и «cyber» -> {kabar, sabar}; «бэкап» и «backup» -> bakap; «мдаемон» и «mdaemon» -> mdaman;
   «криптопро» и «cryptopro» -> kraptapr. Гласные схлопнуты в класс `a`, цифры/знаки отброшены,
   на конце слова гласная и звонкость согласной не различаются. `TranslitIndex` по такому ключу находит
   термин другого алфавита среди известного словаря (бренды, сущности, глоссарий).
2. `ru_to_lat` / `lat_to_ru` (+ `*_variants`) — детерминированная побуквенная транслитерация
   с ограниченным числом вариантов для неоднозначных букв.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

_CYR_RE = re.compile(r"[а-я]")
_LAT_RE = re.compile(r"[a-z]")
_STRIP_RE = re.compile(r"[^a-zа-яё]+")

_VOICED_TO_VOICELESS = {"b": "p", "v": "f", "g": "k", "d": "t", "z": "s", "Z": "S"}
_LAT_VOWELS = set("aeiou")
_CYR_VOWELS = set("аеёиоуыэюя")
_FRONT = set("eiy")

_CYR_CONSONANTS = {
    "б": "b", "в": "v", "г": "g", "д": "d", "ж": "Z", "з": "z", "й": "j", "к": "k", "л": "l", "м": "m",
    "н": "n", "п": "p", "р": "r", "с": "s", "т": "t", "ф": "f", "х": "h", "ц": "s", "ч": "C", "ш": "S", "щ": "S",
}
_LAT_DIGRAPHS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("tch", ("C",)), ("sch", ("S",)), ("ch", ("C",)), ("sh", ("S",)), ("zh", ("Z",)), ("ck", ("k",)),
    ("ph", ("f",)), ("th", ("t",)), ("kh", ("h",)), ("ts", ("s",)), ("tz", ("s",)), ("gh", ("g",)), ("qu", ("k", "v")),
)
_LAT_SINGLE = {
    "b": "b", "d": "d", "f": "f", "g": "g", "h": "h", "j": "Z", "k": "k", "l": "l", "m": "m", "n": "n",
    "p": "p", "q": "k", "r": "r", "s": "s", "t": "t", "v": "v", "w": "v", "z": "z",
}


def compact(term: str) -> str:
    """Слово без пробелов, дефисов, цифр и знаков (нижний регистр, ё->е)."""
    return _STRIP_RE.sub("", (term or "").lower().replace("ё", "е"))


def script_of(text: str) -> str:
    has_cyr, has_lat = bool(_CYR_RE.search(text)), bool(_LAT_RE.search(text))
    if has_cyr and not has_lat:
        return "cyr"
    if has_lat and not has_cyr:
        return "lat"
    return "mixed" if has_cyr and has_lat else "none"


# --------------------------------------------------------------------------- фонетические ключи
def _cyr_symbols(word: str) -> List[List[str]]:
    """Кириллица -> список позиций; каждая позиция — список альтернатив-символов."""
    out: List[List[str]] = []
    prev_vowel_or_start = True
    for ch in word:
        if ch in "ъь":
            prev_vowel_or_start = True
            continue
        if ch in _CYR_VOWELS:
            if ch in "яюё" and prev_vowel_or_start:
                out.append(["j"])
            out.append(["a"])
            prev_vowel_or_start = True
        elif ch in _CYR_CONSONANTS:
            out.append([_CYR_CONSONANTS[ch]])
            prev_vowel_or_start = False
    return out


def _lat_symbols(word: str) -> List[List[str]]:
    out: List[List[str]] = []
    i, n = 0, len(word)
    prev_vowel_or_start = True
    while i < n:
        matched = False
        for digraph, syms in _LAT_DIGRAPHS:
            if word.startswith(digraph, i):
                out.extend([[s] for s in syms])
                i += len(digraph)
                prev_vowel_or_start = False
                matched = True
                break
        if matched:
            continue
        ch = word[i]
        nxt = word[i + 1] if i + 1 < n else ""
        if ch in _LAT_VOWELS:
            out.append(["a"])
            prev_vowel_or_start = True
        elif ch == "y":
            if prev_vowel_or_start and nxt and nxt in _LAT_VOWELS:
                out.append(["j"])
                prev_vowel_or_start = False
            else:
                out.append(["a"])
                prev_vowel_or_start = True
        elif ch == "c":
            out.append(["s", "k"] if nxt in _FRONT and nxt else ["k"])
            prev_vowel_or_start = False
        elif ch == "x":
            out.extend([["k"], ["s"]])
            prev_vowel_or_start = False
        elif ch == "j":
            out.extend([["d"], ["Z"]])
            prev_vowel_or_start = False
        elif ch in _LAT_SINGLE:
            out.append([_LAT_SINGLE[ch]])
            prev_vowel_or_start = False
        i += 1
    return out


def _finalize(symbols: List[List[str]], limit: int) -> FrozenSet[str]:
    keys: List[List[str]] = [[]]
    for alts in symbols:
        keys = [k + [a] for k in keys for a in alts][:limit]
    result: Set[str] = set()
    for k in keys:
        collapsed: List[str] = []
        for s in k:
            if not collapsed or collapsed[-1] != s:
                collapsed.append(s)
        while collapsed and collapsed[-1] == "a":
            collapsed.pop()
        if collapsed:
            collapsed[-1] = _VOICED_TO_VOICELESS.get(collapsed[-1], collapsed[-1])
        result.add("".join(collapsed))
    return frozenset(r for r in result if r)


def phonetic_keys(term: str, limit: int = 8) -> FrozenSet[str]:
    """Фонетические ключи слова (кириллица или латиница); пустое множество для смешанных/нетекстовых строк."""
    word = compact(term)
    sc = script_of(word)
    if sc == "cyr":
        return _finalize(_cyr_symbols(word), limit)
    if sc == "lat":
        return _finalize(_lat_symbols(word), limit)
    return frozenset()


def skeleton_keys(term: str, limit: int = 8) -> FrozenSet[str]:
    """Согласный остов: фонетический ключ без гласных."""
    return frozenset(k.replace("a", "") for k in phonetic_keys(term, limit) if k.replace("a", ""))


def same_sound(a: str, b: str) -> bool:
    return bool(phonetic_keys(a) & phonetic_keys(b))


def levenshtein(a: str, b: str, max_distance: Optional[int] = None) -> int:
    """Расстояние Левенштейна; при превышении max_distance возвращает max_distance + 1."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if max_distance is not None and len(a) - len(b) > max_distance:
        return max_distance + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if max_distance is not None and min(cur) > max_distance:
            return max_distance + 1
        prev = cur
    return prev[-1]


# --------------------------------------------------------------------------- побуквенная транслитерация
_RU_TO_EN: Dict[str, Tuple[str, ...]] = {
    "а": ("a",), "б": ("b",), "в": ("v",), "г": ("g",), "д": ("d",), "е": ("e",), "ё": ("e", "yo"),
    "ж": ("zh", "j"), "з": ("z",), "и": ("i",), "й": ("y", "j", "i"), "к": ("k", "c"), "л": ("l",), "м": ("m",),
    "н": ("n",), "о": ("o",), "п": ("p",), "р": ("r",), "с": ("s", "c"), "т": ("t",), "у": ("u",), "ф": ("f",),
    "х": ("kh", "h"), "ц": ("ts", "c"), "ч": ("ch",), "ш": ("sh",), "щ": ("shch", "sch"), "ъ": ("",), "ы": ("y", "i"),
    "ь": ("",), "э": ("e",), "ю": ("yu", "iu"), "я": ("ya", "ia"),
}
_EN_DIGRAPHS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("sch", ("ш",)), ("tch", ("ч",)), ("sh", ("ш",)), ("ch", ("ч", "к")), ("zh", ("ж",)), ("ts", ("ц",)),
    ("kh", ("х",)), ("th", ("т", "з")), ("ph", ("ф",)), ("ck", ("к",)), ("ee", ("и",)), ("oo", ("у",)),
    ("ya", ("я",)), ("yu", ("ю",)), ("yo", ("ё", "йо")), ("ye", ("е",)),
)
_EN_SINGLE: Dict[str, Tuple[str, ...]] = {
    "a": ("а",), "b": ("б",), "c": ("к", "с"), "d": ("д",), "e": ("е", "э"), "f": ("ф",), "g": ("г",), "h": ("х",),
    "i": ("и",), "j": ("дж", "й"), "k": ("к",), "l": ("л",), "m": ("м",), "n": ("н",), "o": ("о",), "p": ("п",),
    "q": ("к",), "r": ("р",), "s": ("с",), "t": ("т",), "u": ("у", "а"), "v": ("в",), "w": ("в", "у"),
    "x": ("кс", "х"), "y": ("и", "ы", "й"), "z": ("з",),
}


def _expand(options_per_pos: Iterable[Tuple[str, ...]], limit: int) -> List[str]:
    results = [""]
    for options in options_per_pos:
        results = [r + o for r in results for o in options][:limit]
    seen: List[str] = []
    for r in results:
        if r and r not in seen:
            seen.append(r)
    return seen


def ru_to_lat_variants(text: str, limit: int = 6) -> List[str]:
    """Варианты латинской записи русского текста (пробелы и дефисы сохраняются)."""
    low = (text or "").lower()
    return _expand((_RU_TO_EN.get(ch, (ch,)) for ch in low), limit)


def lat_to_ru_variants(text: str, limit: int = 6) -> List[str]:
    """Варианты русской записи латинского текста (пробелы, дефисы и цифры сохраняются)."""
    low = (text or "").lower()
    positions: List[Tuple[str, ...]] = []
    i, n = 0, len(low)
    while i < n:
        for digraph, opts in _EN_DIGRAPHS:
            if low.startswith(digraph, i):
                positions.append(opts)
                i += len(digraph)
                break
        else:
            ch = low[i]
            nxt = low[i + 1] if i + 1 < n else ""
            if ch == "c" and nxt in _FRONT and nxt:
                positions.append(("с", "к", "ц"))
            else:
                positions.append(_EN_SINGLE.get(ch, (ch,)))
            i += 1
    return _expand(positions, limit)


def ru_to_lat(text: str) -> str:
    variants = ru_to_lat_variants(text, limit=1)
    return variants[0] if variants else ""


def lat_to_ru(text: str) -> str:
    variants = lat_to_ru_variants(text, limit=1)
    return variants[0] if variants else ""


# --------------------------------------------------------------------------- индекс словаря
@dataclass(frozen=True)
class TranslitMatch:
    term: str           # термин словаря другого алфавита
    kind: str           # phonetic | skeleton
    source_script: str


class TranslitIndex:
    """Индекс «фонетический ключ -> термины» по двум алфавитам для кросс-алфавитного поиска."""

    def __init__(self, min_len: int = 3) -> None:
        self.min_len = min_len
        self._full: Dict[str, Dict[str, Set[str]]] = {"cyr": {}, "lat": {}}
        self._skel: Dict[str, Dict[str, Set[str]]] = {"cyr": {}, "lat": {}}
        self._keys_of: Dict[Tuple[str, str], FrozenSet[str]] = {}

    def __len__(self) -> int:
        return len(self._keys_of)

    def add(self, term: str) -> None:
        word = compact(term)
        sc = script_of(word)
        if sc not in ("cyr", "lat") or len(word) < self.min_len:
            return
        keys = phonetic_keys(word)
        if not keys:
            return
        self._keys_of[(sc, term)] = keys
        for key in keys:
            self._full[sc].setdefault(key, set()).add(term)
            skel = key.replace("a", "")
            if len(skel) >= self.min_len:
                self._skel[sc].setdefault(skel, set()).add(term)

    def add_many(self, terms: Iterable[str]) -> None:
        for term in terms:
            self.add(term)

    def lookup(self, token: str, *, limit: int = 5) -> List[TranslitMatch]:
        """Термины словаря ДРУГОГО алфавита, звучащие как `token` (токен может быть склеенной фразой)."""
        word = compact(token)
        sc = script_of(word)
        if sc not in ("cyr", "lat") or len(word) < self.min_len:
            return []
        target = "lat" if sc == "cyr" else "cyr"
        keys = phonetic_keys(word)
        found: Dict[str, str] = {}
        for key in sorted(keys):
            for term in sorted(self._full[target].get(key, ())):
                found.setdefault(term, "phonetic")
        for key in sorted(keys):
            skel = key.replace("a", "")
            if len(skel) < self.min_len:
                continue
            for term in sorted(self._skel[target].get(skel, ())):
                if term in found:
                    continue
                term_keys = self._keys_of.get((target, term), frozenset())
                if any(levenshtein(k, tk, 1) <= 1 for k in keys for tk in term_keys):
                    found.setdefault(term, "skeleton")
        ordered = sorted(found.items(), key=lambda kv: (kv[1] != "phonetic", kv[0]))
        return [TranslitMatch(term, kind, sc) for term, kind in ordered[:limit]]
