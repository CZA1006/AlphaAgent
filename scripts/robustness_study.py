#!/usr/bin/env python3
"""Multi-window robustness study: does the loop produce alpha on average?

The three honest case studies (v1 positive, v2/v3 negative) are three
isolated samples of one question.  This orchestrator turns that into a
designed experiment: **rolling disjoint Y1→Y2 splits x LLMs x selection
strategies**, each cell running the exact case-study protocol —

1. Y1 selection: ``validate_strict`` (lenient regime, LLM proposals);
2. Y2 out-of-sample: ``combine_factors`` builds the basket from the Y1
   validation reports and evaluates it under the strict regime on dates
   the selection never saw (with an embargo gap so Y1's forward labels
   cannot leak into Y2).

The whole grid is **predeclared**: every cell is written into the run
record before the first cell executes, and skipped/failed cells stay in
the denominator.  The summary reports, per arm and pooled, the fraction
of baskets with positive Y2 rank-IC (two-sided binomial sign test
against the no-edge null of 0.5) and the count clearing the strict
regime — the number that actually answers the question.

Smoke (no keys, synthetic parquet store):

    uv run python -m scripts.robustness_study --dry-run \\
        --history-start 2024-07-01 --history-end 2026-06-30

Real study (Polygon-backfilled SP-50 parquet + OpenRouter):

    uv run python -m scripts.robustness_study \\
        --data-source parquet --universe configs/universes/sp50.txt \\
        --history-start 2024-07-01 --history-end 2026-06-30 \\
        --llms openrouter:deepseek/deepseek-chat-v3.1,openrouter:qwen/qwen-2.5-72b-instruct \\
        --selection-strategies input_order,persistence
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT_DIR = Path("artifacts/robustness_study")
RUN_SCHEMA_VERSION = 1


# ── Split generation (pure, unit-tested) ────────────────────────────────────


@dataclass(frozen=True)
class Split:
    """One rolling Y1 (selection) → Y2 (validation) window pair."""

    y1_start: date
    y1_end: date
    y2_start: date
    y2_end: date

    @property
    def split_id(self) -> str:
        return f"y1{self.y1_start.isoformat()}_y2{self.y2_start.isoformat()}"


def _add_months(d: date, months: int) -> date:
    month_index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(month_index, 12)
    # Clamp the day so month arithmetic never raises (e.g. Jan 31 + 1 mo).
    day = min(
        d.day,
        [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][month],
    )
    return date(year, month + 1, day)


def generate_splits(
    history_start: date,
    history_end: date,
    *,
    y1_months: int,
    y2_months: int,
    step_months: int,
    gap_days: int,
) -> list[Split]:
    """Rolling splits: Y1 selection window, an embargo gap, Y2 validation.

    The gap keeps Y1's trailing forward-return labels (lag + horizon)
    from overlapping Y2 — stricter than the original case studies,
    which used adjacent windows.
    """
    if y1_months < 1 or y2_months < 1 or step_months < 1 or gap_days < 0:
        raise ValueError("window parameters must be positive (gap_days >= 0)")
    splits: list[Split] = []
    k = 0
    while True:
        y1_start = _add_months(history_start, k * step_months)
        y1_end = _add_months(y1_start, y1_months)
        y2_start = y1_end + timedelta(days=gap_days)
        y2_end = _add_months(y2_start, y2_months)
        if y2_end > history_end:
            break
        splits.append(Split(y1_start, y1_end, y2_start, y2_end))
        k += 1
    return splits


# ── Statistics (pure, unit-tested) ──────────────────────────────────────────


def sign_test(values: list[float]) -> tuple[int, int, float]:
    """Two-sided binomial sign test against p = 0.5; NaNs excluded."""
    vals = [v for v in values if v is not None and not math.isnan(v)]
    n = len(vals)
    if n == 0:
        return 0, 0, float("nan")
    pos = sum(1 for v in vals if v > 0)
    tail = min(pos, n - pos)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(tail + 1)) / 2**n)
    return pos, n, p


# ── Cell execution ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMSpec:
    """``mock`` or ``openrouter:<model-slug>``."""

    backend: str
    model: str | None

    @property
    def label(self) -> str:
        return self.backend if self.model is None else f"{self.backend}:{self.model}"


def parse_llm_specs(raw: str) -> list[LLMSpec]:
    specs: list[LLMSpec] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "mock":
            specs.append(LLMSpec("mock", None))
        elif part.startswith("openrouter:"):
            specs.append(LLMSpec("openrouter", part.split(":", 1)[1]))
        else:
            raise ValueError(f"unrecognised LLM spec: {part!r}")
    if not specs:
        raise ValueError("at least one LLM spec required")
    return specs


def _run(argv: list[str], *, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _tail(text: str, n: int = 2000) -> str:
    return text if len(text) <= n else text[-n:]


def run_cell(
    *,
    split: Split,
    llm: LLMSpec,
    selection: str,
    args: argparse.Namespace,
    cell_dir: Path,
) -> dict[str, Any]:
    """Y1 selection + Y2 basket evaluation for one grid cell."""
    validation_dir = cell_dir / "validations"
    validation_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if llm.model is not None:
        env["OPENROUTER_MODEL"] = llm.model

    data_args = [
        "--data-source",
        args.data_source,
        "--universe",
        args.universe,
    ]
    if args.data_path:
        data_args += ["--data-path", args.data_path]

    y1_argv = [
        sys.executable,
        "-m",
        "scripts.validate_strict",
        *data_args,
        "--start-date",
        split.y1_start.isoformat(),
        "--end-date",
        split.y1_end.isoformat(),
        "--regime",
        "lenient",
        "--llm",
        llm.backend,
        "--n-candidates",
        str(args.n_candidates),
        "--n-cycles",
        str(args.n_cycles),
        "--cycle-id",
        f"{cell_dir.name}-y1",
        "--validation-dir",
        str(validation_dir),
        "--json",
    ]
    if args.cost_budget_usd is not None and llm.backend == "openrouter":
        y1_argv += ["--cost-budget-usd", str(args.cost_budget_usd)]

    result: dict[str, Any] = {
        "split": split.split_id,
        "llm": llm.label,
        "selection": selection,
        "status": "planned",
        "y1_command": y1_argv,
    }
    try:
        y1 = _run(y1_argv, env=env, timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        result.update(status="failed", failure="y1_timeout")
        return result
    if y1.returncode != 0:
        result.update(
            status="failed",
            failure=f"y1_exit_{y1.returncode}",
            y1_stderr_tail=_tail(y1.stderr),
        )
        return result

    y2_argv = [
        sys.executable,
        "-m",
        "scripts.combine_factors",
        "--from-validation-report",
        str(validation_dir),
        "--filter-passes-ic",
        "--filter-passes-rank-ic",
        *data_args,
        "--start-date",
        split.y2_start.isoformat(),
        "--end-date",
        split.y2_end.isoformat(),
        "--regime",
        "strict",
        "--method",
        "rank_aggregate",
        "--selection-strategy",
        selection,
        "--cycle-id",
        f"{cell_dir.name}-y2",
        "--no-write",
        "--json",
    ]
    if selection == "persistence":
        y2_argv += ["--top-k", str(args.top_k)]
    result["y2_command"] = y2_argv
    try:
        y2 = _run(y2_argv, env=env, timeout=args.timeout_seconds)
        # Fewer surviving candidates than --top-k: clamp to the reported
        # bound and retry once (selecting "top 4 of 2" degenerates to
        # "all 2", which is the honest behaviour, not a failure).
        clamp = re.search(r"--top-k must be between \d+ and (\d+)", y2.stderr or "")
        if y2.returncode != 0 and clamp is not None:
            clamped = clamp.group(1)
            y2_argv[y2_argv.index("--top-k") + 1] = clamped
            result["top_k_clamped_to"] = int(clamped)
            y2 = _run(y2_argv, env=env, timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        result.update(status="failed", failure="y2_timeout")
        return result
    if y2.returncode != 0:
        result.update(
            status="no_basket",
            failure=f"y2_exit_{y2.returncode}",
            y2_stderr_tail=_tail(y2.stderr),
        )
        return result

    try:
        summary = json.loads(y2.stdout[y2.stdout.index("{") :])
    except (ValueError, json.JSONDecodeError):
        result.update(
            status="failed",
            failure="y2_output_unparseable",
            y2_stdout_tail=_tail(y2.stdout),
        )
        return result

    basket = summary.get("basket") or {}
    result.update(
        status="executed",
        n_factors=summary.get("n_factors"),
        y2_ic=basket.get("ic"),
        y2_rank_ic=basket.get("rank_ic"),
        y2_quantile_spread=basket.get("quantile_spread"),
        avg_pairwise_rank_corr=summary.get("avg_pairwise_rank_corr"),
        passes_strict=bool(summary.get("passes_regime")),
        regime_decision=summary.get("regime_decision"),
        regime_failure=summary.get("regime_failure"),
    )
    return result


# ── Tally ────────────────────────────────────────────────────────────────────


def summarise(cells: list[dict[str, Any]]) -> dict[str, Any]:
    def _arm(subset: list[dict[str, Any]]) -> dict[str, Any]:
        executed = [c for c in subset if c["status"] == "executed"]
        rank_ics = [c.get("y2_rank_ic") for c in executed]
        pos, n, p = sign_test([v for v in rank_ics if isinstance(v, int | float)])
        return {
            "cells_total": len(subset),
            "cells_executed": len(executed),
            "cells_no_basket": sum(1 for c in subset if c["status"] == "no_basket"),
            "cells_failed": sum(1 for c in subset if c["status"] == "failed"),
            "y2_rank_ic_positive": pos,
            "y2_rank_ic_n": n,
            "sign_test_p": p,
            "strict_clears": sum(1 for c in executed if c.get("passes_strict")),
            "mean_y2_rank_ic": (
                sum(v for v in rank_ics if isinstance(v, int | float)) / n if n else None
            ),
        }

    arms: dict[str, Any] = {}
    for key in sorted({(c["llm"], c["selection"]) for c in cells}):
        llm, selection = key
        arms[f"{llm}|{selection}"] = _arm(
            [c for c in cells if c["llm"] == llm and c["selection"] == selection],
        )
    return {"pooled": _arm(cells), "arms": arms}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp)
        raise


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-source", default="parquet", choices=["parquet", "polygon", "bigquery"])
    p.add_argument("--data-path", default=None, help="Base path for --data-source parquet.")
    p.add_argument("--universe", default="configs/universes/sp50.txt")
    p.add_argument("--history-start", required=True)
    p.add_argument("--history-end", required=True)
    p.add_argument("--y1-months", type=int, default=12)
    p.add_argument("--y2-months", type=int, default=6)
    p.add_argument("--step-months", type=int, default=3)
    p.add_argument(
        "--gap-days",
        type=int,
        default=7,
        help="Embargo gap between Y1 end and Y2 start (calendar days).",
    )
    p.add_argument(
        "--llms", default="mock", help="Comma list: 'mock' or 'openrouter:<model-slug>'."
    )
    p.add_argument("--selection-strategies", default="input_order,persistence")
    p.add_argument("--top-k", type=int, default=4)
    p.add_argument("--n-candidates", type=int, default=6)
    p.add_argument("--n-cycles", type=int, default=3)
    p.add_argument("--cost-budget-usd", type=float, default=None)
    p.add_argument("--timeout-seconds", type=int, default=1800)
    p.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    p.add_argument("--run-id", default=None)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Predeclare and print the grid without executing anything.",
    )
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    history_start = date.fromisoformat(args.history_start)
    history_end = date.fromisoformat(args.history_end)
    splits = generate_splits(
        history_start,
        history_end,
        y1_months=args.y1_months,
        y2_months=args.y2_months,
        step_months=args.step_months,
        gap_days=args.gap_days,
    )
    llms = parse_llm_specs(args.llms)
    selections = [s.strip() for s in args.selection_strategies.split(",") if s.strip()]
    if not splits:
        print("error: no splits fit inside the history window", file=sys.stderr)
        return 2

    run_id = args.run_id or (
        f"robustness-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    artifact_dir = Path(args.artifact_dir)
    run_dir = artifact_dir / run_id

    # Predeclare the full grid before any execution.
    cells: list[dict[str, Any]] = [
        {"split": s.split_id, "llm": llm.label, "selection": sel, "status": "planned"}
        for s in splits
        for llm in llms
        for sel in selections
    ]
    record: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "predeclared": {
            "history": [args.history_start, args.history_end],
            "y1_months": args.y1_months,
            "y2_months": args.y2_months,
            "step_months": args.step_months,
            "gap_days": args.gap_days,
            "splits": [
                {"y1": [s.y1_start, s.y1_end], "y2": [s.y2_start, s.y2_end]} for s in splits
            ],
            "llms": [llm.label for llm in llms],
            "selection_strategies": selections,
            "n_candidates": args.n_candidates,
            "n_cycles": args.n_cycles,
            "primary_statistic": "two-sided binomial sign test on basket Y2 rank-IC vs 0.5",
            "n_cells": len(cells),
        },
        "started_at": datetime.now(UTC),
        "dry_run": args.dry_run,
        "cells": cells,
    }
    _atomic_write(run_dir / "record.json", record)

    print(f"run_id: {run_id}")
    print(
        f"grid: {len(splits)} splits x {len(llms)} llms x {len(selections)} selections "
        f"= {len(cells)} cells"
    )
    for s in splits:
        print(f"  Y1 {s.y1_start}..{s.y1_end}  ->  Y2 {s.y2_start}..{s.y2_end}")
    if args.dry_run:
        print("dry-run: grid predeclared, nothing executed")
        print(f"record: {run_dir / 'record.json'}")
        return 0

    executed_cells: list[dict[str, Any]] = []
    for s in splits:
        for llm in llms:
            for sel in selections:
                cell_id = f"{s.split_id}-{llm.backend}-{sel}".replace("/", "_").replace(":", "_")
                cell_dir = run_dir / cell_id
                print(f"[{len(executed_cells) + 1}/{len(cells)}] {cell_id} ...", flush=True)
                cell = run_cell(split=s, llm=llm, selection=sel, args=args, cell_dir=cell_dir)
                executed_cells.append(cell)
                record["cells"] = executed_cells + cells[len(executed_cells) :]
                record["summary"] = summarise(executed_cells)
                _atomic_write(run_dir / "record.json", record)
                ric = cell.get("y2_rank_ic")
                ric_s = f"{ric:+.4f}" if isinstance(ric, int | float) else "n/a"
                print(
                    f"    -> {cell['status']}  y2_rank_ic={ric_s}  "
                    f"strict={cell.get('passes_strict')}"
                )

    record["cells"] = executed_cells
    record["summary"] = summarise(executed_cells)
    record["finished_at"] = datetime.now(UTC)
    _atomic_write(run_dir / "record.json", record)

    summary = record["summary"]
    print("\nSUMMARY")
    for label, arm in [("pooled", summary["pooled"]), *summary["arms"].items()]:
        print(
            f"  {label}: executed {arm['cells_executed']}/{arm['cells_total']}, "
            f"Y2 rank-IC positive {arm['y2_rank_ic_positive']}/{arm['y2_rank_ic_n']} "
            f"(sign p={arm['sign_test_p']:.2f}), strict clears {arm['strict_clears']}"
        )
    print(f"record: {run_dir / 'record.json'}")
    if args.json:
        print(json.dumps(json.loads(json.dumps(record, default=str)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
