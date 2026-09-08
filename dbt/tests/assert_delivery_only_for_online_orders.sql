-- Business rule discovered during source profiling:
--   a delivery date exists for ONLINE orders (store_key = 0) and for nothing else.
-- Verified on the full source: 13,165 online lines all have a delivery date,
-- 49,719 in-store lines all have none.
--
-- This is why fact_sales.delivery_date_key carries no not_null test - the NULLs
-- are meaningful. This test fails if a row breaks the rule in either direction.

select
    sales_key,
    store_key,
    delivery_date_key

from {{ ref('fact_sales') }}

where (store_key = 0 and delivery_date_key is null)
   or (store_key <> 0 and delivery_date_key is not null)
