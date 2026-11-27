"""S2: токенизация с учётом версий и дефисных слов, кэшируемая лемматизация кириллицы, детект аббревиатур.

pymorphy3 подключается лениво; если пакет не установлен, лемма совпадает с формой слова
(`morph_available()` == False) и пайплайн продолжает работать.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Iterator, List, Optional, Tuple, Union

from services.api.nlp.config import NlpConfig, get_nlp_config
from services.api.nlp.normalize import NormalizedQuery, normalize_query

log = logging.getLogger("smart_search.nlp.tokenize")

LEMMA_CACHE_SIZE = 65536
MAX_WORD_LEN_FOR_MORPH = 64
_TOKEN_RE = re.compile(r"[^\W_]+(?:[-:/.][^\W_]+)*(?:\+\+|#)?", re.U)
_VERSION_RE = re.compile(r"^(?:v\d+(?:\.\d+)*|\d+(?:\.\d+)+)[a-z]?$")
_VOWELS = set("аеёиоуыэюяaeiouy")
_CYR_RE = re.compile(r"[а-я]")
_LAT_RE = re.compile(r"[a-z]")

_morph: Any = None
_morph_lock = threading.Lock()
_morph_failed = False


def _get_morph() -> Any:
    global _morph, _morph_failed
    if _morph is not None or _morph_failed:
        return _morph
    with _morph_lock:
        if _morph is None and not _morph_failed:
            try:
                import pymorphy3

                _morph = pymorphy3.MorphAnalyzer()
            except Exception as exc:  # noqa: BLE001 - нет пакета/словарей
                _morph_failed = True
                log.warning("pymorphy3 unavailable, lemmas equal surface forms: %s", exc)
    return _morph


def morph_available() -> bool:
    return _get_morph() is not None


@lru_cache(maxsize=LEMMA_CACHE_SIZE)
def lemmatize_word(word: str) -> str:
    """Лемма кириллического слова (ё->е); для остальных слов возвращает слово без изменений."""
    if not word or len(word) > MAX_WORD_LEN_FOR_MORPH or not _CYR_RE.search(word) or _LAT_RE.search(word) or any(c.isdigit() for c in word):
        return word
    morph = _get_morph()
    if morph is None:
        return word
    try:
        parses = morph.parse(word)
    except Exception:  # noqa: BLE001
        return word
    return parses[0].normal_form.replace("ё", "е") if parses else word


@lru_cache(maxsize=LEMMA_CACHE_SIZE)
def is_known_ru_word(word: str) -> bool:
    """Есть ли слово в словаре pymorphy3 (False, если pymorphy3 недоступен)."""
    if not word or len(word) > MAX_WORD_LEN_FOR_MORPH or not _CYR_RE.search(word) or _LAT_RE.search(word):
        return False
    morph = _get_morph()
    if morph is None:
        return False
    try:
        return bool(morph.word_is_known(word))
    except Exception:  # noqa: BLE001
        return False


def lemmatize_phrase(phrase: Union[str, Iterable[str]]) -> str:
    words = phrase.split() if isinstance(phrase, str) else list(phrase)
    return " ".join(lemma_of_token(w) for w in words)


def lemma_of_token(text: str) -> str:
    """Лемма токена; для составных (дефис, двоеточие, слеш, точка) лемматизируются части."""
    if any(sep in text for sep in "-:/."):
        parts = re.split(r"([-:/.])", text)
        return "".join(lemmatize_word(p) if i % 2 == 0 else p for i, p in enumerate(parts))
    return lemmatize_word(text)


# --------------------------------------------------------------------------- структуры
@dataclass(frozen=True)
class Token:
    text: str
    lemma: str
    kind: str                       # version | number | word | alnum
    start: int
    end: int
    parts: Tuple[str, ...] = ()
    is_latin: bool = False
    is_cyrillic: bool = False
    is_abbrev: bool = False

    @property
    def has_digit(self) -> bool:
        return any(c.isdigit() for c in self.text)


@dataclass(frozen=True)
class TokenizedQuery:
    text: str
    tokens: Tuple[Token, ...]

    @property
    def texts(self) -> Tuple[str, ...]:
        return tuple(t.text for t in self.tokens)

    @property
    def lemmas(self) -> Tuple[str, ...]:
        return tuple(t.lemma for t in self.tokens)

    @property
    def latin_words(self) -> Tuple[str, ...]:
        return tuple(t.text for t in self.tokens if t.is_latin and t.kind == "word")

    @property
    def abbreviations(self) -> Tuple[str, ...]:
        return tuple(t.text for t in self.tokens if t.is_abbrev)

    @property
    def versions(self) -> Tuple[str, ...]:
        return tuple(t.text for t in self.tokens if t.kind == "version")

    @property
    def numbers(self) -> Tuple[str, ...]:
        return tuple(t.text for t in self.tokens if t.kind == "number")

    def __len__(self) -> int:
        return len(self.tokens)

    def ngrams(self, max_n: int) -> Iterator[Tuple[int, int, str, str]]:
        """(start, end, форма, лемма) для всех окон длиной 1..max_n (end не включается)."""
        n_tokens = len(self.tokens)
        for i in range(n_tokens):
            for j in range(i + 1, min(n_tokens, i + max_n) + 1):
                window = self.tokens[i:j]
                yield i, j, " ".join(t.text for t in window), " ".join(t.lemma for t in window)


def _is_abbrev(text: str, cfg: NlpConfig, uppercase: Tuple[str, ...]) -> bool:
    if text in cfg.abbreviations and cfg.abbreviations[text].match == "token":
        return True
    letters = [c for c in text if c.isalpha()]
    if not letters or len(letters) != len(text):
        return False
    if text in uppercase and len(text) <= 6:
        return True
    return 2 <= len(text) <= 5 and not any(c in _VOWELS for c in text)


def tokenize(query: Union[str, NormalizedQuery], cfg: Optional[NlpConfig] = None) -> TokenizedQuery:
    """Токенизирует нормализованный запрос (строка либо результат `normalize_query`)."""
    cfg = cfg or get_nlp_config()
    nq = query if isinstance(query, NormalizedQuery) else normalize_query(query, cfg)
    text = nq.query_norm
    tokens: List[Token] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        has_lat = bool(_LAT_RE.search(tok))
        has_cyr = bool(_CYR_RE.search(tok))
        if _VERSION_RE.match(tok):
            kind = "version"
        elif tok.isdigit():
            kind = "number"
        elif any(c.isdigit() for c in tok):
            kind = "alnum"
        else:
            kind = "word"
        parts = tuple(p for p in re.split(r"[-:/.]", tok) if p) if kind in ("word", "alnum") and re.search(r"[-:/.]", tok) else ()
        tokens.append(Token(
            text=tok,
            lemma=tok if kind in ("version", "number") else lemma_of_token(tok),
            kind=kind,
            start=m.start(),
            end=m.end(),
            parts=parts,
            is_latin=has_lat and not has_cyr,
            is_cyrillic=has_cyr and not has_lat,
            is_abbrev=kind == "word" and _is_abbrev(tok, cfg, nq.uppercase_tokens),
        ))
    return TokenizedQuery(text=text, tokens=tuple(tokens))
