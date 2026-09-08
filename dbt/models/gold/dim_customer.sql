-- GOLD: customer dimension. Grain: one row per customer.
-- Natural key (customer_key) is used as the PK: profiling showed it is unique
-- and complete, and the source has no history, so no SCD Type 2 in V1.

select
    customer_key,
    customer_name,
    gender,
    city,
    state_code,
    state,
    zip_code,
    country,
    continent,
    birthday

from {{ ref('stg_customers') }}
