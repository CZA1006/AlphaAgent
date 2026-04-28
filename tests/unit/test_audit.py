"""Round 4A.9 — runtime-scope auditors."""

from __future__ import annotations

from pathlib import Path

import pytest

from alpha_harness.audit import (
    AuditError,
    assert_clean_imports,
    assert_no_outbound_io_in_evaluators,
    scan_clean_imports,
    scan_evaluator_io,
)

# ── Real codebase passes ────────────────────────────────────────────────────


def test_real_codebase_imports_are_clean() -> None:
    # The whole point: the live tree must satisfy AGENTS.md #8.
    assert_clean_imports()


def test_real_evaluators_have_no_outbound_io() -> None:
    assert_no_outbound_io_in_evaluators()


# ── Boundary auditor ────────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, body: str) -> Path:
    pkg = tmp_path / "alpha_harness"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    file = pkg / name
    file.write_text(body)
    return pkg


def test_boundary_auditor_flags_hermes_runtime_import(tmp_path: Path) -> None:
    pkg = _write_module(
        tmp_path,
        "leak.py",
        "from hermes.runtime import assemble_prompt\n",
    )
    violations = scan_clean_imports(pkg)
    assert len(violations) == 1
    assert violations[0].imported == "hermes.runtime"
    assert violations[0].line == 1
    with pytest.raises(AuditError) as exc:
        assert_clean_imports(pkg)
    assert "leak.py" in str(exc.value)


def test_boundary_auditor_flags_bare_hermes_import(tmp_path: Path) -> None:
    pkg = _write_module(tmp_path, "leak.py", "import hermes\n")
    assert len(scan_clean_imports(pkg)) == 1


def test_boundary_auditor_does_not_flag_hermes_boundary(tmp_path: Path) -> None:
    """``alpha_harness.hermes_boundary`` is harness-owned and allowed."""
    pkg = _write_module(
        tmp_path,
        "ok.py",
        "from alpha_harness.hermes_boundary.contracts import ThemeCycleRequest\n",
    )
    assert scan_clean_imports(pkg) == []


def test_boundary_auditor_ignores_strings_and_comments(tmp_path: Path) -> None:
    """String literals naming forbidden modules must not trigger the audit."""
    pkg = _write_module(
        tmp_path,
        "ok.py",
        '"this module would import hermes.runtime if it weren\'t a docstring"\n'
        "# from hermes.runtime import x  # commented out\n"
        "x = 'import hermes'\n",
    )
    assert scan_clean_imports(pkg) == []


# ── Evaluator-IO auditor ────────────────────────────────────────────────────


def _write_evaluator(tmp_path: Path, name: str, body: str) -> Path:
    root = tmp_path / "evaluators"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body)
    return root


def test_evaluator_auditor_flags_requests(tmp_path: Path) -> None:
    root = _write_evaluator(tmp_path, "bad.py", "import requests\n")
    violations = scan_evaluator_io(root)
    assert len(violations) == 1
    assert violations[0].imported == "requests"


def test_evaluator_auditor_flags_subprocess(tmp_path: Path) -> None:
    root = _write_evaluator(
        tmp_path,
        "bad.py",
        "from subprocess import run\n",
    )
    assert len(scan_evaluator_io(root)) == 1
    with pytest.raises(AuditError):
        assert_no_outbound_io_in_evaluators(root)


def test_evaluator_auditor_flags_llm_sdk(tmp_path: Path) -> None:
    root = _write_evaluator(tmp_path, "bad.py", "import openai\n")
    assert len(scan_evaluator_io(root)) == 1


def test_evaluator_auditor_allows_numpy_pandas(tmp_path: Path) -> None:
    root = _write_evaluator(
        tmp_path,
        "ok.py",
        "import numpy as np\nimport pandas as pd\nfrom math import sqrt\n",
    )
    assert scan_evaluator_io(root) == []


def test_evaluator_auditor_skips_unparseable_files(tmp_path: Path) -> None:
    root = _write_evaluator(
        tmp_path,
        "broken.py",
        "this is :: not python\n",
    )
    # A SyntaxError shouldn't crash the auditor.
    assert scan_evaluator_io(root) == []


# ── Violation message ──────────────────────────────────────────────────────


def test_audit_error_lists_every_violation(tmp_path: Path) -> None:
    pkg = _write_module(
        tmp_path,
        "leak.py",
        "import hermes\nfrom runtime.foo import bar\n",
    )
    with pytest.raises(AuditError) as exc:
        assert_clean_imports(pkg)
    msg = str(exc.value)
    assert "2 violation" in msg
    assert "hermes" in msg
    assert "runtime.foo" in msg
