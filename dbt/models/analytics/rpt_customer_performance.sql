-- ANALYTICS: customer scorecard. Grain: one row per customer.
--
-- Includes ALL 15,266 customers, not only the 11,887 who have purchased, with a
-- `has_purchased` flag. This is deliberate: "average revenue per customer" means
-- two very different things depending on the denominator, and the flag forces
-- that choice to be explicit instead of accidental.

with sales as (

    select
        f.customer_key,
        count(distinct f.order_id)      as order_count,
        count(distinct f.product_key)   as distinct_products,
        count(distinct f.store_key)     as stores_used,
        sum(f.quantity)                 as units_bought,
        sum(f.gross_sales)              as revenue,
        sum(f.gross_cost)               as cost,
        sum(f.gross_margin)             as profit,
        min(d.full_date)                as first_order_date,
        max(d.full_date)                as last_order_date
    from {{ ref('fact_sales') }} f
    join {{ ref('dim_date') }} d on f.date_key = d.date_key
    group by f.customer_key

)

select
    c.customer_key,
    c.customer_name,
    c.gender,
    c.city,
    c.state,
    c.country,
    c.continent,
    c.birthday,

    coalesce(s.order_count, 0)          as order_count,
    coalesce(s.distinct_products, 0)    as distinct_products,
    coalesce(s.stores_used, 0)          as stores_used,
    coalesce(s.units_bought, 0)         as units_bought,
    coalesce(s.revenue, 0)              as revenue,
    coalesce(s.cost, 0)                 as cost,
    coalesce(s.profit, 0)               as profit,

    round(s.revenue / nullif(s.order_count, 0), 2) as avg_order_value,

    s.first_order_date,
    s.last_order_date,
    date_diff('day', s.first_order_date, s.last_order_date) as active_days,

    s.customer_key is not null      as has_purchased,
    coalesce(s.order_count, 0) > 1  as is_repeat_customer

from {{ ref('dim_customer') }} c
left join sales s on c.customer_key = s.customer_key
