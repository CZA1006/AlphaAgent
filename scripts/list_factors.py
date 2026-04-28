"""List promoted factors from the on-disk factor zoo.

Reads ``artifacts/promoted/_index.jsonl`` (written by
:class:`PromotedArtifactWriter` every time a PROMOTE_CANDIDATE decision
lands).  Supports sorting, date and refinement-round filters, and a
``--lineage`` rendering that groups factors by their refinement root.

Usage
-----
::

    uv run python -m scripts.list_factors
    uv run python -m scripts.list_factors --sort-by rank_ic --limit 10
    uv run python -m scripts.list_factors --since 2026-01-01 --json
    uv run python -m scripts.list_factors --lineage
    uv run python -m scripts.list_factors --min-refinement-round 1

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


def _refinement_round(entry: dict[str, Any]) -> int:
    """Read ``refinement_round`` as an int, defaulting to 0 for legacy rows."""
    raw = entry.get("refinement_round", 0)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


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


def _format_row(entry: dict[str, Any], *, lineage: bool = False) -> str:
    def fmt(v: Any, w: int, spec: str = ".4f") -> str:
        if v is None:
            return "   n/a".rjust(w)
        if isinstance(v, float):
            return f"{v:{spec}}".rjust(w)
        return str(v).rjust(w)

    base = (
        f"{str(entry.get('factor_name', ''))[:28]:<28}  "
        f"{fmt(entry.get('ic'), 8)}  "
        f"{fmt(entry.get('rank_ic'), 8)}  "
        f"{fmt(entry.get('net_quantile_spread'), 10)}  "
        f"{fmt(entry.get('turnover'), 8)}  "
        f"{str(entry.get('promoted_at', ''))[:19]:<19}  "
        f"{str(entry.get('factor_id', ''))[:12]}"
    )
    if not lineage:
        return base
    parent = entry.get("parent_factor_id") or "-"
    return f"{base}  r={_refinement_round(entry):<2}  parent={str(parent)[:12]:<12}"


def _render_table(entries: list[dict[str, Any]], *, lineage: bool = False) -> str:
    header = (
        f"{'factor_name':<28}  "
        f"{'ic':>8}  {'rank_ic':>8}  {'net_qs':>10}  "
        f"{'turnover':>8}  {'promoted_at':<19}  {'factor_id'}"
    )
    if lineage:
        header = f"{header}  {'r':<2}  {'parent':<12}"
    sep = "-" * len(header)
    lines = [header, sep]
    lines.extend(_format_row(e, lineage=lineage) for e in entries)
    return "\n".join(lines)


def _render_lineage_trees(entries: list[dict[str, Any]]) -> str:
    """Group entries into refinement trees rooted at promotion ancestors.

    A "root" within the zoo is any entry whose ``parent_factor_id`` is
    either missing or not present in the visible set.  Children are
    sorted by refinement round, then promotion timestamp.
    """
    by_id: dict[str, dict[str, Any]] = {
        str(e.get("factor_id", "")): e for e in entries if e.get("factor_id")
    }
    children_of: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for e in entries:
        parent = e.get("parent_factor_id")
        if parent and parent in by_id:
            children_of.setdefault(str(parent), []).append(e)
        else:
            roots.append(e)

    def _key(e: dict[str, Any]) -> tuple[int, str]:
        return (_refinement_round(e), str(e.get("promoted_at", "")))

    roots.sort(key=_key)
    for kids in children_of.values():
        kids.sort(key=_key)

    lines: list[str] = ["", "Lineage trees", "-------------"]

    def _walk(node: dict[str, Any], depth: int) -> None:
        prefix = "  " * depth + ("|- " if depth > 0 else "")
        rank_ic = node.get("rank_ic")
        rank_str = f"{rank_ic:.4f}" if isinstance(rank_ic, int | float) else "n/a"
        lines.append(
            f"{prefix}{str(node.get('factor_name', ''))[:32]} "
            f"[{node.get('factor_id', '')}] "
            f"r={_refinement_round(node)} rank_ic={rank_str}"
        )
        for child in children_of.get(str(node.get("factor_id", "")), []):
            _walk(child, depth + 1)

    for root in roots:
        _walk(root, 0)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--promoted-dir",
        default=str(DEFAULT_PROMOTED_DIR),
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
        "--min-refinement-round",
        type=int,
        default=None,
        help="Drop entries whose refinement_round is below this value.",
    )
    p.add_argument(
        "--max-refinement-round",
        type=int,
        default=None,
        help="Drop entries whose refinement_round is above this value.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Keep at most N rows after sorting.",
    )
    p.add_argument(
        "--lineage",
        action="store_true",
        help=(
            "Show parent_factor_id + refinement_round inline and append a "
            "lineage-tree block grouping factors by refinement root."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the filtered index as JSON instead of a formatted table.",
    )
    args = p.parse_args(argv)

    entries = read_index(args.promoted_dir)
    if args.since is not None:
        entries = [
            e for e in entries if (d := _entry_promoted_date(e)) is not None and d >= args.since
        ]
    if args.min_refinement_round is not None:
        entries = [e for e in entries if _refinement_round(e) >= args.min_refinement_round]
    if args.max_refinement_round is not None:
        entries = [e for e in entries if _refinement_round(e) <= args.max_refinement_round]
    entries.sort(key=lambda e: _sort_value(e, args.sort_by))
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        print("(no promoted factors match the filter)", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True, default=str))
    else:
        print(_render_table(entries, lineage=args.lineage))
        if args.lineage:
            print(_render_lineage_trees(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
