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

## 5. Not done / follow-ups

- `docs/data_dictionary.md` and `docs/data_model.md` (Milestone 0 write-up) outstanding
- `dbt build` emits 19 `MissingArgumentsPropertyInGenericTestDeprecation` warnings —
  dbt 1.12 wants generic-test arguments nested under an `arguments:` key. Cosmetic today.
- Three stale Bronze folders (`customer/`, `product/`, `retail_sales/`) survive in MinIO
  from the pre-rename script. Harmless — dbt reads the plural names — but worth deleting.
- `.env.example` was created during the session but is no longer on disk; the README
  quick start references it.
- Prefect is running in docker-compose but nothing is wired to it (deferred by choice).
- Milestones 3–5 (Metabase, ML, FastAPI/Streamlit) not started.
