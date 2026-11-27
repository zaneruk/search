"""S1: нормализация запроса (B.3 query_norm).

NFKC, нижний регистр, ё->е, нестандартные тире и кавычки -> ASCII, исправление латинских/кириллических
гомоглифов ТОЛЬКО внутри токена со смешанным алфавитом, вынос шумовых слов в `noise_tokens`
(если запрос целиком состоит из шума, он сохраняется как есть), дефисные слова в цельной и раздельной
формах, защита версий («5.0», «v5.0») и числовых токенов («365»).

PII маскируется ранее (S0, `core.events.mask_pii`); здесь маскирование не выполняется.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from services.api.nlp.config import NlpConfig, get_nlp_config

_ZERO_WIDTH = {ord(c): None for c in "\u200b\u200c\u200d\u2060\ufeff\u00ad"}
_DASHES = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"}
_DQUOTES = {ord(c): '"' for c in "\u201c\u201d\u201e\u201f\u00ab\u00bb\u2039\u203a\uff02"}
_SQUOTES = {ord(c): "'" for c in "\u2018\u2019\u201a\u201b\u2032\u02bc`\u00b4"}
_PUNCT_TABLE = {**_DASHES, **_DQUOTES, **_SQUOTES}

# визуально совпадающие буквы (после приведения к нижнему регистру)
LAT_TO_CYR: Mapping[str, str] = {"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у", "k": "к", "m": "м", "h": "н", "t": "т", "b": "в"}
CYR_TO_LAT: Mapping[str, str] = {v: k for k, v in LAT_TO_CYR.items()}

_RUN_RE = re.compile(r"[a-zа-я0-9]+")
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
_SEPARATOR_RE = re.compile(r"[()\[\]{}<>|!?\"]|(?<!\d)[,;]|[,;](?!\d)|\s-\s|\s-+(?=\s)")
_EDGE_STRIP = ".,;:!?()[]{}\"'<>|\\/*-_=~^%@&"
_VERSION_RE = re.compile(r"^(?:v\d+(?:\.\d+)*|\d+(?:\.\d+)+)[a-z]?$")
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.U)


@dataclass(frozen=True)
class NormalizedQuery:
    raw: str
    text: str                                   # все токены, включая шумовые
    query_norm: str                             # без шумовых слов (если запрос не из одного шума)
    tokens: Tuple[str, ...]                     # токены query_norm
    noise_tokens: Tuple[str, ...] = ()
    noise_only: bool = False
    hyphen_forms: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    protected_tokens: Tuple[str, ...] = ()      # версии и токены с цифрами: не исправляются
    version_tokens: Tuple[str, ...] = ()
    uppercase_tokens: Tuple[str, ...] = ()      # слова, набранные ЗАГЛАВНЫМИ в смешанном по регистру запросе
    homoglyph_fixes: Tuple[Tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.tokens


def normalize_text(text: str) -> str:
    """Лёгкая нормализация строки (термины словарей, алиасы): без удаления шума и гомоглифов."""
    s = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH).lower().replace("ё", "е").translate(_PUNCT_TABLE)
    return " ".join(s.split())


def fix_homoglyphs(run: str) -> str:
    """Исправляет гомоглифы в слитной последовательности [a-zа-я0-9], если в ней смешаны алфавиты."""
    lat = [c for c in run if "a" <= c <= "z"]
    cyr = [c for c in run if "а" <= c <= "я"]
    if not lat or not cyr:
        return run
    can_to_cyr = all(c in LAT_TO_CYR for c in lat)
    can_to_lat = all(c in CYR_TO_LAT for c in cyr)
    if can_to_cyr and can_to_lat:
        to_cyr = len(cyr) >= len(lat)
    elif can_to_cyr:
        to_cyr = True
    elif can_to_lat:
        to_cyr = False
    else:
        return run
    table = LAT_TO_CYR if to_cyr else CYR_TO_LAT
    return "".join(table.get(c, c) for c in run)


def _uppercase_tokens(text: str) -> Tuple[str, ...]:
    words = _WORD_RE.findall(text)
    if not words or all(w == w.upper() for w in words):
        return ()
    found: List[str] = []
    for w in words:
        if w == w.upper() and w != w.lower() and len(w) <= 8:
            low = w.lower().replace("ё", "е")
            if low not in found:
                found.append(low)
    return tuple(found)


def _clean_token(tok: str) -> str:
    tok = tok.strip(_EDGE_STRIP)
    if tok.endswith("+") and tok[:-1].rstrip("+").isalpha():
        return tok
    return tok


def _match_noise(tokens: List[str], phrases: Tuple[Tuple[str, ...], ...]) -> Tuple[List[str], List[str]]:
    kept: List[str] = []
    noise: List[str] = []
    i = 0
    while i < len(tokens):
        for phrase in phrases:
            n = len(phrase)
            if n and tuple(tokens[i:i + n]) == phrase:
                noise.append(" ".join(phrase))
                i += n
                break
        else:
            kept.append(tokens[i])
            i += 1
    return kept, noise


def normalize_query(raw: str, cfg: Optional[NlpConfig] = None) -> NormalizedQuery:
    cfg = cfg or get_nlp_config()
    raw = raw or ""
    text = unicodedata.normalize("NFKC", raw).translate(_ZERO_WIDTH)
    upper = _uppercase_tokens(text)
    text = text.lower().replace("ё", "е").translate(_PUNCT_TABLE)
    text = _DECIMAL_COMMA_RE.sub(".", text)
    text = _SEPARATOR_RE.sub(" ", text)

    fixes: List[Tuple[str, str]] = []

    def _fix(match: "re.Match[str]") -> str:
        run = match.group(0)
        fixed = fix_homoglyphs(run)
        if fixed != run:
            fixes.append((run, fixed))
        return fixed

    text = _RUN_RE.sub(_fix, text)
    tokens = [t for t in (_clean_token(t) for t in text.split()) if t]
    all_text = " ".join(tokens)

    kept, noise = _match_noise(tokens, cfg.noise_phrases)
    noise_only = bool(tokens) and not kept
    if noise_only:  # защита от полного обнуления: запрос из одних шумовых слов сохраняется
        kept, noise = list(tokens), []

    hyphen_forms: Dict[str, Tuple[str, ...]] = {}
    for tok in kept:
        if "-" in tok and not _VERSION_RE.match(tok):
            parts = [p for p in tok.split("-") if p]
            if len(parts) >= 2:
                forms: List[str] = []
                for form in (tok, " ".join(parts), "".join(parts)):
                    if form not in forms:
                        forms.append(form)
                hyphen_forms[tok] = tuple(forms)

    protected = tuple(t for t in kept if any(ch.isdigit() for ch in t))
    versions = tuple(t for t in kept if _VERSION_RE.match(t))
    return NormalizedQuery(
        raw=raw,
        text=all_text,
        query_norm=" ".join(kept),
        tokens=tuple(kept),
        noise_tokens=tuple(noise),
        noise_only=noise_only,
        hyphen_forms=hyphen_forms,
        protected_tokens=protected,
        version_tokens=versions,
        uppercase_tokens=upper,
        homoglyph_fixes=tuple(fixes),
    )
