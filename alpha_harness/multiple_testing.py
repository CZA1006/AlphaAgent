"""Deterministic multiple-hypothesis pressure for factor promotion."""

from __future__ import annotations

from statistics import NormalDist

DEFAULT_FAMILYWISE_ALPHA = 0.05


def bonferroni_z_threshold_multiplier(
    n_proposals: int,
    *,
    familywise_alpha: float = DEFAULT_FAMILYWISE_ALPHA,
) -> float:
    """Map a predeclared hypothesis-family size to a one-sided z multiplier.

    This is not a p-value correction for the observed IC. It is a transparent
    pressure rule that scales the existing IC thresholds by the ratio between
    a Bonferroni one-sided z critical value and the single-test z critical
    value. ``n_proposals=1`` is exactly backwards-compatible.
    """
    if n_proposals < 1:
        raise ValueError("n_proposals must be >= 1")
    if not 0 < familywise_alpha < 0.5:
        raise ValueError("familywise_alpha must be between 0 and 0.5")
    if n_proposals == 1:
        return 1.0
    normal = NormalDist()
    base_z = normal.inv_cdf(1.0 - familywise_alpha)
    family_z = normal.inv_cdf(1.0 - familywise_alpha / n_proposals)
    return family_z / base_z
