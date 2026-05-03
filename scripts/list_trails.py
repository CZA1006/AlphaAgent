#!/usr/bin/env python3
"""List the standalone trail registry (Round 4J).

Reads ``artifacts/trails/_index.jsonl`` (written by
:class:`alpha_harness.artifacts.TrailRegistryWriter` whenever a new
``trail_id`` lands in a promotion).  Read-only.

Usage::

    uv run python -m scripts.list_trails
    uv run python -m scripts.list_trails --limit 5 --json
    uv run python -m scripts.list_trails --diff <id1> <id2>

The script never mutates the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from alpha_harness.artifacts import (
    DEFAULT_TRAIL_DIR,
    read_trail,
    read_trails,
)


def _format_row(entry: dict[str, Any]) -> str:
    factor_ids = entry.get("factor_ids") or []
    return (
        f"{str(entry.get('trail_id', ''))[:16]:<16}  "
        f"{str(entry.get('first_seen_at', ''))[:19]:<19}  "
        f"{str(entry.get('last_seen_at', ''))[:19]:<19}  "
        f"n_factors={len(factor_ids):<3}  "
        f"factors={','.join(factor_ids[:3])}"
        + (f" (+{len(factor_ids) - 3} more)" if len(factor_ids) > 3 else "")
    )


def _render_table(entries: list[dict[str, Any]]) -> str:
    header = (
        f"{'trail_id':<16}  {'first_seen_at':<19}  {'last_seen_at':<19}  {'count':<10}  factors"
    )
    sep = "-" * max(len(header), 100)
    lines = [header, sep]
    lines.extend(_format_row(e) for e in entries)
    return "\n".join(lines)


def _diff_command(
    trail_id_a: str,
    trail_id_b: str,
    trail_dir: str,
    *,
    json_out: bool,
) -> int:
    """Implements ``list_trails --diff A B`` using PromotionTrail.diff()."""
    a = read_trail(trail_id_a, trail_dir)
    b = read_trail(trail_id_b, trail_dir)
    if a is None:
        print(
            f"error: no trail file for trail_id={trail_id_a!r} in {trail_dir}",
            file=sys.stderr,
        )
        return 2
    if b is None:
        print(
            f"error: no trail file for trail_id={trail_id_b!r} in {trail_dir}",
            file=sys.stderr,
        )
        return 2
    diff = a.diff(b)
    if json_out:
        print(
            json.dumps(
                {
                    "a": trail_id_a,
                    "b": trail_id_b,
                    "diff": {k: list(v) for k, v in diff.items()},
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
        )
        return 0
    print(f"trail_a = {trail_id_a}")
    print(f"trail_b = {trail_id_b}")
    if not diff:
        print("trails are identical (apart from trail_id, which is a hash).")
        return 0
    print(f"differences ({len(diff)} field(s)):")
    for field, (av, bv) in diff.items():
        print(f"  {field}: {av!r} -> {bv!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--trail-dir",
        default=str(DEFAULT_TRAIL_DIR),
        help=f"Directory holding the trail index (default: {DEFAULT_TRAIL_DIR}).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Keep at most N rows after sorting (newest first).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the registry rows as JSON instead of a formatted table.",
    )
    p.add_argument(
        "--diff",
        nargs=2,
        metavar=("TRAIL_ID_A", "TRAIL_ID_B"),
        default=None,
        help="Print a field-level diff between two trails (re-uses 4I.diff).",
    )
    args = p.parse_args(argv)

    if args.diff is not None:
        return _diff_command(
            args.diff[0],
            args.diff[1],
            args.trail_dir,
            json_out=args.json,
        )

    rows = read_trails(args.trail_dir)
    rows.sort(key=lambda r: str(r.get("first_seen_at", "")), reverse=True)
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        print("(no trails recorded)", file=sys.stderr)
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True, default=str))
    else:
        print(_render_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
