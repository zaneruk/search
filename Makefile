# smart-search — Makefile (Волна 0)
SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON       ?= python3.11
VENV         ?= .venv
PY           := $(VENV)/bin/python
PIP          := $(VENV)/bin/pip
API_HOST     ?= 0.0.0.0
API_PORT     ?= 8000
# Для CPU-only torch локально: make setup TORCH_INDEX=https://download.pytorch.org/whl/cpu
TORCH_INDEX  ?=
INDEX_ARGS   ?=
COST_ARGS    ?=
OPENAPI_JSON ?= data/interim/openapi.json
TYPES_OUT    ?= web/src/api/types.gen.ts

export PYTHONPATH := $(CURDIR):$(CURDIR)/services/api

.PHONY: help setup run worker build-index typegen test eval cost-report

help:
	echo "Цели: setup | run | worker | build-index | typegen | test | eval | cost-report"

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	if [ -n "$(TORCH_INDEX)" ]; then $(PIP) install torch --index-url $(TORCH_INDEX); fi
	$(PIP) install -e .
	mkdir -p data/raw data/interim data/db data/artifacts eval/reports
	test -f .env || cp .env.example .env

run:
	$(PY) -m uvicorn app.main:app --reload --reload-dir services/api --host $(API_HOST) --port $(API_PORT)

worker:
	$(PY) -m services.worker.scheduler

build-index:
	$(PY) -m pipelines.build_index.build $(INDEX_ARGS)

typegen:
	mkdir -p $(dir $(OPENAPI_JSON)) $(dir $(TYPES_OUT))
	$(PY) -c "import json, pathlib; from app.main import app; p = pathlib.Path('$(OPENAPI_JSON)'); p.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding='utf-8'); print('wrote', p)"
	npx --yes openapi-typescript $(OPENAPI_JSON) -o $(TYPES_OUT)

test:
	$(PY) -m pytest

eval:
	$(PY) -m eval.run_eval
	$(PY) -m eval.report

cost-report:
	$(PY) scripts/cost_report.py $(COST_ARGS)
