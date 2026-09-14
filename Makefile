# Retail Sales Intelligence Platform - task shortcuts.
#
#   make help        list every target
#   make setup       install dependencies and prepare directories
#   make up          start MinIO, Prefect and the dbt docs site
#   make pipeline    ingest -> build -> publish snapshot -> docs
#   make ask q="..."  ask the warehouse a question
#
# Windows without `make`: use the shim instead, same target names.
#   .\make.ps1 pipeline
#
# Everything runs through the project virtualenv, so no activation step is needed.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Resolve the venv interpreter for either platform; fall back to plain python.
ifeq ($(OS),Windows_NT)
	PY := venv/Scripts/python.exe
	DBT := ../venv/Scripts/dbt.exe
else
	PY := venv/bin/python
	DBT := ../venv/bin/dbt
endif

FLOW := orchestration/prefect/flows/retail_pipeline.py

.PHONY: help setup up down logs ingest build test docs snapshot pipeline \
        serve ask shell api eval schema clean status

help: ## List available targets
	@echo "Retail Sales Intelligence Platform"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Examples:"
	@echo "    make ask q=\"top 5 products by profit\""
	@echo "    make build select=marts"

# ---------------------------------------------------------------- setup / infra

setup: ## Install Python dependencies and dbt packages
	$(PY) -m pip install -r requirements.txt
	cd dbt && $(DBT) deps
	@mkdir -p duckdb_warehouse dbt/target
	@echo "Setup complete. Copy .env.example to .env and fill in the OLTP_* values."

up: ## Start MinIO, Prefect and the dbt docs site
	docker compose up -d
	@echo "MinIO     http://localhost:9002  (minioadmin / minioadminpassword)"
	@echo "Prefect   http://localhost:4200"
	@echo "dbt docs  http://localhost:8081"

down: ## Stop all containers
	docker compose down

logs: ## Tail container logs
	docker compose logs -f --tail=50

status: ## Show container and warehouse status
	@docker compose ps
	@echo ""
	@$(PY) -c "import duckdb,os; p='duckdb_warehouse/warehouse.duckdb'; \
	print('warehouse:', 'missing - run `make pipeline`' if not os.path.exists(p) else \
	str(duckdb.connect(p, read_only=True).execute('select count(*) from xom_retails_gold.fact_sales').fetchone()[0])+' fact rows')"

# ------------------------------------------------------------------- pipeline

ingest: ## Extract SQL Server -> MinIO Bronze (Parquet)
	$(PY) Ingestion/ingestion.py $(if $(tables),--tables $(tables),)

build: ## Build Silver + Gold + analytics and run all tests
	cd dbt && $(DBT) build --profiles-dir . $(if $(select),--select $(select),)

test: ## Run the dbt data tests only
	cd dbt && $(DBT) test --profiles-dir .

docs: ## Regenerate the dbt lineage site (served at :8081)
	cd dbt && $(DBT) docs generate --profiles-dir .

snapshot: ## Publish the serving snapshot the query engine reads
	$(PY) $(FLOW) --no-ingest --no-transform --no-docs

pipeline: ## Full run: ingest -> build -> snapshot -> docs
	$(PY) $(FLOW)

refresh: ## Rebuild the warehouse from Bronze already in MinIO (skips ingestion)
	$(PY) $(FLOW) --no-ingest

serve: ## Register the flow so runs can be triggered from the Prefect UI
	$(PY) $(FLOW) --serve

# --------------------------------------------------------------- text-to-SQL

ask: ## Ask a question: make ask q="top 5 products by profit"
	@$(PY) -m text2sql "$(q)"

shell: ## Interactive question shell
	$(PY) -m text2sql

api: ## Start the HTTP API on :8000
	$(PY) -m uvicorn api.main:app --reload

eval: ## Measure text-to-SQL accuracy against the reference queries
	$(PY) -m text2sql.eval.run_eval $(if $(provider),--provider $(provider),)

schema: ## Print the schema context sent to the model
	$(PY) -m text2sql --schema

# ------------------------------------------------------------------- cleanup

clean: ## Remove build artefacts and the local warehouse
	rm -rf dbt/target/* dbt/logs duckdb_warehouse/*.duckdb duckdb_warehouse/*.wal
	@echo "Removed build artefacts. Run `make pipeline` to rebuild."
