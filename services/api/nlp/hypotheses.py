"""S3: генерация гипотез написания и политика защиты от ложного переисправления.

Источники:
  * exact alias сущностей (status in {seed, verified, auto}) + overlay (alias, synonym)
  * seed_glossary.yaml (slang, abbreviations, translit, numeric_aliases, synonym_groups)
  * дефисные формы (сканер-вс -> сканер вс / сканервс)
  * склейка и расклейка соседних токенов (крипто про -> криптопро, max patrol -> maxpatrol)
  * конвертация раскладки (LayoutFixer: ghbdtn -> привет)
  * фонетический транслит (TranslitIndex: мдаемон -> mdaemon, кибер бэкап -> cyber backup)
  * опечатки (SymSpellDict)

Защита от переисправления (`protect_exact_name_and_brand`):
  если токен/запрос уже совпадает с брендом, сущностью или головным именем, он не перезаписывается
  автоматически (`applied_query` не ломает точный запрос); альтернативы уходят в `did_you_mean` / `hypotheses`.
"""
from __future__ import annotations
from services.api.nlp.tokenize import TokenizedQuery, is_known_ru_word
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from services.api.nlp.config import GlossaryEntry, NlpConfig, basic_norm, get_nlp_config
from services.api.nlp.layout import LayoutFixer
from services.api.nlp.lexicon import Lexicon, get_lexicon
from services.api.nlp.normalize import NormalizedQuery
from services.api.nlp.symspell_dict import SymSpellDict, get_symspell_dict
from services.api.nlp.tokenize import TokenizedQuery


@dataclass(frozen=True)
class SpellingHypothesis:
    query: str
    source: str     # overlay_alias | overlay_synonym | glossary | glue | split | hyphen | layout | translit | symspell | numeric
    confidence: float
    notes: str = ""


@dataclass(frozen=True)
class HypothesesResult:
    original: str
    applied_query: Optional[str] = None
    did_you_mean: Optional[str] = None
    correction_source: Optional[str] = None
    protected_exact: bool = False
    hypotheses: Tuple[SpellingHypothesis, ...] = ()
    expanded_terms: Tuple[str, ...] = ()


def _entry_conditions_ok(entry: GlossaryEntry, tokens_set: Set[str], text: str) -> bool:
    if not entry.requires_any:
        return True
    return any(req in tokens_set or req in text for req in entry.requires_any)


def _add_hyp(store: Dict[str, SpellingHypothesis], query: str, source: str, conf: float, notes: str = "") -> None:
    q = " ".join(basic_norm(query).split())
    if not q:
        return
    prev = store.get(q)
    if prev is None or conf > prev.confidence:
        store[q] = SpellingHypothesis(query=q, source=source, confidence=round(float(conf), 4), notes=notes)


