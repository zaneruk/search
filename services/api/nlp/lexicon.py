"""Словарь времени выполнения для S3/S4: сущности, бренды, алиасы, термины глоссария и таксономии, overlay.

Собирается из `IndexState` (entities.parquet, families.parquet), `NlpConfig` и `OverlaySnapshot`.
Если индекс не загружен (`is_loaded == False`) словарь строится только из глоссария, таксономии и overlay —
пакет остаётся работоспособным. Строится один раз и кэшируется по (index_version, config_version).

Допустимые статусы алиасов сущностей: seed, verified, auto (остальные игнорируются).
Формат значений overlay-правил (терпимый): строки/списки строк под ключами
`to`, `canonical`, `canonical_name`, `terms`, `synonyms`, `aliases`, `targets`, `value`; `entity_id` для alias.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from services.api.nlp.config import NlpConfig, basic_norm, get_nlp_config
from services.api.nlp.tokenize import TokenizedQuery, lemma_of_token, lemmatize_phrase
from services.api.nlp.translit import TranslitIndex

log = logging.getLogger("smart_search.nlp.lexicon")

ALLOWED_ALIAS_STATUSES = frozenset({"seed", "verified", "auto"})
MAX_ALIAS_TOKENS = 6
MAX_POSTINGS = 200

# вес слов в словаре опечаток по источнику (используется как "частота" в SymSpell)
FREQ_BRAND = 1000
FREQ_OVERLAY = 900
FREQ_GLOSSARY = 800
FREQ_TAXONOMY = 600
FREQ_ENTITY_NAME = 500
FREQ_ENTITY_ALIAS = 300

_WORD_SPLIT_RE = re.compile(r"[^\W_]+(?:[-:/.][^\W_]+)*", re.U)
_PART_SPLIT_RE = re.compile(r"[-:/.]")
_KIND_ALIASES = {
    "brand": "brand", "vendor": "brand", "manufacturer": "brand",
    "product_type": "product_type", "type": "product_type", "ptype": "product_type",
    "product_line": "product_line", "line": "product_line", "product": "product_line", "family": "product_line",
    "category": "category", "cat": "category",
    "content": "content", "article": "content", "page": "content",
    "external_product": "external_product", "external": "external_product", "ext": "external_product",
}
_OVERLAY_VALUE_KEYS = ("to", "canonical", "canonical_name", "terms", "synonyms", "aliases", "targets", "value")


def norm_kind(kind: Any, entity_id: str = "") -> str:
    k = str(kind or "").strip().lower()
    if k in _KIND_ALIASES:
        return _KIND_ALIASES[k]
    parts = (entity_id or "").split("_", 2)
    if len(parts) >= 2 and parts[0] == "e" and parts[1].lower() in _KIND_ALIASES:
        return _KIND_ALIASES[parts[1].lower()]
    return "other"


def words_of(term: str) -> List[str]:
    """Токены термина как их видит токенизатор + части составных слов."""
    out: List[str] = []
    for tok in _WORD_SPLIT_RE.findall(basic_norm(term)):
        out.append(tok)
        if _PART_SPLIT_RE.search(tok):
            out.extend(p for p in _PART_SPLIT_RE.split(tok) if p)
    return out


@dataclass(frozen=True)
class EntityRef:
    entity_id: str
    kind: str
    canonical_name: str
    status: str = "auto"
    family_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityHit:
    ref: EntityRef
    start: int                  # индекс первого токена окна
    end: int                    # индекс после последнего токена окна
    surface: str                # совпавший алиас (нормализованный)
    via: str                    # alias | lemma | overlay
    confidence: float


@dataclass(frozen=True, eq=False)
class Lexicon:
    index_version: str
    config_version: int
    is_index_loaded: bool
    alias_index: Mapping[str, Tuple[EntityRef, ...]]
    alias_lemma_index: Mapping[str, Tuple[EntityRef, ...]]
    entities: Tuple[EntityRef, ...]
    entity_by_id: Mapping[str, EntityRef]
    token_index: Mapping[str, Tuple[int, ...]]            # лемма-токен -> позиции в entities
    entity_tokens: Mapping[str, FrozenSet[str]]           # entity_id -> лемма-токены
    brand_terms: FrozenSet[str]
    brand_tokens: FrozenSet[str]
    brand_class_prior: Mapping[str, Mapping[str, float]]
    head_terms: FrozenSet[str]
    known_tokens: FrozenSet[str]
    word_freq: Mapping[str, int]
    translit_index: TranslitIndex
    overlay_alias: Mapping[str, Tuple[str, ...]]
    overlay_synonyms: Mapping[str, Tuple[str, ...]]
    max_alias_tokens: int = MAX_ALIAS_TOKENS

    # ------------------------------------------------------------------ предикаты
    def is_known_token(self, token: str) -> bool:
        tok = basic_norm(token)
        return tok in self.known_tokens or lemma_of_token(tok) in self.known_tokens

    def is_known_term(self, term: str) -> bool:
        """Известный термин: алиас сущности, бренд, головное имя или токен словаря."""
        t = basic_norm(term)
        return bool(t) and (
            t in self.alias_index
            or t in self.brand_terms
            or t in self.head_terms
            or t in self.overlay_alias
            or self.is_known_token(t)
        )

    def is_protected_term(self, term: str) -> bool:
        """Бренд/сущность/головное имя — такие токены нельзя автоматически «исправлять»."""
        t = basic_norm(term)
        return (
            t in self.alias_index
            or t in self.alias_lemma_index
            or t in self.brand_terms
            or t in self.head_terms
            or t in self.overlay_alias
        )

    # ------------------------------------------------------------------ поиск сущностей
    def find_entity_hits(self, tq: TokenizedQuery) -> List[EntityHit]:
        """Все совпадения окон токенов с алиасами сущностей и overlay-алиасами (окна могут пересекаться)."""
        hits: List[EntityHit] = []
        for start, end, surface, lemma in tq.ngrams(self.max_alias_tokens):
            refs = self.alias_index.get(surface)
            via = "alias"
            if not refs:
                refs = self.alias_lemma_index.get(lemma)
                via = "lemma"
            for ref in refs or ():
                hits.append(EntityHit(ref, start, end, surface, via, 0.0))
        hits.sort(key=lambda h: (h.start, -(h.end - h.start), h.ref.entity_id))
        return hits

    def entities_for_tokens(self, lemma_tokens: Iterable[str], limit: int) -> List[Tuple[EntityRef, float]]:
        """Сущности по пересечению лемма-токенов с названием/алиасами; оценка — доля покрытых токенов запроса."""
        query_tokens = {t for t in lemma_tokens if t}
        if not query_tokens:
            return []
        scores: Dict[int, int] = defaultdict(int)
        for tok in query_tokens:
            for pos in self.token_index.get(tok, ()):
                scores[pos] += 1
        ranked: List[Tuple[EntityRef, float]] = []
        for pos, overlap in scores.items():
            ref = self.entities[pos]
            denom = max(len(query_tokens), len(self.entity_tokens.get(ref.entity_id, ())) or 1)
            ranked.append((ref, overlap / denom))
        ranked.sort(key=lambda rv: (-rv[1], rv[0].entity_id))
        return ranked[:limit]


# --------------------------------------------------------------------------- построение
def _parse_aliases(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, float):  # NaN из pandas
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw] if raw.strip() else []
    out: List[str] = []
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    try:
        iterator = list(raw)
    except TypeError:
        return []
    for item in iterator:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping):
            status = str(item.get("status", "auto")).lower()
            value = item.get("alias") or item.get("term") or item.get("name")
            if value and status in ALLOWED_ALIAS_STATUSES:
                out.append(str(value))
    return out


def _overlay_terms(value: Mapping[str, Any]) -> Tuple[str, ...]:
    out: List[str] = []
    for key in _OVERLAY_VALUE_KEYS:
        v = value.get(key) if isinstance(value, Mapping) else None
        if isinstance(v, str):
            out.append(basic_norm(v))
        elif isinstance(v, (list, tuple)):
            out.extend(basic_norm(str(x)) for x in v if isinstance(x, str))
    return tuple(dict.fromkeys(t for t in out if t))


class _Builder:
    def __init__(self, cfg: NlpConfig) -> None:
        self.cfg = cfg
        self.alias: Dict[str, List[EntityRef]] = defaultdict(list)
        self.alias_lemma: Dict[str, List[EntityRef]] = defaultdict(list)
        self.entities: List[EntityRef] = []
        self.entity_by_id: Dict[str, EntityRef] = {}
        self.brand_terms: Set[str] = set()
        self.known: Set[str] = set()
        self.freq: Dict[str, int] = {}
        self.translit_terms: Set[str] = set()

    def register_entity(self, ref: EntityRef) -> EntityRef:
        existing = self.entity_by_id.get(ref.entity_id)
        if existing is not None:
            return existing
        self.entities.append(ref)
        self.entity_by_id[ref.entity_id] = ref
        return ref

    def add_words(self, term: str, freq: int) -> None:
        for w in words_of(term):
            self.known.add(w)
            lemma = lemma_of_token(w)
            self.known.add(lemma)
            for form in (w, lemma):
                if len(form) >= 2 and not any(ch.isdigit() for ch in form):
                    if self.freq.get(form, 0) < freq:
                        self.freq[form] = freq
        t = basic_norm(term)
        if t and any(ch.isalpha() for ch in t):
            self.translit_terms.add(t)

    def add_alias(self, term: str, ref: EntityRef) -> None:
        t = basic_norm(term)
        if not t or len(t.split()) > MAX_ALIAS_TOKENS:
            return
        if ref not in self.alias[t]:
            self.alias[t].append(ref)
        lem = lemmatize_phrase(t)
        if ref not in self.alias_lemma[lem]:
            self.alias_lemma[lem].append(ref)


def _add_entities(b: _Builder, index_state: Any) -> None:
    entities = getattr(index_state, "entities", None)
    if entities is None:
        return
    try:
        records = entities.to_dict("records")
    except Exception as exc:  # noqa: BLE001
        log.error("cannot read entities: %s", exc)
        return
    for row in records:
        entity_id = str(row.get("entity_id") or "").strip()
        canonical = str(row.get("canonical_name") or "").strip()
        if not entity_id or not canonical:
            continue
        status = str(row.get("status") or "auto").strip().lower()
        if status not in ALLOWED_ALIAS_STATUSES:
            continue
        kind = norm_kind(row.get("kind"), entity_id)
        fam = tuple(str(x) for x in _parse_aliases(row.get("target_family_ids_json")))
        ref = b.register_entity(EntityRef(entity_id, kind, canonical, status, fam))
        names = [canonical] + _parse_aliases(row.get("aliases_json"))
        for i, name in enumerate(names):
            b.add_alias(name, ref)
            b.add_words(name, FREQ_ENTITY_NAME if i == 0 else FREQ_ENTITY_ALIAS)
            if kind == "brand":
                b.brand_terms.add(basic_norm(name))
                b.add_words(name, FREQ_BRAND)


def _add_vendors(b: _Builder, index_state: Any) -> Dict[str, Dict[str, float]]:
    """Вендоры из families.parquet: бренды + распределение product_class по бренду."""
    families = getattr(index_state, "families", None)
    prior: Dict[str, Dict[str, float]] = {}
    if families is None or "vendor" not in getattr(families, "columns", ()):
        return prior
    try:
        cols = ["vendor"] + (["product_class"] if "product_class" in families.columns else [])
        counts: Dict[str, Counter] = defaultdict(Counter)
        for row in families[cols].dropna(subset=["vendor"]).itertuples(index=False):
            vendor = basic_norm(str(row[0]))
            if not vendor:
                continue
            counts[vendor][str(row[1]) if len(row) > 1 else "other"] += 1
        for vendor, ctr in counts.items():
            b.brand_terms.add(vendor)
            b.add_words(vendor, FREQ_BRAND)
            total = sum(ctr.values())
            prior[vendor] = {cls: n / total for cls, n in ctr.items()}
    except Exception as exc:  # noqa: BLE001
        log.error("cannot read families.vendor: %s", exc)
    return prior


def _add_glossary_and_taxonomy(b: _Builder) -> Set[str]:
    cfg = b.cfg
    heads: Set[str] = set()
    for table in (cfg.slang, cfg.abbreviations, cfg.translit):
        for term, entry in table.items():
            b.add_words(term, FREQ_GLOSSARY)
            for c in entry.canonical:
                b.add_words(c, FREQ_GLOSSARY)
    for entry in cfg.layout_confusions.values():
        for c in entry.canonical:
            b.add_words(c, FREQ_GLOSSARY)
    for entries in cfg.numeric_aliases.values():
        for entry in entries:
            for c in entry.canonical:
                b.add_words(c, FREQ_GLOSSARY)
    for group in cfg.synonym_groups:
        for member in group:
            b.add_words(member, FREQ_GLOSSARY)
    for head in cfg.head_noun_to_types:
        heads.add(head)
        b.add_words(head, FREQ_TAXONOMY)
    for info in cfg.types.values():
        b.add_words(info.title_ru, FREQ_TAXONOMY)
    for title in cfg.class_titles.values():
        b.add_words(title, FREQ_TAXONOMY)
    return heads


def _add_overlay(b: _Builder, overlay: Any) -> Tuple[Dict[str, Tuple[str, ...]], Dict[str, Tuple[str, ...]]]:
    alias_map: Dict[str, Tuple[str, ...]] = {}
    syn_map: Dict[str, Tuple[str, ...]] = {}
    if overlay is None:
        return alias_map, syn_map
    for kind, target in (("alias", alias_map), ("synonym", syn_map)):
        try:
            rules = overlay.rules(kind)
        except Exception:  # noqa: BLE001
            continue
        for key, items in rules.items():
            terms: List[str] = []
            for item in items:
                value = getattr(item, "value", {}) or {}
                terms.extend(_overlay_terms(value))
                entity_id = value.get("entity_id") if isinstance(value, Mapping) else None
                if kind == "alias" and entity_id:
                    eid = str(entity_id)
                    ref = b.register_entity(EntityRef(eid, "other", (terms[0] if terms else key), "verified"))
                    b.add_alias(key, ref)
            if terms or kind == "alias":
                target[key] = tuple(dict.fromkeys(terms))
            b.add_words(key, FREQ_OVERLAY)
            for t in terms:
                b.add_words(t, FREQ_OVERLAY)
    return alias_map, syn_map


def build_lexicon(cfg: NlpConfig, index_state: Any = None, overlay: Any = None) -> Lexicon:
    b = _Builder(cfg)
    loaded = bool(getattr(index_state, "is_loaded", False))
    if loaded:
        _add_entities(b, index_state)
    brand_prior = _add_vendors(b, index_state) if loaded else {}
    heads = _add_glossary_and_taxonomy(b)
    overlay_alias, overlay_syn = _add_overlay(b, overlay)

    tokens_by_eid: Dict[str, Set[str]] = defaultdict(set)
    for ref in b.entities:
        for w in words_of(ref.canonical_name):
            tokens_by_eid[ref.entity_id].add(lemma_of_token(w))
    for alias, refs in b.alias.items():
        alias_toks = {lemma_of_token(w) for w in words_of(alias)}
        for ref in refs:
            tokens_by_eid[ref.entity_id].update(alias_toks)

    entity_tokens: Dict[str, FrozenSet[str]] = {}
    postings: Dict[str, List[int]] = defaultdict(list)
    for pos, ref in enumerate(b.entities):
        toks = frozenset(t for t in tokens_by_eid.get(ref.entity_id, ()) if t)
        entity_tokens[ref.entity_id] = toks
        for tok in sorted(toks):
            if len(postings[tok]) < MAX_POSTINGS:
                postings[tok].append(pos)

    translit = TranslitIndex()
    translit.add_many(b.translit_terms)

    brand_tokens = frozenset(w for term in b.brand_terms for w in words_of(term))
    return Lexicon(
        index_version=str(getattr(index_state, "index_version", "idx_unloaded")),
        config_version=int(getattr(overlay, "config_version", 0)),
        is_index_loaded=loaded,
        alias_index={k: tuple(v) for k, v in b.alias.items()},
        alias_lemma_index={k: tuple(v) for k, v in b.alias_lemma.items()},
        entities=tuple(b.entities),
        entity_by_id=dict(b.entity_by_id),
        token_index={k: tuple(v) for k, v in postings.items()},
        entity_tokens=entity_tokens,
        brand_terms=frozenset(b.brand_terms),
        brand_tokens=brand_tokens,
        brand_class_prior=brand_prior,
        head_terms=frozenset(heads),
        known_tokens=frozenset(b.known),
        word_freq=dict(b.freq),
        translit_index=translit,
        overlay_alias=overlay_alias,
        overlay_synonyms=overlay_syn,
    )


_cache_lock = threading.Lock()
_cache_key: Optional[Tuple[Any, ...]] = None
_cache_value: Optional[Lexicon] = None


def get_lexicon(cfg: Optional[NlpConfig] = None, index_state: Any = None, overlay: Any = None) -> Lexicon:
    """Словарь, кэшированный по (конфиг, версия индекса, версия overlay)."""
    global _cache_key, _cache_value
    cfg = cfg or get_nlp_config()
    key = (
        id(cfg),
        str(getattr(index_state, "index_version", "idx_unloaded")),
        bool(getattr(index_state, "is_loaded", False)),
        int(getattr(overlay, "config_version", 0)),
        id(index_state),
        id(overlay) if overlay is not None and not hasattr(overlay, "config_version") else 0,
    )
    with _cache_lock:
        if _cache_value is not None and _cache_key == key:
            return _cache_value
    built = build_lexicon(cfg, index_state, overlay)
    with _cache_lock:
        _cache_key, _cache_value = key, built
    return built