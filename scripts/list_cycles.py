"""List cycle audit reports from the on-disk report store.

Reads ``artifacts/reports/_index.jsonl`` (written by
:class:`alpha_harness.reports.CycleReportWriter` after every autonomous
run) and prints a ranked summary.  Read-only.

Usage
-----
::

    uv run python -m scripts.list_cycles
    uv run python -m scripts.list_cycles --limit 5 --json
    uv run python -m scripts.list_cycles --since 2026-04-01

The script never mutates the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any

from alpha_harness.reports import DEFAULT_REPORT_DIR, read_index


def _coerce_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _entry_started_date(entry: dict[str, Any]) -> date | None:
    raw = entry.get("started_at", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return None


def _started_ts(entry: dict[str, Any]) -> float:
    raw = entry.get("started_at", "")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return 0.0


def _format_row(entry: dict[str, Any]) -> str:
    def s(v: Any, w: int) -> str:
        return str("" if v is None else v).ljust(w)[:w]

    started = str(entry.get("started_at", ""))[:19]
    duration = entry.get("duration_s", 0.0)
    duration_str = f"{float(duration):>7.1f}s" if isinstance(duration, int | float) else "    n/a"
    return (
        f"{s(entry.get('cycle_id'), 22)}  "
        f"{started:<19}  "
        f"{duration_str}  "
        f"n={entry.get('n_experiments', 0):<3} "
        f"prom={entry.get('n_promoted', 0):<3} "
        f"refi={entry.get('n_refined', 0):<3} "
        f"rej={entry.get('n_rejected', 0):<3}  "
        f"{s(entry.get('theme'), 60)}"
    )


def _render_table(entries: list[dict[str, Any]]) -> str:
    header = f"{'cycle_id':<22}  {'started_at':<19}  {'duration':>8}  {'counts':<28}  theme"
    sep = "-" * max(len(header), 100)
    lines = [header, sep]
    lines.extend(_format_row(e) for e in entries)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help=f"Directory holding the index (default: {DEFAULT_REPORT_DIR}).",
    )
    p.add_argument(
        "--since",
        type=_coerce_date,
        default=None,
        help="Only include cycles started on or after this date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Keep at most N rows after sorting (most recent first).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the filtered index as JSON instead of a formatted table.",
    )
    args = p.parse_args(argv)

    entries = read_index(args.report_dir)
    if args.since is not None:
        entries = [
            e for e in entries if (d := _entry_started_date(e)) is not None and d >= args.since
        ]
    # Sort newest first.
    entries.sort(key=_started_ts, reverse=True)
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        print("(no cycle reports match the filter)", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True, default=str))
    else:
        print(_render_table(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
