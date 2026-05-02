#!/usr/bin/env python3
"""Local preflight / sanity check for real-mode AlphaAgent runs.

Fast, dependency-light validator that answers a single question:

    "Given the current environment, which run paths will actually work?"

The checks are grouped by concern so a failure tells the human exactly
where to look (env var, Docker, network, code).  No live LLM or Polygon
calls are made — just reachability + presence checks — so running this
is always safe and free.

Usage::

    uv run python -m scripts.doctor              # all checks
    uv run python -m scripts.doctor --mode mock  # only what mock mode needs
    uv run python -m scripts.doctor --mode real  # LLM + Polygon
    uv run python -m scripts.doctor --mode sql   # all of the above + Postgres

Exit code is ``0`` when every required check for the chosen mode passes
and ``1`` otherwise, so CI / Makefile gates can depend on it.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Literal

Mode = Literal["mock", "real", "data", "sql", "all"]

_OK = "  ✓"
_WARN = "  ~"
_FAIL = "  ✗"


@dataclass
class CheckResult:
    name: str
    passed: bool
    required: bool
    detail: str = ""

    def render(self) -> str:
        mark = _OK if self.passed else (_FAIL if self.required else _WARN)
        tail = f" — {self.detail}" if self.detail else ""
        tag = "" if self.required else "  (optional)"
        return f"{mark} {self.name}{tag}{tail}"


# ── Individual checks ───────────────────────────────────────────────────────


def _check_env(var: str, *, required: bool, hint: str = "") -> CheckResult:
    value = os.environ.get(var, "").strip()
    if value:
        return CheckResult(
            name=f"{var} is set",
            passed=True,
            required=required,
            detail=_redact(value),
        )
    return CheckResult(
        name=f"{var} is set",
        passed=False,
        required=required,
        detail=hint or "empty or unset",
    )


def _redact(value: str) -> str:
    """Show only a harmless prefix/suffix so secrets never print in full."""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}…{value[-3:]} ({len(value)} chars)"


def _check_openrouter_config() -> CheckResult:
    """Attempt to construct an OpenRouterConfig from the current env."""
    try:
        from alpha_harness.llm.config import LLMConfigError, OpenRouterConfig
    except Exception as exc:  # pragma: no cover — import failures are bugs
        return CheckResult(
            name="alpha_harness.llm.config imports cleanly",
            passed=False,
            required=True,
            detail=str(exc),
        )

    try:
        cfg = OpenRouterConfig.from_env()
    except LLMConfigError as exc:
        return CheckResult(
            name="OpenRouterConfig.from_env() succeeds",
            passed=False,
            required=True,
            detail=str(exc),
        )

    return CheckResult(
        name="OpenRouterConfig.from_env() succeeds",
        passed=True,
        required=True,
        detail=f"model={cfg.model} temperature={cfg.temperature}",
    )


def _check_postgres_reachable() -> CheckResult:
    """Best-effort TCP/engine connect — does not require psql."""
    from alpha_harness.config import PostgresSettings

    settings = PostgresSettings.from_env()
    try:
        from sqlalchemy import create_engine, text
    except Exception as exc:  # pragma: no cover
        return CheckResult(
            name="SQLAlchemy is importable",
            passed=False,
            required=True,
            detail=str(exc),
        )

    try:
        engine = create_engine(settings.url, pool_pre_ping=False)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:
        return CheckResult(
            name=f"Postgres reachable at {settings.host}:{settings.port}/{settings.database}",
            passed=False,
            required=True,
            detail=(
                f"{type(exc).__name__}: {exc}. "
                "Run `make db-up && make db-bootstrap` or check POSTGRES_* env vars."
            ),
        )
    return CheckResult(
        name=f"Postgres reachable at {settings.host}:{settings.port}/{settings.database}",
        passed=True,
        required=True,
        detail="SELECT 1 succeeded",
    )


def _check_llm_log_dir() -> CheckResult:
    """Verify the LLM call-log directory exists (or is creatable) and is writable."""
    raw = os.environ.get("ALPHA_AGENT_LLM_LOG_DIR", "").strip()
    path = raw or "artifacts/llm_calls"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name=f"LLM call-log dir writable at {path}",
            passed=False,
            required=True,
            detail=f"{type(exc).__name__}: {exc}",
        )

    probe = os.path.join(path, ".doctor_probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        os.remove(probe)
    except OSError as exc:
        return CheckResult(
            name=f"LLM call-log dir writable at {path}",
            passed=False,
            required=True,
            detail=f"cannot write: {type(exc).__name__}: {exc}",
        )

    return CheckResult(
        name=f"LLM call-log dir writable at {path}",
        passed=True,
        required=True,
        detail="directory exists and accepts writes",
    )


def _check_promoted_artifacts_dir() -> CheckResult:
    """Verify the promoted-artifacts directory is writable and (if present)
    has a well-formed index."""
    from alpha_harness.artifacts import PROMOTED_INDEX_NAME, read_index

    path = "artifacts/promoted"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name=f"Promoted-artifacts dir writable at {path}",
            passed=False,
            required=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    probe = os.path.join(path, ".doctor_probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        os.remove(probe)
    except OSError as exc:
        return CheckResult(
            name=f"Promoted-artifacts dir writable at {path}",
            passed=False,
            required=False,
            detail=f"cannot write: {type(exc).__name__}: {exc}",
        )

    index_file = os.path.join(path, PROMOTED_INDEX_NAME)
    if os.path.isfile(index_file):
        try:
            entries = read_index(path)
        except OSError as exc:
            return CheckResult(
                name=f"Promoted-artifacts dir writable at {path}",
                passed=False,
                required=False,
                detail=f"index unreadable: {exc}",
            )
        # Schema-drift probe: every row should at minimum carry a
        # ``factor_id``.  ``parent_factor_id`` and ``refinement_round``
        # are optional (legacy rows are pre-4A.7), but when present
        # ``refinement_round`` must be a non-negative integer.
        bad = [
            i
            for i, e in enumerate(entries, 1)
            if not isinstance(e.get("factor_id"), str)
            or (
                "refinement_round" in e
                and not (isinstance(e["refinement_round"], int) and e["refinement_round"] >= 0)
            )
            or (
                # Round 4F: when trail_id is present at all, it must be
                # a non-empty string.  Legacy v1/v2 rows omit it entirely
                # and stay valid.
                "trail_id" in e
                and e["trail_id"] is not None
                and not (isinstance(e["trail_id"], str) and e["trail_id"])
            )
        ]
        if bad:
            return CheckResult(
                name=f"Promoted-artifacts dir writable at {path}",
                passed=False,
                required=False,
                detail=(
                    f"{len(entries)} indexed; {len(bad)} row(s) "
                    f"with malformed schema (lines {bad[:3]}...)"
                ),
            )
        with_lineage = sum(1 for e in entries if e.get("parent_factor_id"))
        with_trail = sum(1 for e in entries if e.get("trail_id"))
        return CheckResult(
            name=f"Promoted-artifacts dir writable at {path}",
            passed=True,
            required=False,
            detail=(
                f"{len(entries)} promoted factor(s) indexed "
                f"({with_lineage} from refinement, {with_trail} with trail)"
            ),
        )

    return CheckResult(
        name=f"Promoted-artifacts dir writable at {path}",
        passed=True,
        required=False,
        detail="directory ready; no promotions recorded yet",
    )


def _check_smoke_can_run() -> CheckResult:
    """Verify the synthetic-data path constructs and every mock-LLM
    candidate compiles.  Catches a corrupt fixture before the slow
    integration smoke test runs."""
    try:
        from alpha_harness.data.synthetic import generate_price_panel
        from alpha_harness.factors.compiler import (
            DslCompilationError,
            FactorDslCompiler,
        )
        from alpha_harness.schemas.hypothesis import Hypothesis
        from scripts.autonomous_cycle import _MOCK_CANDIDATES
    except Exception as exc:  # pragma: no cover — import bug
        return CheckResult(
            name="Mock-LLM smoke prerequisites",
            passed=False,
            required=False,
            detail=f"import failed: {type(exc).__name__}: {exc}",
        )

    try:
        df = generate_price_panel(n_days=3, symbols=["AAA", "BBB"], seed=0)
    except Exception as exc:
        return CheckResult(
            name="Mock-LLM smoke prerequisites",
            passed=False,
            required=False,
            detail=f"synthetic data failed: {exc}",
        )
    if df.empty:
        return CheckResult(
            name="Mock-LLM smoke prerequisites",
            passed=False,
            required=False,
            detail="generate_price_panel returned empty frame",
        )

    compiler = FactorDslCompiler()
    bad: list[str] = []
    for cand in _MOCK_CANDIDATES:
        try:
            compiler.compile(
                Hypothesis(text=cand.expression, rationale=cand.rationale),
            )
        except DslCompilationError as exc:
            bad.append(f"{cand.expression!r}: {exc}")
    if bad:
        return CheckResult(
            name="Mock-LLM smoke prerequisites",
            passed=False,
            required=False,
            detail=f"{len(bad)} candidate(s) failed to compile: {bad[0]}",
        )
    return CheckResult(
        name="Mock-LLM smoke prerequisites",
        passed=True,
        required=False,
        detail=(f"{len(_MOCK_CANDIDATES)} mock candidates compile; synthetic data path OK"),
    )


def _check_boundary_audit() -> CheckResult:
    """Run static auditors so boundary-rule failures surface early."""
    from alpha_harness.audit import (
        scan_clean_imports,
        scan_evaluator_io,
    )

    violations = scan_clean_imports() + scan_evaluator_io()
    if not violations:
        return CheckResult(
            name="Boundary + evaluator-IO audits clean",
            passed=True,
            required=False,
            detail="alpha_harness imports respect AGENTS.md #8",
        )
    head = violations[0].render()
    return CheckResult(
        name="Boundary + evaluator-IO audits clean",
        passed=False,
        required=False,
        detail=f"{len(violations)} violation(s); first: {head}",
    )


def _check_cycle_reports_dir() -> CheckResult:
    """Verify the cycle-report directory is writable and (if present) has
    a well-formed index — every row must carry ``cycle_id`` and any
    counts must be non-negative integers."""
    from alpha_harness.reports import REPORT_INDEX_NAME, read_index

    path = "artifacts/reports"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name=f"Cycle-report dir writable at {path}",
            passed=False,
            required=False,
            detail=f"{type(exc).__name__}: {exc}",
        )

    probe = os.path.join(path, ".doctor_probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        os.remove(probe)
    except OSError as exc:
        return CheckResult(
            name=f"Cycle-report dir writable at {path}",
            passed=False,
            required=False,
            detail=f"cannot write: {type(exc).__name__}: {exc}",
        )

    index_file = os.path.join(path, REPORT_INDEX_NAME)
    if not os.path.isfile(index_file):
        return CheckResult(
            name=f"Cycle-report dir writable at {path}",
            passed=True,
            required=False,
            detail="directory ready; no cycles recorded yet",
        )
    try:
        entries = read_index(path)
    except OSError as exc:
        return CheckResult(
            name=f"Cycle-report dir writable at {path}",
            passed=False,
            required=False,
            detail=f"index unreadable: {exc}",
        )
    bad = [
        i
        for i, e in enumerate(entries, 1)
        if not isinstance(e.get("cycle_id"), str)
        or any(
            k in e and not (isinstance(e[k], int) and e[k] >= 0)
            for k in ("n_experiments", "n_promoted", "n_refined", "n_rejected")
        )
    ]
    if bad:
        return CheckResult(
            name=f"Cycle-report dir writable at {path}",
            passed=False,
            required=False,
            detail=(
                f"{len(entries)} indexed; {len(bad)} row(s) with malformed "
                f"schema (lines {bad[:3]}...)"
            ),
        )
    promoted = sum(int(e.get("n_promoted", 0)) for e in entries)
    return CheckResult(
        name=f"Cycle-report dir writable at {path}",
        passed=True,
        required=False,
        detail=(
            f"{len(entries)} cycle report(s) indexed ({promoted} promotions across all cycles)"
        ),
    )


def _check_parquet_path() -> CheckResult:
    """Look for a populated local Parquet store under data/silver/equities."""
    path = "data/silver/equities"
    exists = os.path.isdir(path)
    populated = exists and any(os.scandir(path))
    if populated:
        return CheckResult(
            name=f"Local Parquet store present at {path}",
            passed=True,
            required=False,
            detail="directory exists and is non-empty",
        )
    if exists:
        return CheckResult(
            name=f"Local Parquet store present at {path}",
            passed=False,
            required=False,
            detail=(
                "directory exists but is empty; run `make backfill-sp50` for the "
                "Round 4 research universe, or `uv run python -m scripts.sample_ingest` "
                "for a tiny synthetic slice"
            ),
        )
    return CheckResult(
        name=f"Local Parquet store present at {path}",
        passed=False,
        required=False,
        detail="not found; only needed for --data-source parquet",
    )


# ── Orchestration ───────────────────────────────────────────────────────────


def run(mode: Mode) -> int:
    sections: list[tuple[str, list[CheckResult]]] = []

    # Mock mode: we still surface .env hygiene warnings but nothing is required.
    if mode in ("mock", "all"):
        sections.append(
            (
                "Mock mode (offline / no keys required)",
                [
                    _check_env(
                        "ALPHA_AGENT_BACKEND",
                        required=False,
                        hint="default 'memory' will be used",
                    ),
                    _check_parquet_path(),
                    _check_promoted_artifacts_dir(),
                    _check_cycle_reports_dir(),
                    _check_boundary_audit(),
                    _check_smoke_can_run(),
                ],
            ),
        )

    if mode in ("real", "data", "sql", "all"):
        llm_checks: list[CheckResult] = [
            _check_env(
                "OPENROUTER_API_KEY",
                required=True,
                hint="set it in .env; get a key at https://openrouter.ai/keys",
            ),
            _check_openrouter_config(),
            _check_env("OPENROUTER_MODEL", required=False),
            _check_llm_log_dir(),
            _check_env(
                "ALPHA_AGENT_TOKEN_BUDGET",
                required=False,
                hint="optional hard cap on cumulative tokens per cycle",
            ),
            _check_env(
                "ALPHA_AGENT_COST_BUDGET_USD",
                required=False,
                hint="optional hard cap on cumulative LLM cost per cycle (USD)",
            ),
        ]
        sections.append(("Real LLM (OpenRouter)", llm_checks))

    if mode in ("data", "sql", "all"):
        sections.append(
            (
                "Real market data (Polygon)",
                [
                    _check_env(
                        "POLYGON_API_KEY",
                        required=True,
                        hint="only needed for --data-source polygon",
                    ),
                    _check_env(
                        "POLYGON_RPM",
                        required=False,
                        hint="override request pacing; default 5 rpm (free-tier safe)",
                    ),
                ],
            ),
        )

    if mode in ("sql", "all"):
        sections.append(
            (
                "SQL backend (Postgres)",
                [
                    _check_env("POSTGRES_USER", required=True),
                    _check_env("POSTGRES_PASSWORD", required=True),
                    _check_env("POSTGRES_HOST", required=True),
                    _check_env("POSTGRES_PORT", required=True),
                    _check_env("POSTGRES_DB", required=True),
                    _check_postgres_reachable(),
                ],
            ),
        )

    # ── Render ─────────────────────────────────────────────────────────────
    print(f"AlphaAgent doctor — mode: {mode}")
    print("=" * 72)

    any_required_failed = False
    for title, checks in sections:
        print(f"\n[{title}]")
        for result in checks:
            print(result.render())
            if result.required and not result.passed:
                any_required_failed = True

    print()
    if any_required_failed:
        print(
            "Result: FAIL — at least one required check failed for mode "
            f"{mode!r}. Fix the lines marked ✗ above, then re-run `make doctor`.",
        )
        return 1

    print(f"Result: OK — mode {mode!r} is ready to run.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["mock", "real", "data", "sql", "all"],
        default="all",
        help=(
            "Which run path to validate. "
            "mock=offline, real=LLM only, data=LLM+Polygon, sql=LLM+Polygon+Postgres, "
            "all=everything (default)."
        ),
    )
    args = parser.parse_args(argv)
    return run(args.mode)


if __name__ == "__main__":
    sys.exit(main())
