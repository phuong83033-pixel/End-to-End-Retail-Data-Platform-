-- SILVER: cleaned products. Grain: one row per product_key.
-- Drops the `_usd` suffix from the money columns (the whole source is USD - it
-- carries no currency column) and casts them to a fixed-precision decimal.

with source as (

    select * from {{ source('bronze', 'products') }}

),

cleaned as (

    select
        cast(product_key as integer)            as product_key,
        nullif(trim(product_name), '')          as product_name,
        nullif(trim(brand), '')                 as brand,
        nullif(trim(color), '')                 as color,
        cast(unit_cost_usd as decimal(10, 2))   as unit_cost,
        cast(unit_price_usd as decimal(10, 2))  as unit_price,
        cast(subcategory_key as integer)        as subcategory_key,
        nullif(trim(subcategory), '')           as subcategory,
        cast(category_key as integer)           as category_key,
        nullif(trim(category), '')              as category,

        cast(ingested_at_utc as timestamp)      as ingested_at_utc,
        source_system                           as source_system

    from source

)

select * from cleaned
qualify row_number() over (
    partition by product_key order by ingested_at_utc desc
) = 1
