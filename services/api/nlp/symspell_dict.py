"""S3: обёртка над symspellpy для исправления опечаток (edit distance <= 2).

Пороги берутся из `NlpConfig.spelling` (`max_edit_distance`, `prefix_length`,
`min_token_len_edit1`, `min_token_len_edit2`). Поддерживает:
  * загрузку из `index_state.symspell` (объект SymSpell или словарь {term: freq});
  * дообогащение/fallback-построение из `Lexicon.word_freq` (работает при `is_loaded == False`);
  * безопасный pure-Python fallback, если `symspellpy` не установлен.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from services.api.nlp.config import NlpConfig, basic_norm, get_nlp_config
from services.api.nlp.lexicon import Lexicon, get_lexicon
from services.api.nlp.translit import levenshtein

log = logging.getLogger("smart_search.nlp.symspell")

try:
    from symspellpy import SymSpell as _SymSpellLib
    from symspellpy import Verbosity as _Verbosity
except ImportError:  # pragma: no cover
    _SymSpellLib = None  # type: ignore[assignment]
    _Verbosity = None  # type: ignore[assignment]


@dataclass(frozen=True)
class SpellSuggestion:
    term: str
    distance: int
    freq: int
    confidence: float


class SymSpellDict:
    def __init__(self, cfg: NlpConfig, engine: Any = None, fallback_vocab: Optional[Mapping[str, int]] = None) -> None:
        self._cfg = cfg
        self._engine = engine
        self._vocab: Dict[str, int] = dict(fallback_vocab or {})

    @property
    def size(self) -> int:
        if self._engine is not None and hasattr(self._engine, "words"):
            return len(self._engine.words)
        return len(self._vocab)

    @classmethod
    def from_sources(cls, cfg: Optional[NlpConfig] = None, lexicon: Optional[Lexicon] = None, index_state: Any = None) -> "SymSpellDict":
        cfg = cfg or get_nlp_config()
        sp_cfg = cfg.spelling
        max_ed = max(0, min(2, int(sp_cfg.max_edit_distance)))
        prefix_len = max(3, int(sp_cfg.prefix_length))

        vocab: Dict[str, int] = {}
        if lexicon is not None:
            for term, freq in lexicon.word_freq.items():
                t = basic_norm(term)
                if len(t) >= 2 and " " not in t and not any(ch.isdigit() for ch in t):
                    vocab[t] = max(vocab.get(t, 0), int(freq))

        raw_sym = getattr(index_state, "symspell", None) if getattr(index_state, "is_loaded", False) else None
        engine: Any = None

        if _SymSpellLib is not None:
            if isinstance(raw_sym, _SymSpellLib):
                engine = raw_sym
                for term, freq in vocab.items():
                    if len(term) >= 2:
                        engine.create_dictionary_entry(term, freq)
            else:
                engine = _SymSpellLib(max_dictionary_edit_distance=max_ed, prefix_length=prefix_len)
                if isinstance(raw_sym, Mapping):
                    raw_words = raw_sym.get("words") if "words" in raw_sym and isinstance(raw_sym["words"], Mapping) else raw_sym
                    for term, freq in raw_words.items():
                        t = basic_norm(str(term))
                        if len(t) >= 2 and " " not in t and not any(ch.isdigit() for ch in t):
                            try:
                                f = max(1, int(freq))
                            except (TypeError, ValueError):
                                f = 1
                            vocab[t] = max(vocab.get(t, 0), f)
                for term, freq in vocab.items():
                    engine.create_dictionary_entry(term, freq)
        else:
            log.warning("symspellpy unavailable, using pure-Python bounded Levenshtein fallback")
            if isinstance(raw_sym, Mapping):
                raw_words = raw_sym.get("words") if "words" in raw_sym and isinstance(raw_sym["words"], Mapping) else raw_sym
                for term, freq in raw_words.items():
                    t = basic_norm(str(term))
                    if len(t) >= 2 and " " not in t and not any(ch.isdigit() for ch in t):
                        vocab[t] = max(vocab.get(t, 0), int(freq) if isinstance(freq, (int, float)) else 1)

        return cls(cfg=cfg, engine=engine, fallback_vocab=vocab)

    def allowed_edit_distance(self, token: str) -> int:
        sp = self._cfg.spelling
        n = len(token)
        if n < sp.min_token_len_edit1:
            return 0
        if n < sp.min_token_len_edit2:
            return min(1, sp.max_edit_distance)
        return min(2, sp.max_edit_distance)

    def _score(self, token: str, term: str, distance: int, freq: int) -> float:
        if distance <= 0:
            return 1.0
        base = 0.88 if distance == 1 else 0.72
        if token and term and token[0] != term[0]:
            base -= 0.06
        if freq >= 800:
            base += 0.04
        elif freq < 100:
            base -= 0.04
        return round(max(0.1, min(0.96, base)), 4)

    def lookup_token(self, token: str, *, max_edit_distance: Optional[int] = None, limit: int = 5) -> List[SpellSuggestion]:
        tok = basic_norm(token)
        if not tok or " " in tok or any(ch.isdigit() for ch in tok):
            return []
        allowed = self.allowed_edit_distance(tok)
        if max_edit_distance is not None:
            allowed = min(allowed, int(max_edit_distance))
        if allowed <= 0:
            return []

        raw_hits: List[Tuple[str, int, int]] = []
        if self._engine is not None and _Verbosity is not None:
            try:
                suggestions = self._engine.lookup(tok, _Verbosity.CLOSEST, max_edit_distance=allowed, include_unknown=False)
                for s in suggestions:
                    if s.term != tok and int(s.distance) > 0:
                        raw_hits.append((str(s.term), int(s.distance), int(s.count)))
            except Exception as exc:  # noqa: BLE001
                log.warning("symspell lookup failed for %r: %s", tok, exc)
        else:
            for term, freq in self._vocab.items():
                if term == tok or abs(len(term) - len(tok)) > allowed:
                    continue
                d = levenshtein(tok, term, allowed)
                if 0 < d <= allowed:
                    raw_hits.append((term, d, freq))

        out = [SpellSuggestion(term=t, distance=d, freq=f, confidence=self._score(tok, t, d, f)) for t, d, f in raw_hits]
        out.sort(key=lambda s: (s.distance, -s.confidence, -s.freq, s.term))
        return out[:limit]


_sym_lock = threading.Lock()
_sym_key: Optional[Tuple[Any, ...]] = None
_sym_val: Optional[SymSpellDict] = None


def get_symspell_dict(
    cfg: Optional[NlpConfig] = None,
    lexicon: Optional[Lexicon] = None,
    index_state: Any = None,
    overlay: Any = None,
) -> SymSpellDict:
    global _sym_key, _sym_val
    cfg = cfg or get_nlp_config()
    lexicon = lexicon or get_lexicon(cfg, index_state, overlay)
    key = (id(cfg), lexicon.index_version, lexicon.is_index_loaded, lexicon.config_version)
    with _sym_lock:
        if _sym_val is not None and _sym_key == key:
            return _sym_val
    built = SymSpellDict.from_sources(cfg, lexicon, index_state)
    with _sym_lock:
        _sym_key, _sym_val = key, built
    return built