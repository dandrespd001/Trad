"""Tests for the sleeve-position-watch surveillance command (Sprint M9)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trading_ai.cli import main
from trading_ai.execution.sleeve_position_watch import (
    SCHEMA_VERSION,
    WARN_FRACTION,
    run_sleeve_position_watch,
)


def _make_position(
    *,
    symbol: str,
    quantity: float,
    market_value: float,
    unrealized_pl: float | None = None,
    unrealized_plpc: float | None = None,
    avg_entry_price: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        quantity=quantity,
        market_value=market_value,
        avg_entry_price=avg_entry_price if avg_entry_price is not None else 0.0,
        current_price=0.0,
        unrealized_pl=unrealized_pl,
        unrealized_plpc=unrealized_plpc,
    )


def _make_closed_order(
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    filled_quantity: float,
    filled_avg_price: float | None,
    updated_at: str,
    notional: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=client_order_id + "-id",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type="market",
        time_in_force="day",
        status="filled",
        notional=notional,
        quantity=filled_quantity,
        filled_quantity=filled_quantity,
        filled_avg_price=filled_avg_price,
        submitted_at=updated_at,
        created_at=updated_at,
        updated_at=updated_at,
        expires_at=updated_at,
    )


class _FakeBroker:
    """Duck-typed broker with the read-only surface used by the watch."""

    def __init__(
        self,
        *,
        positions: list[Any] | None = None,
        closed_orders: list[Any] | None = None,
        equity: float = 100_000.0,
        last_equity: float = 100_000.0,
        raise_on_list_orders: Exception | None = None,
    ) -> None:
        self._positions = list(positions or [])
        self._closed_orders = list(closed_orders or [])
        self._equity = equity
        self._last_equity = last_equity
        self._raise_on_list_orders = raise_on_list_orders

    def read_positions(self) -> tuple[Any, ...]:
        return tuple(self._positions)

    def read_account(self) -> SimpleNamespace:
        return SimpleNamespace(equity=self._equity, last_equity=self._last_equity)

    def list_orders(self, *, status: str = "open") -> tuple[Any, ...]:
        if self._raise_on_list_orders is not None and status == "closed":
            raise self._raise_on_list_orders
        return tuple(self._closed_orders)


class _HelperBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.as_of = date(2026, 7, 10)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _highwater_path(self, value: float | None) -> Path:
        path = self.tmp_path / "equity_highwater.json"
        if value is not None:
            path.write_text(json.dumps({"high_water_equity": value}), encoding="utf-8")
        return path


class TwoPositionsTwoFillsTests(_HelperBase):
    def test_payload_lists_positions_and_one_sleeve_fill_with_telegram_artifact(self) -> None:
        positions = [
            _make_position(symbol="IWM", quantity=5.0, market_value=1100.0, unrealized_pl=20.0, unrealized_plpc=0.0185),
            _make_position(symbol="XLV", quantity=3.0, market_value=480.0, unrealized_pl=-5.0, unrealized_plpc=-0.0103),
        ]
        as_of_iso = self.as_of.isoformat()
        fills_today = [
            _make_closed_order(
                client_order_id=f"sleeve-{as_of_iso}-IWM-buy",
                symbol="IWM",
                side="buy",
                filled_quantity=5.0,
                filled_avg_price=216.0,
                updated_at=f"{as_of_iso}T13:30:00Z",
                notional=1080.0,
            ),
            _make_closed_order(
                client_order_id="sleeve-2026-07-09-OTHER-sell",  # different day — out
                symbol="IWM",
                side="sell",
                filled_quantity=1.0,
                filled_avg_price=200.0,
                updated_at="2026-07-09T13:30:00Z",
            ),
            _make_closed_order(
                client_order_id="random-order-12345",  # not sleeve-
                symbol="IWM",
                side="buy",
                filled_quantity=1.0,
                filled_avg_price=215.0,
                updated_at=f"{as_of_iso}T14:00:00Z",
            ),
        ]
        broker = _FakeBroker(
            positions=positions,
            closed_orders=fills_today,
            equity=100_580.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "position_watch.json"
        telegram = self.tmp_path / "telegram_watch.json"

        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            telegram_artifact=telegram,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(len(payload["positions"]), 2)
        self.assertEqual(len(payload["fills_today"]), 1)
        only_fill = payload["fills_today"][0]
        self.assertEqual(only_fill["client_order_id"], f"sleeve-{as_of_iso}-IWM-buy")
        self.assertEqual(only_fill["symbol"], "IWM")
        self.assertEqual(only_fill["side"], "buy")
        self.assertEqual(only_fill["filled_qty"], 5.0)
        self.assertEqual(only_fill["notional"], 1080.0)
        self.assertEqual(payload["incidents"], [])
        self.assertEqual(payload["safety"], {"read_only": True, "orders_submitted": False})

        telegram_payload = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertEqual(telegram_payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(telegram_payload["as_of_date"], as_of_iso)
        self.assertEqual(telegram_payload["status"], "OK")
        self.assertEqual(telegram_payload["safety"]["paper_only"], True)
        message = telegram_payload["message"]
        self.assertIn("Posiciones paper", message)
        self.assertIn("IWM", message)
        self.assertIn("Fills hoy", message)


class DailyLossWarnTests(_HelperBase):
    def test_daily_loss_beyond_75pct_of_limit_warns(self) -> None:
        # Risk limits in configs/risk.yml: max_daily_loss_pct=0.02 → WARN at -1.5%.
        # equity vs last_equity = 98_400 vs 100_000 = -1.6% → WARN.
        positions = [
            _make_position(symbol="IWM", quantity=5.0, market_value=1100.0),
        ]
        broker = _FakeBroker(
            positions=positions,
            closed_orders=[],
            equity=98_400.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        self.assertEqual(result.status, "WARN")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("approaching_kill_switch:daily_loss", payload["blockers"])
        self.assertIn("approaching_kill_switch:daily_loss", payload["incidents"])
        self.assertEqual(payload["account_risk"]["daily_pnl_pct"], -0.016)


class DrawdownWarnTests(_HelperBase):
    def test_drawdown_beyond_75pct_of_limit_warns(self) -> None:
        # max_drawdown_pct=0.10 → WARN at 7.5%. high_water=100_000, equity=92_000 → 8% → WARN.
        positions = [
            _make_position(symbol="IWM", quantity=5.0, market_value=1100.0),
        ]
        broker = _FakeBroker(
            positions=positions,
            closed_orders=[],
            equity=92_000.0,
            last_equity=92_000.0,
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(100_000.0),
        )
        self.assertEqual(result.status, "WARN")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("approaching_kill_switch:drawdown", payload["blockers"])
        # 92_000 vs 100_000 → 0.08 → beyond 0.10 * 0.75 = 0.075 → WARN.
        self.assertEqual(payload["account_risk"]["current_drawdown_pct"], 0.08)


class ListOrdersFailureTests(_HelperBase):
    def test_list_orders_failure_records_incident_but_still_lists_positions(self) -> None:
        positions = [
            _make_position(symbol="IWM", quantity=5.0, market_value=1100.0),
        ]
        broker = _FakeBroker(
            positions=positions,
            closed_orders=[],
            raise_on_list_orders=RuntimeError("simulated broker outage"),
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        # No positions lost, but orders_list_failed incident → WARN.
        self.assertEqual(result.status, "WARN")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["positions"]), 1)
        self.assertEqual(payload["fills_today"], [])
        self.assertTrue(
            any(incident.startswith("orders_list_failed") for incident in payload["incidents"])
        )


class EmptyPortfolioTests(_HelperBase):
    def test_no_positions_no_fills_ok_and_message_says_so(self) -> None:
        broker = _FakeBroker(positions=[], closed_orders=[])
        output = self.tmp_path / "position_watch.json"
        telegram = self.tmp_path / "telegram_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            telegram_artifact=telegram,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["positions"], [])
        self.assertEqual(payload["fills_today"], [])
        telegram_payload = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertIn("sin posiciones abiertas", telegram_payload["message"])


class CliTests(_HelperBase):
    def test_cli_real_paper_without_confirm_returns_error(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        argv = ["sleeve-position-watch", "--real-paper"]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(argv)
        self.assertEqual(exit_code, 2)
        self.assertIn("--real-paper requires --confirm-paper", stderr.getvalue())

    def test_cli_without_real_paper_returns_error(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        argv = ["sleeve-position-watch"]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(argv)
        self.assertEqual(exit_code, 2)
        self.assertIn("--real-paper is required", stderr.getvalue())


class BrokerNoneTests(_HelperBase):
    def test_no_broker_blocks_cleanly(self) -> None:
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=None,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        self.assertEqual(result.status, "BLOCKED")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("broker_unavailable", payload["incidents"])
        self.assertEqual(payload["positions"], [])


class TelegramMessageTests(_HelperBase):
    def test_message_includes_equity_dd_pnl_and_aviso_line(self) -> None:
        positions = [
            _make_position(symbol="BTCUSD", quantity=0.05, market_value=3000.0, unrealized_pl=-50.0, unrealized_plpc=-0.0164),
        ]
        broker = _FakeBroker(
            positions=positions,
            closed_orders=[],
            equity=98_400.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "position_watch.json"
        telegram = self.tmp_path / "telegram_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            telegram_artifact=telegram,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        self.assertEqual(result.status, "WARN")
        telegram_payload = json.loads(telegram.read_text(encoding="utf-8"))
        message = telegram_payload["message"]
        self.assertIn("BTCUSD", message)
        self.assertIn("equity $98400.00", message)
        self.assertIn("AVISO: approaching_kill_switch:daily_loss", message)
        self.assertEqual(telegram_payload["status"], "WARN")
        # Generated_at should not appear (the telegram artifact does not include it).
        self.assertNotIn("generated_at", telegram_payload)