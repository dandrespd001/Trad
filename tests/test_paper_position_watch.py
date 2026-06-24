import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_position_plan import build_position_plan


class FakePositionWatchClient:
    def __init__(self, *, symbol: str) -> None:
        self.symbol = symbol
        self.calls: list[str] = []

    def get_account(self) -> object:
        self.calls.append("get_account")

        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        self.calls.append("list_positions")
        symbol = self.symbol

        class Position:
            qty = "0.25"
            market_value = "50.00"

        Position.symbol = symbol
        return [Position()]

    def get_orders(self, filter: object | None = None) -> list[object]:
        self.calls.append("get_orders")
        return []

    def submit_order(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("position watch must not submit orders")

    def cancel_order_by_id(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("position watch must not cancel orders")


class ExecutingPositionWatchClient:
    """Watch client that allows protective-close execution."""

    def __init__(self, *, symbol: str) -> None:
        self.symbol = symbol
        self.submitted: list[dict[str, object]] = []

    def get_account(self) -> object:
        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        symbol = self.symbol

        class Position:
            qty = "0.25"
            market_value = "50.00"
            avg_entry_price = "200.00"
            current_price = "200.00"

        Position.symbol = symbol
        return [Position()]

    def get_orders(self, filter: object | None = None) -> list[object]:
        return []

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submitted.append(kwargs)
        return {"id": "broker-order", "status": "accepted", **kwargs}

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        return {"id": "broker-order", "client_order_id": client_order_id, "symbol": self.symbol, "status": "accepted"}


class PaperPositionWatchTests(unittest.TestCase):
    def test_parser_defaults_keep_watch_read_only_and_explicit(self) -> None:
        args = build_parser().parse_args(["paper-position-watch", "--session-dir", "reports/tmp/paper_session/latest"])

        self.assertFalse(args.confirm_paper)
        self.assertEqual(args.output, "reports/tmp/paper_position_watch/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_position_watch/latest.md")

    def test_missing_confirm_returns_two_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root)
            with mock.patch(
                "trading_ai.execution.paper_position_watch.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(["paper-position-watch", "--session-dir", str(session_dir)])

        self.assertEqual(exit_code, 2)

    def test_open_position_matching_buy_signal_is_hold(self) -> None:
        client = FakePositionWatchClient(symbol="SPY")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root)
            output = root / "watch.json"
            markdown = root / "watch.md"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.build_alpaca_paper_client",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-position-watch",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--output",
                        str(output),
                        "--markdown-output",
                        str(markdown),
                    ]
                )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["position_plan"]["summary"]["hold_count"], 1)
        self.assertEqual(payload["position_plan"]["actions"][0]["action"], "HOLD")
        self.assertEqual(client.calls, ["get_account", "list_positions", "get_orders"])

    def test_open_position_without_buy_signal_is_close_warning(self) -> None:
        client = FakePositionWatchClient(symbol="QQQ")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root, universe_symbols=("SPY", "QQQ"))
            output = root / "watch.json"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.build_alpaca_paper_client",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-position-watch",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--output",
                        str(output),
                    ]
                )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["position_plan"]["summary"]["close_count"], 1)
        self.assertEqual(payload["position_plan"]["actions"][0]["action"], "CLOSE")
        self.assertEqual(payload["position_plan"]["actions"][0]["symbol"], "QQQ")

    def test_parser_exposes_executable_close_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-position-watch",
                "--session-dir",
                "reports/tmp/paper_session/latest",
                "--confirm-paper",
                "--confirm-dynamic-position-actions",
            ]
        )
        self.assertTrue(args.confirm_dynamic_position_actions)
        self.assertEqual(args.as_of_date, "today")

    def test_confirmed_close_is_executed_as_sell(self) -> None:
        client = ExecutingPositionWatchClient(symbol="QQQ")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root, universe_symbols=("SPY", "QQQ"))
            output = root / "watch.json"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.build_alpaca_paper_client",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-position-watch",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-dynamic-position-actions",
                        "--as-of-date",
                        "2026-06-16",
                        "--output",
                        str(output),
                    ]
                )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["safety"]["orders_submitted"])
        self.assertFalse(payload["safety"]["read_only"])
        self.assertTrue(payload["safety"]["closes_only"])
        self.assertEqual(len(payload["position_order_results"]), 1)
        self.assertTrue(payload["position_order_results"][0]["broker_result"]["accepted"])
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(client.submitted[0]["side"], "sell")
        self.assertEqual(client.submitted[0]["symbol"], "QQQ")

    def test_position_plan_treats_non_scalar_numeric_payloads_as_missing(self) -> None:
        plan = build_position_plan(
            signals=[
                {
                    "timestamp": "2026-06-16",
                    "symbol": "QQQ",
                    "probability": {"bad": "number"},
                    "threshold": [0.5],
                    "action": "hold",
                }
            ],
            selected_signal=None,
            positions=[{"symbol": "QQQ", "quantity": ["0.25"]}],
            signal_quality={"allowed": True},
            paper_notional_usd=1.0,
        )

        action = plan["actions"][0]
        self.assertEqual(action["action"], "CLOSE")
        self.assertIsNone(action["quantity"])
        self.assertIsNone(action["signal"]["probability"])
        self.assertIsNone(action["signal"]["threshold"])


