"""Prompt templates for the hypothesis proposer.

The system prompt is *entirely* derived from the current DSL module — if a
new function or field is added to ``dsl_parser.ALLOWED_*``, the prompt
picks it up automatically.  This keeps the prompt and the validator in
lockstep: the model is never told about operators the compiler would
reject.
"""

from __future__ import annotations

from alpha_harness.factors.dsl_parser import ALLOWED_FIELDS, ALLOWED_FUNCTIONS
from alpha_harness.proposer.schemas import ProposalRequest
from alpha_harness.retrieval import RelatedExperiment

# Documented arities & semantics per function.  Authoritative source is
# ``dsl_parser._function_arity`` — we duplicate here for the prompt only.
_FUNCTION_DOCS: dict[str, str] = {
    "lag": "lag(series, window)        — value from `window` bars ago",
    "ts_mean": "ts_mean(series, window)    — rolling mean over `window` bars",
    "ts_std": "ts_std(series, window)     — rolling standard deviation",
    "ts_sum": "ts_sum(series, window)     — rolling sum",
    "ts_min": "ts_min(series, window)     — rolling minimum",
    "ts_max": "ts_max(series, window)     — rolling maximum",
    "ts_delta": "ts_delta(series, window)   — series - lag(series, window)",
    "ts_lag": "ts_lag(series, window)     — alias of lag",
    "event_decay": "event_decay(distance, half_life) — proximity weight; missing event = 0",
    "rank": "rank(series)               — cross-sectional rank in [0,1]",
    "zscore": "zscore(series)             — cross-sectional z-score",
}

# Short glossary for fields whose meaning isn't self-evident.  OHLCV are
# universally understood and omitted; the microstructure fields (HK IPO
# tick-derived, present only on the BigQuery panel) get a one-line gloss
# so the model uses them sensibly.  Fields without an entry are listed
# bare.
_FIELD_DOCS: dict[str, str] = {
    "ofi": "order-flow imbalance: signed volume / total volume in [-1,1] (>0 net buy)",
    "rel_spread": "average relative bid-ask spread (liquidity / trading cost)",
    "realized_vol": "intraday realized volatility from 1-minute sampled prices",
    "n_trades": "number of trades that day (activity)",
    "tick_volume": "summed trade size that day",
    "avg_trade_size": "mean trade size (small = retail, large = institutional)",
    "n_quotes": "number of quote updates (liquidity-provision intensity)",
    "days_since_listing": "calendar days since listing date",
    "days_since_pricing": "calendar days since IPO pricing date",
    "days_to_next_cornerstone_lockup": "days until next cornerstone lockup expiry",
    "days_since_prev_cornerstone_lockup": "days since previous cornerstone lockup expiry",
    "next_cornerstone_unlock_shares": "shares unlocked at next cornerstone lockup expiry",
    "next_cornerstone_unlock_pct_offer": "next cornerstone unlock size as percent of offer",
    "next_cornerstone_unlock_pct_cap": "next cornerstone unlock size as percent of share capital",
    "days_to_next_greenshoe_expiry": "days until greenshoe option expiry",
    "days_since_prev_greenshoe_expiry": "days since previous greenshoe expiry",
    "days_to_next_greenshoe_exercise": "days until next greenshoe exercise announcement",
    "days_since_prev_greenshoe_exercise": "days since previous greenshoe exercise announcement",
    "days_to_next_stabilization_end": "days until stabilization period end",
    "days_since_prev_stabilization_end": "days since previous stabilization period end",
    "days_since_prev_stabilization_start": "days since stabilization period start",
    "is_pre_cornerstone_lockup_5d": "1 in the five days before cornerstone lockup expiry",
    "is_near_cornerstone_lockup_5d": "1 within five days around cornerstone lockup expiry",
    "is_pre_greenshoe_expiry_5d": "1 in the five days before greenshoe expiry",
    "is_near_greenshoe_expiry_5d": "1 within five days around greenshoe expiry",
    "is_near_greenshoe_exercise_5d": "1 within five days around greenshoe exercise",
    "is_pre_stabilization_end_5d": "1 in the five days before stabilization end",
    "is_near_stabilization_end_5d": "1 within five days around stabilization end",
    "is_stabilization_window_active": "1 when the stabilization window is active",
}


