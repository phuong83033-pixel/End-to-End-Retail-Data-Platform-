-- GOLD: store dimension. Grain: one row per store.
-- `is_online` separates the online channel (store_key = 0) from the 66 physical
-- stores; it is what keeps revenue-per-square-meter from dividing by NULL.

select
    store_key,
    country,
    state,
    square_meters,
    open_date,
    store_key = 0 as is_online

from {{ ref('stg_stores') }}
