"""Typed Python facade for market-scoped Alpha Harness operations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from alpha_harness.artifacts import ArtifactKind, ArtifactStore, LocalArtifactStore
from alpha_harness.director import (
    DEFAULT_VALIDATION_DIR,
    ResearchDirector,
    ResearchDirectorPlan,
    build_market_context,
)
from alpha_harness.markets import load_market_pack
from alpha_harness.reports import CombinationReport, StrictValidationReport

if TYPE_CHECKING:
    from scripts.autonomous_researcher import (
        AutonomousRunnerConfig,
        AutonomousRunRecord,
    )


class ValidationRequest(BaseModel):
    """Typed wrapper around the stable ``validate_strict`` argument contract."""

    model_config = ConfigDict(frozen=True)

    argv: tuple[str, ...] = ()


class CombinationRequest(BaseModel):
    """Typed wrapper around the stable ``combine_factors`` argument contract."""

    model_config = ConfigDict(frozen=True)

    argv: tuple[str, ...] = ()


class SdkExecutionError(RuntimeError):
    """Raised when a deterministic SDK operation returns a non-zero status."""

    def __init__(self, operation: str, exit_code: int) -> None:
        super().__init__(f"{operation} failed with exit code {exit_code}")
        self.exit_code = exit_code


def _run_validation(
    market_id: str,
    request: ValidationRequest,
    *,
    emit_output: bool,
) -> tuple[int, list[StrictValidationReport]]:
    pack = load_market_pack(market_id)
    from scripts.validate_strict import _execute_validation

    return _execute_validation(
        list(request.argv),
        dsl_fields=pack.dsl_fields,
        emit_output=emit_output,
    )


def run_validation(
    market_id: str,
    request: ValidationRequest,
) -> StrictValidationReport:
    """Run validation with the selected pack's explicit DSL field set."""
    exit_code, reports = _run_validation(market_id, request, emit_output=False)
    if exit_code or not reports:
        raise SdkExecutionError("validation", exit_code)
    return reports[-1]


def run_validation_cli(market_id: str, request: ValidationRequest) -> int:
    """CLI adapter preserving the validator's existing output and exit codes."""
    exit_code, _ = _run_validation(market_id, request, emit_output=True)
    return exit_code


def _combine(
    market_id: str,
    request: CombinationRequest,
    *,
    emit_output: bool,
) -> tuple[int, CombinationReport | None]:
    pack = load_market_pack(market_id)
    from scripts.combine_factors import _execute_combination

    return _execute_combination(
        list(request.argv),
        dsl_fields=pack.dsl_fields,
        emit_output=emit_output,
    )


def combine(market_id: str, request: CombinationRequest) -> CombinationReport:
    """Combine factors with the selected pack's explicit DSL field set."""
    exit_code, report = _combine(market_id, request, emit_output=False)
    if exit_code or report is None:
        raise SdkExecutionError("combination", exit_code)
    return report


def combine_cli(market_id: str, request: CombinationRequest) -> int:
    """CLI adapter preserving the combiner's existing output and exit codes."""
    exit_code, _ = _combine(market_id, request, emit_output=True)
    return exit_code


def plan(
    market_id: str,
    *,
    validation_dir: Path | str = DEFAULT_VALIDATION_DIR,
) -> ResearchDirectorPlan:
    """Build a director plan after resolving one explicit market pack."""
    pack = load_market_pack(market_id)
    context = build_market_context(pack, validation_dir=validation_dir)
    return ResearchDirector().plan(pack, context)


def run_autonomous(
    market_id: str,
    config: AutonomousRunnerConfig,
) -> AutonomousRunRecord:
    """Run the autonomous researcher through its typed config contract."""
    from scripts.autonomous_researcher import run_autonomous_research

    pack = load_market_pack(market_id)
    effective = config.model_copy(update={"market": market_id})
    return run_autonomous_research(effective, market_pack=pack)


def list_reports(
    kind: ArtifactKind,
    *,
    store: ArtifactStore | None = None,
) -> list[dict[str, Any]]:
    """List artifact index rows through the configured store."""
    return (store or LocalArtifactStore()).list(kind)


def get_report(
    kind: ArtifactKind,
    artifact_id: str,
    *,
    store: ArtifactStore | None = None,
) -> dict[str, Any] | None:
    """Read one artifact through the configured store."""
    return (store or LocalArtifactStore()).read(kind, artifact_id)
