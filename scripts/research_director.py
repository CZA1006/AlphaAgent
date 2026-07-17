#!/usr/bin/env python3
"""Plan the next autonomous quant-research topic.

This is the director layer above the existing validation loop.  It does not
replace ``validate_strict``; it decides which theme/data work should feed the
next validation run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from alpha_harness.director import (
    DEFAULT_VALIDATION_DIR,
    ResearchDirector,
    ResearchDirectorPlan,
    build_market_context,
)
from alpha_harness.markets import list_market_packs, load_market_pack


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--market",
        choices=list_market_packs(),
        default="hk_ipo",
        help="Research market/topic family to plan.",
    )
    parser.add_argument(
        "--validation-dir",
        default=str(DEFAULT_VALIDATION_DIR),
        help="Validation-report directory used to summarize recent loop results.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=4,
        help="How many ranked topics to print in text mode.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the complete director plan as JSON.",
    )
    return parser


def _print_text(plan: ResearchDirectorPlan, *, top_n: int) -> None:
    selected = plan.selected_topic
    print("\nRESEARCH DIRECTOR PLAN")
    print("=" * 72)
    print(f"market           : {plan.market}")
    print(f"selected topic   : {selected.topic_id}")
    print(f"theme            : {selected.theme}")
    print(f"priority         : {selected.priority}")
    print(f"next command     : {selected.validation_command}")
    print("\nwhy:")
    print(f"  {selected.rationale}")
    print("\nranked topics:")
    for topic in plan.topics[:top_n]:
        marker = "*" if topic.topic_id == plan.selected_topic_id else "-"
        print(f"  {marker} {topic.priority:>3}  {topic.topic_id}")
        print(f"       {topic.theme}")
    print("\ndata gaps:")
    for gap in plan.data_gaps:
        print(f"  - [{gap.severity.value}] {gap.name}: {gap.evidence}")
    print("\nextra guidance for selected run:")
    print(f"  {selected.extra_guidance}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    pack = load_market_pack(args.market)
    context = build_market_context(pack, validation_dir=Path(args.validation_dir))
    plan = ResearchDirector().plan(pack, context)
    if args.json:
        print(json.dumps(json.loads(plan.model_dump_json()), indent=2))
    else:
        _print_text(plan, top_n=max(1, args.top_n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
