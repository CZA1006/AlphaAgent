-- Daily HK IPO event features aligned exactly to ipo_daily_prices.
--
-- One output row per (stock_code, date) in ipo_daily_prices.
-- Event dates come from ipo_event_dates_curated, not Bloomberg-only fields.

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.ipo_event_features_daily` AS
WITH calendar AS (
  SELECT DISTINCT stock_code, date
  FROM `bloomberg-database-0629.hk_ipo_research.ipo_daily_prices`
),
listing AS (
  SELECT stock_code, MIN(listing_date) AS listing_date
  FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated`
  WHERE listing_date IS NOT NULL
  GROUP BY stock_code
),
pricing AS (
  SELECT stock_code, MIN(event_date) AS pricing_date
  FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated`
  WHERE event_type = 'pricing_date'
  GROUP BY stock_code
),
cornerstone AS (
  SELECT
    stock_code,
    event_date,
    total_shares,
    total_pct_of_offer,
    total_pct_of_share_capital
  FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated`
  WHERE event_type = 'cornerstone_lockup_expiry'
),
cornerstone_next AS (
  SELECT c.stock_code, c.date, MIN(e.event_date) AS next_date
  FROM calendar c
  JOIN cornerstone e
    ON c.stock_code = e.stock_code
    AND e.event_date >= c.date
  GROUP BY c.stock_code, c.date
),
cornerstone_prev AS (
  SELECT c.stock_code, c.date, MAX(e.event_date) AS prev_date
  FROM calendar c
  JOIN cornerstone e
    ON c.stock_code = e.stock_code
    AND e.event_date <= c.date
  GROUP BY c.stock_code, c.date
),
greenshoe_expiry_next AS (
  SELECT c.stock_code, c.date, MIN(e.event_date) AS next_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type = 'greenshoe_expiry'
    AND e.event_date >= c.date
  GROUP BY c.stock_code, c.date
),
greenshoe_expiry_prev AS (
  SELECT c.stock_code, c.date, MAX(e.event_date) AS prev_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type = 'greenshoe_expiry'
    AND e.event_date <= c.date
  GROUP BY c.stock_code, c.date
),
greenshoe_exercise_next AS (
  SELECT c.stock_code, c.date, MIN(e.event_date) AS next_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type IN ('greenshoe_full_exercise', 'greenshoe_partial_exercise')
    AND e.event_date >= c.date
  GROUP BY c.stock_code, c.date
),
greenshoe_exercise_prev AS (
  SELECT c.stock_code, c.date, MAX(e.event_date) AS prev_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type IN ('greenshoe_full_exercise', 'greenshoe_partial_exercise')
    AND e.event_date <= c.date
  GROUP BY c.stock_code, c.date
),
stabilization_end_next AS (
  SELECT c.stock_code, c.date, MIN(e.event_date) AS next_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type = 'stabilization_end'
    AND e.event_date >= c.date
  GROUP BY c.stock_code, c.date
),
stabilization_end_prev AS (
  SELECT c.stock_code, c.date, MAX(e.event_date) AS prev_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type = 'stabilization_end'
    AND e.event_date <= c.date
  GROUP BY c.stock_code, c.date
),
stabilization_start_prev AS (
  SELECT c.stock_code, c.date, MAX(e.event_date) AS prev_date
  FROM calendar c
  JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` e
    ON c.stock_code = e.stock_code
    AND e.event_type = 'stabilization_start'
    AND e.event_date <= c.date
  GROUP BY c.stock_code, c.date
)
SELECT
  c.stock_code,
  c.date,
  DATE_DIFF(cn.next_date, c.date, DAY) AS days_to_next_cornerstone_lockup,
  DATE_DIFF(c.date, cp.prev_date, DAY) AS days_since_prev_cornerstone_lockup,
  co.total_shares AS next_cornerstone_unlock_shares,
  co.total_pct_of_offer AS next_cornerstone_unlock_pct_offer,
  co.total_pct_of_share_capital AS next_cornerstone_unlock_pct_cap,
  DATE_DIFF(gen.next_date, c.date, DAY) AS days_to_next_greenshoe_expiry,
  DATE_DIFF(c.date, gep.prev_date, DAY) AS days_since_prev_greenshoe_expiry,
  DATE_DIFF(gxn.next_date, c.date, DAY) AS days_to_next_greenshoe_exercise,
  DATE_DIFF(c.date, gxp.prev_date, DAY) AS days_since_prev_greenshoe_exercise,
  DATE_DIFF(sen.next_date, c.date, DAY) AS days_to_next_stabilization_end,
  DATE_DIFF(c.date, sep.prev_date, DAY) AS days_since_prev_stabilization_end,
  DATE_DIFF(c.date, ssp.prev_date, DAY) AS days_since_prev_stabilization_start,
  DATE_DIFF(c.date, l.listing_date, DAY) AS days_since_listing,
  DATE_DIFF(c.date, p.pricing_date, DAY) AS days_since_pricing,
  IF(DATE_DIFF(cn.next_date, c.date, DAY) BETWEEN 0 AND 5, 1, 0)
    AS is_pre_cornerstone_lockup_5d,
  IF(
    ABS(DATE_DIFF(cn.next_date, c.date, DAY)) <= 5
      OR ABS(DATE_DIFF(c.date, cp.prev_date, DAY)) <= 5,
    1,
    0
  ) AS is_near_cornerstone_lockup_5d,
  IF(DATE_DIFF(gen.next_date, c.date, DAY) BETWEEN 0 AND 5, 1, 0)
    AS is_pre_greenshoe_expiry_5d,
  IF(
    ABS(DATE_DIFF(gen.next_date, c.date, DAY)) <= 5
      OR ABS(DATE_DIFF(c.date, gep.prev_date, DAY)) <= 5,
    1,
    0
  ) AS is_near_greenshoe_expiry_5d,
  IF(
    ABS(DATE_DIFF(gxn.next_date, c.date, DAY)) <= 5
      OR ABS(DATE_DIFF(c.date, gxp.prev_date, DAY)) <= 5,
    1,
    0
  ) AS is_near_greenshoe_exercise_5d,
  IF(DATE_DIFF(sen.next_date, c.date, DAY) BETWEEN 0 AND 5, 1, 0)
    AS is_pre_stabilization_end_5d,
  IF(
    ABS(DATE_DIFF(sen.next_date, c.date, DAY)) <= 5
      OR ABS(DATE_DIFF(c.date, sep.prev_date, DAY)) <= 5,
    1,
    0
  ) AS is_near_stabilization_end_5d,
  IF(ssp.prev_date IS NOT NULL AND (sen.next_date IS NULL OR c.date <= sen.next_date), 1, 0)
    AS is_stabilization_window_active
FROM calendar c
LEFT JOIN listing l USING (stock_code)
LEFT JOIN pricing p USING (stock_code)
LEFT JOIN cornerstone_next cn USING (stock_code, date)
LEFT JOIN cornerstone_prev cp USING (stock_code, date)
LEFT JOIN greenshoe_expiry_next gen USING (stock_code, date)
LEFT JOIN greenshoe_expiry_prev gep USING (stock_code, date)
LEFT JOIN greenshoe_exercise_next gxn USING (stock_code, date)
LEFT JOIN greenshoe_exercise_prev gxp USING (stock_code, date)
LEFT JOIN stabilization_end_next sen USING (stock_code, date)
LEFT JOIN stabilization_end_prev sep USING (stock_code, date)
LEFT JOIN stabilization_start_prev ssp USING (stock_code, date)
LEFT JOIN cornerstone co
  ON cn.stock_code = co.stock_code AND cn.next_date = co.event_date;
