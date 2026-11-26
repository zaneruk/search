# CONTRACTS.md — «Умный поиск» для store.softline.ru (MVP)

`contract_version: 2.0.0-mvp` · дата: 2026-11-26 · статус: DRAFT → заморозка F0 через 3 часа после старта (см. B.1)
Основа: обновлённая MVP-архитектура `smart_search_architecture.md` (шаги S0–S12, lanes L1–L5, лестница ≥4, режимы деградации L0–L3, причины R1–R10).

## Как пользоваться этим файлом

1. Это единственный источник истины по метрикам, unit-экономике, форматам данных, SQL-схеме и API. Любое расхождение кода с этим файлом считается багом кода.
2. **Единый источник контрактов API** — Pydantic v2 модели в `services/api/app/schemas/` (раздел B.4). Из них FastAPI автоматически генерирует `/openapi.json`, а фронтенд генерирует TypeScript-типы командой `openapi-typescript`. Отдельные 17 JSON-схем и скрипты-экстракторы в MVP не используются.
3. Правила совместной работы и владения каталогами описаны в B.1.

## Оглавление

- PART A. Оценка качества и unit-экономика (A.1–A.13)
- PART B. Единые контракты (B.1–B.11)

---

# PART A. Оценка качества и unit-экономика

## A.1 Ярусы данных (tiers) и правила честности

| Tier | Код | Что это | Примеры | Что можно утверждать |
|---|---|---|---|---|
| REAL | `real` | Данные от организатора или живые действия пользователей в демо | Фид (136 040 offers), GOLD10 (xlsx), паспорт фида, ответы Q&A, Метрика (если найдена), живые клики жюри (`source=live`) | Факты о каталоге и поведении legacy/живых пользователей |
| DERIVED | `derived` | Синтетика, построенная из REAL правилами, метки по построению | SYN-A, оценки времени ответа и стоимости по счётчику | Качество поиска на классах запросов, воспроизводимо и с ДИ |
| JUDGED | `judged` | Оценки LLM-judge и выборочная ручная проверка | SYN-B + LLM-judge + ручной аудит 30–40 строк | Качество на сложных NL/сленг-запросах с оговоркой о методе |
| SIM | `simulated` | Сессии, клики, заказы от симулятора трафика | Воронка дашборда, наполнение графиков на старте | Только работоспособность механизмов и замкнутого цикла |
| MODEL | `modeled` | Детерминированный расчёт на допущениях (3 сценария) | Потерянные заказы, дополнительная выручка, прибыль | Только как сценарная оценка с явными допущениями |

Правила (обязательны, проверяются в `eval/report.py`, API и UI):

- **R1.** Каждое число в отчёте, API и UI несёт поле `data_tier`.
- **R2.** Числа разных tier не складываются и не усредняются в одно.
- **R3.** `simulated` и `modeled` не используются как доказательство качества поиска.
- **R4.** Ручной подбор весов, порогов и правка синонимов делаются только на `dev`-сплите. `holdout` и `GOLD10` используются только для финального замера.
- **R5.** Доли сопровождаются `n` и доверительным интервалом Уилсона. Страты с `n < 30` публикуются описательно, без ДИ.
- **R6.** Запрещены gold-специфичные хаки: в `config/seed_glossary.yaml` нет строк, дословно совпадающих с запросами `GOLD10` (проверка `eval/check_gold_leak.py`).

## A.2 Наборы данных

| ID | Tier | Размер | Как строится | Метки |
|---|---|---|---|---|
| GOLD10 | `real` | 10 | `Выборка_поисковых_запросов.xlsx` + `eval/datasets/gold_map.csv` | Целевая страница (товар, категория или контент). Маппинг URL → `family_id`/`entity_id` вручную |
| SYN-A | `derived` | 300–500 | Детерминированный генератор `eval/gen/make_syn_a.py` из фида (без LLM), seed фиксирован | По построению: источник-family даёт grade 3 |
| SYN-B | `derived`+`judged` | 50–100 | LLM-генерация (отдельный промпт, не тот, что в `enrich`) + `LLM-judge` по top-5 + ручная проверка 30–40 строк глазами | grade по построению + вердикт судьи (0–3) |
| SIM | `simulated` | по запросу | `services/worker/jobs/simulator.py` | Клики и заказы по модели, всегда `source=simulated` |

Ориентировочные квоты `SYN-A` (≈400 запросов): `exact_name` 60, `brand` 40, `brand_attr` 50 (версия, срок, qty, сегмент), `generic_type` 40, `relational` 30, `typo_1edit` 40, `typo_2edit` 25, `translit` 30, `layout` 20, `split_join` 25, `numeric_only` 10, `out_of_catalog` 20, `nonsense` 10.
Ориентировочные квоты `SYN-B` (≈80 запросов): `slang` 15, `abbreviation` 15, `nl_task` 25, `head_noun_hardware` 10, `ambiguous` 10, `analog` 5.

Градуированная шкала релевантности:
- `grade 3`: целевая family (источник запроса).
- `grade 2`: тот же бренд, тот же `product_type`, та же линейка (соседние редакции/версии).
- `grade 1`: тот же `product_type`.
- `grade 0`: остальное.
- Для `out_of_catalog`: ожидание — баннер «в каталоге нет» и событие `catalog_gap`, без метки «Точное совпадение» у чужих товаров.
- Для `GOLD10`: бинарно, совпадение с целью (`target_kind ∈ {category, content}` засчитывается при попадании в `nav_hits[0]` или целевую секцию).

`eval/datasets/gold_map.csv`: `query,target_url,target_kind,target_family_id,target_entity_id,present_in_feed,notes`. Строки с `present_in_feed=false` считаются отдельно как каталожный пробел.

## A.3 Сплиты и защита от утечек

1. Сплит по `family_id`: `split = dev if int(sha1(family_id)[:8],16) % 10 < 7 else holdout`.
2. `SYN-B` генерируется отдельным промптом. Запрос исключается из `SYN-B`, если `query_norm` дословно совпадает с алиасом из индекса (`eval/gen/dedupe_vs_index.py`).
3. Подбор 6–8 весов ранжирования и порогов проводится только на `dev`. `holdout` открывается только для финальной таблицы отчёта.
4. `seed_glossary.yaml` не содержит дословных строк `GOLD10` (R6).

## A.4 Метрики поиска и инварианты

Единица оценки: distinct `family_id` (в порядке отображения в `sections[*].items`, `nav_hits` учитываются отдельно).

