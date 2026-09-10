-- ANALYTICS: revenue time series. Grain: one row per calendar month with sales.
--
-- `is_partial_period` flags months where the source data does not cover the
-- whole month. Sales stop on 2021-02-20, so February 2021 looks like a collapse
-- in revenue when it is really a truncated month - any trend or MoM answer that
-- ignores this is misleading.

with monthly as (

    select
        date_trunc('month', d.full_date)    as month_start,
        d.year,
        d.month,
        d.month_name,
        d.quarter,
        count(distinct f.order_id)          as order_count,
        count(distinct f.customer_key)      as customer_count,
        count(distinct f.product_key)       as products_sold,
        sum(f.quantity)                     as units_sold,
        sum(f.gross_sales)                  as revenue,
        sum(f.gross_cost)                   as cost,
        sum(f.gross_margin)                 as profit,
        max(d.full_date)                    as last_order_date
    from {{ ref('fact_sales') }} f
    join {{ ref('dim_date') }} d on f.date_key = d.date_key
    group by 1, 2, 3, 4, 5

),

bounds as (

    select
        min(month_start) as first_month,
        max(month_start) as last_month,
        max(last_order_date) as max_order_date
    from monthly

)

select
    m.month_start,
    m.year,
    m.month,
    m.month_name,
    m.quarter,

    m.order_count,
    m.customer_count,
    m.products_sold,
    m.units_sold,
    m.revenue,
    m.cost,
    m.profit,

    round(m.profit / nullif(m.revenue, 0) * 100, 2)     as margin_pct,
    round(m.revenue / nullif(m.order_count, 0), 2)      as avg_order_value,

    -- The final month is partial whenever the last order predates its month end.
    m.month_start = b.last_month
        and b.max_order_date < (m.month_start + interval 1 month - interval 1 day)
        as is_partial_period

from monthly m
cross join bounds b
order by m.month_start
