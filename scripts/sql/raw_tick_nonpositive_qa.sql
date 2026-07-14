-- Read-only QA grouped at the grain required before intraday feature materialization.
SELECT
  stock_code,
  trading_date,
  event_type,
  COUNT(*) AS nonpositive_value_rows
FROM `{{PROJECT}}.{{DATASET}}.tick_events_ext`
WHERE scope = 'target'
  AND trading_date <= DATE '{{END_DATE}}'
  AND (value <= 0 OR value IS NULL)
GROUP BY stock_code, trading_date, event_type
ORDER BY nonpositive_value_rows DESC, stock_code, trading_date, event_type
