-- SILVER: cleaned sales.
-- Grain: ONE ORDER LINE - confirmed by profiling the source (62,884 rows =
-- 62,884 distinct (order_number, line_item) pairs across 26,326 orders).
--
-- `delivery_date` is NULL for ~79% of rows and that is CORRECT, not dirty data:
-- only the online store (store_key = 0) records a delivery date. The rule is
-- enforced by tests/assert_delivery_only_for_online_orders.sql, not by coercing
-- the NULLs away here.

with source as (

    select * from {{ source('bronze', 'sales') }}

),

cleaned as (

    select
        cast(trim(order_number) as varchar) as order_number,
        cast(line_item as integer)          as line_item,
        cast(order_date as date)            as order_date,
        cast(delivery_date as date)         as delivery_date,
        cast(customer_key as integer)       as customer_key,
        cast(store_key as integer)          as store_key,
        cast(product_key as integer)        as product_key,
        cast(quantity as integer)           as quantity,

        cast(ingested_at_utc as timestamp)  as ingested_at_utc,
        source_system                       as source_system

    from source

)

select * from cleaned
qualify row_number() over (
    partition by order_number, line_item order by ingested_at_utc desc
) = 1
