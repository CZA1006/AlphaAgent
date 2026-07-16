#!/usr/bin/env python3
"""Read-only auditor for promoted composite factors (Round 9 Phase C).

Two modes:

* ``--list`` — table of every promoted composite under ``--promoted-dir``,
  one row per recipe with the basket's headline metrics.
* ``--recipe-id <id>`` — detailed view of one composite: recipe,
  components, last-known metrics, regime trail summary, refinement
  ancestry (parent_factor_id chain walked up to the root).

Pure read tool — no writes, no LLM, no network.

Usage::

    uv run python -m scripts.inspect_composite --list
    uv run python -m scripts.inspect_composite --recipe-id 593ca7ddcda1c8d6
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from alpha_harness.artifacts.promoted import DEFAULT_PROMOTED_DIR

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("inspect_composite")


# ── Loaders ─────────────────────────────────────────────────────────────────


def _read_index(promoted_dir: Path) -> list[dict[str, Any]]:
    """Read ``_index.jsonl`` defensively (skip corrupt rows)."""
    idx = promoted_dir / "_index.jsonl"
    if not idx.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with idx.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.warning("Skipping corrupt index line %d: %s", i, exc)
    return rows


def _load_artifact(promoted_dir: Path, factor_id: str) -> dict[str, Any] | None:
    """Read one promoted-artifact JSON, or return ``None`` if missing/corrupt."""
    path = promoted_dir / f"{factor_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unreadable artifact %s: %s", path, exc)
        return None


# ── Composite discovery ─────────────────────────────────────────────────────


def _iter_composites(
    promoted_dir: Path,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(index_row, artifact_payload)`` for every promoted composite.

    Sorted newest first by ``promoted_at``.  Scalar promotions (no
    ``composite_recipe`` block in the artifact) are dropped.
    """
    rows = _read_index(promoted_dir)
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        factor_id = row.get("factor_id")
        if not factor_id:
            continue
        payload = _load_artifact(promoted_dir, factor_id)
        if payload is None or not isinstance(payload.get("composite_recipe"), dict):
            continue
        out.append((row, payload))
    out.sort(key=lambda pair: pair[0].get("promoted_at", ""), reverse=True)
    return out


