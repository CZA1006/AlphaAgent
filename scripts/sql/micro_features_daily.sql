-- Per-(stock, trading_date) microstructure features from the HK IPO tick lake.
--
-- Source: hk_ipo_research.tick_events_ext (BID/ASK/TRADE events, ~176M rows).
-- Output: hk_ipo_research.micro_features_daily — one compact row per stock-day
--         that the harness loads as extra panel columns (DSL fields).
--
-- Quotes arrive as separate BID and ASK events, so best bid/ask are
-- reconstructed by forward-fill (LAST_VALUE ... IGNORE NULLS).  Trade
-- direction uses Lee-Ready: price vs prevailing mid, tick-test fallback
-- when at/through the mid or quotes are missing.  Realized vol is computed
-- on 1-minute sampled last-trade prices to avoid bid-ask-bounce inflation.
--
-- Re-runnable: CREATE OR REPLACE.  One external-parquet scan (~1.6 GB).

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.micro_features_daily` AS
WITH ev AS (
  SELECT stock_code, trading_date, time, event_type, value AS price, size
  FROM `bloomberg-database-0629.hk_ipo_research.tick_events_ext`
  WHERE value > 0
),
q AS (
  SELECT *,
    LAST_VALUE(IF(event_type = 'BID', price, NULL) IGNORE NULLS) OVER w AS bid,
    LAST_VALUE(IF(event_type = 'ASK', price, NULL) IGNORE NULLS) OVER w AS ask
  FROM ev
  WINDOW w AS (
    PARTITION BY stock_code, trading_date ORDER BY time
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  )
),
trades AS (
  SELECT
    stock_code, trading_date, time, price, size, bid, ask,
    (bid + ask) / 2 AS mid,
    price - LAG(price) OVER (
      PARTITION BY stock_code, trading_date ORDER BY time
    ) AS dpx
  FROM q
  WHERE event_type = 'TRADE'
),
trade_feat AS (
  SELECT
    stock_code, trading_date,
    COUNT(*)        AS n_trades,
    SUM(size)       AS tick_volume,
    AVG(size)       AS avg_trade_size,
    -- Order-flow imbalance: signed volume / total volume, Lee-Ready.
    SAFE_DIVIDE(
      SUM(
        CASE
          WHEN bid IS NOT NULL AND ask IS NOT NULL AND ask > bid THEN
            CASE
              WHEN price > mid THEN size
              WHEN price < mid THEN -size
              ELSE SIGN(IFNULL(dpx, 0)) * size
            END
          ELSE SIGN(IFNULL(dpx, 0)) * size
        END
      ),
      SUM(size)
    ) AS ofi,
    -- Average relative bid-ask spread at trade times.
    AVG(
      CASE WHEN bid IS NOT NULL AND ask IS NOT NULL AND ask > bid
           THEN (ask - bid) / mid END
    ) AS rel_spread
  FROM trades
  GROUP BY stock_code, trading_date
),
minute_px AS (  -- last trade price within each minute
  SELECT
    stock_code, trading_date,
    TIMESTAMP_TRUNC(time, MINUTE) AS m,
    ARRAY_AGG(price ORDER BY time DESC LIMIT 1)[OFFSET(0)] AS px
  FROM trades
  GROUP BY stock_code, trading_date, m
),
minute_ret AS (
  SELECT
    stock_code, trading_date,
    POW(
      SAFE.LN(px / NULLIF(
        LAG(px) OVER (PARTITION BY stock_code, trading_date ORDER BY m), 0)),
      2
    ) AS r2
  FROM minute_px
),
rv AS (
  SELECT stock_code, trading_date, SQRT(SUM(r2)) AS realized_vol
  FROM minute_ret
  GROUP BY stock_code, trading_date
),
quote_feat AS (
  SELECT stock_code, trading_date, COUNT(*) AS n_quotes
  FROM ev
  WHERE event_type IN ('BID', 'ASK')
  GROUP BY stock_code, trading_date
)
SELECT
  t.stock_code,
  t.trading_date,
  t.n_trades,
  t.tick_volume,
  t.avg_trade_size,
  t.ofi,
  t.rel_spread,
  rv.realized_vol,
  q.n_quotes
FROM trade_feat t
LEFT JOIN rv         USING (stock_code, trading_date)
LEFT JOIN quote_feat q USING (stock_code, trading_date);
