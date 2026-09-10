-- Every analytics mart aggregates the same fact table, so every mart's total
-- revenue must equal fact_sales' total revenue. This is the guard against the
-- classic mart bug: a join that fans out and quietly double-counts, or one that
-- drops rows and quietly under-counts. Either way the mart still looks
-- plausible on its own - only reconciliation catches it.
--
-- Customer and store marts are LEFT JOINed from their dimension, so they also
-- prove no revenue is stranded against a key the dimension does not hold.
--
-- Tolerance is 0.01 to absorb decimal accumulation, not to hide real drift.

with expected as (

    select sum(gross_sales) as revenue from {{ ref('fact_sales') }}

),

actual as (

    select 'rpt_product_performance'  as mart, sum(revenue) as revenue from {{ ref('rpt_product_performance') }}
    union all
    select 'rpt_category_performance', sum(revenue) from {{ ref('rpt_category_performance') }}
    union all
    select 'rpt_store_performance',    sum(revenue) from {{ ref('rpt_store_performance') }}
    union all
    select 'rpt_customer_performance', sum(revenue) from {{ ref('rpt_customer_performance') }}
    union all
    select 'rpt_monthly_revenue',      sum(revenue) from {{ ref('rpt_monthly_revenue') }}

)

select
    a.mart,
    a.revenue        as mart_revenue,
    e.revenue        as fact_revenue,
    a.revenue - e.revenue as difference

from actual a
cross join expected e
where abs(a.revenue - e.revenue) > 0.01
