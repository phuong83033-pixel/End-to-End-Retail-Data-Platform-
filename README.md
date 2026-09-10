# Retail Sales Intelligence Platform

An end-to-end data platform over the Xóm Data retail dataset: SQL Server → MinIO
(Bronze, Parquet) → DuckDB via dbt (Silver + Gold star schema) → analytics, ML and
serving.

The goal is not an enterprise system but a realistic Data Engineering / Analytics /
ML skeleton that runs entirely on a laptop and can be re-run from scratch.

---

## Architecture

```text
[Xóm Data SQL Server]  retails.{customers, products, sales, stores}
          │
          ▼  Extract & Load (dlt)
┌──────────────────────────────────────────┐
│  MinIO (S3 API)                          │  BRONZE — raw Parquet snapshots
│  s3://bronze/xom_retails/<table>/        │
└──────────────────────────────────────────┘
          │
          ▼  Transform (dbt-duckdb, reads MinIO via httpfs)
┌──────────────────────────────────────────┐
│  DuckDB — duckdb_warehouse/warehouse.duckdb
│  xom_retails_silver   (cleaned/standardised)
│  xom_retails_gold     (star schema)      │
└──────────────────────────────────────────┘
          │
          ▼
[Metabase · FastAPI · ML models]           (not built yet)
```

## Stack

