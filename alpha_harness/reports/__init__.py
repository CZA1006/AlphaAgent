"""Cycle-level audit reports.

Each autonomous run produces a single :class:`CycleReport` capturing the
theme, timing, decision counts, per-experiment thumbnails, lineage, and
optional LLM token usage.  The report is the durable answer to "what
happened in this cycle?" — registry, promoted artifacts, and lineage
memory remain the source of truth; the report is a self-contained
audit mirror.
"""

from alpha_harness.reports.combination import (
    COMBINATION_INDEX_NAME,
    DEFAULT_COMBINATION_DIR,
    CombinationRecipe,
    CombinationReport,
    CombinationReportWriter,
    build_combination_report,
    recipe_id_for,
)
from alpha_harness.reports.combination import (
    read_index as read_combination_index,
)
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
    FactorThumbnail,
    StrictValidationReport,
    StrictValidationReportWriter,
    build_validation_report,
    classify_failure,
)
from alpha_harness.reports.validation import (
    read_index as read_validation_index,
)

__all__ = [
    "COMBINATION_INDEX_NAME",
    "DEFAULT_COMBINATION_DIR",
    "DEFAULT_REPORT_DIR",
    "DEFAULT_VALIDATION_DIR",
    "REPORT_INDEX_NAME",
    "VALIDATION_INDEX_NAME",
    "BudgetSnapshot",
    "CombinationRecipe",
    "CombinationReport",
    "CombinationReportWriter",
    "CycleReport",
    "CycleReportWriter",
    "ExperimentThumbnail",
    "FactorThumbnail",
    "StrictValidationReport",
    "StrictValidationReportWriter",
    "build_combination_report",
    "build_cycle_report",
    "build_validation_report",
    "classify_failure",
    "index_path",
    "read_combination_index",
    "read_index",
    "read_validation_index",
    "recipe_id_for",
]
