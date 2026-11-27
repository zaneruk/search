"""S1: двусторонний конвертер раскладки клавиатуры EN <-> RU («ghbdtn» -> «привет»).

Конвертация по таблице ЙЦУКЕН <-> QWERTY сама по себе небезопасна («astra» -> «фыекф»), поэтому
кандидат принимается только если:
  * исходный токен НЕ известен (не бренд/сущность/термин глоссария/слово словаря),
  * результат подтверждён: записью `layout_confusions` глоссария, известным термином (бренды, сущности,
    глоссарий — через предикат `is_known`) или, для латиница -> кириллица, словарём pymorphy3.
Так валидные латинские бренды («astra», «anydesk», «max») не ломаются.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from services.api.nlp.config import NlpConfig, basic_norm, get_nlp_config
from services.api.nlp.tokenize import is_known_ru_word

_EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбюё"
EN_TO_RU = dict(zip(_EN_KEYS, _RU_KEYS))
RU_TO_EN = dict(zip(_RU_KEYS, _EN_KEYS))
_VOWELS = set("аеёиоуыэюяaeiouy")

MIN_LEN_KNOWN = 2
MIN_LEN_DICT = 3
MIN_LEN_DICT_FULL_CONFIDENCE = 4


def convert_en_to_ru(text: str) -> str:
    return basic_norm("".join(EN_TO_RU.get(ch, ch) for ch in (text or "").lower()))


def convert_ru_to_en(text: str) -> str:
    return "".join(RU_TO_EN.get(ch, ch) for ch in (text or "").lower().replace("ё", "е"))


def looks_mistyped(token: str) -> bool:
    """Эвристика для трассы: токен из 4+ букв без гласных выглядит как набранный не в той раскладке."""
    letters = [c for c in token.lower() if c.isalpha()]
    return len(letters) >= 4 and not any(c in _VOWELS for c in letters)


@dataclass(frozen=True)
class LayoutCandidate:
    original: str
    converted: str
    direction: str          # en2ru | ru2en
    confidence: float
    reason: str             # glossary | known_term | dictionary


@dataclass(frozen=True)
class LayoutResult:
    tokens: Tuple[str, ...]                    # токены после применения кандидатов
    candidates: Tuple[Tuple[int, LayoutCandidate], ...] = ()   # (индекс токена, кандидат)

    @property
    def changed(self) -> bool:
        return bool(self.candidates)

    @property
    def text(self) -> str:
        return " ".join(self.tokens)

    @property
    def confidence(self) -> float:
        return min((c.confidence for _, c in self.candidates), default=0.0)


class LayoutFixer:
    def __init__(self, cfg: Optional[NlpConfig] = None, is_known: Optional[Callable[[str], bool]] = None) -> None:
        """`is_known(term)` — предикат известных терминов (бренды, сущности, глоссарий, словарь индекса)."""
        self._cfg = cfg or get_nlp_config()
        self._is_known = is_known or (lambda term: False)

    def _known(self, term: str) -> bool:
        return bool(self._cfg.glossary_entries(term)) or self._is_known(term)

    def candidate_for_token(self, token: str) -> Optional[LayoutCandidate]:
        if not self._cfg.spelling.layout_fix_enabled:
            return None
        tok = basic_norm(token)
        if len(tok) < MIN_LEN_KNOWN or any(ch.isdigit() for ch in tok):
            return None
        if self._known(tok) or is_known_ru_word(tok):
            return None  # валидный токен: не трогаем
        priors = self._cfg.priors
        if all(ch in EN_TO_RU for ch in tok) and any(ch.isalpha() for ch in tok):
            direction, converted = "en2ru", convert_en_to_ru(tok)
        elif all(ch in RU_TO_EN for ch in tok):
            direction, converted = "ru2en", convert_ru_to_en(tok)
        else:
            return None
        if converted == tok:
            return None
        entry = self._cfg.layout_confusions.get(tok)
        if entry is not None and entry.canonical:
            return LayoutCandidate(tok, entry.canonical[0], direction, priors.layout_glossary, "glossary")
        if self._known(converted):
            return LayoutCandidate(tok, converted, direction, priors.layout_known, "known_term")
        if direction == "en2ru" and len(tok) >= MIN_LEN_DICT and is_known_ru_word(converted):
            conf = priors.layout_dict if len(tok) >= MIN_LEN_DICT_FULL_CONFIDENCE else priors.layout_dict_short
            return LayoutCandidate(tok, converted, direction, conf, "dictionary")
        return None

    def fix_tokens(self, tokens: Sequence[str]) -> LayoutResult:
        out: List[str] = []
        found: List[Tuple[int, LayoutCandidate]] = []
        for i, tok in enumerate(tokens):
            cand = self.candidate_for_token(tok)
            if cand is None:
                out.append(tok)
            else:
                out.append(cand.converted)
                found.append((i, cand))
        return LayoutResult(tokens=tuple(out), candidates=tuple(found))
