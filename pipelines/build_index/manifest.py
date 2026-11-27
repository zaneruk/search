"""
Генерация manifest.json (строго по CONTRACTS.md B.3 и B.5) и переключение указателя data/artifacts/current.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_index_version(seed_hash: str) -> str:
    """Формат B.3: idx_YYYYMMDD_HHMM_<sha7>."""
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return f"idx_{now_utc}_{seed_hash[:7]}"


def write_manifest(
    artifact_dir: Path,
    index_version: str,
    feed_sha256: str,
    embedding_model: str,
    embedding_dim: int,
    n_offers: int,
    n_families: int,
    n_categories: int,
    n_entities: int,
    n_edges: int,
    extra: Optional[Dict] = None,
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)

    files_inventory: Dict[str, Dict] = {}
    for p in sorted(artifact_dir.iterdir()):
        if p.is_file() and p.name != "manifest.json":
            files_inventory[p.name] = {
                "size_bytes": p.stat().st_size,
                "sha256": sha256_file(p),
            }

    manifest = {
        "contract_version": "2.0.0-mvp",
        "index_version": index_version,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feed_sha256": feed_sha256,
        "embedding_model": embedding_model,
        "embedding_dim": int(embedding_dim),
        "n_offers": int(n_offers),
        "n_families": int(n_families),
        "n_categories": int(n_categories),
        "n_entities": int(n_entities),
        "n_edges": int(n_edges),
        "files": files_inventory,
    }
    if extra:
        manifest.update(extra)

    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def switch_current_pointer(artifacts_root: Path, index_version: str) -> None:
    """
    Атомарно обновляет указатель data/artifacts/current -> <index_version>.
    На Linux/macOS создаёт симлинк; дополнительно пишет CURRENT_VERSION (надёжно и на Windows).
    """
    artifacts_root.mkdir(parents=True, exist_ok=True)
    target_dir = artifacts_root / index_version
    if not target_dir.exists():
        raise FileNotFoundError(f"Каталог артефактов не найден: {target_dir}")

    version_file = artifacts_root / "CURRENT_VERSION"
    tmp_version_file = artifacts_root / ".CURRENT_VERSION.tmp"
    tmp_version_file.write_text(index_version, encoding="utf-8")
    os.replace(tmp_version_file, version_file)

    current_link = artifacts_root / "current"
    tmp_link = artifacts_root / f".current_tmp_{os.getpid()}"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()

    try:
        # Относительный симлинк переносим между локальной машиной и Docker-контейнером
        os.symlink(index_version, tmp_link, target_is_directory=True)
        if current_link.exists() and not current_link.is_symlink():
            # Если кто-то раньше создал обычную папку current вместо симлинка — убираем осторожно
            import shutil
            shutil.rmtree(current_link)
        os.replace(tmp_link, current_link)
    except OSError:
        # Запасной вариант (например, Windows без Developer Mode): копируем актуальные файлы или оставляем CURRENT_VERSION
        if tmp_link.exists():
            tmp_link.unlink()