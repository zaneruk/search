"""Типизированная загрузка конфигов NLP-пакета: config/search.yaml, taxonomy.yaml, seed_glossary.yaml.

Все пороги (spelling.*, qu.*), шумовые слова, сленг, аббревиатуры, транслит, числовые алиасы,
маркеры атрибутов и таксономия читаются отсюда. Каталог конфигов: env `SMART_SEARCH_CONFIG_DIR`,
затем `./config`, затем `<корень репозитория>/config`.

Уровни уверенности источников гипотез/правил (`Priors`) заданы значениями по умолчанию и могут быть
переопределены в search.yaml секцией (необязательной):

    nlp:
      max_hypotheses: 8
      priors: {glossary_exact: 0.95, alias_verified: 0.97, ...}

R6: файл не содержит и не читает ни одной gold-специфичной строки.
"""
from __future__ import annotations

import logging
import os
import threading
import unicodedata
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

log = logging.getLogger("smart_search.nlp.config")

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
_DASH_TABLE = {ord(c): "-" for c in _DASHES}

DEFAULT_MODIFIER_MARKERS: Mapping[str, Tuple[str, ...]] = {
    "free": ("бесплатно", "бесплатный", "бесплатная", "free", "даром"),
    "trial": ("триал", "trial", "пробная", "пробный", "демо", "демоверсия"),
}


def basic_norm(text: str) -> str:
    """NFKC, нижний регистр, ё->е, тире -> '-', схлопывание пробелов."""
    s = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е").translate(_DASH_TABLE)
    return " ".join(s.split())


def phrase_tokens(text: str) -> Tuple[str, ...]:
    return tuple(basic_norm(text).split())


# --------------------------------------------------------------------------- структуры
@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    canonical: Tuple[str, ...]
    product_types: Tuple[str, ...] = ()
    source: str = "slang"            # slang | abbreviation | translit | numeric | layout
    match: str = "token"             # token | exact_query
    confidence: Optional[float] = None
    requires_any: Tuple[str, ...] = ()
    kind: Optional[str] = None


@dataclass(frozen=True)
class TypeInfo:
    type_id: str
    title_ru: str
    product_class: str
    group: str = ""
    segments: Tuple[str, ...] = ()
    head_nouns: Tuple[str, ...] = ()
    complements: Tuple[str, ...] = ()
    upsell_to: Tuple[str, ...] = ()
    analog_of: Tuple[str, ...] = ()


@dataclass(frozen=True)
class HeadPrior:
    head: str
    default_product_type: str
    class_probs: Mapping[str, float]


@dataclass(frozen=True)
class SpellingConfig:
    max_edit_distance: int = 2
    prefix_length: int = 7
    min_token_len_edit1: int = 4
    min_token_len_edit2: int = 8
    accept_confidence: float = 0.85
    protect_exact_name_and_brand: bool = True
    max_corrected_tokens: int = 2
    transliteration_enabled: bool = True
    layout_fix_enabled: bool = True


@dataclass(frozen=True)
class QuConfig:
    qu_confidence_threshold: float = 0.65
    llm_timeout_ms: int = 1500
    escalate_on_low_confidence: bool = True
    escalate_on_zero_results: bool = True
    escalate_min_tokens_for_nl: int = 5
    max_candidate_entities: int = 15
    max_tokens_out: int = 150
    retry_on_invalid_json: int = 1
    cache_ttl_seconds: int = 86400


@dataclass(frozen=True)
class Priors:
    glossary_exact: float = 0.95
    alias_verified: float = 0.97
    alias_seed: float = 0.95
    alias_auto: float = 0.90
    synonym: float = 0.90
    glue_alias: float = 0.90
    glue_vocab: float = 0.75
    split_vocab: float = 0.70
    layout_glossary: float = 0.95
    layout_known: float = 0.90
    layout_dict: float = 0.80
    layout_dict_short: float = 0.65
    translit_glossary: float = 0.95
    translit_phonetic: float = 0.85
    translit_skeleton: float = 0.70
    qu_brand_hit: float = 0.95
    qu_type_head: float = 0.90
    qu_type_glossary: float = 0.85
    qu_attr_only: float = 0.30
    max_hypotheses: int = 8


