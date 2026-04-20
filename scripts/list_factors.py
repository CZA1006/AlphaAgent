"""List promoted factors from the on-disk factor zoo.

Reads ``artifacts/promoted/_index.jsonl`` (written by
:class:`PromotedArtifactWriter` every time a PROMOTE_CANDIDATE decision
lands).  Supports sorting and a date filter so researchers can answer
"what's live in my zoo?" without touching the registry.

Usage
-----
::

    uv run python -m scripts.list_factors
    uv run python -m scripts.list_factors --sort-by rank_ic --limit 10
    uv run python -m scripts.list_factors --since 2026-01-01 --json

The script is read-only and never mutates the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any

from alpha_harness.artifacts import DEFAULT_PROMOTED_DIR, read_index

_SORT_KEYS = {"ic", "rank_ic", "net_quantile_spread", "turnover", "promoted_at"}


def _coerce_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _entry_promoted_date(entry: dict[str, Any]) -> date | None:
    raw = entry.get("promoted_at", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _sort_value(entry: dict[str, Any], key: str) -> tuple[int, float]:
    """Return a tuple that sorts None/missing values last under descending order."""
    raw = entry.get(key)
    if raw is None:
        return (1, 0.0)  # None sorts after real numbers
    if key == "promoted_at":
        try:
            return (0, -datetime.fromisoformat(str(raw)).timestamp())
        except ValueError:
            return (1, 0.0)
    return (0, -float(raw))


def _format_row(entry: dict[str, Any]) -> str:
    def fmt(v: Any, w: int, spec: str = ".4f") -> str:
        if v is None:
            return "   n/a".rjust(w)
        if isinstance(v, float):
            return f"{v:{spec}}".rjust(w)
        return str(v).rjust(w)

    return (
        f"{str(entry.get('factor_name', ''))[:28]:<28}  "
        f"{fmt(entry.get('ic'), 8)}  "
        f"{fmt(entry.get('rank_ic'), 8)}  "
        f"{fmt(entry.get('net_quantile_spread'), 10)}  "
        f"{fmt(entry.get('turnover'), 8)}  "
        f"{str(entry.get('promoted_at', ''))[:19]:<19}  "
        f"{str(entry.get('factor_id', ''))[:12]}"
    )


def _render_table(entries: list[dict[str, Any]]) -> str:
    header = (
        f"{'factor_name':<28}  "
        f"{'ic':>8}  {'rank_ic':>8}  {'net_qs':>10}  "
        f"{'turnover':>8}  {'promoted_at':<19}  {'factor_id'}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    lines.extend(_format_row(e) for e in entries)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--promoted-dir", default=str(DEFAULT_PROMOTED_DIR),
        help=f"Directory holding the index (default: {DEFAULT_PROMOTED_DIR}).",
    )
    p.add_argument(
        "--sort-by",
        choices=sorted(_SORT_KEYS),
        default="rank_ic",
        help="Metric to sort descending (default: rank_ic).",
    )
    p.add_argument(
        "--since",
        type=_coerce_date,
        default=None,
        help="Only include factors promoted on or after this date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Keep at most N rows after sorting.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit the filtered index as JSON instead of a formatted table.",
    )
    args = p.parse_args(argv)

    entries = read_index(args.promoted_dir)
    if args.since is not None:
        entries = [
            e for e in entries
            if (d := _entry_promoted_date(e)) is not None and d >= args.since
        ]
    entries.sort(key=lambda e: _sort_value(e, args.sort_by))
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        print("(no promoted factors match the filter)", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True, default=str))
    else:
        print(_render_table(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
