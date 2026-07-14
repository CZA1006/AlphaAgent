-- Candidate intraday features for the HK IPO panel, frozen through {{END_DATE}}.
-- This template is dry-run by the autonomous task. It is not an execution entrypoint.
-- The target is deliberately versioned and separate from micro_features_daily.

CREATE TABLE `{{PROJECT}}.{{DATASET}}.micro_features_intraday_v1_candidate`
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY),
  description = 'Operator-approved HK IPO intraday v1 candidate; auto-expires after 7 days'
)
AS
WITH calendar AS (
  SELECT DISTINCT stock_code, date AS trading_date
  FROM `{{PROJECT}}.{{DATASET}}.ipo_daily_prices`
  WHERE date <= DATE '{{END_DATE}}'
),
events AS (
  SELECT
    stock_code,
    trading_date,
    time,
    TIME(time, "Asia/Hong_Kong") AS session_time,
    event_type,
    value AS price,
    size
  FROM `{{PROJECT}}.{{DATASET}}.tick_events_ext`
  WHERE scope = 'target'
    AND value > 0
    AND trading_date <= DATE '{{END_DATE}}'
),
quoted AS (
  SELECT
    *,
    LAST_VALUE(IF(event_type = 'BID', price, NULL) IGNORE NULLS) OVER quote_window AS bid,
    LAST_VALUE(IF(event_type = 'ASK', price, NULL) IGNORE NULLS) OVER quote_window AS ask
  FROM events
  WINDOW quote_window AS (
    PARTITION BY stock_code, trading_date ORDER BY time
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  )
),
trades AS (
  SELECT
    *,
    (bid + ask) / 2 AS mid,
    price - LAG(price) OVER (
      PARTITION BY stock_code, trading_date ORDER BY time
    ) AS price_change
  FROM quoted
  WHERE event_type = 'TRADE'
),
signed_trades AS (
  SELECT
    *,
    CASE
      WHEN bid IS NOT NULL AND ask IS NOT NULL AND ask > bid THEN
        CASE
          WHEN price > mid THEN size
          WHEN price < mid THEN -size
          ELSE SIGN(IFNULL(price_change, 0)) * size
        END
      ELSE SIGN(IFNULL(price_change, 0)) * size
    END AS signed_size
  FROM trades
),
daily_trade AS (
  SELECT stock_code, trading_date, SUM(size) AS daily_tick_volume
  FROM signed_trades
  GROUP BY stock_code, trading_date
),
auction_trade AS (
  SELECT stock_code, trading_date, SUM(size) AS opening_auction_tick_volume
  FROM signed_trades
  WHERE session_time >= TIME '09:00:00' AND session_time < TIME '09:30:00'
  GROUP BY stock_code, trading_date
),
first_hour_trade AS (
  SELECT
    stock_code,
    trading_date,
    COUNT(*) AS first_hour_n_trades,
    SUM(size) AS first_hour_tick_volume,
    SAFE_DIVIDE(SUM(signed_size), SUM(size)) AS first_hour_ofi
  FROM signed_trades
  WHERE session_time >= TIME '09:30:00' AND session_time < TIME '10:30:00'
  GROUP BY stock_code, trading_date
),
first_hour_quote AS (
  SELECT
    stock_code,
    trading_date,
    COUNTIF(event_type IN ('BID', 'ASK')) AS first_hour_n_quotes,
    AVG(
      IF(
        event_type IN ('BID', 'ASK') AND bid IS NOT NULL AND ask IS NOT NULL AND ask > bid,
        SAFE_DIVIDE(ask - bid, (bid + ask) / 2),
        NULL
      )
    ) AS first_hour_rel_spread
  FROM quoted
  WHERE session_time >= TIME '09:30:00' AND session_time < TIME '10:30:00'
  GROUP BY stock_code, trading_date
),
first_hour_minute_price AS (
  SELECT
    stock_code,
    trading_date,
    TIMESTAMP_TRUNC(time, MINUTE) AS minute_utc,
    ARRAY_AGG(price ORDER BY time DESC LIMIT 1)[OFFSET(0)] AS price
  FROM signed_trades
  WHERE session_time >= TIME '09:30:00' AND session_time < TIME '10:30:00'
  GROUP BY stock_code, trading_date, minute_utc
),
first_hour_minute_return AS (
  SELECT
    stock_code,
    trading_date,
    POW(
      SAFE.LN(
        price / NULLIF(
          LAG(price) OVER (PARTITION BY stock_code, trading_date ORDER BY minute_utc),
          0
        )
      ),
      2
    ) AS squared_return
  FROM first_hour_minute_price
),
first_hour_volatility AS (
  SELECT stock_code, trading_date, SQRT(SUM(squared_return)) AS first_hour_realized_vol
  FROM first_hour_minute_return
  GROUP BY stock_code, trading_date
),
daily_features AS (
  SELECT
    calendar.stock_code,
    calendar.trading_date,
    first_hour_trade.first_hour_n_trades,
    first_hour_trade.first_hour_tick_volume,
    first_hour_trade.first_hour_ofi,
    first_hour_quote.first_hour_rel_spread,
    first_hour_volatility.first_hour_realized_vol,
    first_hour_quote.first_hour_n_quotes,
    SAFE_DIVIDE(
      auction_trade.opening_auction_tick_volume,
      daily_trade.daily_tick_volume
    ) AS opening_auction_trade_share
  FROM calendar
  LEFT JOIN daily_trade USING (stock_code, trading_date)
  LEFT JOIN auction_trade USING (stock_code, trading_date)
  LEFT JOIN first_hour_trade USING (stock_code, trading_date)
  LEFT JOIN first_hour_quote USING (stock_code, trading_date)
  LEFT JOIN first_hour_volatility USING (stock_code, trading_date)
),
point_in_time_baselines AS (
  SELECT
    *,
    AVG(first_hour_rel_spread) OVER trailing_window AS prior_20d_first_hour_rel_spread,
    AVG(first_hour_tick_volume) OVER trailing_window AS prior_20d_first_hour_tick_volume
  FROM daily_features
  WINDOW trailing_window AS (
    PARTITION BY stock_code ORDER BY trading_date
    ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
  )
)
SELECT
  *,
  SAFE_DIVIDE(first_hour_rel_spread, prior_20d_first_hour_rel_spread) - 1
    AS first_hour_spread_shock,
  1 - SAFE_DIVIDE(first_hour_tick_volume, prior_20d_first_hour_tick_volume)
    AS first_hour_liquidity_withdrawal
FROM point_in_time_baselines
