from enum import Enum
from typing import Literal, Optional, Dict, List, Any
from pydantic import BaseModel, Field


class DataTier(str, Enum):
    real = "real"
    derived = "derived"
    judged = "judged"
    simulated = "simulated"
    modeled = "modeled"


class DegradationLevel(str, Enum):
    L0 = "L0"  # полный пайплайн
    L1 = "L1"  # без LLM (правила + кэш + dense)
    L2 = "L2"  # без dense-энкодера (L1, L2, L3, L5)
    L3 = "L3"  # базовый IndexState без overlay и БД


class PriceState(str, Enum):
    priced = "priced"
    on_request = "on_request"


class RelevanceLabel(str, Enum):
    exact = "exact"
    close = "close"
    similar = "similar"
    popular = "popular"


class ProductClass(str, Enum):
    software = "software"
    hardware = "hardware"
    cloud_service = "cloud_service"
    subscription = "subscription"
    certificate = "certificate"
    training_webinar = "training/webinar"
    other = "other"


class Segment(str, Enum):
    smb = "smb"
    enterprise = "enterprise"
    gov = "gov"
    edu = "edu"
    personal = "personal"


class StepId(str, Enum):
    S0 = "S0"   # приём и маскирование PII
    S1 = "S1"   # нормализация, раскладка, гомоглифы
    S2 = "S2"   # токенизация, лемматизация
    S3 = "S3"   # гипотезы написания (склейка, SymSpell, транслит, сленг)
    S4 = "S4"   # query understanding (правила / кэш / LLM)
    S5 = "S5"   # retrieval L1..L5 + RRF fusion + секции
    S6 = "S6"   # контекст и персонализация (±15%)
    S7 = "S7"   # ранжирование и overlay
    S8 = "S8"   # variant resolution (family -> offer)
    S9 = "S9"   # композиция и MMR
    S10 = "S10" # лестница гарантии >= 4 family
    S11 = "S11" # допродажи и ценовые бейджи
    S12 = "S12" # сборка ответа и запись события


class RootCause(str, Enum):
    R1 = "R1"   # опечатка / транслит / раскладка
    R2 = "R2"   # нет синонима / сленга / аббревиатуры
    R3 = "R3"   # бренд не в каталоге (EXTERNAL_PRODUCT)
    R4 = "R4"   # нет линейки / редакции / версии
    R5 = "R5"   # слабая карточка / конфликт параметров
    R6 = "R6"   # проблема ранжирования
    R7 = "R7"   # неоднозначность интента
    R8 = "R8"   # потерянный спрос из-за available=false / price=0
    R9 = "R9"   # не-товарный интент (вебинар, документация)
    R10 = "R10" # мусор / nonsense


class TicketType(str, Enum):
    ADD_SYNONYM = "ADD_SYNONYM"
    ADD_ALIAS = "ADD_ALIAS"
    CATALOG_GAP = "CATALOG_GAP"
    FIX_CARD = "FIX_CARD"
    RANKING_RULE = "RANKING_RULE"
    AVAILABILITY_ALERT = "AVAILABILITY_ALERT"
    INTENT_DISAMBIGUATION = "INTENT_DISAMBIGUATION"


class EventType(str, Enum):
    search_issued = "search_issued"
    suggest_shown = "suggest_shown"
    suggest_clicked = "suggest_clicked"
    result_impression = "result_impression"
    result_click = "result_click"
    chip_click = "chip_click"
    add_to_cart = "add_to_cart"
    quote_request = "quote_request"
    order = "order"
    cross_sell_click = "cross_sell_click"
    refine = "refine"
    abandon = "abandon"
    feedback = "feedback"
    catalog_gap_lead = "catalog_gap_lead"
    admin_action = "admin_action"


class ApiError(BaseModel):
    error_code: str
    message: str
    trace_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None