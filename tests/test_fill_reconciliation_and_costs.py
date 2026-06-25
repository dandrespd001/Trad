import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from trading_ai.execution.paper_execute_session import (
    _fill_issues,
    _fill_reconciliation_summary,
    _record_clean_day,
    _record_error_day,
)
from trading_ai.execution.paper_risk_state import RiskState, load_risk_state
from trading_ai.research.metrics import estimate_slippage_bps
from trading_ai.risk.policy import RiskLimits


class FillReconciliationTests(unittest.TestCase):
    def test_clean_fill_has_no_issues(self) -> None:
        order = {"status": "filled", "filled_quantity": 1.0}
        self.assertEqual(_fill_issues(order, expected_quantity=1.0), [])

    def test_unfilled_pending_order_flagged(self) -> None:
        order = {"status": "accepted", "filled_quantity": 0.0}
        self.assertIn("unfilled_or_pending", _fill_issues(order, expected_quantity=None))

    def test_partial_fill_flagged(self) -> None:
        order = {"status": "partially_filled", "filled_quantity": 0.4}
        self.assertIn("partial_fill", _fill_issues(order, expected_quantity=1.0))

    def test_summary_namespaces_open_and_close_issues(self) -> None:
        open_order = {"status": "accepted", "filled_quantity": 0.0}
        position_order_results: list[dict[str, Any]] = [
            {
                "action": {"symbol": "QQQ", "quantity": 2.0},
                "final_order": {"status": "partially_filled", "filled_quantity": 1.0},
            }
        ]
        summary = cast(dict[str, Any], _fill_reconciliation_summary(open_order, position_order_results))
        self.assertFalse(summary["reconciled"])
        self.assertIn("open:unfilled_or_pending", summary["issues"])
        self.assertIn("close:QQQ:partial_fill", summary["issues"])

    def test_summary_reconciled_when_all_filled(self) -> None:
        open_order = {"status": "filled", "filled_quantity": 1.0}
        summary = cast(dict[str, Any], _fill_reconciliation_summary(open_order, []))
        self.assertTrue(summary["reconciled"])
        self.assertEqual(summary["issues"], [])
        self.assertFalse(summary["requires_attention"])

    def test_pending_open_alone_does_not_require_attention(self) -> None:
        # An EOD market order accepted while the market is closed is reported but is
        # not, on its own, an error day (it would otherwise spuriously trip safe mode).
        open_order = {"status": "accepted", "filled_quantity": 0.0}
        summary = cast(dict[str, Any], _fill_reconciliation_summary(open_order, []))
        self.assertFalse(summary["reconciled"])
        self.assertIn("open:unfilled_or_pending", summary["issues"])
        self.assertFalse(summary["requires_attention"])

    def test_unresolved_close_requires_attention(self) -> None:
        position_order_results: list[dict[str, Any]] = [
            {
                "action": {"symbol": "QQQ", "quantity": 2.0},
                "final_order": {"status": "partially_filled", "filled_quantity": 1.0},
            }
        ]
        summary = cast(dict[str, Any], _fill_reconciliation_summary(None, position_order_results))
        self.assertTrue(summary["requires_attention"])


class ErrorDayStreakTests(unittest.TestCase):
    LIMITS = RiskLimits(max_drawdown_pct=0.10, max_consecutive_error_days=3)

    def test_error_day_increments_and_latches_at_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            state = RiskState(consecutive_error_days=2)
            updated = _record_error_day(state, risk_state_path=path, risk_limits=self.LIMITS)
            self.assertEqual(updated.consecutive_error_days, 3)
            self.assertTrue(updated.kill_switch_active)
            self.assertIn("consecutive_error_days", updated.kill_switch_reason or "")
            # Persisted to disk so the next paper-daily run reads the latched switch.
            self.assertTrue(load_risk_state(path).kill_switch_active)

    def test_error_day_below_limit_does_not_latch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            updated = _record_error_day(RiskState(), risk_state_path=path, risk_limits=self.LIMITS)
            self.assertEqual(updated.consecutive_error_days, 1)
            self.assertFalse(updated.kill_switch_active)

    def test_disabled_limit_never_latches_on_error_streak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            limits = RiskLimits(max_drawdown_pct=0.10, max_consecutive_error_days=0)
            state = RiskState(consecutive_error_days=99)
            updated = _record_error_day(state, risk_state_path=path, risk_limits=limits)
            self.assertEqual(updated.consecutive_error_days, 100)
            self.assertFalse(updated.kill_switch_active)

    def test_clean_day_resets_streak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            updated = _record_clean_day(RiskState(consecutive_error_days=2), risk_state_path=path)
            self.assertEqual(updated.consecutive_error_days, 0)
            self.assertEqual(load_risk_state(path).consecutive_error_days, 0)


class SlippageEstimateTests(unittest.TestCase):
    def test_buy_above_reference_is_positive_slippage(self) -> None:
        bps = estimate_slippage_bps(fill_price=100.10, reference_price=100.0, side="buy")
        self.assertAlmostEqual(bps, 10.0, places=4)

    def test_buy_below_reference_is_negative_slippage(self) -> None:
        bps = estimate_slippage_bps(fill_price=99.90, reference_price=100.0, side="buy")
        self.assertAlmostEqual(bps, -10.0, places=4)

    def test_sell_below_reference_is_positive_slippage(self) -> None:
        bps = estimate_slippage_bps(fill_price=99.90, reference_price=100.0, side="sell")
        self.assertAlmostEqual(bps, 10.0, places=4)

    def test_zero_reference_is_safe(self) -> None:
        self.assertEqual(estimate_slippage_bps(fill_price=100.0, reference_price=0.0, side="buy"), 0.0)


if __name__ == "__main__":
    unittest.main()
