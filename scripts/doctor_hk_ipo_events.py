"""Read-only doctor for curated HK IPO event tables in BigQuery."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

from alpha_harness.data.loader_factory import resolve_market_data_location
from alpha_harness.markets import load_market_pack

PROJECT, DATASET = resolve_market_data_location(load_market_pack("hk_ipo"))


@dataclass
class Check:
    name: str
    sql: str
    hard_fail_on_rows: bool = False


def _run_bq(sql: str) -> list[dict[str, object]]:
    cmd = [
        "bq",
        "--project_id",
        PROJECT,
        "query",
        "--use_legacy_sql=false",
        "--format=json",
        sql,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    text = proc.stdout.strip()
    return json.loads(text) if text else []


CHECKS = [
    Check(
        "event_table_counts",
        f"""
        SELECT
          'hkex_document_registry_curated' AS table_name,
          COUNT(*) AS row_count,
          COUNT(DISTINCT stock_code) AS stocks
        FROM `{PROJECT}.{DATASET}.hkex_document_registry_curated`
        UNION ALL
        SELECT 'ipo_event_terms_curated', COUNT(*), COUNT(DISTINCT stock_code)
        FROM `{PROJECT}.{DATASET}.ipo_event_terms_curated`
        UNION ALL
        SELECT 'ipo_event_terms_needs_review', COUNT(*), COUNT(DISTINCT stock_code)
        FROM `{PROJECT}.{DATASET}.ipo_event_terms_needs_review`
        UNION ALL
        SELECT 'ipo_event_dates_curated', COUNT(*), COUNT(DISTINCT stock_code)
        FROM `{PROJECT}.{DATASET}.ipo_event_dates_curated`
        UNION ALL
        SELECT 'ipo_event_features_daily', COUNT(*), COUNT(DISTINCT stock_code)
        FROM `{PROJECT}.{DATASET}.ipo_event_features_daily`
        """,
    ),
    Check(
        "event_type_coverage",
        f"""
        SELECT event_type, COUNT(*) AS event_dates, COUNT(DISTINCT stock_code) AS stocks
        FROM `{PROJECT}.{DATASET}.ipo_event_dates_curated`
        GROUP BY event_type
        ORDER BY event_type
        """,
    ),
    Check(
        "missing_source_evidence",
        f"""
        SELECT
          stock_code,
          event_type,
          event_date,
          primary_source_doc_id,
          primary_source_url
        FROM `{PROJECT}.{DATASET}.ipo_event_dates_curated`
        WHERE primary_source_doc_id IS NULL
          OR primary_source_url IS NULL
          OR primary_source_text IS NULL
          OR primary_source_text = ''
        ORDER BY stock_code, event_type, event_date
        """,
        hard_fail_on_rows=True,
    ),
    Check(
        "event_feature_daily_alignment",
        f"""
        WITH p AS (
          SELECT stock_code, date AS trading_date FROM `{PROJECT}.{DATASET}.ipo_daily_prices`
        ),
        f AS (
          SELECT stock_code, date AS trading_date
          FROM `{PROJECT}.{DATASET}.ipo_event_features_daily`
        )
        SELECT 'missing_event_feature_key' AS issue, COUNT(*) AS issue_count
        FROM p LEFT JOIN f USING (stock_code, trading_date)
        WHERE f.stock_code IS NULL
        UNION ALL
        SELECT 'extra_event_feature_key', COUNT(*)
        FROM f LEFT JOIN p USING (stock_code, trading_date)
        WHERE p.stock_code IS NULL
        """,
    ),
    Check(
        "known_bloomberg_anomaly_recheck",
        f"""
        SELECT *
        FROM `{PROJECT}.{DATASET}.bbg_lockup_greenshoe_recheck_staging`
        WHERE stock_code IN ('06051', '03636')
        ORDER BY stock_code
        """,
    ),
]


def main() -> int:
    hard_errors = 0
    for check in CHECKS:
        print(f"\n== {check.name} ==")
        try:
            rows = _run_bq(check.sql)
        except Exception as exc:
            hard_errors += 1
            print(f"ERROR: {exc}")
            continue
        if not rows:
            print("ok: no rows")
        else:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            if check.hard_fail_on_rows:
                hard_errors += 1
    return 1 if hard_errors else 0


if __name__ == "__main__":
    sys.exit(main())