| Метрика | Определение | Набор | Tier |
|---|---|---|---|
| Hit@k (k=1,4) | Доля запросов, где среди первых `k` family есть релевантная (`SYN`: grade ≥ 2, `GOLD`: цель) | GOLD10, SYN | real / derived |
| MRR@10 | Среднее `1/rank` первой релевантной | SYN | derived |
| nDCG@10 | DCG с `gain = 2^grade − 1`, нормировка на идеальный порядок | SYN | derived |
| Recall@150 | Доля релевантных (grade ≥ 2) в кандидатах на выходе S5 | SYN | derived |
| Hard-null rate | Доля запросов с 0 кандидатов до лестницы S10 | SYN, Legacy | derived |
| Soft-null rate | Доля запросов, где S10 дошёл до `rung ≥ 3` или `top1_confidence < τ_soft` | SYN | derived |
| Failure rate `f` | `1 − Hit@4` по смеси классов | SYN | derived |
| Intent-P@4 | Доля top-4 с корректным `product_class` (особенно на `head_noun_hardware`: «сервер» и др.) | SYN-B | derived/judged |
| Typo-recovery@4 | Hit@4 на `typo_1edit` и `typo_2edit` | SYN-A | derived |
| False-correction rate (FCR) | Доля `exact_name`/`brand`, у которых авто-исправление сменило верный top-1 | SYN-A | derived |
| OOC precision | Доля `out_of_catalog` с баннером «в каталоге нет» и без «точных» бейджей | SYN-A | derived |
| ExactShare@4 / FillRate | Доля позиций в top-4 с `relevance_label ∈ {exact, close}` / доля запросов с `fill_rung > 0` | SYN | derived |

**Инварианты (автотесты, цель 100%):**
- **I1.** `len(distinct families в sections) ≥ 4` для любой не-ошибочной строки (включая весь eval, пустой запрос и fuzz-набор спецсимволов).
- **I2.** Никогда не отдаётся цена `0`; `price_state = "on_request"` ⇔ `price_minor is None`.
- **I3.** Бейдж `promo` выдаётся тогда и только тогда, когда у представителя `oldprice_minor > price_minor > 0` и `discount_pct ≤ 70`.
- **I4.** Идемпотентность: одинаковые `(query, ctx, config_version, index_version)` → одинаковый порядок `family_id`.
- **I5.** Позиции с `relevance_label ∈ {similar, popular}` обязаны иметь непустой `fill_reason`.
- **I6.** Блок допродаж не содержит `available=false`, `on_request` как основного предложения, `purchased_family_ids` пользователя и дублей из основной выдачи.
- **I7.** При `degradation_level ∈ {L1, L2, L3}` ответ остаётся валидным по Pydantic-схеме и выполняет I1.

## A.5 Метрики подсказок, допродаж и цен

| Метрика | Определение | Набор | Tier |
|---|---|---|---|
| SuggestMRR | `1 / позиция` первой подсказки к целевой family (префиксы длины 3, 5, 7) | SYN-A | derived |
| Product-Hit@4@k | Цель среди 4 товаров на лету для префикса длины `k ∈ {3, 5, 7}` | SYN-A | derived |
| Suggest latency | p50 / p95 серверного времени `/v1/suggest` | load test | real (замер) |
| Cross-sell check | Доля валидных рёбер в выборке top-3 допродаж (выборочная проверка + `LLM-judge`) | 30–50 запросов | judged |
| Evidence coverage | Доля рёбер графа с `evidence_url` из `vendors_seed.yaml` | граф | derived |
| Price-advantage correctness | Доля бейджей «выгоднее аналога» с воспроизводимой нормализацией (`n_compared ≥ 3`) | все бейджи | derived |

## A.6 Бейджи и цена: правила проверки

- `discount_pct = round((oldprice_minor - price_minor) / oldprice_minor * 100, 1)`. При `discount_pct > 70` акция считается подозрительной и не выводится.
- Бейдж «Выгоднее аналога на Y% за пользователя/год» считается **только внутри каталога**: единый `product_type`, сегмент, `license.type`, нормализация цены к `price_per_user_year_minor`, `n_compared ≥ 3`. В `badge.basis` пишутся `n_compared`, `unit`, `compared_family_id`.
- Внешние цены конкурентов (`ExternalPriceProvider`) в MVP исключены и в выдаче не используются.

## A.7 Статистика

- Доли (`Hit@k`, `FCR` и т.п.): 95% интервал Уилсона (для `n=10` в `GOLD10`: `10/10 → [0.72; 1.00]`, `9/10 → [0.60; 0.98]`; `GOLD10` используется как smoke-тест, а не статистическое доказательство).
- Сравнения `Full Smart vs Legacy`, `Smart L1 vs L0`, `Hybrid vs BM25F`: парный бутстрэп по запросам (10 000 ресэмплов, seed фиксирован) и критерий Макнемара для бинарного `Hit@k`. Публикуется `Δ` с 95% ДИ.

## A.8 Проверка аналитики и замкнутого цикла (MVP)

Полноценный стенд из 9 искусственных инъекций (`FAULT`) и автоматического одобрения (`auto-policy`) вынесен за пределы MVP. Работоспособность агента и замкнутого цикла подтверждается следующим протоколом:

1. **Сквозной replay по 3–5 реальным/симулированным кластерам провалов** (недостающий синоним/сленг R2, опечатка/транслит R1, `EXTERNAL_PRODUCT` R3).
2. **Контрфактическая проверка (`dry-run`):** каждый тикет агента обязан содержать результат вызова `POST /internal/probe` (позиция цели до и после кандидатного патча, `dry_run_uplift`).
3. **Защита от регрессий при ручном применении:** нажатие «Одобрить и применить» автоматически прогоняет `GOLD10-check` перед фиксацией `config_version + 1`. При падении `GOLD10` применение блокируется.
4. **Метрики цикла (публикуются как факт прогона):**
   - Время реакции `hot-swap` (мс);
   - Доля восстановленных запросов из кластера после `apply` (мгновенный `replay`);
   - Корректность отката по кнопке «Откатить версию конфига»;
   - Стоимость диагностики: `cost_ledger.purpose = 'agent_diagnose'` / число тикетов.

## A.9 Сравнение конфигураций (4 контрольные точки)

Вместо 10-шаговой матрицы `C0–C9` и Optuna сравниваем **4 понятные конфигурации** (управляются флагами `options` в `SearchRequest` и `config/search.yaml`):

| Точка | Состав | Что проверяет |
|---|---|---|
| **P0** | Legacy-эмулятор | Воспроизведение описанной проблемы («сервер» → ПО для сервера) |
| **P1** | BM25F + Entity/Alias + Fuzzy (`L1 + L2 + L3`) | Базовый лексический поиск без нейросетей |
| **P2** | Hybrid (`P1 + Dense L4 + Type-injection L5 + QU-правила`) | Полное детерминированное ядро (соответствует режиму деградации L1) |
| **P3** | Full Smart (`P2 + Контекст персоны + LLM-QU эскалация`) | Целевой режим работы (L0) |

