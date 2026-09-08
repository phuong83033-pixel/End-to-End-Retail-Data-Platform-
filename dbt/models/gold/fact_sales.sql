-- GOLD: sales fact. Grain: ONE ORDER LINE (one product bought within one order).
--
-- KNOWN LIMITATION: the source `sales` table records no price at transaction
-- time, so the measures below are built from the product's CURRENT unit price
-- and cost in dim_product. Revenue and margin are therefore an approximation at
-- a fixed price point, not restated history. All amounts are USD - the source
-- carries no currency column and no exchange-rate table.
--
-- The join to products is a LEFT join on purpose: profiling found zero orphan
-- product keys, and a left join + not_null test makes any future orphan fail
-- loudly instead of silently dropping revenue from the fact.

with sales as (

    select * from {{ ref('stg_sales') }}

),

products as (

    select
        product_key,
        unit_price,
        unit_cost
    from {{ ref('stg_products') }}

),

joined as (

    select
        {{ dbt_utils.generate_surrogate_key(['sales.order_number', 'sales.line_item']) }} as sales_key,

        sales.order_number  as order_id,
        sales.line_item,

        -- date_key / delivery_date_key are role-playing FKs into dim_date
        cast(strftime(sales.order_date, '%Y%m%d') as integer) as date_key,
        case
            when sales.delivery_date is not null
                then cast(strftime(sales.delivery_date, '%Y%m%d') as integer)
        end as delivery_date_key,

        sales.customer_key,
        sales.product_key,
        sales.store_key,

        sales.quantity,
        products.unit_price,
        products.unit_cost,

        -- Exact decimal arithmetic: quantity is an integer and the unit amounts
        -- are decimal(10,2), so no rounding is introduced and
        -- gross_margin = gross_sales - gross_cost holds to the cent.
        sales.quantity * products.unit_price                            as gross_sales,
        sales.quantity * products.unit_cost                             as gross_cost,
        sales.quantity * products.unit_price
            - sales.quantity * products.unit_cost                       as gross_margin

    from sales
    left join products on sales.product_key = products.product_key

)

select * from joined
