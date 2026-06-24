import unittest

from trading_ai.execution.paper_execute_session import _fill_issues, _fill_reconciliation_summary
from trading_ai.research.metrics import estimate_slippage_bps


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
        position_order_results = [
            {
                "action": {"symbol": "QQQ", "quantity": 2.0},
                "final_order": {"status": "partially_filled", "filled_quantity": 1.0},
            }
        ]
        summary = _fill_reconciliation_summary(open_order, position_order_results)
        self.assertFalse(summary["reconciled"])
        self.assertIn("open:unfilled_or_pending", summary["issues"])
        self.assertIn("close:QQQ:partial_fill", summary["issues"])

    def test_summary_reconciled_when_all_filled(self) -> None:
        open_order = {"status": "filled", "filled_quantity": 1.0}
        summary = _fill_reconciliation_summary(open_order, [])
        self.assertTrue(summary["reconciled"])
        self.assertEqual(summary["issues"], [])


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
