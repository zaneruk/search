"""
Потоковый парсер YML/XML-фида (Mindbox.yml.gz.xml, ~250 МБ).
Поддерживает авто-детект gzip, раскрывает CDATA/HTML-entities,
строит дерево категорий (path, depth, root_id) и читает <offer> + <param> без переполнения RAM.
"""

from __future__ import annotations

import gzip
import html
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from lxml import etree


def open_feed_stream(path: Path):
    """Автоматически определяет, сжат файл в gzip или это обычный XML."""
    with path.open("rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return path.open("rb")


def clean_html_text(text: Optional[str]) -> str:
    if not text:
        return ""
    unescaped = html.unescape(str(text))
    return " ".join(unescaped.split())


def format_cat_id(raw_id: Optional[str]) -> Optional[str]:
    if not raw_id:
        return None
    s = str(raw_id).strip()
    if not s:
        return None
    return s if s.startswith("c_") else f"c_{s}"


def build_category_tree(raw_cats: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Вычисляет path, depth и root_id для каждой категории с защитой от циклов."""
    resolved: Dict[str, Dict[str, Any]] = {}

    def resolve_one(cid: str, visiting: Set[str]) -> Tuple[str, int, str]:
        if cid in resolved:
            r = resolved[cid]
            return r["path"], r["depth"], r["root_id"]

        node = raw_cats.get(cid)
        if not node:
            return cid, 0, cid

        name = node["name"] or cid
        pid = node["parent_id"]
        if not pid or pid not in raw_cats or pid == cid or pid in visiting:
            resolved[cid] = {
                "category_id": cid,
                "category_id_raw": node["raw_id"],
                "parent_id": None,
                "name": name,
                "path": name,
                "depth": 0,
                "root_id": cid,
            }
            return name, 0, cid

        visiting.add(cid)
        p_path, p_depth, root_id = resolve_one(pid, visiting)
        visiting.remove(cid)

        full_path = f"{p_path} / {name}"
        depth = p_depth + 1
        resolved[cid] = {
            "category_id": cid,
            "category_id_raw": node["raw_id"],
            "parent_id": pid,
            "name": name,
            "path": full_path,
            "depth": depth,
            "root_id": root_id,
        }
        return full_path, depth, root_id

    for cid in list(raw_cats.keys()):
        resolve_one(cid, set())
    return resolved


def parse_categories(feed_path: Path) -> Dict[str, Dict[str, Any]]:
    """Первый быстрый проход: собирает только дерево <category> (обычно в самом начале файла)."""
    raw_cats: Dict[str, Dict[str, Any]] = {}
    with open_feed_stream(feed_path) as stream:
        context = etree.iterparse(
            stream,
            events=("end",),
            tag=("category", "offers"),
            recover=True,
            resolve_entities=False,
        )
        for _, elem in context:
            if elem.tag == "offers":
                # Категории в YML всегда идут ДО <offers> — выходим мгновенно
                break
            raw_id = elem.attrib.get("id")
            if raw_id:
                cid = format_cat_id(raw_id)
                pid = format_cat_id(elem.attrib.get("parentId"))
                raw_cats[cid] = {
                    "raw_id": str(raw_id).strip(),
                    "parent_id": pid,
                    "name": clean_html_text(elem.text),
                }
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]
    return build_category_tree(raw_cats)


def iter_raw_offers(feed_path: Path, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """Потоковый генератор сырых словарей по каждому <offer>."""
    count = 0
    with open_feed_stream(feed_path) as stream:
        context = etree.iterparse(
            stream,
            events=("end",),
            tag="offer",
            recover=True,
            resolve_entities=False,
        )
        for _, elem in context:
            raw_id = elem.attrib.get("id")
            if not raw_id:
                elem.clear()
                continue

            offer: Dict[str, Any] = {
                "offer_id_raw": str(raw_id).strip(),
                "available": str(elem.attrib.get("available", "")).strip().lower() == "true",
                "group_id_raw": str(elem.attrib.get("group_id", "")).strip() or None,
                "params": [],
                "pictures": [],
            }

            for child in elem:
                tag = child.tag
                if not isinstance(tag, str):
                    continue
                if tag == "param":
                    pname = clean_html_text(child.attrib.get("name"))
                    pval = clean_html_text(child.text)
                    if pname:
                        offer["params"].append((pname, pval))
                elif tag == "picture":
                    pic = clean_html_text(child.text)
                    if pic:
                        offer["pictures"].append(pic)
                else:
                    val = clean_html_text(child.text)
                    if tag in ("description", "name", "vendor", "url", "price", "oldprice", "currencyId", "categoryId", "vendorCode", "vendorUrl", "model", "typePrefix", "group_id"):
                        offer[tag] = val

            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

            if not offer["group_id_raw"] and offer.get("group_id"):
                offer["group_id_raw"] = offer["group_id"]

            yield offer
            count += 1
            if limit is not None and count >= limit:
                break