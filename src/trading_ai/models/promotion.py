"""Champion/challenger promotion policy for model runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PromotionPolicy:
    min_accuracy_lift: float = 0.02
    min_test_samples: int = 30


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]
    actions: tuple[str, ...]
    challenger_accuracy: float
    baseline_accuracy: float
    accuracy_lift: float
    test_samples: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["actions"] = list(self.actions)
        return payload


def evaluate_promotion(
    *,
    challenger_metrics: Mapping[str, object],
    baseline_metrics: Mapping[str, object],
    policy: PromotionPolicy,
) -> PromotionDecision:
    challenger_accuracy = float(challenger_metrics.get("accuracy", 0.0))
    baseline_accuracy = float(baseline_metrics.get("accuracy", 0.0))
    sample_count = int(float(challenger_metrics.get("sample_count", 0)))
    lift = challenger_accuracy - baseline_accuracy
    reasons: list[str] = []

    if sample_count < policy.min_test_samples:
        reasons.append("insufficient_test_samples")
    if lift < policy.min_accuracy_lift:
        reasons.append("insufficient_accuracy_lift")

    if reasons:
        return PromotionDecision(
            approved=False,
            reasons=tuple(reasons),
            actions=("keep_current_champion",),
            challenger_accuracy=challenger_accuracy,
            baseline_accuracy=baseline_accuracy,
            accuracy_lift=lift,
            test_samples=sample_count,
        )
    return PromotionDecision(
        approved=True,
        reasons=(),
        actions=("eligible_for_paper_challenger",),
        challenger_accuracy=challenger_accuracy,
        baseline_accuracy=baseline_accuracy,
        accuracy_lift=lift,
        test_samples=sample_count,
    )
