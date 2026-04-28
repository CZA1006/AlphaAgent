"""Run-time scope auditors.

Each auditor walks ``alpha_harness/**.py`` with :mod:`ast` (no actual
imports — pure source inspection) and raises :class:`AuditError` when a
violation is found.  These complement the type-checker and the test
suite by enforcing architectural rules that are otherwise only checked
at PR review time.

* :func:`assert_clean_imports` — AGENTS.md rule #8: Alpha Harness must
  not import ``hermes.*`` / ``runtime.*``; the dependency arrow points
  inward.
* :func:`assert_no_outbound_io_in_evaluators` — evaluators must be
  pure functions of their inputs; no network, no subprocess, no LLM
  SDKs.
"""

from alpha_harness.audit.imports import (
    HARNESS_PACKAGE_DIR,
    AuditError,
    AuditViolation,
    assert_clean_imports,
    assert_no_outbound_io_in_evaluators,
    run_all_audits,
    scan_clean_imports,
    scan_evaluator_io,
)

__all__ = [
    "HARNESS_PACKAGE_DIR",
    "AuditError",
    "AuditViolation",
    "assert_clean_imports",
    "assert_no_outbound_io_in_evaluators",
    "run_all_audits",
    "scan_clean_imports",
    "scan_evaluator_io",
]
