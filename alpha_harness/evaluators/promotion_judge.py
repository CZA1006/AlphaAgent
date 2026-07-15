"""Promotion judge — decides whether a factor is promoted, refined, or rejected.

Implements the ExperimentJudge protocol. Checks data sufficiency, profile
thresholds, novelty, and margin in sequence. All inputs arrive through the
method signature — no ambient state.
"""

from __future__ import annotations

from alpha_harness.evaluators.novelty import NoveltyEvaluator
from alpha_harness.multiple_testing import (
    DEFAULT_FAMILYWISE_ALPHA,
    bonferroni_z_threshold_multiplier,
)
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    FailureCategory,
    FailureRecord,
    JudgmentDetail,
    PromotionTrail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


class PromotionJudge:
    """Decide whether an evaluated factor should be promoted, refined, or rejected.

    Implements the ``ExperimentJudge`` protocol from ``service.py``.

    Parameters
    ----------
    novelty_evaluator:
        Novelty checker. If None, a default (empty) one is used.
    refine_margin:
        When a metric passes its threshold by less than this relative margin,
        the factor is sent to REFINE instead of PROMOTE. Default 0.20 means
        a metric must exceed its threshold by at least 20% to promote.
    """

    def __init__(
        self,
        novelty_evaluator: NoveltyEvaluator | None = None,
        refine_margin: float = 0.20,
        min_fraction_positive_folds: float = 0.6,
        max_tail_concentration: float = 0.5,
        min_holdout_decay_ratio: float = 0.5,
        multiple_testing_familywise_alpha: float = DEFAULT_FAMILYWISE_ALPHA,
        max_complement_rank_correlation: float = 0.5,
        min_complement_improvement_fraction: float = 0.6,
    ) -> None:
        self._novelty = novelty_evaluator or NoveltyEvaluator()
        self._refine_margin = refine_margin
        self._min_frac_positive = min_fraction_positive_folds
        self._max_tail_concentration = max_tail_concentration
        self._min_holdout_decay = min_holdout_decay_ratio
        self._multiple_testing_alpha = multiple_testing_familywise_alpha
        if not 0 <= max_complement_rank_correlation <= 1:
            raise ValueError("max_complement_rank_correlation must be in [0, 1]")
        if not 0 <= min_complement_improvement_fraction <= 1:
            raise ValueError("min_complement_improvement_fraction must be in [0, 1]")
        self._max_complement_corr = max_complement_rank_correlation
        self._min_complement_improvement = min_complement_improvement_fraction
        bonferroni_z_threshold_multiplier(
            1,
            familywise_alpha=multiple_testing_familywise_alpha,
        )

    def judge(
        self,
        hypothesis: Hypothesis,
        factor: FactorSpec,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> JudgmentDetail:
        """Apply promotion logic and return a :class:`JudgmentDetail`.

        Order of checks:
            1. Data sufficiency (n_periods, n_assets vs profile).
            2. Profile pass (required metrics present and above thresholds).
            3. Novelty (factor not too similar to existing factors).
            4. Margin check (borderline metrics → REFINE rather than PROMOTE).
        """
        profile = request.profile

        # ── 1. Data sufficiency ────────────────────────────────────────
        failure = self._check_data_sufficiency(evaluation, profile)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Insufficient data coverage.",
            )

        # ── 2. Profile pass ────────────────────────────────────────────
        failure = self._check_profile(evaluation, request)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Signal quality below required thresholds.",
            )

        # ── 2b. Multi-horizon sign consistency ─────────────────────────
        failure = self._check_sign_consistency(evaluation)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="IC sign flipped across forecast horizons.",
            )

        # ── 2c. Walk-forward stability ─────────────────────────────────
        failure = self._check_walk_forward_stability(evaluation)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Signal unstable across walk-forward folds.",
            )

        # ── 2d. Composite-complement incremental value (Round 10) ─────
        failure = self._check_complement(evaluation)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Proposed component does not improve its base basket robustly.",
            )

        # ── 2e. Tail concentration (Round 4C) ──────────────────────────
        failure = self._check_tail_concentration(evaluation)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Long-short return concentrated in a handful of days.",
            )

        # ── 2f. Holdout decay (Round 4E) ───────────────────────────────
        failure = self._check_holdout_decay(evaluation)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="In-sample / holdout disagree on rank-IC.",
            )

        # ── 3. Novelty ────────────────────────────────────────────────
        verdict = self._novelty.check_novelty(factor)
        if not verdict.is_novel:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=FailureRecord(
                    category=FailureCategory.DUPLICATE,
                    detail=verdict.detail,
                ),
                notes=f"Too similar to factor {verdict.most_similar_factor_id}.",
            )

        # ── 4. Margin check → REFINE vs PROMOTE ──────────────────────
        if self._is_borderline(evaluation, request):
            return JudgmentDetail(
                decision=ExperimentDecision.REFINE,
                notes="Metrics pass but are within the refine margin.",
            )

        return JudgmentDetail(
            decision=ExperimentDecision.PROMOTE_CANDIDATE,
            notes="All checks passed — promoting.",
            promotion_trail=self._build_trail(evaluation, request),
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _check_data_sufficiency(
        self, evaluation: EvaluationBundle, profile: EvaluationProfile
    ) -> FailureRecord | None:
        """Reject if observed data coverage is below profile minimums."""
        if evaluation.n_periods is not None and evaluation.n_periods < profile.min_periods:
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail=(f"n_periods={evaluation.n_periods} < min_periods={profile.min_periods}"),
            )
        if evaluation.n_assets is not None and evaluation.n_assets < profile.min_assets:
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail=(f"n_assets={evaluation.n_assets} < min_assets={profile.min_assets}"),
            )
        return None

    def _check_profile(
        self,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> FailureRecord | None:
        """Reject if any required metric is missing or below threshold."""
        profile = request.profile
        for metric in profile.required_metrics:
            value = getattr(evaluation, metric.value, None)
            if value is None:
                return FailureRecord(
                    category=FailureCategory.WEAK_SIGNAL,
                    detail=f"Required metric '{metric.value}' is missing.",
                )
            base_threshold = profile.thresholds.get(metric.value)
            threshold = self._session_adjusted_threshold(
                metric.value,
                base_threshold,
                request,
            )
            if threshold is not None and value < threshold:
                pressure = ""
                if threshold != base_threshold:
                    pressure = (
                        f" (base={base_threshold:.4f}, "
                        f"n_proposals_in_session={request.n_proposals_in_session})"
                    )
                return FailureRecord(
                    category=FailureCategory.WEAK_SIGNAL,
                    detail=(
                        f"{metric.value}={value:.4f} < threshold={threshold:.4f}"
                        f"{pressure}"
                    ),
                )
        return None

    def _check_sign_consistency(self, evaluation: EvaluationBundle) -> FailureRecord | None:
        """Reject multi-horizon evaluations whose IC sign isn't robust.

        When the evaluator computed IC across multiple horizons (indicated
        by ``metadata["ic_by_horizon"]``), require at least two horizons to
        share the sign of the primary horizon's IC.  Absent that metadata,
        this check is a no-op — single-horizon behaviour is unchanged.
        """
        horizons = evaluation.metadata.get("ic_by_horizon")
        if not isinstance(horizons, dict) or len(horizons) < 2:
            return None
        same_sign = evaluation.metadata.get("ic_sign_consistent_horizons")
        if not isinstance(same_sign, int):
            return None
        if same_sign < 2:
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=(
                    f"ic_sign_consistent_horizons={same_sign} "
                    f"across {len(horizons)} horizons (need >= 2)."
                ),
            )
        return None

    def _check_walk_forward_stability(self, evaluation: EvaluationBundle) -> FailureRecord | None:
        """Reject when fewer than ``min_fraction_positive_folds`` folds had positive rank-IC.

        Only fires when the bundle was produced by
        :class:`alpha_harness.evaluators.walk_forward.WalkForwardEvaluator`
        (``metadata["walk_forward"]["n_folds"] >= 2``).  Single-fold and
        legacy bundles bypass the check entirely so the judge is
        backwards-compatible.
        """
        wf = evaluation.metadata.get("walk_forward")
        if not isinstance(wf, dict):
            return None
        n_folds = wf.get("n_folds")
        if not isinstance(n_folds, int) or n_folds < 2:
            return None
        frac = wf.get("fraction_positive_rank_ic")
        if not isinstance(frac, int | float):
            return None
        if frac < self._min_frac_positive:
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=(
                    f"fraction_positive_rank_ic={frac:.2f} across {n_folds} "
                    f"folds (need >= {self._min_frac_positive:.2f})."
                ),
            )
        return None

    def _check_tail_concentration(self, evaluation: EvaluationBundle) -> FailureRecord | None:
        """Reject when the long-short return is concentrated in a few days.

        Looks for ``metadata.portfolio.tail_concentration`` (the top-three-day
        return divided by total long-short return).  When that ratio
        exceeds ``max_tail_concentration``, the spread is fragile —
        a single regime change wipes it out.  Bundles without portfolio
        metadata bypass the gate so legacy callers stay unaffected.
        """
        portfolio = evaluation.metadata.get("portfolio")
        if not isinstance(portfolio, dict):
            return None
        tail = portfolio.get("tail_concentration")
        if not isinstance(tail, int | float):
            return None
        if tail > self._max_tail_concentration:
            return FailureRecord(
                category=FailureCategory.OTHER,
                detail=(
                    f"tail_concentration={tail:.2f} > "
                    f"{self._max_tail_concentration:.2f}; top-3 days carry "
                    f"the majority of the total long-short return."
                ),
            )
        return None

    def _check_complement(self, evaluation: EvaluationBundle) -> FailureRecord | None:
        """Require low correlation and persistent incremental RankIC lift."""
        complement = evaluation.metadata.get("complement")
        if not isinstance(complement, dict):
            return None
        max_corr = complement.get("max_abs_rank_correlation")
        if not isinstance(max_corr, int | float):
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail="complement_max_abs_rank_correlation is missing.",
            )
        if max_corr > self._max_complement_corr:
            return FailureRecord(
                category=FailureCategory.OTHER,
                detail=(
                    f"complement_max_abs_rank_correlation={max_corr:.2f} > "
                    f"{self._max_complement_corr:.2f}."
                ),
            )
        fraction = complement.get("fraction_positive_rank_ic_lift")
        if not isinstance(fraction, int | float):
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail="complement_positive_rank_ic_lift_fraction is missing.",
            )
        if fraction < self._min_complement_improvement:
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=(
                    f"complement_positive_rank_ic_lift_fraction={fraction:.2f} < "
                    f"{self._min_complement_improvement:.2f}."
                ),
            )
        holdout = evaluation.metadata.get("holdout")
        holdout_complement = holdout.get("complement") if isinstance(holdout, dict) else None
        holdout_lift = (
            holdout_complement.get("rank_ic_lift")
            if isinstance(holdout_complement, dict)
            else None
        )
        if not isinstance(holdout_lift, int | float):
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail="complement_holdout_rank_ic_lift is missing.",
            )
        if holdout_lift <= 0:
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=f"complement_holdout_rank_ic_lift={holdout_lift:.4f} <= 0.",
            )
        return None

    def _build_trail(
        self,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> PromotionTrail:
        """Snapshot evaluator + judge config that produced the promotion."""
        wf = evaluation.metadata.get("walk_forward")
        wf_dict: dict[str, int | float | str] = {}
        if isinstance(wf, dict):
            # Keep only the immutable knobs — runtime stats (mean_ic, etc.)
            # don't belong in the reproducibility trail.
            for key in ("n_folds", "fold_size_days", "step_days", "embargo_days"):
                if key in wf and isinstance(wf[key], int | float | str):
                    wf_dict[key] = wf[key]
        complement = evaluation.metadata.get("complement")
        selection: dict[str, str | int | float] | None = None
        if isinstance(complement, dict):
            selection = {
                "strategy": "composite_complement",
                "base_recipe_id": str(complement.get("base_recipe_id", "")),
                "max_abs_rank_correlation": self._max_complement_corr,
                "min_positive_rank_ic_lift_fraction": self._min_complement_improvement,
            }
        return PromotionTrail.from_inputs(
            evaluation_request=request,
            judge_thresholds={
                "refine_margin": self._refine_margin,
                "min_fraction_positive_folds": self._min_frac_positive,
                "max_tail_concentration": self._max_tail_concentration,
                "min_holdout_decay_ratio": self._min_holdout_decay,
                "multiple_testing_familywise_alpha": self._multiple_testing_alpha,
            },
            walk_forward=wf_dict,
            selection=selection,
        )

    def _check_holdout_decay(self, evaluation: EvaluationBundle) -> FailureRecord | None:
        """Reject when out-of-sample rank-IC flips sign or decays sharply.

        Triggered only when the evaluator carved out a holdout slice
        (``metadata.holdout`` present with a numeric ``rank_ic``).
        Fails when:

        * the holdout rank-IC sign disagrees with the in-sample sign, or
        * ``holdout.rank_ic / in_sample.rank_ic < min_holdout_decay``.

        Bundles without holdout metadata bypass the gate so legacy
        callers stay unaffected.
        """
        holdout = evaluation.metadata.get("holdout")
        if not isinstance(holdout, dict):
            return None
        ho_rank = holdout.get("rank_ic")
        is_rank = evaluation.rank_ic
        if not isinstance(ho_rank, int | float) or not isinstance(is_rank, int | float):
            return None
        if is_rank == 0:
            # In-sample is dead — let the threshold gate (already run) catch it.
            return None
        # Sign-flip: opposite signs (treat 0 holdout as "decayed to nothing").
        if (is_rank > 0) != (ho_rank > 0):
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=(
                    f"holdout rank_ic={ho_rank:.4f} disagrees in sign with "
                    f"in-sample rank_ic={is_rank:.4f}."
                ),
            )
        ratio = ho_rank / is_rank
        if ratio < self._min_holdout_decay:
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=(
                    f"holdout/in-sample rank_ic ratio={ratio:.2f} < "
                    f"{self._min_holdout_decay:.2f} (in_sample={is_rank:.4f}, "
                    f"holdout={ho_rank:.4f})."
                ),
            )
        return None

    def _session_adjusted_threshold(
        self,
        metric_name: str,
        base_threshold: float | None,
        request: EvaluationRequest,
    ) -> float | None:
        if base_threshold is None or metric_name not in {"ic", "rank_ic"}:
            return base_threshold
        multiplier = bonferroni_z_threshold_multiplier(
            request.n_proposals_in_session,
            familywise_alpha=self._multiple_testing_alpha,
        )
        return base_threshold * multiplier

    def _is_borderline(
        self,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> bool:
        """Check if any required metric is within the refine margin of its threshold."""
        profile = request.profile
        for metric in profile.required_metrics:
            value = getattr(evaluation, metric.value, None)
            threshold = self._session_adjusted_threshold(
                metric.value,
                profile.thresholds.get(metric.value),
                request,
            )
            if value is not None and threshold is not None and threshold > 0:
                margin = (value - threshold) / threshold
                if margin < self._refine_margin:
                    return True
        return False
