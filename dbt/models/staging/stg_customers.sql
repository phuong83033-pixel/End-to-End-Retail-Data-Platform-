-- SILVER: cleaned customers. Grain: one row per customer_key.
-- Standardises column naming (source `name` -> `customer_name`), casts types and
-- turns empty strings into proper NULLs.

with source as (

    select * from {{ source('bronze', 'customers') }}

),

cleaned as (

    select
        cast(customer_key as integer)       as customer_key,
        nullif(trim(gender), '')            as gender,
        nullif(trim(name), '')              as customer_name,
        nullif(trim(city), '')              as city,
        nullif(trim(state_code), '')        as state_code,
        nullif(trim(state), '')             as state,
        nullif(trim(zip_code), '')          as zip_code,
        nullif(trim(country), '')           as country,
        nullif(trim(continent), '')         as continent,
        cast(birthday as date)              as birthday,

        -- ingestion lineage carried through from Bronze
        cast(ingested_at_utc as timestamp)  as ingested_at_utc,
        source_system                       as source_system

    from source

)

select * from cleaned
-- Idempotency guard: if a Bronze folder ever holds more than one snapshot,
-- keep only the most recently ingested version of each customer.
qualify row_number() over (
    partition by customer_key order by ingested_at_utc desc
) = 1