Веса ранжирования (6–8 основных коэффициентов) подбираются вручную на `dev`. Проверка влияния маржи (`margin_weight ∈ {0, default, 3×default}`): при `default` ΔnDCG@10 должна быть `≥ −0.01`, чтобы маржа не ломала релевантность.

## A.10 Бизнес-эффект: калькулятор 3 сценариев (`tier=modeled`)

Вместо Monte Carlo на 10 000 прогонов в админке и отчётах используется **детерминированный калькулятор по 3 сценариям** с редактируемыми допущениями (`POST /v1/admin/impact-model`):

| Переменная | Смысл | Источник / Tier | Сценарий: Пессимист. | Базовый | Оптимист. |
|---|---|---|---|---|---|
| `Q` | Поисковых сессий в месяц | организатор: 10–20k (`real`) | 10 000 | 15 000 | 20 000 |
| `f_b` | Доля неудачных поисков «до» | организатор: «до 40%» (`real` граница / допущение) | 0,20 | 0,30 | 0,40 |
| `f_n` | Доля неудачных «после» | измеряется `1 − Hit@4` на holdout (`derived`) | из eval | из eval | из eval |
| `CVR_s` | Конверсия успешного поиска | организатор: 5–10% (`real`) | 0,05 | 0,075 | 0,10 |
| `κ` | Отношение CVR неудачного поиска к успешному | допущение (`assumption`) | 0,50 | 0,30 | 0,15 |
| `AOV` | Средний чек | медиана/среднее цен фида (`proxy`) | 0,7×AOV | 1,0×AOV | 1,3×AOV |
| `m` | Маржинальность | допущение / ввод e-com менеджера | 0,15 | 0,20 | 0,25 |
| `p_exit` | Уходы после неудачного поиска | организатор: 30–50% (`real`) | 0,30 | 0,40 | 0,50 |

