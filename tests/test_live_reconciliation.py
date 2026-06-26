import unittest

from trading_ai.execution.live_reconciliation import (
    LiveOrderSnapshot,
    LivePosition,
    reconcile_live_positions,
)


class LiveReconciliationTests(unittest.TestCase):
    def test_clean_positions_have_no_divergences(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[LivePosition(symbol="SPY", quantity=1.0)],
            broker_positions=[LivePosition(symbol="SPY", quantity=1.0)],
            open_orders=[],
            allowlist=("SPY",),
        )

        self.assertEqual(report.status, "OK")
        self.assertEqual(report.divergences, [])

    def test_detects_unexpected_position_quantity_mismatch_pending_order_and_allowlist(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[LivePosition(symbol="SPY", quantity=1.0)],
            broker_positions=[LivePosition(symbol="SPY", quantity=2.0), LivePosition(symbol="TSLA", quantity=1.0)],
            open_orders=[LiveOrderSnapshot(symbol="SPY", client_order_id="o-1", status="accepted", age_seconds=30)],
            allowlist=("SPY",),
        )

        codes = [item["code"] for item in report.divergences]
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("quantity_mismatch", codes)
        self.assertIn("unexpected_position", codes)
        self.assertIn("symbol_not_allowlisted", codes)
        self.assertIn("pending_order", codes)

    def test_detects_fill_timeout(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[],
            broker_positions=[],
            open_orders=[LiveOrderSnapshot(symbol="SPY", client_order_id="o-2", status="new", age_seconds=301)],
            allowlist=("SPY",),
            fill_timeout_seconds=300,
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("fill_timeout", [item["code"] for item in report.divergences])


if __name__ == "__main__":
    unittest.main()
