"""Read-only doctor for HK IPO daily/tick/microstructure BigQuery tables."""

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
        "coverage_counts",
        f"""
        SELECT
          'ipo_daily_prices' AS table_name,
          COUNT(*) AS row_count,
          COUNT(DISTINCT stock_code) AS stocks
        FROM `{PROJECT}.{DATASET}.ipo_daily_prices`
        UNION ALL
        SELECT 'micro_features_daily', COUNT(*), COUNT(DISTINCT stock_code)
        FROM `{PROJECT}.{DATASET}.micro_features_daily`
        UNION ALL
        SELECT 'tick_manifest_target', SUM(total_rows), COUNT(DISTINCT stock_code)
        FROM `{PROJECT}.{DATASET}.tick_manifest_target`
        """,
    ),
    Check(
        "daily_micro_key_alignment",
        f"""
        WITH p AS (
          SELECT stock_code, date AS trading_date FROM `{PROJECT}.{DATASET}.ipo_daily_prices`
        ),
        m AS (
          SELECT stock_code, trading_date FROM `{PROJECT}.{DATASET}.micro_features_daily`
        )
        SELECT 'missing_micro_key' AS issue, COUNT(*) AS issue_count
        FROM p LEFT JOIN m USING (stock_code, trading_date)
        WHERE m.stock_code IS NULL
        UNION ALL
        SELECT 'extra_micro_key', COUNT(*)
        FROM m LEFT JOIN p USING (stock_code, trading_date)
        WHERE p.stock_code IS NULL
        """,
    ),
    Check(
        "target_tick_manifest_quality",
        f"""
        SELECT
          COUNT(*) AS stock_count,
          COUNTIF(total_rows = 0) AS zero_total_row_stocks,
          COUNTIF(trade_rows = 0) AS zero_trade_row_stocks,
          COUNTIF(bid_rows = 0) AS zero_bid_row_stocks,
          COUNTIF(ask_rows = 0) AS zero_ask_row_stocks,
          COUNTIF(nonpositive_value_rows > 0) AS stocks_with_nonpositive_value_rows,
          SUM(nonpositive_value_rows) AS nonpositive_value_rows,
          COUNTIF(invalid_size_rows > 0) AS stocks_with_invalid_size_rows,
          SUM(invalid_size_rows) AS invalid_size_rows
        FROM `{PROJECT}.{DATASET}.tick_manifest_target`
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
