import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.live_reconciliation import LivePosition
from trading_ai.execution.live_safe_flatten import run_live_safe_flatten


class FakeLiveBroker:
    def __init__(self, positions: list[LivePosition]) -> None:
        self._positions = positions

    def read_positions(self) -> list[LivePosition]:
        return self._positions


class LiveSafeFlattenTests(unittest.TestCase):
    def test_missing_reviewer_or_reason_blocks_without_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_live_safe_flatten(
                as_of_date="2026-06-16",
                broker=FakeLiveBroker([LivePosition(symbol="SPY", quantity=1.0)]),
                allowlist=("SPY",),
                reviewer="",
                reason="",
                output_dir=Path(tmp),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("human_review_required", result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_generates_simulated_close_orders_without_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_live_safe_flatten(
                as_of_date="2026-06-16",
                broker=FakeLiveBroker(
                    [LivePosition(symbol="SPY", quantity=1.5), LivePosition(symbol="TLT", quantity=-2.0)]
                ),
                allowlist=("SPY", "TLT"),
                reviewer="ops",
                reason="breaker tripped rehearsal",
                output_dir=Path(tmp),
            )
            payload = json.loads(result.output_path.read_text(encoding="utf-8"))
            markdown = result.markdown_path.read_text(encoding="utf-8")

        self.assertEqual(result.status, "DRY_RUN_READY")
        self.assertEqual(payload["flatten_count"], 2)
        self.assertEqual(payload["flatten_orders"][0]["side"], "sell")
        self.assertEqual(payload["flatten_orders"][1]["side"], "buy")
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertIn("Live Safe Flatten", markdown)

    def test_blocks_non_allowlisted_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_live_safe_flatten(
                as_of_date="2026-06-16",
                broker=FakeLiveBroker([LivePosition(symbol="TSLA", quantity=1.0)]),
                allowlist=("SPY",),
                reviewer="ops",
                reason="breaker tripped rehearsal",
                output_dir=Path(tmp),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("symbol_not_allowlisted:TSLA", result.payload["blockers"])
        self.assertEqual(result.payload["flatten_count"], 0)


if __name__ == "__main__":
    unittest.main()