| Layer | Technology |
|---|---|
| Source | SQL Server (read-only) |
| Ingestion | Python 3.12 + [dlt](https://dlthub.com) |
| Object storage | MinIO (S3-compatible), Parquet |
| Transformation | dbt Core 1.12 + dbt-duckdb, dbt_utils |
| Warehouse | DuckDB (local file) |
| Orchestration | Prefect 3 (flow at `orchestration/prefect/flows/`) |
| Pipeline docs | dbt docs served by nginx |
| Local infra | Docker Compose |

---

## Quick start

### Prerequisites

- Docker Desktop running
- Python virtualenv at `venv/`
- A `.env` at the project root — copy `.env.example` and fill in the `OLTP_*` values

### 1. Install dependencies

```bash
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

### 2. Start MinIO

```bash
docker compose up -d minio
```

Console at http://localhost:9002 (`minioadmin` / `minioadminpassword`). Name the
service explicitly — a bare `docker compose up -d` also starts the Prefect server,
which nothing uses yet.

### 3. Ingest to Bronze
Activate the python venv :'venv\Scripts\activate.bat (cmd)'

```bash
./venv/Scripts/python.exe Ingestion/ingestion.py
```

Creates the `bronze` bucket and writes Parquet to
`bronze/xom_retails/{customers,products,sales,stores}/`.

Expected row counts: **customers 15,266 · products 2,517 · sales 62,884 · stores 67**.

The load is a full snapshot (`write_disposition="replace"`), so re-running is safe and
idempotent. Use `--tables sales` to reload a single table.

### 4. Create the warehouse directory

```bash
mkdir duckdb_warehouse
```

DuckDB does not create missing parent directories; skipping this makes dbt fail with
an opaque IO error.

### 5. Build Silver + Gold

```bash
cd dbt; ../venv/Scripts/dbt.exe build --profiles-dir .
```

`build` runs the 9 models and every data test in dependency order. `dbt deps` only
needs re-running if `packages.yml` changes.

### 6. Verify

```bash
./venv/Scripts/python.exe -c "import duckdb; c=duckdb.connect('duckdb_warehouse/warehouse.duckdb'); print(c.execute('select count(*), count(distinct order_id) from xom_retails_gold.fact_sales').fetchall())"
```

Expect `62884` rows across `26326` distinct orders — matching the source exactly.

---

## Running the pipeline with Prefect

Steps 3–5 above are the manual path. The Prefect flow runs the same three stages with
run history, timings and retries — and lets you re-run **one** stage instead of all of
them.

```bash
./venv/Scripts/python.exe orchestration/prefect/flows/retail_pipeline.py
```

| Flag | Effect |
|---|---|
| *(none)* | ingest → `dbt build` → `dbt docs generate` |
| `--no-ingest` | Rebuild the warehouse from the Bronze data already in MinIO |
| `--no-transform` | Ingest only |
| `--tables sales` | Ingest a subset of source tables |
| `--select marts` | Restrict the dbt build to selected nodes |
| `--serve` | Register a deployment and wait for runs triggered from the Prefect UI |

Every run appears at **http://localhost:4200** with per-task logs and durations. The
ingestion task retries twice — it's the only stage crossing an external network — and
because the load is a full snapshot, a retry can't duplicate rows.

To trigger runs from the UI (with parameters, or on a schedule), leave the deployment
serving in its own terminal:

```bash
./venv/Scripts/python.exe orchestration/prefect/flows/retail_pipeline.py --serve
```

---

## Pipeline UIs

| URL | What it shows |
|---|---|
| **http://localhost:8081** | **dbt docs** — lineage DAG from Bronze sources through Silver to Gold, model and column descriptions, test coverage, compiled SQL |
| **http://localhost:4200** | **Prefect** — flow run history: which stages ran, how long they took, what failed |
| http://localhost:9002 | MinIO console — the Bronze Parquet files |

The two pipeline views answer different questions: dbt docs shows what the pipeline *is*
(structure and lineage), Prefect shows what it *did* (execution). dbt docs covers only
the dbt half — the SQL Server → MinIO ingestion isn't a dbt model, which is exactly the
gap Prefect fills.

nginx serves `dbt/target/` directly, so refreshing the docs is just:

```bash
cd dbt; ../venv/Scripts/dbt.exe docs generate --profiles-dir .
```

No container restart needed — reload the page. The Prefect flow does this automatically
as its last stage.

---

## Project structure

```text
retail_sales/
├── config.py                  # Single source of credentials/settings (reads .env)
├── docker-compose.yaml        # MinIO + Prefect + dbt-docs
├── requirements.txt
├── .env.example
├── Ingestion/
│   └── ingestion.py           # dlt pipeline: SQL Server → Bronze Parquet
├── orchestration/prefect/flows/
│   └── retail_pipeline.py     # Prefect flow: ingest → dbt build → dbt docs
├── dbt/
│   ├── dbt_project.yml        # staging → silver schema, marts → gold schema
│   ├── profiles.yml           # duckdb + httpfs → MinIO
│   ├── packages.yml           # dbt_utils
│   ├── models/
│   │   ├── staging/           # SILVER: stg_customers/products/sales/stores
│   │   └── marts/
│   │       ├── dimensions/    # dim_customer, dim_product, dim_store, dim_date
│   │       └── facts/         # fact_sales
│   └── tests/                 # singular business-rule tests
├── duckdb_warehouse/          # DuckDB file (git-ignored)
├── claude_code_plan.md        # Milestone checklist (authoritative)
└── implementation_plan.md     # Full design document
```

---

## Data model

Star schema in `xom_retails_gold`:

```text
                    dim_date
                       │
dim_customer ───── fact_sales ───── dim_product
                       │
                   dim_store
```

**`fact_sales` grain: one order line** (one product within one order). Confirmed by
profiling — 62,884 rows equal 62,884 distinct `(order_number, line_item)` pairs across
26,326 orders. Surrogate key `sales_key = md5(order_id, line_item)`.

Measures: `gross_sales = quantity × unit_price`, `gross_cost = quantity × unit_cost`,
`gross_margin = gross_sales − gross_cost`.

### Source profiling findings

These drove the model design and are worth knowing before changing anything:

| Finding | Consequence |
|---|---|
| Referential integrity is perfect — 0 orphan customer/product/store keys | Natural keys used as dimension PKs; `relationships` tests guard it |
| `delivery_date` is NULL in 79% of rows — populated **only** for `store_key = 0` (the Online channel) | Not dirty data. No `not_null` test; the rule is asserted by `tests/assert_delivery_only_for_online_orders.sql` |
| `store_key = 0` is "Online" and the only store with NULL `square_meters` | `dim_store.is_online` flag; NULL preserved so revenue-per-sqm never divides by zero |
| Orders span 2016-01-01 → 2021-02-20, but only 1,641 of ~1,878 calendar days have sales | `dim_date` is generated (2015–2022), never derived from the fact |
| `quantity` ∈ [1,10], prices and costs all > 0, no product priced below cost | Range tests encode these as expectations |
| 75 `(order_number, product_key)` pairs span multiple lines of one order | Doesn't break the grain, but Market Basket must dedupe products per basket |

### Known limitations

- **The source records no price at transaction time.** `fact_sales` measures use the
  product's *current* price and cost, so revenue and margin are an approximation at a
  fixed price point, not restated history.
- All amounts are USD — the source carries no currency column or exchange-rate table.
- No SCD Type 2: the source has no history for customers, products or stores.

---

## Data quality

Every model is tested; `dbt build` fails the pipeline if any test fails.

- **Keys** — `unique` + `not_null` on every dimension PK and on `sales_key`;
  `unique_combination_of_columns` on `(order_number, line_item)`.
- **Referential integrity** — `relationships` from `fact_sales` to all four dimensions,
  including both date roles.
- **Ranges** — `quantity` 1–10, prices/costs strictly positive, `gross_sales > 0`,
  `unit_margin >= 0`.
- **Business rules** — `gross_margin = gross_sales − gross_cost`;
  `delivery_date >= order_date`; delivery date exists if and only if the order is online.
- **Idempotency** — each staging model applies a `qualify row_number()` guard, so a
  duplicated Bronze snapshot cannot duplicate Silver rows.

Ingestion lineage (`ingested_at_utc`, `source_system`, `source_table`) is stamped on every
row at extract time and carried through Silver.

---

## Status

| Milestone | State |
|---|---|
| 0 — Profiling & architecture | Source profiled (results above); `docs/` write-up outstanding |
| 1 — Ingestion & Bronze | Code complete: env-driven config, table allowlist, ingestion metadata, logging, `--tables` CLI |
| 2 — Silver & Gold (dbt-duckdb) | Models, sources and tests written; **not yet executed end to end** |
| 3 — Analytics / Metabase | Not started |
| 4 — ML (Market Basket first) | Not started |
| 5 — FastAPI + Streamlit | Not started |

`claude_code_plan.md` holds the full checklist. Where it and `implementation_plan.md`
disagree, `claude_code_plan.md` wins — notably the warehouse is **DuckDB**, not the
ClickHouse named in the older design document.

---

## Troubleshooting

**`docker ps` fails with a named-pipe error** — Docker Desktop isn't running.

**dbt can't read `s3://bronze/...`** — check MinIO is up, and that `profiles.yml` has
`s3_url_style: path`; MinIO serves buckets as paths, not virtual hosts. DuckDB also
downloads the `httpfs` extension on first use, which needs internet access.

**dbt sees no data after renaming things** — the dlt resources are named `customers`,
`products`, `sales`, `stores`. An older version of the script used `customer`, `product`
and `retail_sales`; delete those stale folders from the bucket so nothing reads them.

**dbt ignores `.env`** — it does not load it. Every `env_var()` in `profiles.yml` carries a
default matching `docker-compose.yaml`, so the defaults work out of the box; export the
variables explicitly to override them.

**DuckDB "file is locked"** — DuckDB allows a single writer. Close any notebook, Metabase
connection or Python session holding `warehouse.duckdb` before running dbt.
