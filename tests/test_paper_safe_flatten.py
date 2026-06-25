import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.execution.paper_risk_state import RiskState, load_risk_state, save_risk_state
from trading_ai.execution.paper_safe_flatten import (
    PaperSafeFlattenOperationalError,
    run_paper_safe_flatten,
)


class _Position:
    def __init__(self, symbol: str, qty: str) -> None:
        self.symbol = symbol
        self.qty = qty
        self.market_value = "100.00"
        self.avg_entry_price = "100.00"
        self.current_price = "100.00"


class FakeFlattenClient:
    def __init__(self, positions: list[_Position]) -> None:
        self._positions = positions
        self.submitted: list[dict[str, Any]] = []

    def list_positions(self) -> list[_Position]:
        return self._positions

    def submit_order(self, **kwargs: object) -> dict[str, Any]:
        self.submitted.append(kwargs)
        return {"id": "broker-order", "status": "accepted", **kwargs}


class PaperSafeFlattenTests(unittest.TestCase):
    def test_requires_both_confirmations(self) -> None:
        with self.assertRaises(PaperSafeFlattenOperationalError):
            run_paper_safe_flatten(confirm_paper=True, confirm_flatten=False)

    def test_flattens_allowlisted_positions(self) -> None:
        client = FakeFlattenClient([_Position("SPY", "1.5"), _Position("QQQ", "2.0")])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "flatten.json"
            md = Path(tmp) / "flatten.md"
            with mock.patch(
                "trading_ai.execution.paper_safe_flatten.build_alpaca_paper_client",
                return_value=client,
            ):
                result = run_paper_safe_flatten(
                    confirm_paper=True,
                    confirm_flatten=True,
                    output=out,
                    markdown_output=md,
                )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.payload["flatten_count"], 2)
        self.assertEqual(result.payload["unflattened_count"], 0)
        # Every submitted order is a sell.
        self.assertTrue(all(order["side"] == "sell" for order in client.submitted))
        self.assertEqual({order["symbol"] for order in client.submitted}, {"SPY", "QQQ"})

    def test_no_positions_is_warn(self) -> None:
        client = FakeFlattenClient([])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "trading_ai.execution.paper_safe_flatten.build_alpaca_paper_client",
            return_value=client,
        ):
            result = run_paper_safe_flatten(
                confirm_paper=True,
                confirm_flatten=True,
                output=Path(tmp) / "f.json",
                markdown_output=Path(tmp) / "f.md",
            )
        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.payload["flatten_count"], 0)

    def test_reset_kill_switch_after_successful_flatten(self) -> None:
        client = FakeFlattenClient([_Position("SPY", "1.0")])
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "risk_state.json"
            save_risk_state(RiskState(kill_switch_active=True, kill_switch_reason="drawdown"), state_path)
            with mock.patch(
                "trading_ai.execution.paper_safe_flatten.build_alpaca_paper_client",
                return_value=client,
            ):
                result = run_paper_safe_flatten(
                    confirm_paper=True,
                    confirm_flatten=True,
                    reset_kill_switch_after=True,
                    risk_state_path=state_path,
                    output=Path(tmp) / "f.json",
                    markdown_output=Path(tmp) / "f.md",
                )
            reloaded = load_risk_state(state_path)
        self.assertTrue(result.payload["kill_switch_reset"])
        self.assertFalse(reloaded.kill_switch_active)


if __name__ == "__main__":
    unittest.main()
