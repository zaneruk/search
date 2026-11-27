#!/usr/bin/env python3
"""
Детерминированная классификация product_class и product_type (~85–90% каталога за секунды).
Запуск:
  python -m pipelines.enrich.classify_rules --families data/interim/families.parquet
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import List, Tuple

import pandas as pd
import yaml

# Закрытая таксономия (50+ чётких типов для B2B ПО и электроники).
# Используется и как fallback, если config/taxonomy.yaml ещё не создан.
DEFAULT_TAXONOMY_RULES: List[Tuple[str, str, List[str]]] = [
    # (product_class, product_type, ключевые маркеры в category_path / name)
    ("training/webinar", "Обучение, курсы и вебинары", ["вебинар", "дистанционн", "обучен", "курс", "тренинг"]),
    ("certificate", "Сертификаты и техподдержка", ["сертификат", "техподдержк", "сервисн"]),
    ("cloud_service", "Облачная инфраструктура (IaaS)", ["iaas", "виртуальный сервер", "аренда сервер", "vps", "vds"]),
    ("cloud_service", "Облачные сервисы (SaaS)", ["облачн", "saas", "1с:фреш", "битрикс24"]),

    # --- Железо (hardware) ---
    ("hardware", "Серверное оборудование и СХД", ["серверн", "сервер", "стоек сервер", "схд", "nas", "rackmount", "xeon", "epyc"]),
    ("hardware", "ИБП и защита питания", ["ибп", "источник бесперебой", "ups", "стабилизатор", "ippon", "powercom"]),
    ("hardware", "Сетевое оборудование и Wi-Fi", ["коммутатор", "маршрутизатор", "роутер", "точка доступа", "wi-fi", "switch", "vlan", "tp-link", "mikrotik", "cisco", "keenetic"]),
    ("hardware", "Видеонаблюдение и ВКС-оборудование", ["видеонаблюд", "ip-камера", "видеорегистратор", "dahua", "hikvision", "yealink", "веб-камера", "конференц"]),
    ("hardware", "Принтеры, МФУ, сканеры и расходники", ["принтер", "мфу", "сканер", "картридж", "тонер", "canon", "cactus", "xerox", "kyocera"]),
    ("hardware", "Ноутбуки, ПК и моноблоки", ["ноутбук", "моноблок", "системный блок", "неттоп", "рабочая станция", "мини-пк"]),
    ("hardware", "Мониторы и дисплеи", ["монитор", "дисплей", "интерактивная панель", "проектор"]),
    ("hardware", "Накопители, память и комплектующие", ["ssd", "hdd", "накопитель", "флеш", "оперативная память", "видеокарта", "материнская плата", "процессор", "netac", "kingston"]),
    ("hardware", "Периферия и аксессуары", ["клавиатур", "мыш", "гарнитур", "наушник", "коврик", "кабель", "переходник", "a4tech", "logitech", "бюрократ"]),

    # --- Информационная безопасность (software) ---
    ("software", "Антивирусы и защита конечных точек (EPP/EDR)", ["антивирус", "endpoint", "kaspersky", "dr.web", "eset", "maxpatrol", "edr", "защита рабочих"]),
    ("software", "Криптография и электронная подпись (СКЗИ/ЭЦП)", ["крипто", "скзи", "эцп", "электронная подпись", "cryptopro", "vipnet", "рутокен", "jacarta"]),
    ("software", "Межсетевые экраны, DLP и SIEM", ["межсетевой экран", "firewall", "dlp", "siem", "анализ защищен", "сканер-вс", "уязвимост", "сетевая безопасн", "континент"]),
    ("software", "Резервное копирование и восстановление", ["резервн", "бэкап", "backup", "киберпротект", "кибер бэкап", "acronis", "veeam"]),

    # --- Системное и инфраструктурное ПО ---
    ("software", "Операционные системы", ["операционн", "astra linux", "windows 10", "windows 11", "windows server", "ред ос", "альт", "alt linux", "ubuntu", "red hat"]),
    ("software", "Виртуализация и контейнеризация", ["виртуализац", "гипервизор", "vmware", "zvirt", "брест", "proxmox", "docker", "kubernetes"]),
    ("software", "СУБД и работа с данными", ["субд", "баз данных", "postgres", "sql server", "oracle", "mysql", "аналитика данных", "bi"]),
    ("software", "Удалённый доступ и администрирование", ["удаленн", "удалённ", "anydesk", "teamviewer", "ассистент", "rudesktop", "rdp", "администрирован"]),
    ("software", "Системные утилиты и работа с дисками", ["системные программ", "работа с файлами", "архиватор", "диск", "дефрагмент"]),

    # --- Бизнес и Офис ---
    ("software", "Офисные пакеты и работа с документами", ["офисн", "microsoft office", "microsoft 365", "мойофис", "р7-офис", "word", "excel", "incopy"]),
    ("software", "Распознавание текста (OCR) и PDF", ["распознаван", "ocr", "pdf", "finereader", "content ai", "irisreadiris", "ridoc", "переводчик", "promt"]),
    ("software", "Бухгалтерия, кадры, CRM и ERP", ["бухгалтер", "кадр", "1с", "crm", "erp", "управление бизнесом", "склад", "торговл", "эдо", "диадок"]),
    ("software", "ВКС, корпоративная почта и коммуникации", ["видеоконференц", "вкс", "почтов", "мессенджер", "коммуникац", "trueconf", "контур.толк", "exchange", "мтс", "webinar", "zoom"]),
    ("software", "Управление проектами и таск-трекеры", ["управление проектами", "таск", "jira", "kaiten", "трекер"]),

    # --- Специализированное ПО ---
    ("software", "САПР, CAD и ГИС", ["сапр", "гис", "cad", "черчен", "компас", "autocad", "nanocad", "revit", "3d модел", "карты, навигация"]),
    ("software", "Графика, дизайн и мультимедиа", ["график", "дизайн", "мультимедиа", "фото", "видео", "аудио", "photoshop", "coreldraw", "illustrator", "монтаж"]),
    ("software", "Инструменты разработки (IDE и DevTools)", ["программирован", "ide", "разработк", "sdk", "jetbrains", "visual studio", "git", "компилятор"]),
    ("software", "Искусственный интеллект и ML", ["искусственный интеллект", "нейросет", "машинное обучен", "speech", "stenogram"]),
    ("software", "Образование и наука", ["образован", "наук", "математик", "статистик"]),
    ("software", "Мобильные программы", ["смартфон", "android", "ios", "мобильн"]),
]

# Ключевые слова, для которых ОБЯЗАТЕЛЬНЫ точные границы слова (иначе 'nas' ловит 'panasonic', 'edr' -> 'edrawmax')
_EXACT_WORD_KWS = {
    "nas", "edr", "eset", "ибп", "схд", "эцп", "скзи", "вкс", "сапр",
    "гис", "бд", "ocr", "pdf", "cad", "erp", "crm", "ide", "sdk", "git",
    "ai", "bi", "rdp", "dlp", "siem", "ups", "vlan", "ssd", "hdd",
    "vps", "vds", "iaas", "saas", "1с", "мфу", "эдо", "мтс", "zoom",
    "word", "excel", "альт", "брест", "xeon", "epyc", "cisco", "asus",
    "сканер", "сервер",
}

# Морфологические основы, которые должны начинаться с границы слова (иначе 'мыш' ловит 'промышленный')
_PREFIX_WORD_KWS = {
    "мыш", "монитор", "принтер", "роутер", "дисплей", "кабель", "курс",
}

_KW_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _kw_match(kw: str, text: str) -> bool:
    if not text or not kw:
        return False
    # Специальная защита: "монитор" не должен матчить "мониторинг"
    if kw == "монитор":
        pat = _KW_PATTERN_CACHE.get("__monitor__")
        if pat is None:
            pat = re.compile(r"(?<![a-zа-яё0-9])монитор(?!инг)", re.IGNORECASE)
            _KW_PATTERN_CACHE["__monitor__"] = pat
        return pat.search(text) is not None

    if kw in _EXACT_WORD_KWS or len(kw) <= 4:
        pat = _KW_PATTERN_CACHE.get(f"exact:{kw}")
        if pat is None:
            pat = re.compile(rf"(?<![a-zа-яё0-9]){re.escape(kw)}(?![a-zа-яё0-9])", re.IGNORECASE)
            _KW_PATTERN_CACHE[f"exact:{kw}"] = pat
        return pat.search(text) is not None

    if kw in _PREFIX_WORD_KWS:
        pat = _KW_PATTERN_CACHE.get(f"prefix:{kw}")
        if pat is None:
            pat = re.compile(rf"(?<![a-zа-яё0-9]){re.escape(kw)}", re.IGNORECASE)
            _KW_PATTERN_CACHE[f"prefix:{kw}"] = pat
        return pat.search(text) is not None

    return kw in text


def load_taxonomy_allowed(taxonomy_path: Path) -> List[str]:
    if taxonomy_path.exists():
        data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("product_types"), list):
            return [str(x) for x in data["product_types"]]
    return sorted({pt for _, pt, _ in DEFAULT_TAXONOMY_RULES} | {"Прочее ПО", "Прочее оборудование"})


def classify_row(row: pd.Series) -> Tuple[str, str, bool]:
    """Возвращает (product_class, product_type, is_confident)."""
    raw_pt = str(row.get("product_type_raw") or "").lower()
    cat_path = str(row.get("category_path") or "").lower()
    name = str(row.get("canonical_name") or "").lower()
    vendor = str(row.get("vendor") or "").lower()
    has_hw = bool(row.get("has_hardware_params"))
    current_class = str(row.get("product_class") or "other")

    blob_high = f"{name} | {cat_path}"
    blob_all = f"{name} | {vendor} | {cat_path}"

    is_elec = ("электроник" in raw_pt) or has_hw
    hw_name_markers = ("коммутатор", "маршрутизатор", "ибп", "ноутбук", "монитор", "принтер", "мфу")

    # Если в самом названии явно "сертификат / техподдержка" и нет "вебинар/курс" — не пускаем в training/webinar
    looks_like_cert = ("сертификат" in name or "техподдерж" in name) and not (
        _kw_match("вебинар", name) or _kw_match("курс", name)
    )

    for p_class, p_type, keywords in DEFAULT_TAXONOMY_RULES:
        if p_class == "training/webinar" and looks_like_cert:
            continue
        if is_elec and p_class == "software":
            continue
        if (not is_elec) and p_class == "hardware":
            if not any(_kw_match(kw, name) for kw in hw_name_markers):
                continue

        if any(_kw_match(kw, blob_all) for kw in keywords):
            if p_type == "Серверное оборудование и СХД" and ("софт" in raw_pt and not has_hw):
                continue
            return p_class, p_type, True

    if is_elec:
        return "hardware", "Прочее оборудование", False
    if "софт" in raw_pt or "программ" in blob_high:
        return "software", "Прочее ПО", False
    return current_class if current_class != "other" else "software", "Прочее ПО", False


def run_rules(families_path: Path, taxonomy_path: Path, out_path: Path) -> pd.DataFrame:
    df = pd.read_parquet(families_path)
    classes: List[str] = []
    types: List[str] = []
    confident_flags: List[bool] = []

    for _, row in df.iterrows():
        pc, pt, conf = classify_row(row)
        classes.append(pc)
        types.append(pt)
        confident_flags.append(conf)

    df["product_class"] = classes
    df["product_type"] = types
    df["classify_confident"] = confident_flags

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    n_total = len(df)
    n_conf = sum(confident_flags)
    print(f"Классифицировано правилами: {n_conf}/{n_total} ({n_conf * 100.0 / max(1, n_total):.1f}%)")
    print("Распределение product_class:", dict(Counter(classes)))
    print("Топ-10 product_type:", Counter(types).most_common(10))

    unmapped_cats = df[~df["classify_confident"]]["category_id"].nunique()
    print(f"Осталось неразмеченных/общих категорий для classify_llm.py: {unmapped_cats}")
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--families", default="data/interim/families.parquet")
    parser.add_argument("--taxonomy", default="config/taxonomy.yaml")
    parser.add_argument("--out", default="data/interim/families.parquet")
    args = parser.parse_args()
    run_rules(Path(args.families), Path(args.taxonomy), Path(args.out))


if __name__ == "__main__":
    main()