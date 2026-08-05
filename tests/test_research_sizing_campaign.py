import math
import unittest
from dataclasses import replace
from datetime import date, timedelta

from trading_ai.research.sizing_campaign import (
    EXPECTED_TRIAL_SPECS,
    ScenarioEvidence,
    TrialEvidence,
    select_sizing_challenger,
)


def sessions() -> tuple[str, ...]:
    current = date(2025, 1, 3)
    end = date(2025, 11, 28)
    middle: list[str] = []
    while len(middle) < 226 and current < end:
        if current.weekday() < 5:
            middle.append(current.isoformat())
        current += timedelta(days=1)
    return ("2025-01-02", *middle, "2025-11-28")


def scenario(
    scenario_id: str,
    *,
    cagr: float = 0.10,
    active: float = 0.03,
    bootstrap_p05: float = 0.01,
) -> ScenarioEvidence:
    return ScenarioEvidence(
        scenario_id=scenario_id,
        cagr=cagr,
        sharpe=1.5,
        max_drawdown=0.05,
        worst_daily_return=-0.01,
        turnover=20.0,
        estimated_costs=0.01,
        trade_count=150,
        compound_active_return_vs_baseline=active,
        bootstrap_active_return_p05=bootstrap_p05,
        bootstrap_max_drawdown_p95=0.07,
    )


def evidence(index: int) -> TrialEvidence:
    spec = EXPECTED_TRIAL_SPECS[index]
    if index == 0:
        utilization = 0.04
        scenarios = tuple(
            scenario(name, cagr=0.06, active=0.0, bootstrap_p05=0.0)
            for name in ("1.0x", "1.5x", "2.0x")
        )
        train_active = 0.0
    else:
        utilization = 0.08 + index * 0.005
        scenarios = tuple(
            scenario(name, cagr=0.09 + index * 0.005)
            for name in ("1.0x", "1.5x", "2.0x")
        )
        train_active = 0.02
    return TrialEvidence(
        spec=spec,
        train_active_return_vs_baseline_1x=train_active,
        average_gross_exposure=utilization,
        maximum_gross_exposure=min(0.18, spec.max_single_position * 3),
        maximum_single_position=spec.max_single_position,
        dsr=0.97 if index else 0.0,
        scenarios=scenarios,
    )


class SizingCampaignTests(unittest.TestCase):
    def test_selects_exactly_one_best_eligible_candidate(self) -> None:
        decision = select_sizing_challenger(
            tuple(evidence(index) for index in range(6)),
            validation_sessions=sessions(),
            registry_complete=True,
        )

        self.assertEqual(decision.status, "CHALLENGER_SELECTED_FORWARD_OBSERVATION_ONLY")
        self.assertEqual(decision.selected_trial_id, "T06")
        self.assertEqual(sum(review.eligible for review in decision.reviews), 5)

    def test_returns_no_candidate_when_every_candidate_fails_one_frozen_gate(self) -> None:
        trials = list(evidence(index) for index in range(6))
        for index in range(1, 6):
            base = trials[index].scenarios[0]
            trials[index] = replace(
                trials[index],
                scenarios=(replace(base, bootstrap_active_return_p05=0.0), *trials[index].scenarios[1:]),
            )

        decision = select_sizing_challenger(
            trials,
            validation_sessions=sessions(),
            registry_complete=True,
        )

        self.assertEqual(decision.status, "NO_CANDIDATE")
        self.assertIsNone(decision.selected_trial_id)
        self.assertTrue(
            all(
                "bootstrap_active_p05_not_positive" in review.rejection_reasons
                for review in decision.reviews
            )
        )

    def test_incomplete_registry_blocks_selection(self) -> None:
        decision = select_sizing_challenger(
            tuple(evidence(index) for index in range(6)),
            validation_sessions=sessions(),
            registry_complete=False,
        )

        self.assertEqual(decision.status, "NO_CANDIDATE")
        self.assertEqual(decision.blockers, ("trial_registry_incomplete",))

    def test_rejects_missing_changed_or_duplicate_grid(self) -> None:
        complete = tuple(evidence(index) for index in range(6))
        cases = (
            complete[:-1],
            (*complete[:-1], complete[0]),
            (
                replace(
                    complete[0],
                    spec=replace(complete[0].spec, max_single_position=0.03),
                ),
                *complete[1:],
            ),
        )
        for trials in cases:
            with self.subTest(trials=len(trials)), self.assertRaises(ValueError):
                select_sizing_challenger(
                    trials,
                    validation_sessions=sessions(),
                    registry_complete=True,
                )

    def test_rejects_nonfinite_evidence_and_wrong_scenario_order(self) -> None:
        complete = list(evidence(index) for index in range(6))
        invalid = replace(complete[2], dsr=math.nan)
        with self.assertRaisesRegex(ValueError, "non-finite"):
            select_sizing_challenger(
                (*complete[:2], invalid, *complete[3:]),
                validation_sessions=sessions(),
                registry_complete=True,
            )

        wrong_order = replace(complete[2], scenarios=tuple(reversed(complete[2].scenarios)))
        with self.assertRaisesRegex(ValueError, "scenarios must be ordered"):
            select_sizing_challenger(
                (*complete[:2], wrong_order, *complete[3:]),
                validation_sessions=sessions(),
                registry_complete=True,
            )

    def test_rejects_any_non_2025_or_misaligned_validation_sessions(self) -> None:
        complete = tuple(evidence(index) for index in range(6))
        cases = (
            sessions()[:-1],
            (*sessions()[:-1], "2026-01-02"),
            tuple(reversed(sessions())),
        )
        for invalid_sessions in cases:
            with self.subTest(last=invalid_sessions[-1]), self.assertRaises(ValueError):
                select_sizing_challenger(
                    complete,
                    validation_sessions=invalid_sessions,
                    registry_complete=True,
                )


if __name__ == "__main__":
    unittest.main()
