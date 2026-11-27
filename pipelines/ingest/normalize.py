"""
Нормализация сырых офферов (I1–I2):
- price<=0 -> price_state='on_request', price_minor=None
- скидки только при oldprice > price > 0 и <= 70%
- очистка плейсхолдеров в params -> None
- разбор description (' • ' и 'Ключ: значение')
- очистка названия от дубля вендора + извлечение term_months, qty_users, license_type, version
- определение сегмента с порядком доверия I2 и фиксацией конфликтов.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

PLACEHOLDERS = {
    "", "не определено", "другое или отсутствует", "другое", "отсутствует",
    "нет", "не указано", "n/a", "na", "none", "null", "-", "—",
}

HARDWARE_PARAM_NAMES = {
    "форм-фактор", "совместимый сокет", "семейство процессоров", "управлениеvlan",
    "управление vlan", "количество процессоров", "тип памяти", "интерфейсы",
    "слоты расширения", "мощность блока питания", "число портов", "размер экрана",
}

_NUM_RE = re.compile(r"[^\d.,]")
_TERM_MONTHS_RE = re.compile(r"(\d+)\s*(?:мес\.?|месяц(?:а|ев)?|m\b)", re.IGNORECASE)
_TERM_YEARS_RE = re.compile(r"(\d+)\s*(?:год(?:а|ов)?|лет|yr|year(?:s)?)\b", re.IGNORECASE)
_QTY_RE = re.compile(
    r"(?:на\s+)?(\d{1,5})\s*(?:лиценз(?:ий|ии|ия)|пк|pc|польз(?:ователей|ователя)?|мест(?:а)?|устройств|узл(?:а|ов)|уч[её]тн|user(?:s)?|seat(?:s)?)",
    re.IGNORECASE,
)
_LICENSE_TYPE_RE = re.compile(
    r"\b(VIP|OVL|OLP|ESD|FPP|OEM|CSP|CAL|NFR|SA|Subscription|Perpetual|Base|Standard|Pro|Enterprise|Ultimate|Home|Business)\b",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"\b(20\d{2}|v?\d+\.\d+(?:\.\d+)?)\b", re.IGNORECASE)


def parse_price_minor(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    s = _NUM_RE.sub("", str(raw).replace(",", ".")).strip()
    if not s:
        return None
    try:
        # Защита от нескольких точек
        if s.count(".") > 1:
            parts = s.split(".")
            s = "".join(parts[:-1]) + "." + parts[-1]
        val = float(s)
        if val <= 0.0:
            return None
        return int(round(val * 100))
    except ValueError:
        return None


def clean_param_value(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    s = " ".join(str(val).split())
    if s.lower() in PLACEHOLDERS:
        return None
    return s


def parse_description(raw_desc: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """Разбирает description по ' • ' и 'Ключ: значение'."""
    if not raw_desc:
        return "", {}
    chunks = [c.strip() for c in re.split(r"\s*•\s*|\n+", raw_desc) if c.strip()]
    kv: Dict[str, str] = {}
    free_parts: List[str] = []

    for ch in chunks:
        if ":" in ch and len(ch.split(":", 1)[0]) <= 45:
            k, v = ch.split(":", 1)
            k_clean = k.strip()
            v_clean = clean_param_value(v.strip())
            if k_clean and v_clean:
                kv[k_clean] = v_clean
        else:
            free_parts.append(ch)

    desc_clean = " • ".join(free_parts) if free_parts else raw_desc
    return desc_clean[:2000], kv


def deduplicate_vendor_prefix(name_raw: str, vendor: Optional[str]) -> str:
    """Убирает дублирующий префикс вендора (например 'Adobe Systems Adobe InCopy...')."""
    s = " ".join(str(name_raw or "").split())
    if not s or not vendor:
        return s
    v = " ".join(vendor.split())
    v_tokens = v.split()

    # Случай 'Adobe Systems Adobe ...'
    if len(v_tokens) >= 2 and s.lower().startswith(v.lower() + " "):
        rest = s[len(v) :].strip()
        first_v = v_tokens[0].lower()
        if rest.lower().startswith(first_v + " "):
            return rest
    # Случай прямого дубля 'Kaspersky Kaspersky ...'
    double = f"{v} {v}"
    if s.lower().startswith(double.lower()):
        return s[len(v) :].strip()
    return s


def clean_product_name(name_dedup: str) -> str:
    """Вычищает хвосты с типом лицензии/сроком для более чистого canonical_name."""
    s = name_dedup
    # Аккуратно отрезаем хвосты после запятой со сроком/лицензией, но сохраняем основу
    s = re.sub(r",\s*(?:лицензия|подписка|продление|электронная\s+лицензия)\b.*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r",\s*\d+\s*(?:мес\.?|месяц(?:а|ев)?|год(?:а|ов)?|лет)\b.*$", "", s, flags=re.IGNORECASE)
    return " ".join(s.split()).strip(" ,.-") or name_dedup


def extract_attributes(name_raw: str, params_dict: Dict[str, str]) -> Dict[str, Any]:
    blob = f"{name_raw} " + " ".join(f"{k} {v}" for k, v in params_dict.items())

    term_months: Optional[int] = None
    m_mon = _TERM_MONTHS_RE.search(blob)
    if m_mon:
        val = int(m_mon.group(1))
        if 1 <= val <= 120:
            term_months = val
    else:
        m_yr = _TERM_YEARS_RE.search(blob)
        if m_yr:
            val = int(m_yr.group(1))
            if 1 <= val <= 10:
                term_months = val * 12

    qty_users: Optional[int] = None
    # Приоритет — прямому параметру с количеством пользователей/устройств
    for pk, pv in params_dict.items():
        pkl = pk.lower()
        if any(x in pkl for x in ("к-во пользователей", "кол-во пользователей", "количество пользователей", "число пк", "количество мест")):
            nums = re.findall(r"\d+", pv)
            if nums:
                qty_users = int(nums[-1])
                break
    if qty_users is None:
        m_qty = _QTY_RE.search(name_raw)
        if m_qty:
            qty_users = int(m_qty.group(1))

    license_type: Optional[str] = None
    m_lic = _LICENSE_TYPE_RE.search(name_raw)
    if m_lic:
        license_type = m_lic.group(1)
    elif "подписк" in name_raw.lower():
        license_type = "Subscription"
    elif "бессроч" in name_raw.lower():
        license_type = "Perpetual"

    version: Optional[str] = None
    m_ver = _VERSION_RE.search(name_raw)
    if m_ver:
        version = m_ver.group(1)

    return {
        "term_months": term_months,
        "qty_users": qty_users,
        "license_type": license_type,
        "version": version,
    }


def map_segment_label(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    if any(k in t for k in ("гос", "gov", "муниципаль", "бюджетн", "фстэк", "реестр")):
        return "gov"
    if any(k in t for k in ("образов", "школ", "вуз", "учеб", "academic", "edu", "студент", "преподав")):
        return "edu"
    if any(k in t for k in ("физлиц", "физ. лиц", "физическ", "домашн", "for family", "home", "personal", "для дома")):
        return "personal"
    if any(k in t for k in ("enterprise", "корпоратив", "крупн")):
        return "enterprise"
    if any(k in t for k in ("коммерческ", "юрлиц", "юр. лиц", "бизнес", "организац", "малог", "smb")):
        return "smb"
    return None


def resolve_segment_with_conflicts(
    name_raw: str,
    params_dict: Dict[str, str],
    desc_kv: Dict[str, str],
) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """
    Порядок доверия I2:
    1. Параметр 'Тип организации' + ключевые слова в name_raw
    2. Параметр 'Пользователь' и ключ description 'Тип клиента'
    Возвращает (выбранный сегмент, список конфликтов для data_quality_issues).
    """
    conflicts: List[Dict[str, str]] = []
    primary_sources: List[Tuple[str, str]] = []
    secondary_sources: List[Tuple[str, str]] = []

    name_seg = map_segment_label(name_raw)
    if name_seg:
        primary_sources.append(("name_raw", name_seg))

    for pk, pv in params_dict.items():
        pkl = pk.lower()
        seg = map_segment_label(pv)
        if not seg:
            continue
        if "тип организац" in pkl:
            primary_sources.append((f"param:{pk}", seg))
        elif "пользовател" in pkl or "тип клиент" in pkl:
            secondary_sources.append((f"param:{pk}", seg))

    for dk, dv in desc_kv.items():
        if "тип клиент" in dk.lower() or "пользовател" in dk.lower():
            seg = map_segment_label(dv)
            if seg:
                secondary_sources.append((f"desc:{dk}", seg))

    chosen = primary_sources[0][1] if primary_sources else (secondary_sources[0][1] if secondary_sources else None)

    if primary_sources and secondary_sources:
        p_seg = primary_sources[0][1]
        for sec_field, sec_seg in secondary_sources:
            # Конфликт, например: имя "для коммерческих" (smb) vs description "Тип клиента: физлицо" (personal)
            if p_seg != sec_seg and not ({p_seg, sec_seg} == {"smb", "enterprise"}):
                conflicts.append(
                    {
                        "field": sec_field,
                        "issue_type": "segment_conflict",
                        "details": f"{primary_sources[0][0]}={p_seg} конфликтует с {sec_field}={sec_seg}",
                    }
                )

    return chosen, conflicts


def normalize_offer(raw: Dict[str, Any], categories: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, str]], List[Tuple[str, str, str]]]:
    """Превращает сырой <offer> в нормализованную строку для offers.parquet + список проблем + params_long."""
    offer_id_raw = raw["offer_id_raw"]
    offer_id = offer_id_raw if offer_id_raw.startswith("o_") else f"o_{offer_id_raw}"
    group_raw = raw.get("group_id_raw")
    family_id = f"f_{group_raw}" if group_raw else f"f_o{offer_id_raw}"

    issues: List[Dict[str, str]] = []
    name_raw = raw.get("name") or raw.get("model") or ""
    vendor = raw.get("vendor") or None
    if vendor and vendor.lower() in PLACEHOLDERS:
        vendor = None

    name_dedup = deduplicate_vendor_prefix(name_raw, vendor)
    if name_dedup != " ".join(name_raw.split()):
        issues.append({"field": "name", "issue_type": "noisy_vendor_prefix", "details": f"Удалён дубль вендора: {name_raw[:80]}"})
    product_name_clean = clean_product_name(name_dedup)

    # Цены и скидки
    price_minor = parse_price_minor(raw.get("price"))
    oldprice_minor = parse_price_minor(raw.get("oldprice"))
    price_state = "priced" if price_minor is not None else "on_request"
    discount_pct: Optional[float] = None

    if price_minor is None:
        issues.append({"field": "price", "issue_type": "missing_price", "details": f"price={raw.get('price')}"})
    elif oldprice_minor is not None:
        if oldprice_minor < price_minor:
            issues.append({"field": "oldprice", "issue_type": "invalid_discount_oldprice_lt_price", "details": f"old={oldprice_minor} < price={price_minor}"})
        elif oldprice_minor > price_minor:
            pct = round((oldprice_minor - price_minor) / oldprice_minor * 100.0, 1)
            if pct > 70.0:
                issues.append({"field": "oldprice", "issue_type": "suspicious_discount_gt_70", "details": f"discount={pct}%"})
            elif pct > 0.0:
                discount_pct = pct

    # Параметры и description
    desc_clean, desc_kv = parse_description(raw.get("description"))
    params_dict: Dict[str, str] = {}
    params_long: List[Tuple[str, str, str]] = []
    for k, v in raw.get("params", []):
        v_clean = clean_param_value(v)
        if v_clean is not None:
            params_dict[k] = v_clean
            params_long.append((offer_id, k, v_clean))

    # Дополняем параметрами из description, если их нет в <param>
    for dk, dv in desc_kv.items():
        params_dict.setdefault(dk, dv)

    attrs = extract_attributes(name_raw, params_dict)
    segment, seg_conflicts = resolve_segment_with_conflicts(name_raw, params_dict, desc_kv)
    issues.extend(seg_conflicts)

    # Признаки класса и реестра
    product_type_raw = None
    delivery_type_raw = None
    is_ru_registry = False
    has_hw_params = False
    for pk, pv in params_dict.items():
        pkl = pk.lower()
        if pkl == "тип продукта":
            product_type_raw = pv
        elif pkl == "тип поставки":
            delivery_type_raw = pv
        elif "реестр" in pkl:
            low_pv = pv.lower()
            if not any(neg in low_pv for neg in ("нет", "не включ", "отсутств")):
                is_ru_registry = True
        if pkl in HARDWARE_PARAM_NAMES:
            has_hw_params = True

    if not is_ru_registry and "реестр" in name_raw.lower():
        is_ru_registry = True

    cat_raw = raw.get("categoryId")
    cat_id = f"c_{cat_raw}" if cat_raw and not str(cat_raw).startswith("c_") else cat_raw
    cat_info = categories.get(cat_id or "", {})
    image_url = raw["pictures"][0] if raw.get("pictures") else None
    if not image_url:
        issues.append({"field": "picture", "issue_type": "missing_image", "details": "Нет картинки"})

    params_text = "; ".join(f"{k}: {v}" for k, v in list(params_dict.items())[:15])

    offer_row = {
        "offer_id": offer_id,
        "family_id": family_id,
        "name_raw": name_raw,
        "product_name_clean": product_name_clean,
        "vendor": vendor,
        "category_id": cat_id,
        "category_path": cat_info.get("path", ""),
        "available": bool(raw.get("available")),
        "price_state": price_state,
        "price_minor": price_minor,
        "oldprice_minor": oldprice_minor,
        "discount_pct": discount_pct,
        "term_months": attrs["term_months"],
        "qty_users": attrs["qty_users"],
        "license_type": attrs["license_type"],
        "segment": segment,
        "version": attrs["version"],
        "url": raw.get("url") or None,
        "vendor_url": raw.get("vendorUrl") or None,
        "image_url": image_url,
        "description_clean": desc_clean,
        "params_text": params_text,
        "params_json": json.dumps(params_dict, ensure_ascii=False),
        "is_ru_registry": is_ru_registry,
        "product_type_raw": product_type_raw,
        "delivery_type_raw": delivery_type_raw,
        "has_hardware_params": has_hw_params,
    }
    return offer_row, issues, params_long