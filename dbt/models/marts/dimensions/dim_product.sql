-- GOLD: product dimension. Grain: one row per product.
-- The category > subcategory > product hierarchy is kept denormalised inside
-- this dimension (V1 decision) so analytics stays a single join away.

select
    product_key,
    product_name,
    brand,
    color,
    unit_cost,
    unit_price,
    unit_price - unit_cost as unit_margin,
    subcategory_key,
    subcategory,
    category_key,
    category

from {{ ref('stg_products') }}
