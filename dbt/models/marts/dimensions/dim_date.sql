-- GOLD: date dimension. Grain: one row per calendar day.
-- Generated, NOT derived from sales: the source has orders on only 1,641 of the
-- ~1,878 days it spans, so a date_key built from the fact would leave holes in
-- any time series. Range 2015-01-01 .. 2022-12-31 brackets the observed sales
-- window (2016-01-01 .. 2021-02-20) with headroom on both sides.

with date_spine as (

    select cast(range as date) as full_date
    from range(date '2015-01-01', date '2023-01-01', interval 1 day)

)

select
    cast(strftime(full_date, '%Y%m%d') as integer)  as date_key,
    full_date,
    extract(day from full_date)                     as "day",
    extract(month from full_date)                   as "month",
    strftime(full_date, '%B')                       as month_name,
    extract(quarter from full_date)                 as "quarter",
    extract(year from full_date)                    as "year",
    extract(week from full_date)                    as "week",
    extract(isodow from full_date)                  as day_of_week,
    strftime(full_date, '%A')                       as day_name,
    extract(isodow from full_date) >= 6             as is_weekend

from date_spine
