"""Static import auditors for ``alpha_harness/``.

Both auditors share the same engine: walk every ``.py`` file under a
target directory, parse it with :mod:`ast`, then inspect ``Import`` /
``ImportFrom`` nodes against a forbidden-prefix set.  Pure source
inspection — never executes module top-level code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Root of the alpha_harness package (this file is at .../audit/imports.py).
HARNESS_PACKAGE_DIR = Path(__file__).resolve().parent.parent

# AGENTS.md #8 — Alpha Harness never depends on the Hermes runtime.
# Anything starting with one of these prefixes is forbidden anywhere in
# ``alpha_harness/``.
_FORBIDDEN_RUNTIME_PREFIXES: tuple[str, ...] = ("hermes", "runtime")

# Evaluators must be pure: no network, no subprocess, no LLM SDKs.  The
# allow-list (numpy, pandas, scipy, etc.) is implicit — we only forbid
# the modules below.  Adding a new violator here is intentional;
# adding a permitted dependency requires no change.
_FORBIDDEN_EVALUATOR_PREFIXES: tuple[str, ...] = (
    "requests",
    "urllib",
    "urllib3",
    "httpx",
    "aiohttp",
    "socket",
    "subprocess",
    "openai",
    "anthropic",
    "alpha_harness.llm",
)

EVALUATORS_DIR = HARNESS_PACKAGE_DIR / "evaluators"
_MARKET_LITERAL_EXEMPT_DIRS = frozenset({"markets", "director"})
_FORBIDDEN_MARKET_LITERALS = (
    "hk" + "_ipo",
    "bloomberg" + "-database-0629",
)


# ── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuditViolation:
    """A single forbidden import located by an auditor."""

    file: Path
    line: int
    imported: str
    rule: str
    action: str = "imports"

    def render(self) -> str:
        rel = self._relative_to_package()
        return f"{rel}:{self.line}  {self.action} {self.imported!r}  ({self.rule})"

    def _relative_to_package(self) -> str:
        try:
            return str(self.file.relative_to(HARNESS_PACKAGE_DIR.parent))
        except ValueError:
            return str(self.file)


class AuditError(AssertionError):
    """Raised when an auditor finds at least one violation.

    The string form lists every violation, one per line, so the
    Makefile / CI surface points the reviewer straight at the offender.
    """

    def __init__(self, violations: list[AuditViolation]) -> None:
        self.violations = violations
        body = "\n".join(f"  - {v.render()}" for v in violations)
        super().__init__(f"audit failed with {len(violations)} violation(s):\n{body}")


# ── Engine ──────────────────────────────────────────────────────────────────


def _iter_py_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        # Skip caches and dunder-only files (no actual code to audit).
        if "__pycache__" in path.parts:
            continue
        yield path


def _imported_names(tree: ast.AST) -> Iterable[tuple[int, str]]:
    """Yield ``(lineno, dotted-name)`` for every Import / ImportFrom."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (``from . import x``) have ``module=None``;
            # they cannot reference an absolute forbidden prefix.
            if node.module is None:
                continue
            yield node.lineno, node.module


def _matches_prefix(name: str, prefixes: tuple[str, ...]) -> str | None:
    """Return the matching forbidden prefix, or ``None``.

    ``hermes`` matches ``hermes`` and ``hermes.runtime``;
    ``hermes_boundary`` does NOT match (different package).
    """
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    return None


def _scan(
    root: Path,
    prefixes: tuple[str, ...],
    *,
    rule: str,
) -> list[AuditViolation]:
    violations: list[AuditViolation] = []
    for path in _iter_py_files(root):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            # Unreadable / unparseable files aren't audit failures —
            # the type-checker / parser will flag them separately.
            continue
        for line, name in _imported_names(tree):
            hit = _matches_prefix(name, prefixes)
            if hit is None:
                continue
            violations.append(AuditViolation(file=path, line=line, imported=name, rule=rule))
    return violations


# ── Public scanners + assertions ────────────────────────────────────────────


def scan_clean_imports(root: Path | None = None) -> list[AuditViolation]:
    """Return runtime-prefix violations under ``root`` (default: alpha_harness/)."""
    return _scan(
        root or HARNESS_PACKAGE_DIR,
        _FORBIDDEN_RUNTIME_PREFIXES,
        rule="alpha_harness must not import hermes.* or runtime.*",
    )


def assert_clean_imports(root: Path | None = None) -> None:
    """Raise :class:`AuditError` if any harness module imports a runtime prefix."""
    violations = scan_clean_imports(root)
    if violations:
        raise AuditError(violations)


def scan_evaluator_io(root: Path | None = None) -> list[AuditViolation]:
    """Return outbound-IO violations in ``alpha_harness/evaluators/``."""
    return _scan(
        root or EVALUATORS_DIR,
        _FORBIDDEN_EVALUATOR_PREFIXES,
        rule="evaluators must be pure (no network / subprocess / LLM SDKs)",
    )


def assert_no_outbound_io_in_evaluators(root: Path | None = None) -> None:
    """Raise :class:`AuditError` if an evaluator imports a forbidden module."""
    violations = scan_evaluator_io(root)
    if violations:
        raise AuditError(violations)


def scan_market_literals(root: Path | None = None) -> list[AuditViolation]:
    """Return market literals outside packs and the Stage 2 director boundary."""
    scan_root = root or HARNESS_PACKAGE_DIR
    violations: list[AuditViolation] = []
    for path in _iter_py_files(scan_root):
        try:
            relative = path.relative_to(scan_root)
            if relative.parts and relative.parts[0] in _MARKET_LITERAL_EXEMPT_DIRS:
                continue
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for literal in _FORBIDDEN_MARKET_LITERALS:
            for match in re.finditer(re.escape(literal), source, flags=re.IGNORECASE):
                violations.append(
                    AuditViolation(
                        file=path,
                        line=source.count("\n", 0, match.start()) + 1,
                        imported=literal,
                        rule="market literals belong in market packs",
                        action="contains",
                    )
                )
    return violations


def assert_no_market_literals(root: Path | None = None) -> None:
    """Raise when non-exempt core modules contain a market literal."""
    violations = scan_market_literals(root)
    if violations:
        raise AuditError(violations)


def run_all_audits() -> None:
    """Run every auditor in sequence; the first violation aborts.

    Intentional fail-fast: a runtime-leak failure is more informative
    than a long combined report when CI tries to surface both at once.
    """
    assert_clean_imports()
    assert_no_outbound_io_in_evaluators()
    assert_no_market_literals()
