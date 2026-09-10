-- ANALYTICS: product scorecard. Grain: one row per product.
--
-- Built with a LEFT JOIN from dim_product so products that never sold appear
-- with zeros rather than vanishing - "which products never sell?" is a real
-- question, and an inner join would silently answer it wrong.

with sales as (

    select
        product_key,
        count(distinct order_id)        as order_count,
        count(*)                        as line_count,
        count(distinct customer_key)    as customer_count,
        sum(quantity)                   as units_sold,
        sum(gross_sales)                as revenue,
        sum(gross_cost)                 as cost,
        sum(gross_margin)               as profit
    from {{ ref('fact_sales') }}
    group by product_key

),

sold_dates as (

    select
        f.product_key,
        min(d.full_date) as first_sold_date,
        max(d.full_date) as last_sold_date
    from {{ ref('fact_sales') }} f
    join {{ ref('dim_date') }} d on f.date_key = d.date_key
    group by f.product_key

)

select
    p.product_key,
    p.product_name,
    p.brand,
    p.color,
    p.category,
    p.subcategory,
    p.unit_price,
    p.unit_cost,

    coalesce(s.order_count, 0)      as order_count,
    coalesce(s.line_count, 0)       as line_count,
    coalesce(s.customer_count, 0)   as customer_count,
    coalesce(s.units_sold, 0)       as units_sold,
    coalesce(s.revenue, 0)          as revenue,
    coalesce(s.cost, 0)             as cost,
    coalesce(s.profit, 0)           as profit,

    -- NULL rather than 0 when there is no revenue: a product with no sales has
    -- no margin, and reporting 0% would drag down any average taken over this column.
    round(s.profit / nullif(s.revenue, 0) * 100, 2) as margin_pct,

    d.first_sold_date,
    d.last_sold_date,
    s.product_key is not null as has_sold

from {{ ref('dim_product') }} p
left join sales s on p.product_key = s.product_key
left join sold_dates d on p.product_key = d.product_key
