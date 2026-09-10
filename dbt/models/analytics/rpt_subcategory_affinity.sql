-- ANALYTICS: "what sells together", at subcategory level. Grain: one row per
-- directional subcategory pair.
--
-- WHY THIS EXISTS ALONGSIDE rpt_product_affinity: product-level basket signal on
-- this dataset is effectively nil. The strongest product pair co-occurs in just
-- 5 orders, and 91% of surviving pairs co-occur exactly twice - noise, not
-- pattern. That is a consequence of 2,492 products spread across 17,121
-- multi-item baskets averaging 2.4 items.
--
-- Rolling up to the 32 subcategories concentrates the same baskets into far
-- fewer cells: the top pair here co-occurs 1,548 times. This is the model to use
-- for "which things are usually bought together"; the product-level one is kept
-- for drill-down and for its item-item similarity value, with its weakness
-- documented rather than hidden.
--
-- Same construction as rpt_product_affinity: distinct items per order, multi-item
-- baskets only, directional pairs.

with basket_subcategories as (

    select distinct
        f.order_id,
        p.subcategory_key,
        p.subcategory,
        p.category
    from {{ ref('fact_sales') }} f
    join {{ ref('dim_product') }} p on f.product_key = p.product_key

),

multi_item_baskets as (

    select order_id
    from basket_subcategories
    group by order_id
    having count(*) >= 2

),

basket_items as (

    select b.*
    from basket_subcategories b
    join multi_item_baskets m on b.order_id = m.order_id

),

basket_total as (

    select count(*) as n_baskets from multi_item_baskets

),

item_counts as (

    select
        subcategory_key,
        count(distinct order_id) as order_count
    from basket_items
    group by subcategory_key

),

pairs as (

    select
        a.subcategory_key           as antecedent_subcategory_key,
        any_value(a.subcategory)    as antecedent_subcategory,
        any_value(a.category)       as antecedent_category,
        b.subcategory_key           as consequent_subcategory_key,
        any_value(b.subcategory)    as consequent_subcategory,
        any_value(b.category)       as consequent_category,
        count(distinct a.order_id)  as pair_order_count
    from basket_items a
    join basket_items b
      on a.order_id = b.order_id
     and a.subcategory_key <> b.subcategory_key
    group by a.subcategory_key, b.subcategory_key

)

select
    p.antecedent_subcategory_key,
    p.antecedent_subcategory,
    p.antecedent_category,

    p.consequent_subcategory_key,
    p.consequent_subcategory,
    p.consequent_category,

    p.pair_order_count,
    ia.order_count as antecedent_order_count,
    ic.order_count as consequent_order_count,

    round(p.pair_order_count::double / t.n_baskets, 6)      as support,
    round(p.pair_order_count::double / ia.order_count, 4)   as confidence,
    round(
        (p.pair_order_count::double / ia.order_count)
        / (ic.order_count::double / t.n_baskets)
    , 4) as lift

from pairs p
join item_counts ia on p.antecedent_subcategory_key = ia.subcategory_key
join item_counts ic on p.consequent_subcategory_key = ic.subcategory_key
cross join basket_total t
