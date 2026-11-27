"""
Сбор и агрегация проблем качества карточек в data_quality_issues.parquet (I2, I7).
"""

from __future__ import annotations

from typing import Dict, List

import pandas as pd


def build_quality_issues_table(raw_issues: List[Dict[str, str]]) -> pd.DataFrame:
    if not raw_issues:
        return pd.DataFrame(columns=["family_id", "offer_id", "field", "issue_type", "details"])

    df = pd.DataFrame(raw_issues)
    # Оставляем уникальные по (family_id, field, issue_type), чтобы не раздувать таблицу 50 вариантами одной family
    df = df.drop_duplicates(subset=["family_id", "field", "issue_type"], keep="first").reset_index(drop=True)
    return df[["family_id", "offer_id", "field", "issue_type", "details"]]