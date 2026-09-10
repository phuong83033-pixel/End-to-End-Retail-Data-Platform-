-- ANALYTICS: "products bought together". Grain: one row per ordered product pair
-- (antecedent -> consequent). Each unordered pair appears twice, once in each
-- direction, so "what goes with product X?" is a simple filter on
-- antecedent_product_key rather than an OR across two columns.
--
-- This is association-rule mining expressed as SQL - no mining library needed.
-- It also doubles as an item-item similarity table if a recommender is ever built.
--
-- Two deliberate choices, both driven by profiling:
--   1. Products are deduplicated per order first. 75 (order, product) pairs span
--      more than one line of the same order; without the DISTINCT they would
--      inflate co-occurrence counts.
--   2. Support, confidence and lift are computed over MULTI-ITEM baskets only
--      (17,121 of 26,326 orders). The 9,205 single-item orders cannot express a
--      pairing, so including them would deflate every support figure by 35%.
--
-- Pairs seen only once are excluded: with a median product appearing in just 15
-- orders, a single co-occurrence is noise, and keeping them would bury the real
-- signal under tens of thousands of coincidences.

with basket_products as (

    select distinct order_id, product_key
    from {{ ref('fact_sales') }}

),

multi_item_baskets as (

    select order_id
    from basket_products
    group by order_id
    having count(*) >= 2

),

basket_items as (

    select bp.order_id, bp.product_key
    from basket_products bp
    join multi_item_baskets m on bp.order_id = m.order_id

),

basket_total as (

    select count(*) as n_baskets from multi_item_baskets

),

item_counts as (

    select
        product_key,
        count(distinct order_id) as order_count
    from basket_items
    group by product_key

),

pairs as (

    select
        a.product_key                as antecedent_product_key,
        b.product_key                as consequent_product_key,
        count(distinct a.order_id)   as pair_order_count
    from basket_items a
    join basket_items b
      on a.order_id = b.order_id
     and a.product_key <> b.product_key
    group by 1, 2
    having count(distinct a.order_id) >= 2

)

select
    p.antecedent_product_key,
    pa.product_name     as antecedent_product_name,
    pa.category         as antecedent_category,

    p.consequent_product_key,
    pc.product_name     as consequent_product_name,
    pc.category         as consequent_category,

    p.pair_order_count,
    ia.order_count      as antecedent_order_count,
    ic.order_count      as consequent_order_count,

    -- P(A and B): how often the pair appears among all multi-item baskets
    round(p.pair_order_count::double / t.n_baskets, 6) as support,

    -- P(B | A): of the orders containing A, how many also contain B
    round(p.pair_order_count::double / ia.order_count, 4) as confidence,

    -- confidence / P(B). Above 1 means A genuinely raises the odds of B;
    -- around 1 means the pairing is no better than B's overall popularity.
    round(
        (p.pair_order_count::double / ia.order_count)
        / (ic.order_count::double / t.n_baskets)
    , 4) as lift

from pairs p
join item_counts ia on p.antecedent_product_key = ia.product_key
join item_counts ic on p.consequent_product_key = ic.product_key
join {{ ref('dim_product') }} pa on p.antecedent_product_key = pa.product_key
join {{ ref('dim_product') }} pc on p.consequent_product_key = pc.product_key
cross join basket_total t
