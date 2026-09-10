# Changes Log

Everything built and changed in this session, in the order it happened.
Scope: **pipeline only** — Bronze → Silver → Gold. No ML work.

Branch: `feat/silver-gold-dbt-warehouse` → PR
[#1](https://github.com/phuong83033-pixel/End-to-End-Retail-Data-Platform-/pull/1)

---

## 0. Source profiling (Milestone 0)

Before writing any model SQL, I profiled the live SQL Server source read-only. This
settled the open questions in `implementation_plan.md` §18 that everything downstream
depends on.

| Table | Rows | Findings |
|---|---|---|
| `sales` | 62,884 | **Grain = one order line.** PK `(order_number, line_item)`, 0 duplicates. 26,326 orders, avg 2.39 lines/order, max 7 |
| `customers` | 15,266 | `customer_key` unique. 10 NULL `state_code`. `gender` ∈ {Male, Female} |
| `products` | 2,517 | `product_key` unique. No NULL cost/price, none ≤ 0, none priced below cost. 8 categories, 32 subcategories, 11 brands |
| `stores` | 67 | `store_key` unique. `store_key = 0` = "Online", the only NULL `square_meters`. 9 countries |

Findings that changed the design:

- **Referential integrity is perfect** — 0 orphan customer/product/store keys in `sales`.
- **`delivery_date` NULL in 49,719 of 62,884 rows (79%) is correct, not dirty.** It is
  populated only for `store_key = 0`; every physical store has it NULL, always.
- **Date range 2016-01-01 → 2021-02-20, but only 1,641 of ~1,878 calendar days have sales.**
- `quantity` ∈ [1,10], no NULLs. 0 rows with `delivery_date < order_date`.
- 75 `(order_number, product_key)` pairs span multiple lines of one order — doesn't break
  the grain, but Market Basket will need to dedupe products per basket later.
- Source has **no currency column and no transaction-time price**.

---

## 1. Milestone 1 — Ingestion hardening

### `config.py` — rewritten (13 → 127 lines)

The original was broken and dead: it defined `MINIO_*` constants, then
`get_minio_client()` read `config.MINIO_ENDPOINT` where `config` was never imported —
any call raised `NameError`. Nothing imported the module.

Now the single source of credentials:

- Loads `.env` from the project root regardless of cwd
- Exposes `OLTP_*`, `OLTP_SCHEMA`, `SOURCE_SYSTEM`, `MINIO_*`, `DUCKDB_PATH`
- `validate_config()` — fails fast, naming every missing variable
- `get_minio_client()`, `get_minio_endpoint_url()`, `get_dlt_filesystem_credentials()`,
  `get_bronze_bucket_url()`, `get_sql_server_connection_kwargs()`

### `Ingestion/ingestion.py` — rewritten (92 → 183 lines)

| Before | After |
|---|---|
| Read env vars itself; MinIO credentials hardcoded **twice** | Imports `config`; credentials live only in `.env` |
| Resources `customer`, `product`, `retail_sales`, `stores` | `customers`, `products`, `sales`, `stores` — these become the Bronze folder names dbt reads |
| 4 near-identical resource functions | `SOURCE_TABLES` tuple + `build_table_reader()` factory |
| `f"SELECT * FROM retails.{table_name}"`, no guard | Allowlist check before the name reaches SQL; schema from `config.OLTP_SCHEMA` |
| No lineage | Every row stamped `ingested_at_utc`, `source_system`, `source_table` |
| `print(info)` | `logging` — per-table row counts, load ids, duration |
| No arguments | `--tables sales` to reload one table |

Metadata column names deliberately avoid a leading underscore — dlt reserves `_dlt_*`.

### Supporting files

- **`.env`** — added `OLTP_SCHEMA` and five `MINIO_*` keys; changed `DUCKDB_PATH` to an
  absolute path (dbt runs from `dbt/`, so a relative path would resolve there)
- **`requirements.txt`** — new; ingestion stack + `duckdb` + `dbt-duckdb`
- **`docker-compose.yaml`** — `PREFECT_UI_URL` pointed at port 4300, service publishes 4200

---

## 2. Milestone 2 — dbt-duckdb warehouse

New `dbt/` project, 18 files. dbt reads Bronze Parquet straight out of MinIO via
`httpfs`; nothing is copied before Silver.

### Scaffolding

- **`dbt_project.yml`** — staging → `+schema: silver`, marts → `+schema: gold`, both
  materialised as **tables**. dbt appends custom schemas to the target, producing
  `xom_retails_silver` / `xom_retails_gold`.
- **`profiles.yml`** — duckdb + `httpfs`, `s3_url_style: path` (**required** — MinIO
  serves buckets as paths; the virtual-host default fails). Every `env_var()` carries a
  default matching docker-compose, because dbt does not read `.env`.
- **`packages.yml`** — dbt_utils 1.4.1

### Silver — `models/staging/`

`stg_customers`, `stg_products`, `stg_sales`, `stg_stores`. Each: explicit column list
(never `select *`), type casts, `nullif(trim(col), '')`, and a `qualify row_number()
over (partition by <pk> order by ingested_at_utc desc) = 1` guard so a duplicated Bronze
snapshot cannot duplicate rows.

- `stg_products` drops the `_usd` suffix → `unit_cost` / `unit_price`, `decimal(10,2)`
- `stg_customers` renames source `name` → `customer_name`
- `stg_sales` preserves `delivery_date` NULLs as-is
- `stg_stores` preserves NULL `square_meters` for the online store

`_sources.yml` maps Bronze via
`external_location: s3://bronze/xom_retails/{name}/*.parquet`.

### Gold — `models/marts/`

- **`dim_customer`**, **`dim_product`** (+`unit_margin`, category hierarchy denormalised),
  **`dim_store`** (+`is_online`), all on natural keys — profiling showed they're clean
  and the source has no history, so no SCD Type 2 in V1
- **`dim_date`** — generated with `range()` over 2015-01-01 … 2022-12-31 (2,922 rows).
  Generated, **not** derived from sales, because sales only covers 1,641 of the days it
  spans — a fact-derived calendar would leave holes in every time series
- **`fact_sales`** — grain one order line. `sales_key = md5(order_id, line_item)`,
  `date_key` + nullable `delivery_date_key` as role-playing date FKs, measures
  `gross_sales` / `gross_cost` / `gross_margin`. LEFT join to products on purpose: a
  future orphan then fails the `not_null` test loudly instead of silently dropping revenue

### Data quality — 64 tests

- **Keys** — `unique` + `not_null` on every dimension PK and `sales_key`;
  `unique_combination_of_columns` on `(order_number, line_item)`
- **Referential integrity** — `relationships` to all four dimensions, both date roles
- **Ranges** — `quantity` 1–10, prices/costs > 0, `gross_sales` > 0, `unit_margin` ≥ 0
- **Business rules** — `gross_margin = gross_sales - gross_cost`,
  `delivery_date >= order_date`
- **`tests/assert_delivery_only_for_online_orders.sql`** — asserts a delivery date exists
  if and only if `store_key = 0`. This encodes the real rule profiling found, instead of
  the wrong `not_null` test someone would otherwise reach for.

---

## 3. Repo housekeeping

- **`.gitignore`** — removed the `dbt/` entry, which predated the dbt project and would
  have excluded the entire warehouse layer from version control. Now ignores only build
  artefacts (`dbt/target/`, `dbt/dbt_packages/`, `dbt/logs/`, `dbt/.user.yml`) and the
  7 MB local `duckdb_warehouse/` file.
- **`README.md`** — rewritten from a one-line stub into a full guide: architecture, stack,
  6-step quick start, project tree, data model, profiling findings, data quality, status
  table, troubleshooting.
- **`venv`** — installed `duckdb 1.5.5`, `dbt-duckdb 1.11.0`, `dbt-core 1.12.3`.

---

## 4. Verification

`dbt build` — **9 models, 64 data tests, 73 PASS / 0 ERROR / 0 WARN** in 2.34s.

Warehouse output checked against the source profile:

```
fact_sales      62,884 rows / 26,326 distinct orders   ✓ matches source exactly
dim_customer    15,266     dim_product   2,517
dim_store           67     dim_date      2,922
stg_customers   15,266     stg_products  2,517
stg_sales       62,884     stg_stores       67
sum(gross_margin) = sum(gross_sales) - sum(gross_cost)  ✓ true

revenue by year
  2016   6,946,794     2019  18,264,382
  2017   7,421,422     2020   9,294,632
  2018  12,788,961     2021   1,039,288  (partial — data stops 2021-02-20)
```

---

## 5. Pipeline UIs + orchestration

### dbt docs — http://localhost:8081

New `dbt-docs` service in docker-compose: `nginx:alpine` serving `./dbt/target` read-only
on port 8081. Shows the lineage DAG (Bronze sources → 4 Silver models → 5 Gold models),
model and column descriptions, test coverage and compiled SQL.

Static files only, so `dbt docs generate` refreshes the site with no container restart.

### Prefect flow — http://localhost:4200

`orchestration/prefect/flows/retail_pipeline.py` wraps the whole pipeline in three tasks:

```
ingest-bronze  →  dbt-build  →  dbt-docs-generate
```

Each stage has its own switch, so you never repeat work that already succeeded:

| Flag | Effect |
|---|---|
| *(none)* | full pipeline |
| `--no-ingest` | rebuild the warehouse from Bronze already in MinIO |
| `--no-transform` | ingest only |
| `--tables sales` | ingest a subset |
| `--select marts` | restrict the dbt build |
| `--serve` | register a deployment for UI-triggered runs |

`ingest-bronze` retries twice (the only stage crossing an external network); the load is
a full snapshot, so a retry cannot duplicate rows. dbt runs as a subprocess rather than
an import, keeping its dependency tree isolated from Prefect's.

### Fixed: Prefect had no persistent storage

The `prefect-server` service had **no volume** — its SQLite database (all run history,
deployments and logs) lived inside the container, so any `docker compose down` or image
update would wipe the history the orchestrator exists to provide. Added a
`prefect_data` volume at `/root/.prefect`. Caught while only 1 test run existed, so
nothing was lost.

### Verified

- Full flow end to end: ingest 7s → `dbt build` 73 PASS → docs regenerated
- `--no-ingest` fast path: transform only, 73 PASS
- Deployment registered and **READY**; a run triggered through the API with
  `{run_ingestion: false, run_transform: true}` completed successfully — this is the
  same path the UI's "Run" button uses
- Both UIs return HTTP 200

---

## 6. Milestone 0 completed + cleanup

- **`docs/data_dictionary.md`** — every column across Source, Bronze, Silver and Gold with
  observed types, nullability, ranges and cardinality.
- **`docs/data_model.md`** — Mermaid ERD, the grain statement, each design decision with
  the profiling result behind it, and answers to all 10 open questions from
  `implementation_plan.md` §18.
- **dbt deprecations fixed** — all 19 `MissingArgumentsPropertyInGenericTestDeprecation`
  warnings resolved by nesting generic-test arguments under `arguments:` in both YAML
  files. dbt 1.12 warns today; dbt Fusion will reject the old form. Build re-verified:
  9 models, 64 tests, 73 PASS, zero warnings.
- **`.env.example` restored** — the README quick start tells you to copy it, and it was
  missing.
- **dbt model folders renamed** `staging/` → `silver/`, `marts/` → `gold/` (flattened),
  so the layout names the medallion layers. Schema names unchanged.

---

## 7. ML plan — how data reaches the models

*Planning only. No ML code written yet.*

### 7.1 The connection: dbt feature marts, not ad-hoc SQL in notebooks

The instinct is to have each training script open DuckDB and run its own SQL. That gets
messy fast: the same "what counts as a basket" logic ends up copied across notebooks and
drifts. Instead, **every ML input becomes a dbt model in a `models/ml/` folder**, so
feature engineering is versioned, tested, and visible in the same lineage graph as the
rest of the warehouse:

```text
xom_retails_gold.fact_sales + dims
        │  dbt (models/ml/, schema xom_retails_ml)
        ▼
ml_basket_transactions       one row per (order_id, product_key), deduped
ml_customer_product_matrix   one row per (customer_key, product_key) with counts
ml_category_monthly_demand   one row per (category, month) with units + revenue
        │  Python reads a finished table - no business logic in the notebook
        ▼
mlxtend / scikit-learn / PyTorch
        │
        ▼
results written back to DuckDB (ml_basket_rules, ...) and/or ml/artifacts/
```

Python's data layer stays thin — a single `ml/common/data.py` with one loader per mart,
each doing `select * from xom_retails_ml.<table>` into a DataFrame.

### 7.2 The DuckDB locking constraint (this drives the design)

DuckDB allows **one writer**. A notebook holding the warehouse open blocks `dbt build`,
and vice versa. Two rules follow, and they are not optional:

1. **All ML reads use `duckdb.connect(path, read_only=True)`.** Multiple readers coexist
   fine; a reader only conflicts with an active writer.
2. **Training is sequenced after the build by Prefect, never run concurrently.** The
   existing flow already gives this for free — an `ml-train` task added after `dbt-build`
   cannot overlap with it.

This is the same constraint that made Metabase awkward. Sequencing through the
orchestrator sidesteps it entirely, which is a good argument for the flow we already have.

### 7.3 What the data will actually support

Profiled against the Gold layer before committing to any model. Two of the three
milestone-4 models need their scope changed:

| | Finding | Consequence |
|---|---|---|
| **Market Basket** | 26,326 orders, but **9,205 (35%) are single-item** — useless for association rules. 17,121 usable baskets. Median product appears in just **15 orders** → support ≈ 0.0009 | **Product-level rules are marginal.** Run FP-Growth at **category (8) and subcategory (32) level** as the primary analysis, and product-level only on the **338 products with ≥50 orders** |
| **Recommendation** | 11,887 customers have purchases (3,379 never bought). **7,272 have >1 order**; interaction density **0.21%** | Feasible. Train on repeat buyers; 4,615 single-order customers are cold-start and belong to the popularity baseline |
| **Forecasting** | Products average **25 line items across 5 years** (median 15). **75% of product-months have zero sales** (39,212 of 156,054) | **Per-product forecasting is not viable.** Aggregate to category-month (8 × 62 = 496 points) or total-monthly (62 points). Enough for naive/seasonal baselines; **not** enough for LSTM — the plan's "only if justified" clause applies, and it isn't |

### 7.4 Proposed model sequence

**Model 1 — Market Basket (FP-Growth, mlxtend).** First because it's simple, needs no
train/test split, and validates the transaction grain end to end. Input
`ml_basket_transactions` filtered to multi-item baskets; must dedupe the 75
`(order_id, product_key)` pairs that span multiple lines. Output: antecedent, consequent,
support, confidence, lift — written back to DuckDB so rules can join to `dim_product`.

**Model 2 — Recommendation.** Popularity baseline → item-item cosine similarity →
matrix factorisation (implicit ALS). Evaluated with a **temporal** split (train on orders
before a cutoff date, test after) rather than a random one — a random split leaks future
purchases. Metrics: Precision@K, Recall@K, MAP@K.

**Model 3 — Demand forecasting.** Only at category-month grain, and only after 1 and 2.
Naive and seasonal-naive baselines first; a simple ML model only if it beats them on
MAE/MAPE. No deep learning — 62 monthly observations cannot support it.

### 7.5 Proposed layout

```text
dbt/models/ml/          ml_basket_transactions.sql, ml_customer_product_matrix.sql,
                        ml_category_monthly_demand.sql  (+ tests)
ml/common/data.py       read-only DuckDB loaders, one per mart
ml/basket/              mining.py, rules.py
ml/recommendation/      data.py, model.py, train.py, evaluate.py
ml/artifacts/           serialised models (git-ignored)
```

Prefect gains an optional `ml-train` task after `dbt-build`, off by default so the daily
pipeline stays fast.

---

## 8. Not done / follow-ups

- `docs/data_dictionary.md` and `docs/data_model.md` (Milestone 0 write-up) outstanding
- `dbt build` emits 19 `MissingArgumentsPropertyInGenericTestDeprecation` warnings —
  dbt 1.12 wants generic-test arguments nested under an `arguments:` key. Cosmetic today.
- Three stale Bronze folders (`customer/`, `product/`, `retail_sales/`) survive in MinIO
  from the pre-rename script. Harmless — dbt reads the plural names — but worth deleting.
- `.env.example` was created during the session but is no longer on disk; the README
  quick start references it.
- The Prefect deployment only accepts UI-triggered runs while `--serve` is running in a
  terminal. Making that survive reboots means a worker service in docker-compose.
- No schedule is set on the deployment yet — `retail_pipeline.serve(cron=...)` would add
  one (e.g. nightly).
- Milestones 3–5 (Metabase, ML, FastAPI/Streamlit) not started.
