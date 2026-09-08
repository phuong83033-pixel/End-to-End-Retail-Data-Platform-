-- SILVER: cleaned stores. Grain: one row per store_key.
-- square_meters stays NULL for store_key = 0 (the "Online" channel) - it has no
-- physical floor space, so filling it with 0 would corrupt revenue-per-sqm.

with source as (

    select * from {{ source('bronze', 'stores') }}

),

cleaned as (

    select
        cast(store_key as integer)              as store_key,
        nullif(trim(country), '')               as country,
        nullif(trim(state), '')                 as state,
        cast(square_meters as decimal(10, 2))   as square_meters,
        cast(open_date as date)                 as open_date,

        cast(ingested_at_utc as timestamp)      as ingested_at_utc,
        source_system                           as source_system

    from source

)

select * from cleaned
qualify row_number() over (
    partition by store_key order by ingested_at_utc desc
) = 1