def generate_hypotheses(
    nq: NormalizedQuery,
    tq: TokenizedQuery,
    lexicon: Optional[Lexicon] = None,
    symspell: Optional[SymSpellDict] = None,
    cfg: Optional[NlpConfig] = None,
) -> HypothesesResult:
    cfg = cfg or get_nlp_config()
    lexicon = lexicon or get_lexicon(cfg)
    symspell = symspell or get_symspell_dict(cfg, lexicon)
    sp = cfg.spelling
    priors = cfg.priors

    base = nq.query_norm
    if not base:
        return HypothesesResult(original="")

    tokens = list(tq.texts)
    lemmas_set = set(tq.lemmas)
    tokens_set = set(tokens) | lemmas_set
    store: Dict[str, SpellingHypothesis] = {}
    expanded: List[str] = []

    def add_exp(term: str) -> None:
        t = basic_norm(term)
        if t and t != base and t not in expanded:
            expanded.append(t)

    # 1. Защита от ложного переисправления: точные совпадения бренда / сущности / alias
    entity_hits = lexicon.find_entity_hits(tq)
    protected_indices: Set[int] = set()
    for i, tok in enumerate(tokens):
        lem = tq.tokens[i].lemma
        if (
            tok in nq.protected_tokens
            or lexicon.is_protected_term(tok)
            or lexicon.is_protected_term(lem)
        ):
            protected_indices.add(i)
    for hit in entity_hits:
        for i in range(hit.start, hit.end):
            protected_indices.add(i)
    protected_exact = sp.protect_exact_name_and_brand and (
        lexicon.is_protected_term(base) or (bool(tokens) and len(protected_indices) == len(tokens))
    )

    # 2. Дефисные формы (сканер-вс -> сканер вс, сканервс)
    for tok, forms in nq.hyphen_forms.items():
        for f in forms:
            if f != tok:
                rewritten = " ".join(f if t == tok else t for t in tokens)
                _add_hyp(store, rewritten, "hyphen", priors.glossary_exact, f"{tok}->{f}")
                add_exp(f)

    # 3. Overlay alias / synonym
    if base in lexicon.overlay_alias:
        for t in lexicon.overlay_alias[base]:
            _add_hyp(store, t, "overlay_alias", priors.alias_verified, "exact")
            add_exp(t)
    if base in lexicon.overlay_synonyms:
        for t in lexicon.overlay_synonyms[base]:
            _add_hyp(store, t, "overlay_synonym", priors.synonym, "exact")
            add_exp(t)

    for i, tok in enumerate(tokens):
        for mapping, src, conf in (
            (lexicon.overlay_alias, "overlay_alias", priors.alias_verified),
            (lexicon.overlay_synonyms, "overlay_synonym", priors.synonym),
        ):
            for key in (tok, tq.tokens[i].lemma):
                if key in mapping:
                    for repl in mapping[key]:
                        _add_hyp(store, " ".join(repl if j == i else tokens[j] for j in range(len(tokens))), src, conf, key)
                        add_exp(repl)

    # 4. Glossary (slang, abbreviations, translit, numeric_aliases, synonym_groups)
    for table in (cfg.slang, cfg.abbreviations, cfg.translit):
        if base in table and _entry_conditions_ok(table[base], tokens_set, base):
            e = table[base]
            c_val = e.confidence if e.confidence is not None else priors.glossary_exact
            for can in e.canonical:
                _add_hyp(store, can, "glossary", c_val, e.source)
                add_exp(can)
        for start, end, surface, lemma in tq.ngrams(3):
            for probe in (surface, lemma):
                e = table.get(probe)
                if e is not None and e.match == "token" and _entry_conditions_ok(e, tokens_set, base):
                    c_val = e.confidence if e.confidence is not None else priors.glossary_exact
                    for can in e.canonical:
                        rew = " ".join([*tokens[:start], can, *tokens[end:]])
                        _add_hyp(store, rew, "glossary", c_val, f"{probe}->{can}")
                        add_exp(can)

    if len(tokens) == 1 and base in cfg.numeric_aliases:
        for e in cfg.numeric_aliases[base]:
            if _entry_conditions_ok(e, tokens_set, base):
                c_val = e.confidence if e.confidence is not None else priors.glossary_exact
                for can in e.canonical:
                    _add_hyp(store, can, "numeric", c_val, base)
                    add_exp(can)

    for group in cfg.synonym_groups:
        if base in group:
            for m in group:
                if m != base:
                    _add_hyp(store, m, "glossary", priors.synonym, "synonym_group")
                    add_exp(m)
        else:
            for i, tok in enumerate(tokens):
                lem = tq.tokens[i].lemma
                if tok in group or lem in group:
                    for m in group:
                        if m not in (tok, lem):
                            add_exp(m)

    # 5. Склейка соседних токенов (крипто про -> криптопро, max patrol -> maxpatrol) и расклейка
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if a.isdigit() or b.isdigit():
            continue
        glued = a + b
        if lexicon.is_protected_term(glued) or glued in lexicon.overlay_alias:
            rew = " ".join([*tokens[:i], glued, *tokens[i + 2:]])
            _add_hyp(store, rew, "glue", priors.glue_alias, f"{a}+{b}")
            add_exp(glued)
        elif lexicon.is_known_token(glued):
            rew = " ".join([*tokens[:i], glued, *tokens[i + 2:]])
            _add_hyp(store, rew, "glue", priors.glue_vocab, f"{a}+{b}")
            add_exp(glued)

    for i, tok in enumerate(tokens):
        if i in protected_indices or len(tok) < 7 or any(ch.isdigit() for ch in tok):
            continue
        for k in range(3, len(tok) - 2):
            left, right = tok[:k], tok[k:]
            if lexicon.is_known_token(left) and lexicon.is_known_token(right):
                rew = " ".join([*tokens[:i], f"{left} {right}", *tokens[i + 1:]])
                _add_hyp(store, rew, "split", priors.split_vocab, f"{tok}->{left} {right}")
                add_exp(f"{left} {right}")
                break

    # 6. Раскладка EN <-> RU
    if sp.layout_fix_enabled:
        fixer = LayoutFixer(cfg, is_known=lexicon.is_known_term)
        unprotected_tokens = [tok if i not in protected_indices else "" for i, tok in enumerate(tokens)]
        l_res = fixer.fix_tokens(unprotected_tokens)
        if l_res.changed and len(l_res.candidates) <= sp.max_corrected_tokens:
            merged = [l_res.tokens[i] if i not in protected_indices else tokens[i] for i in range(len(tokens))]
            _add_hyp(store, " ".join(merged), "layout", l_res.confidence, "keyboard_layout")

    # 7. Транслит по TranslitIndex (мдаемон -> mdaemon, кибер бэкап -> cyber backup)
    if sp.transliteration_enabled and len(lexicon.translit_index) > 0:
        for i in range(len(tokens) - 1):
            pair = f"{tokens[i]} {tokens[i + 1]}"
            if i in protected_indices and (i + 1) in protected_indices:
                continue
            for m in lexicon.translit_index.lookup(pair, limit=2):
                conf = priors.translit_phonetic if m.kind == "phonetic" else priors.translit_skeleton
                rew = " ".join([*tokens[:i], m.term, *tokens[i + 2:]])
                _add_hyp(store, rew, "translit", conf, f"{pair}->{m.term}")
                add_exp(m.term)
        for i, tok in enumerate(tokens):
            if (
                i in protected_indices
                or len(tok) < 3
                or any(c.isdigit() for c in tok)
                or lexicon.is_known_token(tok)
                or is_known_ru_word(tok)
            ):
                continue
            for m in lexicon.translit_index.lookup(tok, limit=2):
                conf = priors.translit_phonetic if m.kind == "phonetic" else priors.translit_skeleton
                rew = " ".join([*tokens[:i], m.term, *tokens[i + 1:]])
                _add_hyp(store, rew, "translit", conf, f"{tok}->{m.term}")
                add_exp(m.term)

    # 8. Опечатки (SymSpell) только для незащищённых и неизвестных токенов
    typo_repls: List[Tuple[int, str, float]] = []
    for i, tok in enumerate(tokens):
        if i in protected_indices or lexicon.is_known_token(tok):
            continue
        suggestions = symspell.lookup_token(tok, limit=2)
        if suggestions:
            best = suggestions[0]
            rew = " ".join([*tokens[:i], best.term, *tokens[i + 1:]])
            _add_hyp(store, rew, "symspell", best.confidence, f"{tok}->{best.term}(d={best.distance})")
            typo_repls.append((i, best.term, best.confidence))
    if 2 <= len(typo_repls) <= sp.max_corrected_tokens:
        merged_typos = list(tokens)
        min_conf = 1.0
        for idx, term, conf in typo_repls:
            merged_typos[idx] = term
            min_conf = min(min_conf, conf)
        _add_hyp(store, " ".join(merged_typos), "symspell", min_conf, "multi_token")

    store.pop(base, None)
    hyps = tuple(sorted(store.values(), key=lambda h: (-h.confidence, h.query))[: priors.max_hypotheses])

    # Политика автоприменения vs did_you_mean
    applied: Optional[str] = None
    did_you_mean: Optional[str] = None
    corr_src: Optional[str] = None
    auto_sources = {"layout", "symspell", "glue", "split", "hyphen", "translit", "overlay_alias", "numeric"}

    best_auto = next((h for h in hyps if h.source in auto_sources), None)
    if best_auto is not None:
        # Числовой запрос (365) расширяет, но не затирает оригинал; защищённый точный запрос не переисправляется
        if protected_exact:
            did_you_mean = best_auto.query
        elif best_auto.source == "numeric" and len(tokens) == 1:
            applied = None
            did_you_mean = best_auto.query
        elif best_auto.confidence >= sp.accept_confidence:
            applied = best_auto.query
            corr_src = best_auto.source
        elif best_auto.confidence >= 0.65:
            did_you_mean = best_auto.query

    return HypothesesResult(
        original=base,
        applied_query=applied,
        did_you_mean=did_you_mean,
        correction_source=corr_src,
        protected_exact=protected_exact,
        hypotheses=hyps,
        expanded_terms=tuple(expanded[:12]),
    )