"""Pure selection policy for the preregistered ETF sizing campaign."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

_EXPECTED_SCENARIOS = ("1.0x", "1.5x", "2.0x")


@dataclass(frozen=True, slots=True)
class SizingTrialSpec:
    trial_id: str
    max_single_position: float
    target_annual_volatility: float


EXPECTED_TRIAL_SPECS = (
    SizingTrialSpec("T01", 0.02, 0.12),
    SizingTrialSpec("T02", 0.02, 0.08),
    SizingTrialSpec("T03", 0.04, 0.12),
    SizingTrialSpec("T04", 0.04, 0.08),
    SizingTrialSpec("T05", 0.06, 0.12),
    SizingTrialSpec("T06", 0.06, 0.08),
)


@dataclass(frozen=True, slots=True)
class ScenarioEvidence:
    scenario_id: str
    cagr: float
    sharpe: float
    max_drawdown: float
    worst_daily_return: float
    turnover: float
    estimated_costs: float
    trade_count: int
    compound_active_return_vs_baseline: float
    bootstrap_active_return_p05: float
    bootstrap_max_drawdown_p95: float


@dataclass(frozen=True, slots=True)
class TrialEvidence:
    spec: SizingTrialSpec
    train_active_return_vs_baseline_1x: float
    average_gross_exposure: float
    maximum_gross_exposure: float
    maximum_single_position: float
    dsr: float
    scenarios: tuple[ScenarioEvidence, ...]


@dataclass(frozen=True, slots=True)
class CandidateReview:
    trial_id: str
    eligible: bool
    objective: float | None
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CampaignDecision:
    status: str
    selected_trial_id: str | None
    selected_objective: float | None
    baseline_objective: float | None
    reviews: tuple[CandidateReview, ...]
    blockers: tuple[str, ...]


def select_sizing_challenger(
    trials: Iterable[TrialEvidence],
    *,
    validation_sessions: Iterable[str],
    registry_complete: bool,
) -> CampaignDecision:
    """Select zero or one challenger under the frozen validation policy."""

    sessions = _validate_validation_sessions(validation_sessions)
    if len(sessions) != 228:
        raise ValueError("validation_sessions must contain exactly 228 observations")
    if type(registry_complete) is not bool:
        raise TypeError("registry_complete must be a bool")

    evidence = tuple(trials)
    _validate_complete_grid(evidence)
    by_id = {trial.spec.trial_id: trial for trial in evidence}
    baseline = by_id["T01"]
    baseline_scenarios = _scenario_map(baseline)
    baseline_objective = _objective(baseline_scenarios)
    blockers: list[str] = []
    if not registry_complete:
        blockers.append("trial_registry_incomplete")
    if baseline.average_gross_exposure <= 0.0:
        blockers.append("baseline_utilization_zero")

    reviews: list[CandidateReview] = []
    for spec in EXPECTED_TRIAL_SPECS[1:]:
        trial = by_id[spec.trial_id]
        scenarios = _scenario_map(trial)
        reasons = _candidate_rejection_reasons(
            trial,
            scenarios=scenarios,
            baseline=baseline,
            baseline_scenarios=baseline_scenarios,
        )
        objective = _objective(scenarios)
        if objective <= baseline_objective:
            reasons.append("objective_not_strictly_above_baseline")
        if blockers:
            reasons.extend(blockers)
        reviews.append(
            CandidateReview(
                trial_id=spec.trial_id,
                eligible=not reasons,
                objective=objective,
                rejection_reasons=tuple(reasons),
            )
        )

    eligible = [review for review in reviews if review.eligible]
    if not eligible:
        return CampaignDecision(
            status="NO_CANDIDATE",
            selected_trial_id=None,
            selected_objective=None,
            baseline_objective=baseline_objective,
            reviews=tuple(reviews),
            blockers=tuple(blockers),
        )

    def review_rank(review: CandidateReview) -> tuple[float, float, float, str]:
        if review.objective is None:
            raise ValueError("eligible review must have an objective")
        spec = by_id[review.trial_id].spec
        return (
            -review.objective,
            spec.max_single_position,
            spec.target_annual_volatility,
            review.trial_id,
        )

    ranked = sorted(eligible, key=review_rank)
    selected = ranked[0]
    return CampaignDecision(
        status="CHALLENGER_SELECTED_FORWARD_OBSERVATION_ONLY",
        selected_trial_id=selected.trial_id,
        selected_objective=selected.objective,
        baseline_objective=baseline_objective,
        reviews=tuple(reviews),
        blockers=(),
    )


def _candidate_rejection_reasons(
    trial: TrialEvidence,
    *,
    scenarios: dict[str, ScenarioEvidence],
    baseline: TrialEvidence,
    baseline_scenarios: dict[str, ScenarioEvidence],
) -> list[str]:
    reasons: list[str] = []
    utilization = trial.average_gross_exposure
    baseline_utilization = baseline.average_gross_exposure
    if utilization - baseline_utilization < 0.01:
        reasons.append("utilization_absolute_increase_below_0_01")
    if baseline_utilization <= 0.0 or utilization / baseline_utilization < 1.25:
        reasons.append("utilization_ratio_below_1_25")
    if trial.train_active_return_vs_baseline_1x <= 0.0:
        reasons.append("train_active_return_not_positive")
    if any(
        scenario.compound_active_return_vs_baseline <= 0.0
        for scenario in scenarios.values()
    ):
        reasons.append("validation_active_return_not_positive_all_costs")

    base = scenarios["1.0x"]
    baseline_base = baseline_scenarios["1.0x"]
    if base.cagr < 0.05:
        reasons.append("validation_cagr_below_0_05")
    if base.sharpe < 1.0:
        reasons.append("validation_sharpe_below_1_0")
    if base.max_drawdown > 0.10:
        reasons.append("validation_max_drawdown_above_0_10")
    if base.worst_daily_return < -0.02:
        reasons.append("validation_worst_daily_return_below_minus_0_02")
    if base.max_drawdown > baseline_base.max_drawdown + 0.02:
        reasons.append("validation_drawdown_increment_above_0_02")
    if base.sharpe < baseline_base.sharpe - 0.05:
        reasons.append("validation_sharpe_decrement_above_0_05")
    if trial.maximum_gross_exposure > 0.18:
        reasons.append("observed_gross_exposure_above_0_18")
    if trial.maximum_single_position > 0.06:
        reasons.append("observed_single_position_above_0_06")
    if base.turnover > 200.0:
        reasons.append("validation_turnover_above_200")
    if base.estimated_costs > 0.05:
        reasons.append("validation_estimated_costs_above_0_05")
    if base.trade_count < 100:
        reasons.append("validation_trade_count_below_100")
    if base.bootstrap_active_return_p05 <= 0.0:
        reasons.append("bootstrap_active_p05_not_positive")
    if base.bootstrap_max_drawdown_p95 > 0.10:
        reasons.append("bootstrap_drawdown_p95_above_0_10")
    if trial.dsr < 0.95:
        reasons.append("dsr_below_0_95")
    return reasons


def _validate_complete_grid(trials: tuple[TrialEvidence, ...]) -> None:
    if len(trials) != len(EXPECTED_TRIAL_SPECS):
        raise ValueError("trials must contain the complete six-trial grid")
    actual_specs = tuple(sorted((trial.spec for trial in trials), key=lambda spec: spec.trial_id))
    if actual_specs != EXPECTED_TRIAL_SPECS:
        raise ValueError("trials do not match the preregistered sizing grid")
    for trial in trials:
        _validate_trial(trial)


def _validate_trial(trial: TrialEvidence) -> None:
    numbers = (
        trial.spec.max_single_position,
        trial.spec.target_annual_volatility,
        trial.train_active_return_vs_baseline_1x,
        trial.average_gross_exposure,
        trial.maximum_gross_exposure,
        trial.maximum_single_position,
        trial.dsr,
    )
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in numbers):
        raise ValueError(f"{trial.spec.trial_id} contains a non-finite numeric value")
    if any(value < 0.0 for value in numbers[3:6]):
        raise ValueError(f"{trial.spec.trial_id} contains a negative exposure")
    if not 0.0 <= trial.dsr <= 1.0:
        raise ValueError(f"{trial.spec.trial_id} dsr must be in [0, 1]")
    _scenario_map(trial)


def _scenario_map(trial: TrialEvidence) -> dict[str, ScenarioEvidence]:
    if tuple(scenario.scenario_id for scenario in trial.scenarios) != _EXPECTED_SCENARIOS:
        raise ValueError(
            f"{trial.spec.trial_id} scenarios must be ordered as {_EXPECTED_SCENARIOS}"
        )
    for scenario in trial.scenarios:
        numbers = (
            scenario.cagr,
            scenario.sharpe,
            scenario.max_drawdown,
            scenario.worst_daily_return,
            scenario.turnover,
            scenario.estimated_costs,
            scenario.compound_active_return_vs_baseline,
            scenario.bootstrap_active_return_p05,
            scenario.bootstrap_max_drawdown_p95,
        )
        if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in numbers):
            raise ValueError(
                f"{trial.spec.trial_id}/{scenario.scenario_id} contains a non-finite value"
            )
        if type(scenario.trade_count) is not int or scenario.trade_count < 0:
            raise ValueError(
                f"{trial.spec.trial_id}/{scenario.scenario_id} trade_count must be non-negative"
            )
        if (
            scenario.max_drawdown < 0.0
            or scenario.turnover < 0.0
            or scenario.estimated_costs < 0.0
            or scenario.bootstrap_max_drawdown_p95 < 0.0
        ):
            raise ValueError(
                f"{trial.spec.trial_id}/{scenario.scenario_id} has a negative risk metric"
            )
    return {scenario.scenario_id: scenario for scenario in trial.scenarios}


def _objective(scenarios: dict[str, ScenarioEvidence]) -> float:
    return min(
        scenario.cagr / max(scenario.max_drawdown, 0.01)
        for scenario in scenarios.values()
    )


def _validate_validation_sessions(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("validation_sessions must be an iterable of strings")
    sessions = tuple(values)
    if not sessions:
        raise ValueError("validation_sessions must not be empty")
    if sessions[0] != "2025-01-02" or sessions[-1] != "2025-11-28":
        raise ValueError("validation_sessions must match the frozen 2025 boundaries")
    for index, session in enumerate(sessions):
        if not isinstance(session, str) or not session.startswith("2025-"):
            raise ValueError("validation_sessions may contain only 2025 sessions")
        if index and session <= sessions[index - 1]:
            raise ValueError("validation_sessions must be strictly increasing")
    return sessions
