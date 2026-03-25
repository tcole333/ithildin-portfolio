SHELL := /bin/zsh

ROOT := $(CURDIR)
DEMO_CONTENT_ROOT := $(ROOT)/examples/portfolio-demo/content
DEMO_INVESTIGATION_DB := $(ROOT)/examples/portfolio-demo/investigation.db
DEMO_REGISTRY_DB := $(ROOT)/examples/portfolio-demo/registry.db
DEMO_DOJ_DB := $(ROOT)/examples/portfolio-demo/doj_documents.db
DEMO_ENV := ITHILDIN_CONTENT_ROOT=$(DEMO_CONTENT_ROOT) ITHILDIN_INVESTIGATION_DB=$(DEMO_INVESTIGATION_DB) ITHILDIN_REGISTRY_DB=$(DEMO_REGISTRY_DB) ITHILDIN_DOJ_DB=$(DEMO_DOJ_DB) PUBLIC_ENABLE_EVIDENCE_MODE=true

.PHONY: setup test build demo deploy deploy-preview demo-e2e

setup:
	uv sync
	npm ci --prefix web
	cd web && npx playwright install chromium

test:
	uv run pytest -m 'not live_data' -q
	$(DEMO_ENV) npm run check --prefix web
	npm run test:citations --prefix web
	$(DEMO_ENV) npm run test:citations:snapshots --prefix web
	$(DEMO_ENV) npm run build --prefix web
	$(DEMO_ENV) npm run test:citations:build --prefix web

build:
	$(DEMO_ENV) uv run python -m ithildin.cli build demo

demo:
	$(DEMO_ENV) uv run python -m ithildin.cli build demo
	$(DEMO_ENV) node web/scripts/test-demo-build.mjs

deploy:
	cd web && npm run deploy

deploy-preview:
	cd web && npm run deploy:preview

demo-e2e:
	$(DEMO_ENV) npm run test:e2e:smoke --prefix web
