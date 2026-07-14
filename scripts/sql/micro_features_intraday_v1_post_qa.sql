-- Read-only acceptance checks after an operator-approved candidate materialization.
WITH duplicate_keys AS (
  SELECT stock_code, trading_date, COUNT(*) AS row_count
  FROM `{{PROJECT}}.{{DATASET}}.micro_features_intraday_v1_candidate`
  GROUP BY stock_code, trading_date
  HAVING COUNT(*) > 1
)
SELECT
  COUNT(*) AS row_count,
  COUNT(DISTINCT stock_code) AS stock_count,
  MIN(trading_date) AS min_date,
  MAX(trading_date) AS max_date,
  COUNTIF(trading_date > DATE '{{END_DATE}}') AS post_end_date_rows,
  (SELECT IFNULL(SUM(row_count - 1), 0) FROM duplicate_keys) AS duplicate_key_rows,
  COUNTIF(
    first_hour_n_trades IS NULL
    AND first_hour_tick_volume IS NULL
    AND first_hour_ofi IS NULL
    AND first_hour_rel_spread IS NULL
    AND first_hour_realized_vol IS NULL
    AND first_hour_n_quotes IS NULL
  ) AS no_first_hour_feature_rows
FROM `{{PROJECT}}.{{DATASET}}.micro_features_intraday_v1_candidate`
