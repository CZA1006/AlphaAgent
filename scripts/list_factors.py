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

from alpha_harness.artifacts import DEFAULT_PROMOTED_DIR, read_artifact, read_index
from alpha_harness.schemas.experiment import PromotionTrail

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


def _load_trail(
    factor_id: str,
    promoted_dir: str,
) -> PromotionTrail | None:
    """Read the per-factor JSON and return its promotion_trail, if any."""
    payload = read_artifact(factor_id, promoted_dir)
    if payload is None:
        return None
    raw = payload.get("promotion_trail")
    if not isinstance(raw, dict) or not raw.get("trail_id"):
        return None
    return PromotionTrail.model_validate(raw)


def _diff_trails_command(
    factor_id_a: str,
    factor_id_b: str,
    promoted_dir: str,
    *,
    json_out: bool,
) -> int:
    """Implements ``list_factors --diff-trails A B``."""
    trail_a = _load_trail(factor_id_a, promoted_dir)
    trail_b = _load_trail(factor_id_b, promoted_dir)
    if trail_a is None:
        print(
            f"error: no promotion_trail for factor_id={factor_id_a!r} in {promoted_dir}",
            file=sys.stderr,
        )
        return 2
    if trail_b is None:
        print(
            f"error: no promotion_trail for factor_id={factor_id_b!r} in {promoted_dir}",
            file=sys.stderr,
        )
        return 2

    diff = trail_a.diff(trail_b)
    if json_out:
        print(
            json.dumps(
                {
                    "a": factor_id_a,
                    "b": factor_id_b,
                    "trail_id_a": trail_a.trail_id,
                    "trail_id_b": trail_b.trail_id,
                    "diff": {k: list(v) for k, v in diff.items()},
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
        )
        return 0

    print(f"trail_id_a ({factor_id_a}) = {trail_a.trail_id}")
    print(f"trail_id_b ({factor_id_b}) = {trail_b.trail_id}")
    if not diff:
        print("trails are identical (apart from trail_id, which is a hash).")
        return 0
    print(f"differences ({len(diff)} field(s)):")
    for field, (av, bv) in diff.items():
        print(f"  {field}: {av!r} -> {bv!r}")
    return 0


def _dump_trails(entries: list[dict[str, Any]], promoted_dir: str) -> None:
    """For each shown row, read its per-factor JSON and print the trail block.

    Best-effort: missing or v2 artifacts (no trail) print a one-line note
    so the operator can tell the difference between "no trail" and
    "JSON file vanished".
    """
    base = __import__("pathlib").Path(promoted_dir)
    print()
    print("Promotion trails")
    print("----------------")
    for entry in entries:
        fid = entry.get("factor_id", "")
        path = base / f"{fid}.json"
        if not path.is_file():
            print(f"{fid}: artifact missing at {path}")
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{fid}: cannot read trail ({type(exc).__name__})")
            continue
        trail = payload.get("promotion_trail")
        if trail is None:
            print(f"{fid}: legacy artifact, no trail recorded")
            continue
        print(f"{fid}:")
        print(json.dumps(trail, indent=2, sort_keys=True, default=str))


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
        "--trail-id",
        default=None,
        help="Only show factors promoted under this trail_id (Round 4F).",
    )
    p.add_argument(
        "--show-trail",
        action="store_true",
        help=(
            "After the table, dump the full promotion_trail block for "
            "each shown factor by reading its per-factor JSON."
        ),
    )
    p.add_argument(
        "--diff-trails",
        nargs=2,
        metavar=("FACTOR_ID_A", "FACTOR_ID_B"),
        default=None,
        help=(
            "Print a field-level diff between two factors' promotion "
            "trails (Round 4I).  Bypasses the table; exits 2 when either "
            "factor is missing or has no trail."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the filtered index as JSON instead of a formatted table.",
    )
    args = p.parse_args(argv)

    if args.diff_trails is not None:
        return _diff_trails_command(
            args.diff_trails[0],
            args.diff_trails[1],
            args.promoted_dir,
            json_out=args.json,
        )

    entries = read_index(args.promoted_dir)
    if args.since is not None:
        entries = [
            e for e in entries if (d := _entry_promoted_date(e)) is not None and d >= args.since
        ]
    if args.min_refinement_round is not None:
        entries = [e for e in entries if _refinement_round(e) >= args.min_refinement_round]
    if args.max_refinement_round is not None:
        entries = [e for e in entries if _refinement_round(e) <= args.max_refinement_round]
    if args.trail_id is not None:
        entries = [e for e in entries if e.get("trail_id") == args.trail_id]
    entries.sort(key=lambda e: _sort_value(e, args.sort_by))
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        print("(no promoted factors match the filter)", file=sys.stderr)
        return 0

    if args.show_trail:
        _dump_trails(entries, args.promoted_dir)

    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True, default=str))
    else:
        print(_render_table(entries, lineage=args.lineage))
        if args.lineage:
            print(_render_lineage_trees(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
