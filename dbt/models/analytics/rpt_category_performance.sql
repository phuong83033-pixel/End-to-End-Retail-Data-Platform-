-- ANALYTICS: category scorecard. Grain: one row per (category, subcategory).
--
-- Kept at subcategory grain with the parent category alongside, so it rolls up
-- to the 8 categories with a GROUP BY instead of needing a second model.

with sales as (

    select
        p.category_key,
        p.category,
        p.subcategory_key,
        p.subcategory,
        count(distinct f.order_id)      as order_count,
        count(distinct f.customer_key)  as customer_count,
        count(distinct f.product_key)   as products_sold,
        sum(f.quantity)                 as units_sold,
        sum(f.gross_sales)              as revenue,
        sum(f.gross_cost)               as cost,
        sum(f.gross_margin)             as profit
    from {{ ref('fact_sales') }} f
    join {{ ref('dim_product') }} p on f.product_key = p.product_key
    group by 1, 2, 3, 4

),

catalogue as (

    -- Full product counts including never-sold products, so "products_sold" can
    -- be compared against what the catalogue actually offers.
    select
        category_key,
        subcategory_key,
        count(*) as products_in_catalogue
    from {{ ref('dim_product') }}
    group by 1, 2

),

total as (

    select sum(gross_sales) as total_revenue from {{ ref('fact_sales') }}

)

select
    s.category_key,
    s.category,
    s.subcategory_key,
    s.subcategory,

    c.products_in_catalogue,
    s.products_sold,
    s.order_count,
    s.customer_count,
    s.units_sold,
    s.revenue,
    s.cost,
    s.profit,

    round(s.profit / nullif(s.revenue, 0) * 100, 2)          as margin_pct,
    round(s.revenue / nullif(t.total_revenue, 0) * 100, 2)   as revenue_share_pct

from sales s
join catalogue c
  on s.category_key = c.category_key
 and s.subcategory_key = c.subcategory_key
cross join total t
