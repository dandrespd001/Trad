import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.execution import paper_position_watch as paper_position_watch_module
from trading_ai.execution.alpaca_paper import (
    PaperAccount,
    PaperOrder,
    PaperOrderSnapshot,
    PaperPosition,
)
from trading_ai.execution.paper_position_plan import build_position_plan
from trading_ai.execution.paper_risk_state import RiskState, load_risk_state, save_risk_state


class FakePaperExecutorBrokerClient:
    """High-level, credential-free executor facade used by position-watch tests."""

    def __init__(
        self,
        *,
        symbol: str,
        current_price: float = 200.0,
        avg_entry_price: float = 200.0,
        open_orders: tuple[PaperOrderSnapshot, ...] = (),
    ) -> None:
        self.symbol = symbol
        self.current_price = current_price
        self.avg_entry_price = avg_entry_price
        self.open_orders = open_orders
        self.calls: list[str] = []
        self.submit_calls = 0

    def read_account(self) -> PaperAccount:
        self.calls.append("read_account")
        return PaperAccount(
            account_id="paper-account",
            status="ACTIVE",
            cash=10_000.0,
            equity=10_000.0,
            buying_power=9_999.0,
            last_equity=10_000.0,
        )

    def read_positions(self) -> tuple[PaperPosition, ...]:
        self.calls.append("read_positions")
        return (
            PaperPosition(
                symbol=self.symbol,
                quantity=0.25,
                market_value=50.0,
                avg_entry_price=self.avg_entry_price,
                current_price=self.current_price,
            ),
        )

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        self.calls.append(f"list_orders:{status}")
        return self.open_orders

    def submit_order(self, _order: PaperOrder) -> object:
        self.submit_calls += 1
        raise AssertionError("read-only position watch must not submit orders")