def write_watch_session(root: Path, *, universe_symbols: tuple[str, ...] = ("SPY",)) -> Path:
    session_dir = root / "paper_session"
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    config = root / "universe.yml"
    config.write_text(
        textwrap.dedent(
            f"""
            universe:
              symbols: [{", ".join(universe_symbols)}]
            """
        ),
        encoding="utf-8",
    )
    risk = root / "risk.yml"
    risk.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              paper_notional_usd: 1.0
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    signal = {
        "mode": "dry-run",
        "broker": "alpaca",
        "freshness_allowed": True,
        "preflight": {"allowed": True, "reasons": []},
        "submitted": True,
        "selected_signal": {
            "timestamp": "2026-06-16",
            "symbol": "SPY",
            "probability": 0.93,
            "threshold": 0.5,
            "action": "buy",
        },
        "signals": [
            {
                "timestamp": "2026-06-16",
                "symbol": "SPY",
                "probability": 0.93,
                "threshold": 0.5,
                "action": "buy",
            },
            *[
                {
                    "timestamp": "2026-06-16",
                    "symbol": symbol,
                    "probability": 0.42,
                    "threshold": 0.5,
                    "action": "hold",
                }
                for symbol in universe_symbols
                if symbol != "SPY"
            ],
        ],
        "signal_quality": {"allowed": True, "reasons": [], "buy_signal_count": 1},
        "order_intent": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "signal-spy-20260616",
            "type": "market",
            "time_in_force": "day",
            "notional": 1.0,
        },
        "order_result": {"accepted": True, "status": "dry_run_accepted", "dry_run": True, "reasons": []},
    }
    session = {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "ready_for_paper_review": True,
        "inputs": {"config": str(config), "risk": str(risk), "campaign_report": None, "phase_review": None},
        "paths": {
            "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
            "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
            "audit_report": str(session_dir / "audit" / "paper_audit.json"),
        },
        "summary": {"fail_count": 0},
        "paper_graduation": {"stage": "CANARY", "paper_notional_usd": 1.0, "allowed": True},
    }
    audit = {"ready_for_paper_review": True, "summary": {"fail_count": 0}, "findings": []}
    freshness = {"allowed": True, "reasons": []}
    write_json(session_dir / "session.json", session)
    write_json(session_dir / "audit" / "paper_audit.json", audit)
    write_json(session_dir / "paper" / "paper_signal_order.json", signal)
    write_json(session_dir / "fresh_data" / "freshness.json", freshness)
    return session_dir


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