Формулы:
```text
R             = Q · max(0, f_b − f_n)           # восстановленные сессии / мес
ΔOrders       = R · CVR_s · (1 − κ)             # дополнительные заказы / мес
ΔRevenue      = ΔOrders · AOV                   # доп. выручка, ₽ / мес
ΔProfit       = ΔRevenue · m                    # доп. валовая прибыль, ₽ / мес
LostOrders(f) = Q · f · CVR_s · (1 − κ)         # потерянные онлайн-заказы при уровне f
Exits(f)      = Q · f · p_exit                  # уходы после неудачного поиска

KPI организатора в интерфейсе:
KPI	Как считаем	Tier	Допустимая подпись в UI
Доля запросов без результата	hard_null / soft_null на SYN, Legacy vs Smart	derived	«На синтетической выборке из каталога»
Уходы после неудачного поиска	Exits(f_b) vs Exits(f_n) + воронка симулятора	modeled / simulated	«Модель при p_exit 30–50% / демо-трафик»
Конверсия поиска в заказ	Воронка симулятора сессий	simulated	«Демо-воронка (симулятор)»
Потеря онлайн-заказов	LostOrders(f_b) − LostOrders(f_n)	modeled	«Сценарный расчёт (Песс./Баз./Опт.)»

A.11 Протокол отчёта и лестница утверждений
Артефакты: eval/reports/<run_id>.json, eval/reports/REPORT.md, eval/reports/cost_report.csv. В каждом отчёте фиксируются git_sha, config_version, index_version, версии моделей и seed.
Утверждение |	Допустимо	| Недопустимо
Качество на GOLD10	|«k из 10 (ДИ Уилсона …), нейтральный контекст, без gold-хаков»	|«Качество поиска 100%» без n и ДИ
Качество на синтетике|	«DERIVED: на holdout SYN-A/B Hit@4 = …»|	«Качество на реальных пользователях»
Конверсия|	«В демо-воронке (SIM) …»|	«Конверсия выросла на N%»
Бизнес-эффект|	«MODEL (базовый сценарий): … ₽/год при допущениях …»|	«Система принесёт N млн ₽»
Стоимость запроса|	«Замерено счётчиком cost_ledger на N запросов по тарифам на дату D»	| Оценка «на глаз»

## A.12 Unit-экономика: стоимость одного запроса
Итоговая выгрузка формируется в eval/reports/cost_report.csv с колонками component, driver, unit, quantity_per_query, unit_price_rub, price_source, price_date, cost_per_query_rub, share_pct, basis (basis ∈ {measured, assumed}) и переносится в шаблон организатора. Все деньги хранятся в целых микрорублях (cost_micro_rub).

### Состав и формулы

```
C_q            = C_llm + C_cpu + C_fixed_loaded + C_off + C_agent   # ₽ / запрос
C_llm          = P_esc · (1 − h_qu) · [ (t_in − t_c)·p_in + t_c·p_c + t_out·p_out ] · (1 + ε_retry)
C_cpu          = Σ_stage cpu_s_stage · c_cpu                          # маржинальная CPU-стоимость (включая CPU-энкодер)
c_cpu          = Price_VPS_month / (vCPU · 2 592 000 с)               # ₽ / CPU-сек
C_fixed_loaded = Price_VPS_month / Q_month                            # хостинг на 1 запрос
C_off          = (Σ разовых офлайн-затрат) / (H · Q_month)            # амортизация на H ∈ {1, 3, 12} мес
C_agent        = (runs_month · cost_per_run) / Q_month
```
Бюджеты токенов (ориентиры MVP; после прогона заменяются фактом из cost_ledger)


| Purpose | Вход, токенов | Выход, токенов | Частота |
|---|---|---|---|
| qu (онлайн)|	≈ 1 000–1 400|	≈ 80–150|	P_esc · (1 − h_qu) от запросов
classify_category|	≈ 350|	≈ 60	|Только спорные крупные категории (~300–600 вызовов разово)
enrich_family|	≈ 700|	≈ 500|	Топ-500–1000 популярных family + демо-сценарии (разово)
vendor_extract|	≈ 2 500|	≈ 400|	1–2 эталонные страницы для demo-скрипта (основа — ручной vendors_seed.yaml)
agent_diagnose|	≈ 10 000–25 000|	≈ 1 500|	По запуску / расписанию
judge|	≈ 450|	≈ 20|	SYN-B top-5 (≈ 250–500 пар)
synth_queries|	≈ 600|	≈ 400|	Генерация SYN-B (разово)


### Измерение и основные рычаги оптимизации
Единый источник правды — таблица cost_ledger в SQLite (purpose, provider, model, tokens_in, tokens_cached, tokens_out, latency_ms, price_snapshot_id, cost_micro_rub, trace_id, status).
Скрипт scripts/cost_report.py считает среднее, p50 и p95 по прогону ≥ 500 смешанных запросов (отдельно с холодным и прогретым кэшем QU).
Основные рычаги снижения стоимости:
Порог эскалации к LLM (эскалация только при низком confidence правила / NL / 0 результатах).
Кэш QU (query_norm + ctx_bucket + overlay_version) с прогревом для демо-сценариев.
Компактный промпт (коды таксономии и entity_id, max_tokens по схеме).
Точечное офлайн-обогащение (только топ-500–1000 вместо 136k).
Каскад моделей и жёсткий дневной лимит бюджета с автопереходом в L1.
A.13 Гейты готовности к сдаче
Проверка	Порог
Инварианты I1–I7	100%
GOLD10	0 регрессий относительно базовой версии
Hit@4 на SYN-A (exact_name, brand)	≥ 0,95
Typo-recovery@4 (1 правка)	≥ 0,90
FCR (ложное переисправление)	≤ 1%
p95 /v1/suggest (сервер)	≤ 60 мс
p95 /v1/search в L1 (без LLM)	≤ 400 мс
p95 /v1/search в L0 (таймаут LLM 1,5 с + fallback на L1)	≤ 1,7 с
OpenAPI ↔ TS types генерация и smoke-тесты	100%

---

# PART B. Единые контракты

## B.1 Работа между аккаунтами и сессиями

### Треки и владение

Контур	Каталоги	Потребляет	Производит
1. Search Core & API	services/api/app/{schemas,routers/public.py,routers/internal.py}, services/api/{core,nlp,retrieval,ranking,growth,suggest,legacy,llm}, config/search.yaml	артефакты индекса, overlay, конфиги	/v1/search, /v1/search/stream, /v1/suggest, /internal/probe, OpenAPI
2. Data & Index	pipelines/**, config/{taxonomy.yaml,seed_glossary.yaml,vendors_seed.yaml}, prompts/{classify_category,enrich_family,vendor_extract}/	feed.yml, gold.xlsx	data/artifacts/<index_version>/, qc_report.json
3. Analytics & Agent	services/worker/**, services/api/app/routers/admin_*.py, prompts/agent_diagnose/	события, трассы, /internal/probe	админ-API, тикеты, overlay-версии, симулятор
4. Frontend (4 экрана)	web/**	/openapi.json → types.gen.ts, SSE	4 экрана SPA (Витрина+Trace, E-com+Head, Агент+Контент, Ops+Cost)
5. Eval & Cost	eval/**, scripts/cost_report.py, config/price_snapshots.json, prompts/{judge,synth_queries}/	API search, cost_ledger	REPORT.md, eval_report.json, cost_report.csv



### Заморозка и версионирование
F0 (T+3 ч): заморозка схем в services/api/app/schemas/ и contracts/sql/001_init.sql.
После F0: добавление опциональных полей в Pydantic (Optional[...] = None) не ломает клиентов; переименование/удаление обязательных полей требует уведомления всех контуров и перегенерации web/src/api/types.gen.ts.
Закрытые enum (менять только согласованно): StepId, DegradationLevel, EventType, TicketType, RootCause, DataTier.
Открытые enum (можно расширять): BadgeType, Segment, ProductClass, EdgeType.


## B.2 Структура репозитория

```
smart-search/
├── README.md
├── CONTRACTS.md                       # этот файл
├── LICENSES.md                        # перечень моделей, библиотек и лицензий
├── Makefile                           # setup | run | build-index | typegen | test | eval | cost-report
├── pyproject.toml
├── docker-compose.yml                 # api, worker, web (caddy)
├── .env.example                       # ключи LLM, ADMIN_TOKEN, INTERNAL_TOKEN, LLM_DAILY_BUDGET_MICRO_RUB
│
├── contracts/
│   └── sql/
│       └── 001_init.sql               # единая SQL-схема SQLite (B.6)
│
├── config/
│   ├── search.yaml                    # ручные веса, пороги, таймауты
│   ├── taxonomy.yaml                  # product_class, product_type, матрица допустимых пар допродаж
│   ├── seed_glossary.yaml             # сленг, аббревиатуры, транслит-алиасы (без GOLD10!)
│   ├── vendors_seed.yaml              # ручной seed по 8–12 брендам с evidence_url и EXTERNAL_PRODUCT
│   ├── personas.json                  # синтетические демо-профили покупателей
│   ├── demo_scenarios.json            # сценарные кнопки витрины
│   └── price_snapshots.json           # тарифы LLM-провайдеров и курс USD/RUB
│
├── prompts/                           # промпты по задачам
│   ├── qu/                v1.md
│   ├── classify_category/ v1.md
│   ├── enrich_family/     v1.md
│   ├── vendor_extract/    v1.md       # для demo-скрипта
│   ├── agent_diagnose/    v1.md
│   ├── judge/             v1.md
│   └── synth_queries/     v1.md
│
├── pipelines/                         # Контур 2: Data & Index
│   ├── ingest/{parse_yml.py,normalize.py,families.py,quality.py,qc_report.py}
│   ├── enrich/{classify_rules.py,classify_llm.py,enrich_top.py,demo_vendor_extract.py}
│   ├── embed/{embed_families.py,bake_off.py}
│   ├── gold/map_gold.py
│   └── build_index/{build.py,manifest.py}
│
├── services/
│   ├── api/                           # Контур 1 + admin-роутеры Контура 3
│   │   ├── app/
│   │   │   ├── main.py, deps.py, settings.py, errors.py
│   │   │   ├── schemas/               # ИСТОЧНИК ИСТИНЫ API (B.4): common.py, search.py, admin.py, llm.py
│   │   │   └── routers/               # public.py, internal.py, admin_analytics.py, admin_agent.py, admin_ops.py
│   │   ├── core/{index_state.py,overlay.py,degradation.py,cache.py,trace.py,cost_meter.py,events.py}
│   │   ├── nlp/{normalize.py,tokenize.py,hypotheses.py,symspell_dict.py,translit.py,layout.py,qu_rules.py,qu_llm.py}
│   │   ├── retrieval/{lanes/{l1_entity.py,l2_bm25f.py,l3_ngram.py,l4_dense.py,l5_type.py},fusion.py,filters.py}
│   │   ├── ranking/{features.py,scorer.py,variants.py,compose.py,ladder.py}
│   │   ├── growth/{crosssell.py,promo.py,price_benchmark.py}
│   │   ├── suggest/{typeahead.py,trie.py}
│   │   ├── legacy/emulator.py
│   │   └── llm/{gateway.py,breaker.py,providers/}
│   └── worker/                        # Контур 3: Агент и симулятор
│       ├── scheduler.py
│       ├── jobs/{agent_diagnose.py,mine_synonyms.py,kpi_rollup.py,simulator.py}
│       └── agent/{tools.py,tickets.py}
│
├── web/                               # Контур 4: SPA (4 экрана)
│   ├── src/api/{client.ts,types.gen.ts,sse.ts}
│   ├── src/pages/{StorefrontPage.tsx,EcomHeadPage.tsx,AgentContentPage.tsx,OpsCostPage.tsx}
│   ├── src/components/{SearchBox,Typeahead,ResultCard,SectionBlock,TraceDrawer,FeatureBars,BadgeChip,TierTag}/
│   └── src/state/
│
├── eval/                              # Контур 5: Eval & Cost
│   ├── datasets/{gold10.csv,gold_map.csv,syn_a/,syn_b/}
│   ├── gen/{make_syn_a.py,make_syn_b.py,dedupe_vs_index.py}
│   ├── {run_eval.py,stats.py,judge.py,report.py,impact_model.py,check_gold_leak.py}
│   └── reports/
│
├── data/                              # .gitignore
│   ├── raw/{feed.yml,gold.xlsx}
│   ├── interim/
│   ├── db/smart_search.sqlite         # WAL БД событий, overlay, тикетов, cost_ledger
│   └── artifacts/<index_version>/     # B.5
│
├── scripts/{load_test.py,cost_report.py,backup.sh,restore.sh}
├── tests/{unit/,property/,api/}
└── docs/{RUNBOOK.md,DEMO_SCRIPT.md}
```

## B.3 Общие конвенции

- JSON: snake_case, UTF-8. Отсутствующее значение — null (никогда не 0 и не "").
Время: RFC 3339 UTC с суффиксом Z (2026-11-26T12:00:00Z).
Деньги:
Цены товаров и выручка: целые *_minor (копейки), валюта RUB (RUR при ingest приводится к RUB). Для «цена по запросу» price_minor = null.
Стоимость вызовов LLM/CPU: целые микро-рубли *_micro_rub (1 ₽ = 1 000 000 micro_rub).
Идентификаторы:
family_id: f_<group_id> или f_o<offer_id> (для одиночного offer без group_id).
offer_id: o_<id>, category_id: c_<id>, entity_id: e_<type>_<slug>.
trace_id: tr_<ULID>, session_id: ss_<token>, event_id: <ULID>, ticket_id: tkt_<ULID>.
index_version: idx_YYYYMMDD_HHMM_<sha7>.
config_version: целое число, растёт монотонно на +1 при каждом изменении overlay (или откате).
Нормализация query_norm: NFKC, нижний регистр, ё→е, схлопывание пробелов, приведение дефисов и кавычек к ASCII, вынос шумовых слов («купить», «цена», «скачать») в noise_tokens.
PII-маскирование: email, телефоны и ИНН (10/12 цифр) заменяются масками до записи в events и trace.
Заголовки: запрос может передавать X-Request-Id; ответ всегда возвращает X-Trace-Id, X-Config-Version, X-Degradation-Level. Админка защищена X-Admin-Token, внутренний пробник — X-Internal-Token.

B.4 Модели данных и Pydantic-схемы (источник истины)
Все контракты API и LLM описаны в services/api/app/schemas/. Ниже приведён эталонный состав полей.
1. базовые enum и типы
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
2. Поиск, выдача, подсказки и трассировка (schemas/search.py)
class ClientContext(BaseModel):
    persona_id: Optional[str] = None
    segment: Segment = Segment.smb
    page_category_id: Optional[str] = None
    recent_queries: List[str] = Field(default_factory=list)
    clicked_family_ids: List[str] = Field(default_factory=list)
    purchased_family_ids: List[str] = Field(default_factory=list)


class SearchOptions(BaseModel):
    mode: Literal["smart", "legacy"] = "smart"
    llm_enabled: bool = True
    disable_dense: bool = False
    limit: int = Field(default=20, ge=4, le=50)
    include_trace: bool = True


class SearchRequest(BaseModel):
    query: str = Field(..., max_length=300)
    session_id: str = "ss_demo"
    context: ClientContext = Field(default_factory=ClientContext)
    options: SearchOptions = Field(default_factory=SearchOptions)


class Badge(BaseModel):
    type: str  # "promo" | "price_advantage" | "available" | "on_request" | "ru_registry"
    label: str
    basis: Optional[Dict[str, Any]] = None


class OfferView(BaseModel):
    offer_id: str
    name_raw: str
    available: bool
    price_state: PriceState
    price_minor: Optional[int] = None      # null если on_request
    oldprice_minor: Optional[int] = None
    discount_pct: Optional[float] = None
    term_months: Optional[int] = None
    qty_users: Optional[int] = None
    license_type: Optional[str] = None
    segment_fit: Optional[str] = None
    url: Optional[str] = None


class FeatureContribution(BaseModel):
    feature: str
    value: float
    contribution: float
    source: str  # "real" | "proxy" | "simulated" | "rule"


class ProductCard(BaseModel):
    family_id: str
    canonical_name: str
    vendor: str
    product_class: ProductClass
    product_type: str
    image_url: Optional[str] = None
    representative: OfferView
    n_variants: int
    variant_axes: Dict[str, List[str]] = Field(default_factory=dict)
    price_min_minor: Optional[int] = None
    badges: List[Badge] = Field(default_factory=list)
    relevance_label: RelevanceLabel
    fill_reason: Optional[str] = None
    matched_fields: List[str] = Field(default_factory=list)
    feature_contributions: List[FeatureContribution] = Field(default_factory=list)
    score: float


class CrossSellItem(BaseModel):
    family_id: str
    canonical_name: str
    vendor: str
    relation_type: str  # "complements" | "upsell_to" | "analog_of"
    source: str         # "vendor_evidence" | "catalog_structure" | "llm_validated" | "co_click"
    evidence_label: Optional[str] = None
    evidence_url: Optional[str] = None
    price_min_minor: Optional[int] = None
    available: bool


class SearchSection(BaseModel):
    section_id: str
    title: str
    intent_class: Optional[ProductClass] = None
    empty_reason: Optional[str] = None
    items: List[ProductCard] = Field(default_factory=list)


class NavHit(BaseModel):
    entity_id: str
    kind: Literal["CATEGORY", "CONTENT", "EXTERNAL_PRODUCT"]
    title: str
    url: Optional[str] = None
    note: Optional[str] = None


class IntentChip(BaseModel):
    label: str
    query_rewrite: str
    intent_hint: Optional[str] = None


class TraceStep(BaseModel):
    id: StepId
    title: str
    status: Literal["ok", "skipped", "fallback", "error"]
    ms: float
    cpu_ms: float
    summary: str
    decision: Optional[str] = None
    fallback_used: bool = False
    data: Dict[str, Any] = Field(default_factory=dict)


class CostMeter(BaseModel):
    llm_calls: int
    tokens_in: int
    tokens_cached: int
    tokens_out: int
    cpu_ms_total: float
    cost_micro_rub: int
    data_tier: DataTier = DataTier.derived


class SearchResponse(BaseModel):
    contract_version: str = "2.0.0-mvp"
    trace_id: str
    query_raw: str
    query_norm: str
    applied_query: Optional[str] = None
    did_you_mean: Optional[str] = None
    mode: Literal["smart", "legacy"]
    degradation_level: DegradationLevel
    config_version: int
    index_version: str
    hard_null: bool
    soft_null: bool
    fill_rung: int
    out_of_catalog_banner: Optional[str] = None
    nav_hits: List[NavHit] = Field(default_factory=list)
    intent_chips: List[IntentChip] = Field(default_factory=list)
    sections: List[SearchSection] = Field(default_factory=list)
    cross_sell: List[CrossSellItem] = Field(default_factory=list)
    related_queries: List[str] = Field(default_factory=list)
    trace: List[TraceStep] = Field(default_factory=list)
    cost_meter: CostMeter


class SuggestCompletion(BaseModel):
    text: str
    kind: Literal["brand", "product", "alias", "category", "seed_query", "layout_fix"]
    score: float


class SuggestProductHit(BaseModel):
    family_id: str
    canonical_name: str
    vendor: str
    image_url: Optional[str] = None
    price_min_minor: Optional[int] = None
    available: bool
    has_promo: bool


class SuggestResponse(BaseModel):
    contract_version: str = "2.0.0-mvp"
    prefix_raw: str
    layout_corrected: Optional[str] = None
    completions: List[SuggestCompletion] = Field(default_factory=list)
    products: List[SuggestProductHit] = Field(default_factory=list)
    related_queries: List[str] = Field(default_factory=list)
    intent_chips: List[IntentChip] = Field(default_factory=list)
    latency_ms: float
3. События, аналитика, тикеты и админка (schemas/admin.py)
class EventIn(BaseModel):
    event_type: EventType
    session_id: str
    trace_id: Optional[str] = None
    source: Literal["live", "simulated", "gold_replay"] = "live"
    payload: Dict[str, Any] = Field(default_factory=dict)


class OverlayRulePayload(BaseModel):
    kind: Literal["synonym", "alias", "pin", "demote", "boost", "catalog_patch"]
    key: str
    value: Dict[str, Any]


class TicketItem(BaseModel):
    ticket_id: str
    ticket_type: TicketType
    root_cause: RootCause
    target_role: Literal["content", "product", "ecom"]
    cluster_key: str
    affected_queries: List[str]
    frequency: int
    estimated_monthly_rub: int
    estimation_tier: DataTier = DataTier.modeled
    confidence: float
    summary: str
    evidence: List[str] = Field(default_factory=list)
    proposed_patch: Optional[OverlayRulePayload] = None
    dry_run_before_hit4: Optional[float] = None
    dry_run_after_hit4: Optional[float] = None
    status: Literal["open", "applied", "rejected", "rolled_back"]
    applied_config_version: Optional[int] = None
    created_at: str


class ApplyTicketResponse(BaseModel):
    ticket_id: str
    gold10_passed: bool
    gold10_details: List[Dict[str, Any]]
    new_config_version: int
    replay_results: List[Dict[str, Any]]


class ImpactAssumptions(BaseModel):
    q_month: int = 15000
    f_b: float = 0.30
    f_n: Optional[float] = None  # если None — берётся из последнего eval
    cvr_s: float = 0.075
    kappa: float = 0.30
    aov_rub: Optional[float] = None  # если None — медиана/среднее фида
    margin: float = 0.20
    p_exit: float = 0.40


class ImpactScenarioOut(BaseModel):
    scenario: Literal["pessimistic", "base", "optimistic"]
    recovered_sessions_month: float
    delta_orders_month: float
    delta_revenue_rub_month: float
    delta_profit_rub_month: float
    lost_orders_before_month: float
    lost_orders_after_month: float
    exits_before_month: float
    exits_after_month: float
    data_tier: DataTier = DataTier.modeled


class ImpactReport(BaseModel):
    assumptions_used: ImpactAssumptions
    scenarios: List[ImpactScenarioOut]
    data_tier: DataTier = DataTier.modeled
    note: str = "Сценарный расчёт на допущениях, не измерение реального трафика"
4. Строгие схемы ответов LLM (schemas/llm.py)
class QuLlmOutput(BaseModel):
    intent: str
    type_probs: Dict[str, float]
    entity_ids: List[str] = Field(default_factory=list)  # только из переданного closed-set или []
    attr_filters: Dict[str, Any] = Field(default_factory=dict)
    rewrites: List[str] = Field(default_factory=list, max_length=3)
    clarification_chips: List[IntentChip] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)


class EnrichFamilyOutput(BaseModel):
    canonical_name_ru: str
    canonical_name_en: str
    aliases: List[str] = Field(default_factory=list, max_length=12)
    typical_queries: List[str] = Field(default_factory=list, max_length=6)
    use_cases: List[str] = Field(default_factory=list, max_length=4)
    buyer_roles: List[str] = Field(default_factory=list, max_length=4)
    analogs: List[str] = Field(default_factory=list, max_length=5)
    complements: List[str] = Field(default_factory=list, max_length=5)


class AgentDiagnoseOutput(BaseModel):
    root_cause: RootCause
    ticket_type: TicketType
    target_role: Literal["content", "product", "ecom"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    proposed_patch: Optional[OverlayRulePayload] = None


class JudgeOutput(BaseModel):
    grade: int = Field(ge=0, le=3)
    reason: str
B.5 Формат артефактов индекса (data/artifacts/<index_version>/)
Каждая сборка индекса создаёт иммутабельный каталог data/artifacts/<index_version>/ и обновляет симлинк/указатель data/artifacts/current.
Обязательные файлы:
manifest.json — паспорт сборки (index_version, created_at, feed_sha256, embedding_model, embedding_dim, n_offers, n_families, n_categories, n_entities, n_edges).
families.parquet — таблица family (family_id, canonical_name, vendor, product_class, product_type, category_id, category_path, is_ru_registry, available_any, price_min_minor, price_max_minor, discount_best, data_quality_score, pop_prior, representative_offer_id, n_variants, variant_axes_json).
offers.parquet — нормализованные предложения (offer_id, family_id, name_raw, product_name_clean, available, price_state, price_minor, oldprice_minor, discount_pct, term_months, qty_users, license_type, segment, version, url).
entities.parquet — единый реестр (entity_id, kind, canonical_name, aliases_json, status, target_family_ids_json, url, evidence_snippet).
graph_edges.parquet — граф допродаж (src_family_id, dst_family_id, dst_external_name, edge_type, source, evidence_url, snippet, confidence).
data_quality_issues.parquet — выявленные противоречия и дефекты карточек (для контент-менеджера).
embeddings.npy — матрица float32 размера (n_families, dim) (L2-нормированная, порядок строк совпадает с families.parquet).
bm25f.npz / char3gram.npz — разреженные матрицы scipy + словари термов.
symspell.pkl / typeahead_trie.pkl / popular_by_root.json / qc_report.json.

B.6 Схема БД SQLite (contracts/sql/001_init.sql)
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    trace_id        TEXT,
    source          TEXT NOT NULL CHECK (source IN ('live', 'simulated', 'gold_replay')),
    config_version  INTEGER,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_type_src ON events(event_type, source);

CREATE TABLE IF NOT EXISTS search_traces (
    trace_id            TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    source              TEXT NOT NULL,
    query_raw           TEXT NOT NULL,
    query_norm          TEXT NOT NULL,
    mode                TEXT NOT NULL,
    degradation_level   TEXT NOT NULL,
    config_version      INTEGER NOT NULL,
    hard_null           INTEGER NOT NULL,
    soft_null           INTEGER NOT NULL,
    fill_rung           INTEGER NOT NULL,
    latency_ms          REAL NOT NULL,
    response_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_nulls ON search_traces(hard_null, soft_null, ts);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    usd_per_mtok_in     REAL NOT NULL,
    usd_per_mtok_cached REAL NOT NULL,
    usd_per_mtok_out    REAL NOT NULL,
    usd_to_rub          REAL NOT NULL,
    price_source_url    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    tokens_in           INTEGER NOT NULL,
    tokens_cached       INTEGER NOT NULL,
    tokens_out          INTEGER NOT NULL,
    latency_ms          REAL NOT NULL,
    price_snapshot_id   TEXT NOT NULL REFERENCES price_snapshots(snapshot_id),
    cost_micro_rub      INTEGER NOT NULL,
    trace_id            TEXT,
    status              TEXT NOT NULL,
    retry_n             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cost_ts_purpose ON cost_ledger(ts, purpose);

CREATE TABLE IF NOT EXISTS qu_cache (
    cache_key           TEXT PRIMARY KEY,
    response_json       TEXT NOT NULL,
    model               TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    config_version      INTEGER PRIMARY KEY,
    created_at          TEXT NOT NULL,
    author              TEXT NOT NULL,
    ticket_id           TEXT,
    action              TEXT NOT NULL,   -- 'init' | 'apply_ticket' | 'manual_edit' | 'rollback'
    comment             TEXT,
    overlay_snapshot_json TEXT NOT NULL,
    ranking_weights_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overlay_items (
    item_id             TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('synonym', 'alias', 'pin', 'demote', 'boost', 'catalog_patch')),
    key_norm            TEXT NOT NULL,
    value_json          TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1,
    config_version      INTEGER NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_overlay_active ON overlay_items(active, kind, key_norm);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id               TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    ticket_type             TEXT NOT NULL,
    root_cause              TEXT NOT NULL,
    target_role             TEXT NOT NULL,
    cluster_key             TEXT NOT NULL,
    affected_queries_json   TEXT NOT NULL,
    frequency               INTEGER NOT NULL,
    estimated_monthly_rub   INTEGER NOT NULL,
    confidence              REAL NOT NULL,
    summary                 TEXT NOT NULL,
    evidence_json           TEXT NOT NULL,
    proposed_patch_json     TEXT,
    dry_run_before_hit4     REAL,
    dry_run_after_hit4      REAL,
    status                  TEXT NOT NULL CHECK (status IN ('open', 'applied', 'rejected', 'rolled_back')),
    applied_config_version  INTEGER
);
B.7 Конфигурационные файлы (config/*.yaml|json)
config/search.yaml: ручные веса полей BM25F, пороги RRF, 6–8 весов ранжирования (rel_fused, exact_name_match, availability, popularity, margin_proxy, promo, version_recency, segment_fit), полоса δ для маржи/акций, пороги эскалации к LLM (qu_confidence_threshold), таймауты (llm_timeout_ms: 1500).
config/vendors_seed.yaml: курированный список по 8–12 ключевым брендам: aliases, complements, upsell_to, analog_of, external_products (например, MDaemon), evidence_url, snippet (до 200 символов).
config/seed_glossary.yaml: сленг («винда», «антивирь», «ось»), сокращения («эцп», «бд»), числовые алиасы (365 → Microsoft 365). Запрещено включать строки GOLD10.
config/taxonomy.yaml: список product_type (50–80 узлов), привязка к product_class и матрица допустимых пар для допродаж.
config/personas.json: 3–4 синтетические персоны для переключателя на витрине (например: «Бухгалтер малого бизнеса», «Сисадмин госкомпании», «Закупщик ВУЗа»).
config/price_snapshots.json: актуальные тарифы выбранных LLM и курс USD/RUB с датой и ссылкой на прайс.
B.8 HTTP API и OpenAPI-контракты
Группа	Метод и путь	Назначение	Auth	Запрос / Ответ
Health	GET /healthz	Liveness-проба	—	200 {"status":"ok"}
Health	GET /readyz	Готовность индекса и БД	—	200 {"index_version":..., "config_version":..., "degradation":"L0"}
Public	GET /v1/suggest	Typeahead (?q=...&segment=smb)	—	SuggestResponse
Public	POST /v1/search	Полный поиск (S0–S12)	—	SearchRequest → SearchResponse
Public	POST /v1/search/stream	Поиск с потоком шагов trace по SSE	—	SearchRequest → SSE (span, final, error)
Public	POST /v1/events	Логирование кликов, корзины, 👎, catalog_gap_lead	—	EventIn → 202 {"event_id":...}
Public	GET /v1/personas	Список демо-персон	—	personas.json
Public	GET /v1/scenarios	Сценарные кнопки (gold-10 + сложные кейсы)	—	demo_scenarios.json
Internal	POST /internal/probe	Dry-run прогон запроса с временным overlay-патчем	X-Internal-Token	{query, temp_patch} → SearchResponse
Admin (E-com & Head)	GET /v1/admin/kpi	4 KPI, воронка, сравнение Legacy vs Smart (?source=all|live|simulated)	X-Admin-Token	JSON со сводкой и data_tier у каждой метрики
Admin (E-com & Head)	POST /v1/admin/impact-model	Пересчёт 3 сценариев эффекта	X-Admin-Token	ImpactAssumptions → ImpactReport
Admin (E-com & Head)	POST /v1/admin/ranking-weights	Изменение весов маржи/акций/популярности (горячий апдейт + аудит)	X-Admin-Token	{margin_weight, promo_weight, pop_weight} → {config_version}
Admin (E-com & Head)	GET /v1/admin/eval-summary	Последний отчёт eval (P0..P3, GOLD10)	X-Admin-Token	eval_report.json
Admin (Agent & Content)	POST /v1/admin/agent/run	Запуск агента диагностики	X-Admin-Token	{created_tickets, clusters_found}
Admin (Agent & Content)	GET /v1/admin/tickets	Очередь тикетов агента (?status=open)	X-Admin-Token	List[TicketItem]
Admin (Agent & Content)	POST /v1/admin/tickets/{id}/apply	Ручное одобрение: GOLD10-check → hot-swap → мгновенный replay	X-Admin-Token	ApplyTicketResponse
Admin (Agent & Content)	POST /v1/admin/overlay/rollback	Откат к config_version - 1 (или указанной версии)	X-Admin-Token	{target_version} → {new_config_version}
Admin (Agent & Content)	GET /v1/admin/audit	Лента версий конфига и действий	X-Admin-Token	List[ConfigVersionMeta]
Admin (Agent & Content)	GET /v1/admin/data-quality	Проблемы карточек и конфликты из data_quality_issues	X-Admin-Token	пагинация дефектов
Admin (Agent & Content)	GET /v1/admin/demand-gaps	Спрос vs каталог по брендам и EXTERNAL_PRODUCT	X-Admin-Token	агрегаты по кластерам R3/R4/R8
Admin (Ops & Cost)	GET /v1/admin/ops	Уровень деградации, breaker, p50/p95, cache hit	X-Admin-Token	JSON-статус
Admin (Ops & Cost)	POST /v1/admin/chaos	Вкл/выкл chaos-флагов (force_disable_llm, force_disable_dense)	X-Admin-Token	{degradation_level}
Admin (Ops & Cost)	GET /v1/admin/cost	Сводка по cost_ledger (средняя, p50, p95, по шагам)	X-Admin-Token	JSON + ссылка на CSV
Admin (Ops & Cost)	GET /v1/admin/qc-report	Отчёт качества фида (qc_report.json)	X-Admin-Token	JSON
Ошибки: 4xx/5xx всегда отдают ApiError. Публичный поиск при живом API всегда возвращает 200 с актуальным degradation_level.
B.9 Контракт SSE-стриминга (/v1/search/stream)
Формат событий SSE (Content-Type: text/event-stream):
Промежуточные шаги (S0–S12):
event: span
data: {"id":"S4","title":"Понимание запроса (QU)","status":"ok","ms":12.4,"cpu_ms":1.8,"summary":"Интент: hardware (0.72), cloud (0.18), software_for (0.10)","decision":"rules_only","fallback_used":false,"data":{"product_class_probs":{"hardware":0.72}}}
Итоговый ответ:
event: final
data: <полный JSON SearchResponse>
Завершение / ошибка:
При нормальном завершении после final поток закрывается.
При сбое до final отправляется event: error с ApiError.
Фронтенд рендерит TraceDrawer по событиям span в реальном времени и подставляет карточки по событию final.
B.10 Промпт-контракты LLM и защита от галлюцинаций
Изоляция пользовательского ввода: любой запрос или текст карточки передаётся в LLM строго внутри тегов <user_input>...</user_input> с инструкцией игнорировать любые команды внутри данных.
Closed-set для QU: промпт prompts/qu/v1.md получает не весь каталог, а только коды таксономии и список до 15 кандидатов-сущностей (entity_id, canonical_name). Любой entity_id, отсутствующий в переданном списке, отбрасывается на уровне валидации.
Строгий JSON и Retry: ответ парсится через соответствующую Pydantic-модель (B.4.4). При ошибке валидации выполняется 1 retry; при повторном провале срабатывает fallback на правила (режим L1 для данного запроса).
Обязательный учёт затрат: каждый вызов LLM (успешный, упавший или retry) в блоке finally пишет строку в cost_ledger с привязкой к price_snapshot_id.
B.11 Тестирование контрактов и Definition of Done (MVP)
Автоматические проверки (make test)
OpenAPI & Typegen sync: генерация /openapi.json → npx openapi-typescript проходит без ошибок; web/src/api/types.gen.ts не расходится с services/api/app/schemas/.
Инварианты (I1–I7): прогон tests/property/test_invariants.py на GOLD10, выборке SYN-A, пустой строке и строках со спецсимволами / SQL-инъекциями.
Контракты SQL и индекса: проверка чтения всех файлов из manifest.json и миграции 001_init.sql в чистую SQLite БД.
SSE и деградация: интеграционный тест /v1/search/stream в L0 и при включённом chaos-флаге force_disable_llm=true (L1).
No Gold Leak: eval/check_gold_leak.py подтверждает отсутствие дословных запросов GOLD10 в seed_glossary.yaml.
Definition of Done к code freeze (T+42 ч)
Стенд поднят по docker compose up -d на VPS, доступен из РФ, /readyz возвращает 200.
Все 4 экрана SPA работают на реальном API (без mock-заглушек).
Пройдены все 5 автопроверок выше.
Сформированы eval/reports/REPORT.md (сравнение P0..P3 и GOLD10) и eval/reports/cost_report.csv для заполнения шаблона стоимости организатора.
Записано резервное видео сквозного сценария питча.
---

Что теперь синхронизировано между обоими файлами:
- **Единый источник контрактов:** Pydantic v2 → `/openapi.json` → `openapi-typescript` (без 17 JSON-схем и `extract_contracts.py`).
- **Оценка качества:** `GOLD10` + `SYN-A` (300–500) + `SYN-B` (50–100) + `LLM-judge` и выборочная проверка глазами (без двойной разметки с $\kappa$ и 9 `FAULT`-инъекций), 4 контрольные точки сравнения `P0..P3`.
- **Обогащение и вендоры:** правила + точечный LLM по категориям, обогащение топ-500–1000 family, `vendors_seed.yaml` + demo-скрипт, цены только внутри каталога.
- **Аналитика и UI:** 3 сценария калькулятора эффекта вместо Monte Carlo, группировка агента по `query_norm`, ручное одобрение тикетов с `GOLD10-check` и кнопкой отката, 4 экрана в SPA, режимы деградации `L0–L3`.
- **Добавлены недостающие B.4–B.11:** готовые Pydantic-схемы, формат файлов индекса, полный SQL `001_init.sql`, таблица всех эндпоинтов, формат SSE и правила защиты LLM.