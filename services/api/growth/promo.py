"""Бейджи карточек (S11) со строгим соблюдением инвариантов I2 и I3 (CONTRACTS.md A.4, A.6)."""
from __future__ import annotations

from typing import List, Optional

from services.api.app.schemas import Badge, OfferView, PriceState


def compute_discount_pct(price_minor: Optional[int], oldprice_minor: Optional[int]) -> Optional[float]:
    if price_minor is None or oldprice_minor is None:
        return None
    if oldprice_minor > price_minor > 0:
        pct = round((oldprice_minor - price_minor) / oldprice_minor * 100.0, 1)
        return pct if 0.0 < pct <= 70.0 else None
    return None


def build_badges(
    representative: OfferView,
    *,
    is_ru_registry: bool = False,
    price_advantage_badge: Optional[Badge] = None,
) -> List[Badge]:
    badges: List[Badge] = []

    # I3: бейдж promo выдаётся ТОГДА И ТОЛЬКО ТОГДА, когда oldprice_minor > price_minor > 0 и discount_pct <= 70
    if (
        representative.oldprice_minor is not None
        and representative.price_minor is not None
        and representative.oldprice_minor > representative.price_minor > 0
    ):
        pct = compute_discount_pct(representative.price_minor, representative.oldprice_minor)
        if pct is not None:
            badges.append(
                Badge(
                    type="promo",
                    label=f"Акция −{pct:g}%",
                    basis={"oldprice_minor": representative.oldprice_minor, "price_minor": representative.price_minor, "discount_pct": pct},
                )
            )

    if price_advantage_badge is not None:
        badges.append(price_advantage_badge)

    if representative.available:
        badges.append(Badge(type="available", label="В наличии"))

    if representative.price_state == PriceState.on_request:
        badges.append(Badge(type="on_request", label="Цена по запросу"))

    if is_ru_registry:
        badges.append(Badge(type="ru_registry", label="Реестр РФ"))

    return badges