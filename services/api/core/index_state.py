"""Загрузка артефактов индекса (B.5) из `data/artifacts/current` или `CURRENT_VERSION`.

* `embeddings.npy` открывается через `np.load(..., mmap_mode='r')` (без копирования в RAM).
* Если каталога/обязательных файлов нет, возвращается безопасное состояние `is_loaded=False`
  (API продолжает работать: /readyz сообщает о проблеме, поиск отвечает деградированно).
* Обязательные файлы: manifest.json, families.parquet, offers.parquet. Остальные необязательны:
  отсутствие фиксируется в `missing_files`, ошибка чтения — в `load_errors`.
* Артефакты строятся доверенным Контуром 2, поэтому pickle-файлы (symspell, trie, vocab) читаются без ограничений.

`current` может быть симлинком/каталогом либо текстовым указателем (`current` или `CURRENT_VERSION`)
с именем версии (`idx_YYYYMMDD_HHMM_sha7`). Путь переопределяется переменной `INDEX_ARTIFACTS_CURRENT`.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import numpy as np

from services.api.core.cost_meter import utc_now_iso

log = logging.getLogger("smart_search.index")

DEFAULT_CURRENT = "data/artifacts/current"
UNLOADED_VERSION = "idx_unloaded"

REQUIRED_FILES = ("manifest.json", "families.parquet", "offers.parquet")
OPTIONAL_FILES = (
    "entities.parquet",
    "graph_edges.parquet",
    "data_quality_issues.parquet",
    "embeddings.npy",
    "bm25f.npz",
    "bm25f_vocab.pkl",
    "char3gram.npz",
    "char3gram_vocab.pkl",
    "symspell.pkl",
    "typeahead_trie.pkl",
    "popular_by_root.json",
    "qc_report.json",
)


def resolve_artifact_dir(base: Optional[Union[str, Path]] = None) -> Optional[Path]:
    """Каталог текущей сборки: каталог/симлинк `current` или файл-указатель (`current` / `CURRENT_VERSION`)."""
    current = Path(base or os.getenv("INDEX_ARTIFACTS_CURRENT", DEFAULT_CURRENT))
    try:
        if current.is_dir():
            return current.resolve()
        for pointer_file in (current, current.parent / "CURRENT_VERSION"):
            if pointer_file.is_file():
                name = pointer_file.read_text(encoding="utf-8").strip()
                if name:
                    target = Path(name)
                    if target.is_dir():
                        return target.resolve()
                    candidate = pointer_file.parent / target
                    if candidate.is_dir():
                        return candidate.resolve()
    except OSError as exc:
        log.error("cannot resolve artifacts dir %s: %s", current, exc)
    return None


@dataclass(frozen=True, eq=False)
class IndexState:
    is_loaded: bool = False
    index_version: str = UNLOADED_VERSION
    path: Optional[str] = None
    loaded_at: str = ""
    manifest: Mapping[str, Any] = field(default_factory=dict)
    families: Any = None             # pandas.DataFrame
    offers: Any = None               # pandas.DataFrame
    entities: Any = None             # pandas.DataFrame
    graph_edges: Any = None          # pandas.DataFrame
    data_quality_issues: Any = None  # pandas.DataFrame
    embeddings: Optional[np.ndarray] = None  # np.memmap (n_families, dim), float32, L2-нормированная
    bm25f: Any = None
    bm25f_vocab: Any = None
    char3gram: Any = None
    char3gram_vocab: Any = None
    symspell: Any = None
    typeahead_trie: Any = None
    popular_by_root: Mapping[str, Any] = field(default_factory=dict)
    qc_report: Mapping[str, Any] = field(default_factory=dict)
    family_pos: Mapping[str, int] = field(default_factory=dict)
    offers_by_family: Mapping[str, Any] = field(default_factory=dict)   # family_id -> np.ndarray позиций в offers
    entity_pos: Mapping[str, int] = field(default_factory=dict)
    missing_files: Tuple[str, ...] = ()
    load_errors: Tuple[str, ...] = ()

    # ------------------------------------------------------------------ свойства
    @property
    def has_dense(self) -> bool:
        return self.embeddings is not None

    @property
    def n_families(self) -> int:
        return 0 if self.families is None else int(len(self.families))

    @property
    def n_offers(self) -> int:
        return 0 if self.offers is None else int(len(self.offers))

    @property
    def embedding_dim(self) -> Optional[int]:
        return None if self.embeddings is None else int(self.embeddings.shape[1])

    # ------------------------------------------------------------------ доступ к данным
    def family_index(self, family_id: str) -> Optional[int]:
        return self.family_pos.get(family_id)

    def family_id_at(self, idx: int) -> str:
        return str(self.families["family_id"].iat[idx])

    def family_row(self, family_id: str) -> Optional[Dict[str, Any]]:
        idx = self.family_pos.get(family_id)
        if idx is None or self.families is None:
            return None
        return self.families.iloc[idx].to_dict()

    def offer_rows(self, family_id: str) -> List[Dict[str, Any]]:
        positions = self.offers_by_family.get(family_id)
        if positions is None or self.offers is None:
            return []
        return self.offers.iloc[positions].to_dict("records")

    def embedding_row(self, family_id: str) -> Optional[np.ndarray]:
        idx = self.family_pos.get(family_id)
        if idx is None or self.embeddings is None:
            return None
        return self.embeddings[idx]

    def entity_row(self, entity_id: str) -> Optional[Dict[str, Any]]:
        idx = self.entity_pos.get(entity_id)
        if idx is None or self.entities is None:
            return None
        return self.entities.iloc[idx].to_dict()

    def describe(self) -> Dict[str, Any]:
        """Краткое описание для /readyz и /v1/admin/ops."""
        return {
            "is_loaded": self.is_loaded,
            "index_version": self.index_version,
            "path": self.path,
            "loaded_at": self.loaded_at,
            "n_families": self.n_families,
            "n_offers": self.n_offers,
            "n_entities": 0 if self.entities is None else int(len(self.entities)),
            "has_dense": self.has_dense,
            "embedding_dim": self.embedding_dim,
            "missing_files": list(self.missing_files),
            "load_errors": list(self.load_errors),
        }

    @classmethod
    def unloaded(cls, *errors: str, missing: Tuple[str, ...] = (), path: Optional[str] = None) -> "IndexState":
        return cls(is_loaded=False, path=path, loaded_at=utc_now_iso(), missing_files=tuple(missing), load_errors=tuple(errors))


# --------------------------------------------------------------------------- загрузка
def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {"value": data}


def _load_sparse_or_bundle(path: Path) -> Any:
    """`.npz` со scipy-матрицей (save_npz) или обычный npz-архив (словари термов и пр.)."""
    try:
        import scipy.sparse as sp

        return sp.load_npz(path)
    except Exception:  # noqa: BLE001 - не sparse-формат, пробуем как общий npz
        return np.load(path, allow_pickle=True)


def _load_pickle(path: Path) -> Any:
    with path.open("rb") as fh:
        return pickle.load(fh)  # noqa: S301 - артефакты построены доверенным пайплайном


def load_index_state(base: Optional[Union[str, Path]] = None) -> IndexState:
    """Загружает все артефакты. Никогда не бросает исключений: при проблемах возвращает is_loaded=False."""
    directory = resolve_artifact_dir(base)
    if directory is None:
        shown = str(base or os.getenv("INDEX_ARTIFACTS_CURRENT", DEFAULT_CURRENT))
        log.warning("index artifacts not found at %s; starting with unloaded IndexState", shown)
        return IndexState.unloaded(f"artifacts directory not found: {shown}", missing=REQUIRED_FILES)

    errors: List[str] = []
    missing: List[str] = []

    def present(name: str) -> bool:
        if (directory / name).is_file():
            return True
        missing.append(name)
        return False

    def attempt(name: str, loader: Any) -> Any:
        if not present(name):
            return None
        try:
            return loader(directory / name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log.error("failed to load %s: %s", name, exc)
            return None

    try:
        import pandas as pd

        read_parquet = pd.read_parquet
    except ImportError as exc:
        return IndexState.unloaded(f"pandas/pyarrow not available: {exc}", path=str(directory))

    manifest = attempt("manifest.json", _read_json) or {}
    families = attempt("families.parquet", read_parquet)
    offers = attempt("offers.parquet", read_parquet)
    entities = attempt("entities.parquet", read_parquet)
    graph_edges = attempt("graph_edges.parquet", read_parquet)
    dq_issues = attempt("data_quality_issues.parquet", read_parquet)
    bm25f = attempt("bm25f.npz", _load_sparse_or_bundle)
    bm25f_vocab = attempt("bm25f_vocab.pkl", _load_pickle)
    char3gram = attempt("char3gram.npz", _load_sparse_or_bundle)
    char3gram_vocab = attempt("char3gram_vocab.pkl", _load_pickle)
    symspell = attempt("symspell.pkl", _load_pickle)
    trie = attempt("typeahead_trie.pkl", _load_pickle)
    popular = attempt("popular_by_root.json", _read_json) or {}
    qc_report = attempt("qc_report.json", _read_json) or {}
    embeddings = attempt("embeddings.npy", lambda p: np.load(p, mmap_mode="r"))

    # --- проверка обязательных частей
    if not manifest or families is None or offers is None:
        errors.append("required artifacts are missing or unreadable (manifest/families/offers)")
        return IndexState.unloaded(*errors, missing=tuple(missing), path=str(directory))
    if "family_id" not in families.columns or "family_id" not in offers.columns:
        errors.append("families/offers have no 'family_id' column")
        return IndexState.unloaded(*errors, missing=tuple(missing), path=str(directory))

    # --- согласованность эмбеддингов: при несовпадении dense отключается, остальное работает
    if embeddings is not None:
        if embeddings.ndim != 2 or embeddings.shape[0] != len(families):
            errors.append(
                f"embeddings.npy shape {tuple(embeddings.shape)} does not match families ({len(families)}); dense disabled"
            )
            embeddings = None
        elif embeddings.dtype != np.float32:
            errors.append(f"embeddings.npy dtype is {embeddings.dtype}, expected float32; dense disabled")
            embeddings = None

    family_pos = {str(fid): i for i, fid in enumerate(families["family_id"].tolist())}
    offers_by_family = {str(k): v for k, v in offers.groupby("family_id", sort=False).indices.items()}
    entity_pos: Dict[str, int] = {}
    if entities is not None and "entity_id" in entities.columns:
        entity_pos = {str(eid): i for i, eid in enumerate(entities["entity_id"].tolist())}

    index_version = str(manifest.get("index_version") or directory.name)
    log.info(
        "index %s loaded: families=%d offers=%d dense=%s missing=%s errors=%d",
        index_version, len(families), len(offers), embeddings is not None, missing, len(errors),
    )
    return IndexState(
        is_loaded=True,
        index_version=index_version,
        path=str(directory),
        loaded_at=utc_now_iso(),
        manifest=manifest,
        families=families,
        offers=offers,
        entities=entities,
        graph_edges=graph_edges,
        data_quality_issues=dq_issues,
        embeddings=embeddings,
        bm25f=bm25f,
        bm25f_vocab=bm25f_vocab,
        char3gram=char3gram,
        char3gram_vocab=char3gram_vocab,
        symspell=symspell,
        typeahead_trie=trie,
        popular_by_root=popular,
        qc_report=qc_report,
        family_pos=family_pos,
        offers_by_family=offers_by_family,
        entity_pos=entity_pos,
        missing_files=tuple(missing),
        load_errors=tuple(errors),
    )


# --------------------------------------------------------------------------- держатель состояния
class IndexStateHolder:
    """Потокобезопасный держатель текущего IndexState с атомарной подменой при перезагрузке."""

    def __init__(self, base: Optional[Union[str, Path]] = None) -> None:
        self._base = base
        self._lock = threading.RLock()
        self._state: Optional[IndexState] = None
        self._signature: Optional[Tuple[str, int]] = None

    def current(self) -> IndexState:
        state = self._state
        if state is None:
            with self._lock:
                if self._state is None:
                    self._swap(load_index_state(self._base))
                state = self._state
        assert state is not None
        return state

    def _compute_signature(self) -> Optional[Tuple[str, int]]:
        directory = resolve_artifact_dir(self._base)
        if directory is None:
            return None
        try:
            return (str(directory), (directory / "manifest.json").stat().st_mtime_ns)
        except OSError:
            return (str(directory), 0)

    def _swap(self, new_state: IndexState) -> None:
        self._state = new_state
        self._signature = self._compute_signature()

    def reload(self, *, keep_previous_on_failure: bool = True) -> IndexState:
        """Перечитывает артефакты и атомарно подменяет состояние."""
        new_state = load_index_state(self._base)
        with self._lock:
            old = self._state
            if not new_state.is_loaded and keep_previous_on_failure and old is not None and old.is_loaded:
                log.error("index reload failed, keeping previous version %s: %s", old.index_version, new_state.load_errors)
                return old
            self._swap(new_state)
            return new_state

    def reload_if_changed(self) -> bool:
        """Перезагружает индекс, если сменилась сборка (`current` указывает на другой каталог/manifest)."""
        signature = self._compute_signature()
        if signature is None or signature == self._signature:
            return False
        before = self._state
        after = self.reload()
        return after is not before


_holder: Optional[IndexStateHolder] = None
_holder_lock = threading.Lock()


def get_index_holder() -> IndexStateHolder:
    global _holder
    with _holder_lock:
        if _holder is None:
            _holder = IndexStateHolder()
        return _holder


def get_index_state() -> IndexState:
    return get_index_holder().current()


def reload_index_state() -> IndexState:
    return get_index_holder().reload()