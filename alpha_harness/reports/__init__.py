"""Cycle-level audit reports.

Each autonomous run produces a single :class:`CycleReport` capturing the
theme, timing, decision counts, per-experiment thumbnails, lineage, and
optional LLM token usage.  The report is the durable answer to "what
happened in this cycle?" — registry, promoted artifacts, and lineage
memory remain the source of truth; the report is a self-contained
audit mirror.
"""

from alpha_harness.reports.cycle_report import (
    DEFAULT_REPORT_DIR,
    REPORT_INDEX_NAME,
    BudgetSnapshot,
    CycleReport,
    CycleReportWriter,
    ExperimentThumbnail,
    build_cycle_report,
    index_path,
    read_index,
)
from alpha_harness.reports.validation import (
    DEFAULT_VALIDATION_DIR,
    VALIDATION_INDEX_NAME,
    StrictValidationReport,
    StrictValidationReportWriter,
    build_validation_report,
    classify_failure,
)
from alpha_harness.reports.validation import (
    read_index as read_validation_index,
)

__all__ = [
    "DEFAULT_REPORT_DIR",
    "DEFAULT_VALIDATION_DIR",
    "REPORT_INDEX_NAME",
    "VALIDATION_INDEX_NAME",
    "BudgetSnapshot",
    "CycleReport",
    "CycleReportWriter",
    "ExperimentThumbnail",
    "StrictValidationReport",
    "StrictValidationReportWriter",
    "build_cycle_report",
    "build_validation_report",
    "classify_failure",
    "index_path",
    "read_index",
    "read_validation_index",
]
