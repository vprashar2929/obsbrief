SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

CONFIG     ?= config/runtime.yaml
ENV        ?= dev

.PHONY: help env lint fmt fmt-check test typecheck check preflight weekly clean

help:
	@echo "Available targets:"
	@echo "  env        Create dev environment (hatch env create)"
	@echo "  lint       Run Ruff lint checks"
	@echo "  fmt        Format code with Ruff"
	@echo "  fmt-check  Check code formatting without modifying files"
	@echo "  test       Run Pytest suite"
	@echo "  typecheck  Run strict MyPy"
	@echo "  check      Run lint + fmt-check + typecheck + test"
	@echo "  preflight  Run preflight (PROJECT, REGION required)"
	@echo "  weekly     Generate weekly report (PROJECT, REGION, OUTPUT_DIR required)"
	@echo "  clean      Remove build artifacts and caches"

env:
	hatch env create

lint:
	hatch run lint

fmt:
	hatch run fmt

fmt-check:
	hatch run fmt-check

test:
	hatch run test

typecheck:
	hatch run typecheck

check: lint fmt-check typecheck test

preflight:
ifndef PROJECT
	$(error PROJECT is required — usage: make preflight PROJECT=my-proj REGION=us-central1)
endif
ifndef REGION
	$(error REGION is required — usage: make preflight PROJECT=my-proj REGION=us-central1)
endif
	hatch run opsbrief preflight --config $(CONFIG) --env $(ENV) --project $(PROJECT) --region $(REGION)

weekly:
ifndef PROJECT
	$(error PROJECT is required — usage: make weekly PROJECT=my-proj REGION=us-central1 OUTPUT_DIR=./reports)
endif
ifndef REGION
	$(error REGION is required — usage: make weekly PROJECT=my-proj REGION=us-central1 OUTPUT_DIR=./reports)
endif
ifndef OUTPUT_DIR
	$(error OUTPUT_DIR is required — usage: make weekly PROJECT=my-proj REGION=us-central1 OUTPUT_DIR=./reports)
endif
	hatch run opsbrief weekly --config $(CONFIG) --env $(ENV) --project $(PROJECT) --region $(REGION) --output-dir $(OUTPUT_DIR)

clean:
	rm -rf dist/ build/ *.egg-info .mypy_cache .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
