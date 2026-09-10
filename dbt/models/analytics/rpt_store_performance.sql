-- ANALYTICS: store scorecard. Grain: one row per store.
--
-- revenue_per_sqm is deliberately NULL for the online store (store_key = 0),
-- which has no floor space. Substituting 0 would make it look infinitely
-- productive; excluding the row would hide the largest channel by revenue.

with sales as (

    select
        store_key,
        count(distinct order_id)        as order_count,
        count(distinct customer_key)    as customer_count,
        count(distinct product_key)     as products_sold,
        sum(quantity)                   as units_sold,
        sum(gross_sales)                as revenue,
        sum(gross_cost)                 as cost,
        sum(gross_margin)               as profit
    from {{ ref('fact_sales') }}
    group by store_key

)

select
    s.store_key,
    s.country,
    s.state,
    s.is_online,
    s.square_meters,
    s.open_date,

    coalesce(f.order_count, 0)      as order_count,
    coalesce(f.customer_count, 0)   as customer_count,
    coalesce(f.products_sold, 0)    as products_sold,
    coalesce(f.units_sold, 0)       as units_sold,
    coalesce(f.revenue, 0)          as revenue,
    coalesce(f.cost, 0)             as cost,
    coalesce(f.profit, 0)           as profit,

    round(f.profit / nullif(f.revenue, 0) * 100, 2)     as margin_pct,
    round(f.revenue / nullif(f.order_count, 0), 2)      as avg_order_value,
    round(f.revenue / nullif(s.square_meters, 0), 2)    as revenue_per_sqm

from {{ ref('dim_store') }} s
left join sales f on s.store_key = f.store_key