def _resolve_recipe(
    promoted_dir: Path,
    recipe_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find the most recent artifact whose composite_recipe.recipe_id matches."""
    for row, payload in _iter_composites(promoted_dir):
        if payload["composite_recipe"].get("recipe_id") == recipe_id:
            return row, payload
    return None


def _ancestry(
    promoted_dir: Path,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Walk parent_factor_id up to the root; return list newest → oldest.

    Stops if a parent is missing on disk — a missing parent is logged
    but doesn't raise.
    """
    chain: list[dict[str, Any]] = [payload]
    cursor: dict[str, Any] | None = payload
    seen_ids: set[str] = {payload.get("factor_id", "")}
    while cursor is not None:
        parent_id = cursor.get("parent_factor_id")
        if not parent_id or parent_id in seen_ids:
            break
        parent = _load_artifact(promoted_dir, parent_id)
        if parent is None:
            logger.info(
                "Parent %s not on disk under %s — ancestry truncated.",
                parent_id,
                promoted_dir,
            )
            break
        chain.append(parent)
        seen_ids.add(parent_id)
        cursor = parent
    return chain


# ── Renderers ───────────────────────────────────────────────────────────────


def _fmt_metric(value: Any, width: int = 8) -> str:
    if isinstance(value, int | float):
        return f"{value:+.4f}".rjust(width)
    return "n/a".rjust(width)


def render_list(promoted_dir: Path) -> str:
    composites = _iter_composites(promoted_dir)
    if not composites:
        return f"No promoted composites found under {promoted_dir}.\n"
    lines = [
        f"Promoted composites in {promoted_dir} (newest first):",
        "",
        f"  {'recipe_id':<16}  {'method':<14}  {'n':>2}  {'ic':>8}  {'rank_ic':>8}  promoted_at",
        f"  {'-' * 16}  {'-' * 14}  {'-' * 2}  {'-' * 8}  {'-' * 8}  {'-' * 25}",
    ]
    for row, payload in composites:
        recipe = payload["composite_recipe"]
        n_components = len(recipe.get("components", []))
        lines.append(
            f"  {recipe.get('recipe_id', '?'):<16}  "
            f"{recipe.get('method', '?'):<14}  "
            f"{n_components:>2}  "
            f"{_fmt_metric(row.get('ic'))}  "
            f"{_fmt_metric(row.get('rank_ic'))}  "
            f"{row.get('promoted_at', '?')}",
        )
    lines.append("")
    lines.append(f"  total: {len(composites)} composite(s)")
    return "\n".join(lines) + "\n"


def render_detail(promoted_dir: Path, recipe_id: str) -> str:
    hit = _resolve_recipe(promoted_dir, recipe_id)
    if hit is None:
        return f"No composite with recipe_id={recipe_id!r} under {promoted_dir}.\n"
    _row, payload = hit
    recipe = payload["composite_recipe"]
    trail = payload.get("promotion_trail") or {}
    ev = payload.get("evaluation") or {}
    ancestry = _ancestry(promoted_dir, payload)

    lines: list[str] = []
    border = "=" * 78
    lines.append(border)
    lines.append(f"  COMPOSITE  recipe_id={recipe.get('recipe_id', '?')}")
    lines.append(border)
    lines.append(f"  factor_id      : {payload.get('factor_id', '?')}")
    lines.append(f"  factor_name    : {payload.get('factor_name', '?')}")
    lines.append(f"  promoted_at    : {payload.get('promoted_at', '?')}")
    lines.append(f"  cycle_id       : {payload.get('cycle_id', '?')}")
    lines.append(f"  method         : {recipe.get('method', '?')}")
    lines.append("")
    lines.append("  Components:")
    for i, expr in enumerate(recipe.get("components", [])):
        lines.append(f"    {i}. {expr}")
    component_ids = recipe.get("component_factor_ids", [])
    if component_ids:
        lines.append("")
        lines.append("  Component factor_ids (lineage):")
        for fid in component_ids:
            lines.append(f"    - {fid}")
    lines.append("")
    lines.append("  Metrics:")
    lines.append(f"    ic                  : {_fmt_metric(ev.get('ic'))}")
    lines.append(f"    rank_ic             : {_fmt_metric(ev.get('rank_ic'))}")
    lines.append(f"    quantile_spread     : {_fmt_metric(ev.get('quantile_spread'))}")
    lines.append(
        f"    net_quantile_spread : {_fmt_metric(ev.get('net_quantile_spread'))}",
    )
    lines.append(f"    turnover            : {_fmt_metric(ev.get('turnover'))}")
    lines.append(f"    n_periods           : {ev.get('n_periods', '?')}")
    lines.append(f"    n_assets            : {ev.get('n_assets', '?')}")
    lines.append("")
    lines.append("  Regime trail:")
    lines.append(f"    trail_id            : {trail.get('trail_id', '?')}")
    lines.append(f"    neutralize          : {trail.get('neutralize', '?')}")
    lines.append(f"    cost_bps            : {trail.get('cost_bps', '?')}")
    lines.append(
        f"    forecast_horizon    : {trail.get('forecast_horizon_bars', '?')} bars",
    )
    lines.append(f"    holdout_strategy    : {trail.get('holdout_strategy', '?')}")
    lines.append("")
    lines.append(f"  Refinement ancestry ({len(ancestry)} step(s), newest first):")
    for step in ancestry:
        rid = (step.get("composite_recipe") or {}).get("recipe_id", "?")
        rr = step.get("refinement_round", 0)
        fid = step.get("factor_id", "?")
        lines.append(f"    round={rr}  factor_id={fid}  recipe_id={rid}")
    lines.append(border)
    return "\n".join(lines) + "\n"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--promoted-dir",
        default=str(DEFAULT_PROMOTED_DIR),
        help=f"Promoted-artifact directory (default: {DEFAULT_PROMOTED_DIR}).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--list",
        action="store_true",
        help="Print a table of every promoted composite.",
    )
    g.add_argument(
        "--recipe-id",
        default=None,
        help="Render the detail view for one composite by recipe id.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    promoted_dir = Path(args.promoted_dir)
    if not promoted_dir.is_dir():
        print(f"error: --promoted-dir not found: {promoted_dir}", file=sys.stderr)
        return 2
    if args.list:
        sys.stdout.write(render_list(promoted_dir))
    else:
        sys.stdout.write(render_detail(promoted_dir, args.recipe_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
