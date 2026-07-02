-- Curate HK IPO document-derived event terms from the latest refill run.
--
-- Inputs are staging tables written by the Bloomberg/HKEX refill agent.
-- Bloomberg lockup/greenshoe fields are review hints, not truth.

DECLARE latest_run_id STRING DEFAULT (
  SELECT run_id
  FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_refill_candidate_staging`
  GROUP BY run_id
  ORDER BY run_id DESC
  LIMIT 1
);

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.hkex_document_registry_curated` AS
SELECT *
FROM `bloomberg-database-0629.hk_ipo_research.hkex_document_registry_refill_staging`
WHERE run_id = latest_run_id;

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_needs_review` AS
SELECT
  *,
  CASE
    WHEN status IS NULL OR status != 'ok' THEN COALESCE(status, 'missing_status')
    WHEN event_date IS NULL THEN 'missing_event_date'
    WHEN source_url IS NULL OR source_url = '' THEN 'missing_source_url'
    WHEN source_doc_id IS NULL OR source_doc_id = '' THEN 'missing_source_doc_id'
    WHEN source_text IS NULL OR source_text = '' THEN 'missing_source_text'
    WHEN confidence IS NOT NULL AND confidence < 0.5 THEN 'low_confidence'
    ELSE 'needs_review'
  END AS review_reason,
  CURRENT_TIMESTAMP() AS curated_at_utc
FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_refill_candidate_staging`
WHERE run_id = latest_run_id
  AND (
    status IS NULL OR status != 'ok'
    OR event_date IS NULL
    OR source_url IS NULL OR source_url = ''
    OR source_doc_id IS NULL OR source_doc_id = ''
    OR source_text IS NULL OR source_text = ''
    OR (confidence IS NOT NULL AND confidence < 0.5)
  );

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_curated` AS
SELECT
  c.*,
  CASE
    WHEN c.source_priority = 'prospectus' THEN 1
    WHEN c.source_priority = 'allotment_results' THEN 2
    WHEN c.source_priority = 'hkex_announcement' THEN 3
    ELSE 9
  END AS source_priority_rank,
  CURRENT_TIMESTAMP() AS curated_at_utc
FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_refill_candidate_staging` c
LEFT JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_needs_review` r
  ON c.run_id = r.run_id
  AND c.stock_code = r.stock_code
  AND c.event_type = r.event_type
  AND COALESCE(CAST(c.event_date AS STRING), '') = COALESCE(CAST(r.event_date AS STRING), '')
  AND COALESCE(c.source_doc_id, '') = COALESCE(r.source_doc_id, '')
WHERE c.run_id = latest_run_id
  AND r.stock_code IS NULL;

CREATE OR REPLACE TABLE `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated` AS
WITH ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY stock_code, event_type, event_date
      ORDER BY source_priority_rank, confidence DESC, source_doc_id
    ) AS rn
  FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_terms_curated`
  WHERE event_date IS NOT NULL
),
listing AS (
  SELECT stock_code, MIN(event_date) AS listing_date
  FROM ranked
  WHERE event_type = 'listing_date'
  GROUP BY stock_code
),
agg AS (
  SELECT
    TO_HEX(MD5(CONCAT(stock_code, '|', event_type, '|', CAST(event_date AS STRING))))
      AS event_id,
    stock_code,
    CAST(NULL AS STRING) AS english_name,
    CAST(NULL AS STRING) AS chinese_name,
    event_type,
    event_date,
    COUNT(*) AS term_count,
    COUNT(DISTINCT party_name) AS party_count,
    SUM(shares) AS total_shares,
    SUM(pct_of_offer) AS total_pct_of_offer,
    SUM(pct_of_share_capital) AS total_pct_of_share_capital,
    MAX(lockup_months) AS max_lockup_months,
    AVG(confidence) AS avg_confidence,
    MAX(confidence) AS max_confidence
  FROM ranked
  GROUP BY stock_code, event_type, event_date
)
SELECT
  a.event_id,
  a.stock_code,
  a.english_name,
  a.chinese_name,
  l.listing_date,
  a.event_type,
  a.event_date,
  DATE_DIFF(a.event_date, l.listing_date, DAY) AS days_from_listing,
  a.term_count,
  a.party_count,
  a.total_shares,
  a.total_pct_of_offer,
  a.total_pct_of_share_capital,
  a.max_lockup_months,
  a.avg_confidence,
  a.max_confidence,
  r.source_priority AS primary_source_priority,
  r.source_doc_id AS primary_source_doc_id,
  r.source_url AS primary_source_url,
  r.source_page AS primary_source_page,
  r.source_text AS primary_source_text,
  CURRENT_TIMESTAMP() AS curated_at_utc
FROM agg a
LEFT JOIN listing l USING (stock_code)
LEFT JOIN ranked r
  ON a.stock_code = r.stock_code
  AND a.event_type = r.event_type
  AND a.event_date = r.event_date
  AND r.rn = 1;
