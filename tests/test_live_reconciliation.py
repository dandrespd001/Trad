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

    def test_unknown_flat_broker_position_does_not_crash_or_diverge(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[],
            broker_positions=[LivePosition(symbol="TSLA", quantity=0.0)],
            open_orders=[],
            allowlist=("TSLA",),
        )

        self.assertEqual(report.status, "OK")
        self.assertEqual(report.divergences, [])

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

    def test_duplicate_and_nonfinite_positions_fail_closed(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[
                LivePosition(symbol="SPY", quantity=1.0),
                LivePosition(symbol="spy", quantity=1.0),
                LivePosition(symbol="QQQ", quantity=float("nan")),
            ],
            broker_positions=[
                LivePosition(symbol="SPY", quantity=1.0),
                LivePosition(symbol="SPY", quantity=1.0),
                LivePosition(symbol="QQQ", quantity=float("inf")),
            ],
            open_orders=[],
            allowlist=("SPY", "QQQ"),
        )

        codes = {item["code"] for item in report.divergences}
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("duplicate_expected_position", codes)
        self.assertIn("duplicate_broker_position", codes)
        self.assertIn("expected_position_invalid", codes)
        self.assertIn("broker_position_invalid", codes)

    def test_unknown_or_terminal_open_order_status_fails_closed(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[],
            broker_positions=[],
            open_orders=[
                LiveOrderSnapshot(
                    symbol="SPY",
                    client_order_id="unknown-1",
                    status="mystery",
                ),
                LiveOrderSnapshot(
                    symbol="SPY",
                    client_order_id="terminal-1",
                    status="rejected",
                ),
            ],
            allowlist=("SPY",),
        )

        codes = {item["code"] for item in report.divergences}
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("unknown_order_status", codes)
        self.assertIn("terminal_order_in_open_snapshot", codes)

    def test_invalid_timeout_allowlist_and_order_age_fail_closed(self) -> None:
        report = reconcile_live_positions(
            expected_positions=[],
            broker_positions=[],
            open_orders=[
                LiveOrderSnapshot(
                    symbol="SPY",
                    client_order_id="pending-1",
                    status="accepted",
                    age_seconds=True,
                )
            ],
            allowlist=("SPY", "spy"),
            fill_timeout_seconds=0,
        )

        codes = {item["code"] for item in report.divergences}
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("allowlist_invalid", codes)
        self.assertIn("fill_timeout_config_invalid", codes)
        self.assertIn("open_order_age_invalid", codes)


if __name__ == "__main__":
    unittest.main()