class PaperPositionWatchTests(unittest.TestCase):
    def test_parser_defaults_keep_watch_read_only_and_explicit(self) -> None:
        args = build_parser().parse_args(["paper-position-watch", "--session-dir", "reports/tmp/paper_session/latest"])

        self.assertFalse(args.confirm_paper)
        self.assertEqual(args.risk_state_path, "reports/tmp/paper_risk_state.json")
        self.assertEqual(args.output, "reports/tmp/paper_position_watch/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_position_watch/latest.md")

    def test_missing_confirm_returns_two_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root)
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
                side_effect=AssertionError("client should not be built"),
            ) as constructor:
                exit_code = main(["paper-position-watch", "--session-dir", str(session_dir)])

        self.assertEqual(exit_code, 2)
        constructor.assert_not_called()

    def test_source_has_no_direct_alpaca_builder_or_broker(self) -> None:
        source = Path(paper_position_watch_module.__file__).read_text(encoding="utf-8")

        self.assertNotIn("build_alpaca_paper_client", source)
        self.assertNotIn("AlpacaPaperBroker", source)

    def test_open_position_matching_buy_signal_is_hold(self) -> None:
        client = FakePaperExecutorBrokerClient(symbol="SPY")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root)
            output = root / "watch.json"
            markdown = root / "watch.md"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
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
        self.assertEqual(client.calls, ["read_account", "read_positions", "list_orders:open"])
        self.assertEqual(client.submit_calls, 0)

    def test_open_position_without_buy_signal_is_close_warning(self) -> None:
        client = FakePaperExecutorBrokerClient(symbol="QQQ")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root, universe_symbols=("SPY", "QQQ"))
            output = root / "watch.json"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
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
        self.assertEqual(client.submit_calls, 0)

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

    def test_confirmed_close_is_blocked_before_executor_construction(self) -> None:
        client = FakePaperExecutorBrokerClient(symbol="QQQ")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root, universe_symbols=("SPY", "QQQ"))
            output = root / "watch.json"
            markdown = root / "watch.md"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
                return_value=client,
            ) as constructor:
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
                        "--markdown-output",
                        str(markdown),
                    ]
                )
            payload = read_json(output)

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("server-verified durable reconciliation", payload["reason"])
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        constructor.assert_not_called()
        self.assertEqual(client.submit_calls, 0)

    def test_broker_runtime_error_writes_redacted_error_artifacts(self) -> None:
        class RaisingAccountClient(FakePaperExecutorBrokerClient):
            def read_account(self) -> PaperAccount:
                raise RuntimeError("broker failed token=sk-live-secret")

        client = RaisingAccountClient(symbol="SPY")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root)
            output = root / "watch.json"
            markdown = root / "watch.md"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
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
            json_text = output.read_text(encoding="utf-8")
            markdown_text = markdown.read_text(encoding="utf-8")
            payload = json.loads(json_text)

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("token=[redacted]", payload["reason"])
        self.assertNotIn("sk-live-secret", json_text)
        self.assertNotIn("sk-live-secret", markdown_text)
        self.assertFalse(payload["safety"]["live_trading_allowed"])

    def test_watch_updates_trailing_state_and_reports_missing_protective_orders_without_submitting(self) -> None:
        client = FakePaperExecutorBrokerClient(
            symbol="SPY",
            current_price=112.0,
            avg_entry_price=100.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root, signal_atr=5.0, protective_exit_limits=True)
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(trailing_stops={"SPY": 105.0}), risk_state)
            output = root / "watch.json"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-position-watch",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--risk-state-path",
                        str(risk_state),
                        "--output",
                        str(output),
                    ]
                )
            payload = read_json(output)
            saved_state = load_risk_state(risk_state)

        levels = payload["position_plan"]["actions"][0]["protective_levels"]
        protective_plan = payload.get("protective_order_plan", {})
        protective_actions = protective_plan.get("actions", []) if isinstance(protective_plan, dict) else []
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertTrue(payload["safety"]["read_only"])
        self.assertEqual(saved_state.trailing_stops, {"SPY": 112.0})
        self.assertEqual(levels["stop_loss_price"], 90.0)
        self.assertEqual(levels["take_profit_price"], 120.0)
        self.assertEqual(levels["trailing_stop_price"], 97.0)
        # effective_stop_price is the ratchet: the highest of stop_loss/breakeven/trailing
        # (97.0 trailing here beats the static 90.0 stop_loss), and the protective order
        # plan targets that level so real stop orders track the ratchet.
        self.assertEqual(levels["effective_stop_price"], 97.0)
        self.assertEqual(protective_plan["status"], "WARN")
        self.assertEqual(protective_plan["summary"]["missing_stop_loss_count"], 1)
        self.assertEqual(protective_plan["summary"]["missing_take_profit_count"], 1)
        self.assertEqual(protective_plan["summary"]["review_count"], 2)
        self.assertEqual([action["protection_type"] for action in protective_actions], ["stop_loss", "take_profit"])
        self.assertEqual([action["action"] for action in protective_actions], ["CREATE_PROTECTIVE_ORDER"] * 2)
        self.assertEqual([action["target_price"] for action in protective_actions], [97.0, 120.0])

    def test_watch_reports_stale_existing_protective_orders_without_submitting(self) -> None:
        client = FakePaperExecutorBrokerClient(
            symbol="SPY",
            current_price=112.0,
            avg_entry_price=100.0,
            open_orders=(
                raw_order(symbol="SPY", order_type="stop", stop_price=85.0),
                raw_order(symbol="SPY", order_type="limit", limit_price=120.0),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(root, signal_atr=5.0, protective_exit_limits=True)
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(trailing_stops={"SPY": 105.0}), risk_state)
            output = root / "watch.json"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-position-watch",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--risk-state-path",
                        str(risk_state),
                        "--output",
                        str(output),
                    ]
                )
            payload = read_json(output)

        protective_plan = payload.get("protective_order_plan", {})
        protective_actions = protective_plan.get("actions", []) if isinstance(protective_plan, dict) else []
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertEqual(protective_plan["summary"]["stale_stop_loss_count"], 1)
        self.assertEqual(protective_plan["summary"]["missing_take_profit_count"], 0)
        self.assertEqual(protective_plan["summary"]["review_count"], 1)
        self.assertEqual(protective_actions[0]["action"], "UPDATE_PROTECTIVE_ORDER")
        self.assertEqual(protective_actions[0]["protection_type"], "stop_loss")
        self.assertEqual(protective_actions[0]["current_price"], 85.0)
        # Target tracks effective_stop_price (the ratchet), which here is the
        # trailing stop (97.0) since it is higher than the static stop_loss (90.0).
        self.assertEqual(protective_actions[0]["target_price"], 97.0)

    def test_protective_order_plan_targets_breakeven_ratchet_when_armed(self) -> None:
        # entry 100, ATR 5. stop_loss=90, trailing_stop=93 (high 108, 3*ATR=15),
        # breakeven arms at high>=105 (1*ATR) with buffer 1*ATR => breakeven_stop=105,
        # the highest of the three -- the protective order must target 105, not the
        # static stop_loss (90).
        client = FakePaperExecutorBrokerClient(
            symbol="SPY",
            current_price=108.0,
            avg_entry_price=100.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_watch_session(
                root,
                signal_atr=5.0,
                protective_exit_limits=True,
                breakeven_trigger_atr_mult=1.0,
                breakeven_buffer_atr_mult=1.0,
            )
            output = root / "watch.json"
            with mock.patch(
                "trading_ai.execution.paper_position_watch.PaperExecutorBrokerClient",
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

        levels = payload["position_plan"]["actions"][0]["protective_levels"]
        protective_plan = payload.get("protective_order_plan", {})
        protective_actions = protective_plan.get("actions", []) if isinstance(protective_plan, dict) else []
        self.assertEqual(exit_code, 0)
        self.assertEqual(levels["stop_loss_price"], 90.0)
        self.assertEqual(levels["trailing_stop_price"], 93.0)
        self.assertEqual(levels["breakeven_stop_price"], 105.0)
        self.assertEqual(levels["effective_stop_price"], 105.0)
        stop_actions = [action for action in protective_actions if action["protection_type"] == "stop_loss"]
        self.assertEqual(len(stop_actions), 1)
        self.assertEqual(stop_actions[0]["target_price"], 105.0)

    def test_parser_defaults_unchanged_by_breakeven_feature(self) -> None:
        args = build_parser().parse_args(["paper-position-watch", "--session-dir", "reports/tmp/paper_session/latest"])

        self.assertFalse(args.confirm_paper)
        self.assertEqual(args.risk_state_path, "reports/tmp/paper_risk_state.json")
        self.assertEqual(args.output, "reports/tmp/paper_position_watch/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_position_watch/latest.md")

    def test_position_plan_treats_non_scalar_numeric_payloads_as_missing(self) -> None:
        plan = cast(
            dict[str, Any],
            build_position_plan(
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
            ),
        )

        action = plan["actions"][0]
        self.assertEqual(action["action"], "CLOSE")
        self.assertIsNone(action["quantity"])
        self.assertIsNone(action["signal"]["probability"])
        self.assertIsNone(action["signal"]["threshold"])


def write_watch_session(
    root: Path,
    *,
    universe_symbols: tuple[str, ...] = ("SPY",),
    signal_atr: float | None = None,
    protective_exit_limits: bool = False,
    breakeven_trigger_atr_mult: float = 0.0,
    breakeven_buffer_atr_mult: float = 0.0,
) -> Path:
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
            f"""
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              paper_notional_usd: 1.0
              stop_loss_atr_mult: {2.0 if protective_exit_limits else 0.0}
              take_profit_atr_mult: {4.0 if protective_exit_limits else 0.0}
              trailing_atr_mult: {3.0 if protective_exit_limits else 0.0}
              breakeven_trigger_atr_mult: {breakeven_trigger_atr_mult}
              breakeven_buffer_atr_mult: {breakeven_buffer_atr_mult}
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
            "atr": signal_atr,
        },
        "signals": [
            {
                "timestamp": "2026-06-16",
                "symbol": "SPY",
                "probability": 0.93,
                "threshold": 0.5,
                "action": "buy",
                "atr": signal_atr,
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def raw_order(
    *,
    symbol: str,
    order_type: str,
    stop_price: float | None = None,
    limit_price: float | None = None,
    qty: float = 0.25,
) -> PaperOrderSnapshot:
    return PaperOrderSnapshot(
        order_id="order-id",
        client_order_id="protective-order",
        symbol=symbol,
        side="sell",
        order_type=order_type,
        time_in_force="day",
        status="accepted",
        notional=None,
        quantity=qty,
        filled_quantity=0.0,
        filled_avg_price=None,
        submitted_at="",
        created_at="",
        updated_at="",
        expires_at="",
        stop_price=stop_price,
        limit_price=limit_price,
    )


if __name__ == "__main__":
    unittest.main()