def build_system_prompt() -> str:
    """Return the system prompt describing the DSL and the required JSON shape."""
    # Render fields with a one-line gloss when we have one, bare otherwise.
    fields = "\n".join(
        f"    - {name}" + (f"  — {_FIELD_DOCS[name]}" if name in _FIELD_DOCS else "")
        for name in sorted(ALLOWED_FIELDS)
    )

    function_lines = "\n".join(
        f"    - {_FUNCTION_DOCS.get(name, name)}" for name in sorted(ALLOWED_FUNCTIONS)
    )

    return (
        "You are an alpha-research assistant that proposes quantitative "
        "factor hypotheses for a disciplined research loop.\n"
        "\n"
        "Every hypothesis MUST be expressible in the following restricted "
        "factor DSL — any proposal outside this grammar will be rejected "
        "by the compiler and discarded.\n"
        "\n"
        "## Grammar\n"
        "    expression  = term (('+' | '-') term)*\n"
        "    term        = unary (('*' | '/') unary)*\n"
        "    unary       = '-' unary | atom\n"
        "    atom        = NUMBER | FIELD | function_call | '(' expression ')'\n"
        "    function_call = IDENTIFIER '(' arg_list ')'\n"
        "\n"
        f"## Allowed fields\n{fields}\n"
        "\n"
        "## Allowed functions (name, arity, semantics)\n"
        f"{function_lines}\n"
        "\n"
        "## Rules\n"
        "  - Only use the fields and functions listed above.\n"
        "  - Window arguments must be positive integer literals.\n"
        "  - Do not invent new functions, fields, or operators.\n"
        "  - Do not use arithmetic operators outside +, -, *, /.\n"
        "  - Prefer cross-sectional transforms (rank / zscore) on the outer layer.\n"
        "\n"
        "## Required output\n"
        "Reply with a single JSON object matching this schema:\n"
        "{\n"
        '  "proposals": [\n'
        "    {\n"
        '      "expression": "<DSL expression>",\n'
        '      "rationale":  "<one-sentence economic intuition>",\n'
        '      "name":       "<short snake_case identifier, optional>",\n'
        '      "tags":       ["optional", "tags"],\n'
        '      "base_recipe_id": "<promoted recipe id when complement mode is active>"\n'
        "    },\n"
        "    ...\n"
        "  ]\n"
        "}\n"
        "Return only the JSON — no prose, no markdown fences."
    )


def build_user_prompt(request: ProposalRequest) -> str:
    """Render the request into a concrete user-turn prompt."""
    sections: list[str] = [
        f"Research theme: {request.theme}",
        f"Asset class: {request.asset_class.value}",
        f"Produce {request.n_candidates} distinct candidate hypotheses.",
    ]

    if request.related:
        sections.append("\n## Related prior experiments (for context)")
        sections.append(_format_related(request.related))
        sections.append(
            "Avoid proposing expressions that are structurally identical to "
            "the PROMOTE/REFINE entries above, and avoid repeating the "
            "failure modes of the REJECTED entries."
        )

    if request.prior_memory.strip():
        sections.append(
            "\n## What has already been tried (rolling memory)\n"
            f"{request.prior_memory.strip()}\n"
            "Use this to avoid re-proposing near-duplicates of prior "
            "promoted factors and to steer clear of the recent failure "
            "modes above."
        )

    if request.composite_anchors:
        sections.append("\n## Mandatory composite-complement task")
        sections.append(
            "Every proposal must be one NEW scalar DSL component that extends "
            "exactly one promoted basket below. Set base_recipe_id to that "
            "basket's exact recipe id. Do not emit a combine.* expression and "
            "do not repeat or lightly rename an existing component. Prefer a "
            "different economic mechanism and horizon likely to have low "
            "cross-sectional rank correlation with the base basket. The "
            "deterministic harness will evaluate base + component and reject "
            "candidates that fail correlation or incremental RankIC gates."
        )
        for anchor in request.composite_anchors:
            components = ", ".join(f"`{item}`" for item in anchor.recipe.components)
            metrics: list[str] = []
            if anchor.ic is not None:
                metrics.append(f"ic={anchor.ic:+.3f}")
            if anchor.rank_ic is not None:
                metrics.append(f"rank_ic={anchor.rank_ic:+.3f}")
            suffix = f" ({', '.join(metrics)})" if metrics else ""
            sections.append(
                f"  - recipe_id={anchor.recipe.recipe_id} "
                f"method={anchor.recipe.method.value} components=[{components}]{suffix}"
            )

    if request.extra_guidance.strip():
        sections.append(f"\n## Extra guidance\n{request.extra_guidance.strip()}")

    sections.append("\nReturn a single JSON object with the required schema and nothing else.")
    return "\n".join(sections)


def build_repair_prompt(
    dropped: list[tuple[str, str]],
    n_needed: int,
) -> str:
    """User-turn prompt asking the model to replace invalid candidates.

    ``dropped`` is a list of ``(expression, reason)`` pairs so the model can
    see exactly which candidates failed and why.
    """
    lines = [
        (f"{len(dropped)} of the previous candidates failed DSL validation and were discarded:"),
    ]
    for expression, reason in dropped:
        lines.append(f"  - {expression!r}  →  {reason}")
    lines.extend(
        [
            "",
            f"Propose {n_needed} fresh candidates that satisfy the DSL grammar "
            "described in the system prompt.  Do not repeat any of the failing "
            "expressions above.  Return the same JSON shape as before.",
        ]
    )
    return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _format_related(related: list[RelatedExperiment]) -> str:
    """Compact bullet list of prior experiments."""
    lines: list[str] = []
    for item in related:
        metric_bits: list[str] = []
        if item.ic is not None:
            metric_bits.append(f"ic={item.ic:.3f}")
        if item.rank_ic is not None:
            metric_bits.append(f"rank_ic={item.rank_ic:.3f}")
        metric = f" [{', '.join(metric_bits)}]" if metric_bits else ""

        failure = f" failure={item.failure_category}" if item.failure_category else ""

        lines.append(
            f"  - {item.factor_name}: `{item.expression}`"
            f" — decision={item.decision.value}{metric}{failure}"
        )
    return "\n".join(lines)
