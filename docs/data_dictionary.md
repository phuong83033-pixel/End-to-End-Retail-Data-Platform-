# Data Dictionary

Every column across the three layers, with the observed profile from the live source
(profiled 2026-08-25, re-verified against the warehouse 2026-09-08).

Row counts: `customers` 15,266 · `products` 2,517 · `sales` 62,884 · `stores` 67.

---

## 1. Source — SQL Server `retails` schema

### `retails.customers` — 15,266 rows

| Column | Type | Null | Notes |
|---|---|---|---|
| `customer_key` | int | NO | **PK.** Unique, no duplicates |
| `gender` | nvarchar(100) | YES | No NULLs observed. Only `Male` (7,748) / `Female` (7,518) |
| `name` | nvarchar(100) | NO | Renamed `customer_name` in Silver |
| `city` | nvarchar(50) | YES | |
| `state_code` | nvarchar(100) | YES | **10 NULLs** |
| `state` | nvarchar(50) | YES | |
| `zip_code` | nvarchar(20) | YES | Kept as text — leading zeros are significant |
| `country` | nvarchar(50) | YES | |
| `continent` | nvarchar(50) | YES | |
| `birthday` | date | YES | No NULLs observed |

### `retails.products` — 2,517 rows

| Column | Type | Null | Notes |
|---|---|---|---|
| `product_key` | int | NO | **PK.** Unique |
| `product_name` | nvarchar(255) | NO | |
| `brand` | nvarchar(100) | YES | 11 distinct |
| `color` | nvarchar(50) | YES | |
| `unit_cost_usd` | decimal(10,2) | YES | No NULLs, none ≤ 0. Min 0.48 |
| `unit_price_usd` | decimal(10,2) | YES | No NULLs, none ≤ 0. Max 3,199.99 |
| `subcategory_key` | int | YES | 32 distinct |
| `subcategory` | nvarchar(100) | YES | |
| `category_key` | int | YES | 8 distinct |
| `category` | nvarchar(100) | YES | |

**0 products** are priced below cost, so margin is non-negative everywhere.

### `retails.sales` — 62,884 rows

**Grain: one order line.** 62,884 rows = 62,884 distinct `(order_number, line_item)`
pairs across 26,326 orders (avg 2.39 lines/order, max 7).

| Column | Type | Null | Notes |
|---|---|---|---|
| `order_number` | nvarchar(50) | NO | **PK part 1.** Repeats across the lines of one order |
| `line_item` | int | NO | **PK part 2** |
| `order_date` | date | YES | No NULLs. Range 2016-01-01 → 2021-02-20, 1,641 distinct dates |
| `delivery_date` | date | YES | **49,719 NULLs (79%) — by design**, see below |
| `customer_key` | int | YES | **FK** → customers. No NULLs, 0 orphans |
| `store_key` | int | YES | **FK** → stores. No NULLs, 0 orphans |
| `product_key` | int | YES | **FK** → products. No NULLs, 0 orphans |
| `quantity` | int | YES | No NULLs. Range [1, 10] |

> **The `delivery_date` NULLs are not a data-quality defect.** A delivery date is
> recorded *only* for the online store (`store_key = 0`): all 13,165 online lines have
> one, all 49,719 in-store lines do not. No row has `delivery_date < order_date`.

### `retails.stores` — 67 rows

| Column | Type | Null | Notes |
|---|---|---|---|
| `store_key` | int | NO | **PK.** Unique. `0` = the Online channel |
| `country` | nvarchar(50) | YES | 9 distinct (plus `Online`) |
| `state` | nvarchar(50) | YES | |
| `square_meters` | decimal(10,2) | YES | **1 NULL — store 0 (Online)**, which has no floor space |
| `open_date` | date | YES | Store 0 opened 2010-01-01 |

---

## 2. Bronze — `s3://bronze/xom_retails/<table>/*.parquet`

Raw snapshots, replaced in full on every ingestion run. Source columns are carried
through unchanged, plus:

| Column | Type | Purpose |
|---|---|---|
| `ingested_at_utc` | timestamp | One value per pipeline run — lineage and Silver dedupe ordering |
| `source_system` | varchar | `xom_data_sqlserver` |
| `source_table` | varchar | e.g. `retails.sales` |
| `_dlt_load_id`, `_dlt_id` | varchar | dlt's own load tracking |

Metadata names avoid a leading underscore because dlt reserves the `_dlt_` prefix.

---

## 3. Silver — `xom_retails_silver`

Cleaned and standardised: explicit casts, `nullif(trim(col), '')` on text, and a
`qualify row_number()` guard keeping the newest `ingested_at_utc` per key.

| Model | Rows | Changes from source |
|---|---|---|
| `stg_customers` | 15,266 | `name` → `customer_name` |
| `stg_products` | 2,517 | `unit_cost_usd` → `unit_cost`, `unit_price_usd` → `unit_price`, `decimal(10,2)` |
| `stg_sales` | 62,884 | Types cast; `delivery_date` NULLs preserved |
| `stg_stores` | 67 | `square_meters` NULL preserved for store 0 |

All four carry `ingested_at_utc` and `source_system` through as lineage.

---

## 4. Gold — `xom_retails_gold`

### `dim_customer` — 15,266 rows, one per customer

`customer_key` (PK) · `customer_name` · `gender` · `city` · `state_code` · `state` ·
`zip_code` · `country` · `continent` · `birthday`

> Only **11,887** of these customers appear in `fact_sales`; 3,379 have never purchased.

### `dim_product` — 2,517 rows, one per product

`product_key` (PK) · `product_name` · `brand` · `color` · `unit_cost` · `unit_price` ·
`unit_margin` · `subcategory_key` · `subcategory` · `category_key` · `category`

`unit_margin = unit_price - unit_cost`. Category hierarchy is denormalised here (V1).

### `dim_store` — 67 rows, one per store

`store_key` (PK) · `country` · `state` · `square_meters` · `open_date` · `is_online`

`is_online = (store_key = 0)`.

### `dim_date` — 2,922 rows, one per day 2015-01-01 → 2022-12-31

`date_key` (PK, `yyyymmdd` integer) · `full_date` · `day` · `month` · `month_name` ·
`quarter` · `year` · `week` · `day_of_week` · `day_name` · `is_weekend`

Generated, not derived from sales — orders fall on only 1,641 of the ~1,878 days spanned,
so a fact-derived calendar would leave gaps in every time series.

### `fact_sales` — 62,884 rows

**Grain: one order line.**

| Column | Notes |
|---|---|
| `sales_key` | **PK.** Surrogate, `md5(order_id, line_item)` |
| `order_id` | Source `order_number`. 26,326 distinct |
| `line_item` | Line number within the order |
| `date_key` | **FK** → `dim_date`. Order date |
| `delivery_date_key` | **FK** → `dim_date`. NULL for in-store orders |
| `customer_key` | **FK** → `dim_customer` |
| `product_key` | **FK** → `dim_product` |
| `store_key` | **FK** → `dim_store` |
| `quantity` | 1–10 |
| `unit_price`, `unit_cost` | Joined from `dim_product` — see limitation below |
| `gross_sales` | `quantity × unit_price` |
| `gross_cost` | `quantity × unit_cost` |
| `gross_margin` | `gross_sales - gross_cost` |

Total revenue across the fact: **$55,755,480**.

> **Limitation — measures use current prices.** The source records no price at
> transaction time, so revenue and margin are computed from each product's *current*
> price and cost. They are an approximation at a fixed price point, not restated
> history. All amounts are USD; the source has no currency column or exchange-rate table.