@dataclass(frozen=True, eq=False)
class NlpConfig:
    spelling: SpellingConfig
    qu: QuConfig
    priors: Priors
    noise_phrases: Tuple[Tuple[str, ...], ...]
    slang: Mapping[str, GlossaryEntry]
    abbreviations: Mapping[str, GlossaryEntry]
    translit: Mapping[str, GlossaryEntry]
    layout_confusions: Mapping[str, GlossaryEntry]
    numeric_aliases: Mapping[str, Tuple[GlossaryEntry, ...]]
    synonym_groups: Tuple[Tuple[str, ...], ...]
    markers: Mapping[str, Any]
    product_classes: Tuple[str, ...]
    types: Mapping[str, TypeInfo]
    head_noun_priors: Mapping[str, HeadPrior]
    head_noun_to_types: Mapping[str, Tuple[str, ...]]
    class_titles: Mapping[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ загрузка
    @classmethod
    def from_dicts(cls, search: Mapping[str, Any], taxonomy: Mapping[str, Any], glossary: Mapping[str, Any]) -> "NlpConfig":
        spelling = _dataclass_from(SpellingConfig, _merge(search.get("spelling", {}).get("symspell", {}), search.get("spelling", {}).get("autocorrect", {}), {
            "transliteration_enabled": search.get("spelling", {}).get("transliteration_enabled", True),
            "layout_fix_enabled": search.get("spelling", {}).get("layout_fix_enabled", True),
        }))
        qu_raw = dict(search.get("qu", {}))
        qu_raw["cache_ttl_seconds"] = (qu_raw.get("cache") or {}).get("ttl_seconds", QuConfig.cache_ttl_seconds)
        qu = _dataclass_from(QuConfig, qu_raw)
        nlp_raw = search.get("nlp", {}) or {}
        priors_raw = dict(nlp_raw.get("priors", {}) or {})
        if "max_hypotheses" in nlp_raw:
            priors_raw["max_hypotheses"] = nlp_raw["max_hypotheses"]
        priors = _dataclass_from(Priors, priors_raw)

        noise = sorted({phrase_tokens(t) for t in glossary.get("noise_tokens", []) if basic_norm(str(t))}, key=lambda p: (-len(p), p))

        def entries(key: str, source: str) -> List[GlossaryEntry]:
            out: List[GlossaryEntry] = []
            for raw in glossary.get(key, []) or []:
                term = basic_norm(str(raw.get("term", "")))
                if not term:
                    continue
                out.append(GlossaryEntry(
                    term=term,
                    canonical=tuple(basic_norm(str(c)) for c in raw.get("canonical", []) if basic_norm(str(c))),
                    product_types=tuple(str(t) for t in raw.get("product_types", []) or []),
                    source=source,
                    match=str(raw.get("match", "token")),
                    confidence=float(raw["confidence"]) if raw.get("confidence") is not None else None,
                    requires_any=tuple(basic_norm(str(r)) for r in raw.get("requires_any", []) or []),
                    kind=raw.get("kind"),
                ))
            return out

        numeric: Dict[str, List[GlossaryEntry]] = {}
        for e in entries("numeric_aliases", "numeric"):
            numeric.setdefault(e.term, []).append(e)

        classes_raw = taxonomy.get("product_classes", {}) or {}
        types: Dict[str, TypeInfo] = {}
        head_to_types: Dict[str, List[str]] = {}
        for type_id, raw in (taxonomy.get("product_types", {}) or {}).items():
            info = TypeInfo(
                type_id=str(type_id),
                title_ru=str(raw.get("title_ru", type_id)),
                product_class=str(raw.get("product_class", "other")),
                group=str(raw.get("group", "")),
                segments=tuple(raw.get("segments", []) or []),
                head_nouns=tuple(basic_norm(str(h)) for h in raw.get("head_nouns", []) or []),
                complements=tuple(raw.get("complements", []) or []),
                upsell_to=tuple(raw.get("upsell_to", []) or []),
                analog_of=tuple(raw.get("analog_of", []) or []),
            )
            types[info.type_id] = info
            for head in info.head_nouns:
                head_to_types.setdefault(head, []).append(info.type_id)

        priors_map: Dict[str, HeadPrior] = {}
        for head, raw in (taxonomy.get("head_noun_priors", {}) or {}).items():
            h = basic_norm(str(head))
            priors_map[h] = HeadPrior(h, str(raw.get("default_product_type", "")), {str(k): float(v) for k, v in (raw.get("class_probs", {}) or {}).items()})
            if priors_map[h].default_product_type:
                lst = head_to_types.setdefault(h, [])
                if priors_map[h].default_product_type not in lst:
                    lst.append(priors_map[h].default_product_type)

        groups = tuple(tuple(basic_norm(str(m)) for m in g if basic_norm(str(m))) for g in glossary.get("synonym_groups", []) or [])
        return cls(
            spelling=spelling, qu=qu, priors=priors,
            noise_phrases=tuple(noise),
            slang={e.term: e for e in entries("slang", "slang")},
            abbreviations={e.term: e for e in entries("abbreviations", "abbreviation")},
            translit={e.term: e for e in entries("translit", "translit")},
            layout_confusions={e.term: e for e in entries("layout_confusions", "layout")},
            numeric_aliases={k: tuple(v) for k, v in numeric.items()},
            synonym_groups=groups,
            markers=dict(glossary.get("markers", {}) or {}),
            product_classes=tuple(str(k) for k in classes_raw.keys()),
            types=types,
            head_noun_priors=priors_map,
            head_noun_to_types={k: tuple(v) for k, v in head_to_types.items()},
            class_titles={str(k): str((v or {}).get("section_title_ru", k)) for k, v in classes_raw.items()},
        )

    @classmethod
    def load(cls, config_dir: Optional[Union[str, Path]] = None) -> "NlpConfig":
        base = find_config_dir(config_dir)
        return cls.from_dicts(_read_yaml(base / "search.yaml"), _read_yaml(base / "taxonomy.yaml"), _read_yaml(base / "seed_glossary.yaml"))

    # ------------------------------------------------------------------ удобные выборки
    def glossary_entries(self, term: str) -> List[GlossaryEntry]:
        """Все записи глоссария (сленг, аббревиатуры, транслит) для термина."""
        out: List[GlossaryEntry] = []
        for table in (self.slang, self.abbreviations, self.translit):
            if term in table:
                out.append(table[term])
        return out

    def glossary_terms(self) -> List[str]:
        return sorted({*self.slang, *self.abbreviations, *self.translit})

    def marker_phrases(self, *path: str) -> List[Tuple[str, ...]]:
        """Фразы маркера по пути в `markers` (например, ('relational_intent', 'analog_of'))."""
        node: Any = self.markers
        for key in path:
            node = node.get(key, {}) if isinstance(node, Mapping) else {}
        if isinstance(node, (list, tuple)):
            return [phrase_tokens(str(p)) for p in node]
        return []

    def marker_groups(self, *path: str) -> Dict[str, List[Tuple[str, ...]]]:
        """Группы маркеров вида {метка: [фразы]} по пути в `markers`."""
        node: Any = self.markers
        for key in path:
            node = node.get(key, {}) if isinstance(node, Mapping) else {}
        out: Dict[str, List[Tuple[str, ...]]] = {}
        if isinstance(node, Mapping):
            for label, phrases in node.items():
                if isinstance(phrases, (list, tuple)):
                    out[str(label)] = [phrase_tokens(str(p)) for p in phrases]
        return out

    def modifier_markers(self) -> Dict[str, List[Tuple[str, ...]]]:
        groups = self.marker_groups("modifiers")
        for label, words in DEFAULT_MODIFIER_MARKERS.items():
            groups.setdefault(label, [phrase_tokens(w) for w in words])
        return groups


# --------------------------------------------------------------------------- вспомогательное
def _merge(*dicts: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for d in dicts:
        out.update(d or {})
    return out


def _dataclass_from(cls: Any, raw: Mapping[str, Any]) -> Any:
    names = {f.name: f for f in fields(cls)}
    kwargs: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key in names and value is not None:
            default = names[key].default
            try:
                kwargs[key] = type(default)(value) if isinstance(default, (bool, int, float)) and not isinstance(default, str) else value
            except (TypeError, ValueError):
                log.warning("invalid config value %s=%r, default used", key, value)
    return cls(**kwargs)


def _read_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def find_config_dir(explicit: Optional[Union[str, Path]] = None) -> Path:
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.getenv("SMART_SEARCH_CONFIG_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(Path("config"))
    candidates.append(Path(__file__).resolve().parents[3] / "config")
    for cand in candidates:
        if (cand / "search.yaml").is_file() and (cand / "taxonomy.yaml").is_file() and (cand / "seed_glossary.yaml").is_file():
            return cand
    raise FileNotFoundError("config dir with search.yaml, taxonomy.yaml, seed_glossary.yaml not found; tried: " + ", ".join(str(c) for c in candidates))


_config: Optional[NlpConfig] = None
_config_lock = threading.Lock()


def get_nlp_config(*, reload: bool = False) -> NlpConfig:
    global _config
    with _config_lock:
        if _config is None or reload:
            _config = NlpConfig.load()
        return _config


def set_nlp_config(cfg: Optional[NlpConfig]) -> None:
    """Подмена конфига (тесты, hot-reload)."""
    global _config
    with _config_lock:
        _config = cfg
