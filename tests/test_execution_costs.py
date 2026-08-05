from __future__ import annotations

import unittest
from decimal import Decimal

from trading_ai.execution.execution_costs import (
    ExecutionCostEvidenceError,
    execution_cost_components,
    execution_latency_ms,
    fee_cost_from_net_amount,
    signed_price_cost_bps,
    summarize_signed_bps,
)


class SignedPriceCostTests(unittest.TestCase):
    def test_adverse_and_favorable_costs_keep_their_sign(self) -> None:
        self.assertEqual(
            signed_price_cost_bps(side="buy", benchmark_price="100", execution_price="101"),
            Decimal("100"),
        )
        self.assertEqual(
            signed_price_cost_bps(side="buy", benchmark_price="100", execution_price="99"),
            Decimal("-100"),
        )
        self.assertEqual(
            signed_price_cost_bps(side="sell", benchmark_price="100", execution_price="99"),
            Decimal("100"),
        )
        self.assertEqual(
            signed_price_cost_bps(side="sell", benchmark_price="100", execution_price="101"),
            Decimal("-100"),
        )

    def test_invalid_inputs_never_become_zero_cost(self) -> None:
        for value in (0, -1, "nan", "inf", None, True):
            with self.subTest(value=value), self.assertRaises(ExecutionCostEvidenceError):
                signed_price_cost_bps(side="buy", benchmark_price=value, execution_price=100)
        with self.assertRaises(ExecutionCostEvidenceError):
            signed_price_cost_bps(side="hold", benchmark_price=100, execution_price=100)

    def test_fee_cashflow_sign_is_inverted_without_absolute_value(self) -> None:
        self.assertEqual(fee_cost_from_net_amount("-1.25"), Decimal("1.25"))
        self.assertEqual(fee_cost_from_net_amount("0.10"), Decimal("-0.10"))


class ExecutionCostComponentTests(unittest.TestCase):
    def test_decomposition_adds_exactly_to_signed_shortfall(self) -> None:
        result = execution_cost_components(
            side="buy",
            quantity="10",
            decision_mid="100",
            arrival_mid="100.20",
            fill_mid="100.30",
            fill_price="100.40",
            fee_cost_usd="1",
        )
        self.assertEqual(result.gap_cost_usd, Decimal("2.00"))
        self.assertEqual(result.latency_price_cost_usd, Decimal("1.00"))
        self.assertEqual(result.effective_spread_cost_usd, Decimal("1.00"))
        self.assertEqual(result.price_shortfall_usd, Decimal("4.00"))
        self.assertEqual(result.total_shortfall_usd, Decimal("5.00"))
        self.assertEqual(result.implementation_shortfall_bps, Decimal("50.0000"))

    def test_sell_and_rebate_can_produce_favorable_total_cost(self) -> None:
        result = execution_cost_components(
            side="sell",
            quantity=2,
            decision_mid=100,
            fill_price=101,
            fee_cost_usd=-0.25,
        )
        self.assertEqual(result.price_shortfall_usd, Decimal("-2"))
        self.assertEqual(result.total_shortfall_usd, Decimal("-2.25"))
        self.assertEqual(result.implementation_shortfall_bps, Decimal("-112.5000"))

    def test_unfilled_quantity_requires_horizon_price(self) -> None:
        with self.assertRaises(ExecutionCostEvidenceError):
            execution_cost_components(
                side="buy",
                quantity=1,
                decision_mid=100,
                fill_price=100,
                unfilled_quantity=1,
            )

    def test_partial_decomposition_is_rejected(self) -> None:
        with self.assertRaises(ExecutionCostEvidenceError):
            execution_cost_components(
                side="buy",
                quantity=1,
                decision_mid=100,
                arrival_mid=100,
                fill_price=100,
            )


class LatencyAndSummaryTests(unittest.TestCase):
    def test_latency_requires_ordered_timezone_aware_timestamps(self) -> None:
        self.assertEqual(
            execution_latency_ms(
                submitted_at="2026-07-14T13:30:00+00:00",
                filled_at="2026-07-14T13:30:00.250+00:00",
            ),
            250,
        )
        for submitted, filled in (
            ("2026-07-14T13:30:00", "2026-07-14T13:30:01+00:00"),
            ("2026-07-14T13:30:02+00:00", "2026-07-14T13:30:01+00:00"),
        ):
            with self.subTest(submitted=submitted), self.assertRaises(ExecutionCostEvidenceError):
                execution_latency_ms(submitted_at=submitted, filled_at=filled)

    def test_summary_preserves_favorable_tail_and_reports_p90(self) -> None:
        summary = summarize_signed_bps([-20, -10, 0, 5, 50])
        self.assertEqual(summary["min"], -20.0)
        self.assertEqual(summary["median"], 0.0)
        self.assertEqual(summary["p90"], 50.0)
        self.assertEqual(summary["adverse_n"], 2)
        self.assertEqual(summary["favorable_n"], 2)
        self.assertEqual(summary["zero_n"], 1)

    def test_summary_rejects_non_finite_observation(self) -> None:
        with self.assertRaises(ExecutionCostEvidenceError):
            summarize_signed_bps([1, "nan"])


if __name__ == "__main__":
    unittest.main()
