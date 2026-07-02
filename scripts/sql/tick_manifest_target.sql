-- Manifest for the HK IPO target tick universe.
--
-- This deliberately summarizes only scope='target'.  The legacy/wider
-- tick_manifest can include non-target capture and should not be used as
-- the research universe contract.

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.tick_manifest_target` AS
SELECT
  scope,
  stock_code,
  MIN(trading_date) AS first_date,
  MAX(trading_date) AS last_date,
  COUNT(DISTINCT trading_date) AS trading_days,
  COUNT(*) AS total_rows,
  COUNTIF(event_type = 'TRADE') AS trade_rows,
  COUNTIF(event_type = 'BID') AS bid_rows,
  COUNTIF(event_type = 'ASK') AS ask_rows,
  COUNTIF(event_type NOT IN ('TRADE', 'BID', 'ASK')) AS other_rows,
  COUNTIF(value <= 0 OR value IS NULL) AS nonpositive_value_rows,
  COUNTIF(size < 0) AS invalid_size_rows,
  MIN(time) AS first_event_utc,
  MAX(time) AS last_event_utc,
  CURRENT_TIMESTAMP() AS built_at_utc
FROM `bloomberg-database-0629.hk_ipo_research.tick_events_ext`
WHERE scope = 'target'
GROUP BY scope, stock_code;
