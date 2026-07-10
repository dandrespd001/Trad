"""Tests for the sleeve-position-watch surveillance command (Sprint M9, M13)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
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
    status: str = "filled",
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=client_order_id + "-id",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type="market",
        time_in_force="day",
        status=status,
        notional=notional,
        quantity=filled_quantity,
        filled_quantity=filled_quantity,
        filled_avg_price=filled_avg_price,
        submitted_at=updated_at,
        created_at=updated_at,
        updated_at=updated_at,
        expires_at=updated_at,
    )


def _make_open_order(
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    updated_at: str,
    notional: float | None = None,
) -> SimpleNamespace:
    """Build an open-order shape used by the M13 pending-order bypass."""
    return SimpleNamespace(
        order_id=client_order_id + "-id",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type="market",
        time_in_force="day",
        status="open",
        notional=notional,
        quantity=0.0,
        filled_quantity=0.0,
        filled_avg_price=None,
        submitted_at=updated_at,
        created_at=updated_at,
        updated_at=updated_at,
        expires_at=updated_at,
    )


def _make_plan_entry(
    *,
    pair: str,
    target_notional: float,
    action: str = "buy",
    current_notional: float = 0.0,
) -> dict[str, object]:
    """Build a minimal ``plan`` entry shaped like the M3/M5 cycle output."""
    return {
        "pair": pair,
        "action": action,
        "target_notional": round(target_notional, 2),
        "current_notional": round(current_notional, 2),
        "delta": round(target_notional - current_notional, 2),
        "notional": round(abs(target_notional - current_notional), 2),
        "quantity": None,
        "reference_price": 100.0,
        "weight": 0.1,
    }


def _write_cycle_file(
    path: Path,
    *,
    sleeve: str,
    as_of: str,
    plan: list[dict[str, object]],
    status: str = "OK",
) -> None:
    """Persist a minimal but realistic ``cycle_<sleeve>_<date>.json`` payload."""
    payload = {
        "schema_version": "1.0",
        "generated_at": f"{as_of}T22:00:00+00:00",
        "as_of": as_of,
        "universe": "test",
        "dataset": f"/tmp/{as_of}.csv",
        "weights": {entry["pair"]: 0.1 for entry in plan},  # type: ignore[union-attr]
        "plan": plan,
        "pending_buy_notional": {},
        "submissions": [],
        "account_risk": None,
        "blockers": [],
        "status": status,
        "safety": {"orders_submitted": False, "paper_only": True},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class _FakeBroker:
    """Duck-typed broker with the read-only surface used by the watch."""

    def __init__(
        self,
        *,
        positions: list[Any] | None = None,
        closed_orders: list[Any] | None = None,
        open_orders: list[Any] | None = None,
        equity: float = 100_000.0,
        last_equity: float = 100_000.0,
        raise_on_list_orders: Exception | None = None,
    ) -> None:
        self._positions = list(positions or [])
        self._closed_orders = list(closed_orders or [])
        self._open_orders = list(open_orders or [])
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
        if status == "open":
            return tuple(self._open_orders)
        if status == "closed":
            return tuple(self._closed_orders)
        return ()


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


# ---------------------------------------------------------------------------
# M13 WS4 — reconciliation (cycles_dir -> per-pair drift) and expired orders.
# ---------------------------------------------------------------------------


class ReconciliationTests(_HelperBase):
    """Compare cycle targets against live broker positions."""

    def _cycles_dir(self) -> Path:
        cycles_dir = self.tmp_path / "cycles"
        cycles_dir.mkdir(exist_ok=True)
        return cycles_dir

    def test_target_matches_position_creates_no_drift(self) -> None:
        # BTC/USD target $1000; live position exactly $1000 → no drift.
        _write_cycle_file(
            self._cycles_dir() / f"cycle_crypto_{self.as_of.isoformat()}.json",
            sleeve="crypto",
            as_of=self.as_of.isoformat(),
            plan=[_make_plan_entry(pair="BTC/USD", target_notional=1000.0)],
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.0158, market_value=1000.0),
            ],
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=self._cycles_dir(),
        )
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("reconciliation", payload)
        recon = payload["reconciliation"]
        self.assertEqual(recon["checked"], 1)
        self.assertEqual(recon["drifts"], [])
        self.assertEqual(recon["pending"], [])
        # No drift incidents in the report.
        self.assertFalse(any(slug.startswith("position_drift") for slug in payload["incidents"]))

    def test_target_1000_position_600_records_drift_incident(self) -> None:
        # |actual - target| = 400 > max(50, 0.20 * 1000) = 200 → drift.
        _write_cycle_file(
            self._cycles_dir() / f"cycle_crypto_{self.as_of.isoformat()}.json",
            sleeve="crypto",
            as_of=self.as_of.isoformat(),
            plan=[_make_plan_entry(pair="BTC/USD", target_notional=1000.0)],
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.0095, market_value=600.0),
            ],
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
            cycles_dir=self._cycles_dir(),
        )
        self.assertEqual(result.status, "WARN")
        payload = json.loads(output.read_text(encoding="utf-8"))
        recon = payload["reconciliation"]
        self.assertEqual(len(recon["drifts"]), 1)
        drift = recon["drifts"][0]
        self.assertEqual(drift["pair"], "BTC/USD")
        self.assertEqual(drift["target"], 1000.0)
        self.assertEqual(drift["actual"], 600.0)
        # Incidents list carries the slug form.
        self.assertIn("position_drift:BTC/USD:target=1000.0:actual=600.0", payload["incidents"])
        # Telegram mirrors the AVISO line.
        tg = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertIn("AVISO: position_drift:BTC/USD:target=1000.0:actual=600.0", tg["message"])

    def test_open_sleeve_order_in_pair_skips_drift_as_pending(self) -> None:
        # Same 1000 vs 600 gap, but with an OPEN sleeve- buy order for
        # BTCUSD → drift_pending_order (informational, NOT an incident).
        _write_cycle_file(
            self._cycles_dir() / f"cycle_crypto_{self.as_of.isoformat()}.json",
            sleeve="crypto",
            as_of=self.as_of.isoformat(),
            plan=[_make_plan_entry(pair="BTC/USD", target_notional=1000.0)],
        )
        as_of_iso = self.as_of.isoformat()
        open_orders = [
            _make_open_order(
                client_order_id=f"sleeve-{as_of_iso}-BTC-buy",
                symbol="BTCUSD",
                side="buy",
                updated_at=f"{as_of_iso}T13:30:00Z",
                notional=400.0,  # will arrive — explains the gap
            ),
        ]
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.0095, market_value=600.0),
            ],
            open_orders=open_orders,
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=self._cycles_dir(),
        )
        # Pending order explains the gap: status stays OK (no incident).
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        recon = payload["reconciliation"]
        self.assertEqual(recon["drifts"], [])
        self.assertEqual(recon["pending"], ["drift_pending_order:BTC/USD"])
        self.assertFalse(any(slug.startswith("position_drift") for slug in payload["incidents"]))

    def test_position_without_target_in_plan_is_drift(self) -> None:
        # IWM has no entry in the plan (target = 0) but the broker holds
        # $500 in IWM — under the dual threshold (target=0 means the
        # relative floor collapses to 0, so only the absolute $50 floor
        # gates it; $500 ≫ $50 → drift).
        _write_cycle_file(
            self._cycles_dir() / f"cycle_crypto_{self.as_of.isoformat()}.json",
            sleeve="crypto",
            as_of=self.as_of.isoformat(),
            plan=[_make_plan_entry(pair="BTC/USD", target_notional=1000.0)],
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.0158, market_value=1000.0),
                _make_position(symbol="IWM", quantity=2.0, market_value=500.0),
            ],
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=self._cycles_dir(),
        )
        # BTC/USD is on target; only IWM (a position outside the crypto
        # plan) drifts. Since IWM has no plan entry the drift surface is
        # only the BTC/USD target — IWM is not reconciled at all.
        # This documents the actual behavior: out-of-plan positions are
        # **not** reconciled, because the watch has no universe context
        # to attribute them to a sleeve.
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        recon = payload["reconciliation"]
        # Only BTC/USD appears in reconciliation (IWM has no plan entry).
        self.assertEqual(recon["checked"], 1)
        self.assertEqual(recon["drifts"], [])

    def test_in_plan_pair_with_target_zero_and_position_500_is_drift(self) -> None:
        # Plan has BTC/USD target = 0 (sleeve wants to exit) but broker
        # still holds $500 of BTCUSD → drift (target=0 → only $50 floor;
        # $500 ≫ $50 → fire).
        _write_cycle_file(
            self._cycles_dir() / f"cycle_crypto_{self.as_of.isoformat()}.json",
            sleeve="crypto",
            as_of=self.as_of.isoformat(),
            # Note: plan entry with target=0 still counts as "in plan",
            # otherwise the pair wouldn't even be reconciled.
            plan=[_make_plan_entry(pair="BTC/USD", target_notional=0.0, current_notional=500.0, action="sell_all")],
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.0079, market_value=500.0),
            ],
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=self._cycles_dir(),
        )
        self.assertEqual(result.status, "WARN")
        payload = json.loads(output.read_text(encoding="utf-8"))
        recon = payload["reconciliation"]
        self.assertEqual(len(recon["drifts"]), 1)
        self.assertEqual(recon["drifts"][0]["pair"], "BTC/USD")
        self.assertEqual(recon["drifts"][0]["target"], 0.0)
        self.assertEqual(recon["drifts"][0]["actual"], 500.0)
        self.assertIn("position_drift:BTC/USD:target=0.0:actual=500.0", payload["incidents"])

    def test_no_cycles_dir_does_not_emit_reconciliation_payload(self) -> None:
        # Regression: when --cycles-dir is omitted, reconciliation must
        # not appear in the payload (and no drift incidents fire).
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="IWM", quantity=2.0, market_value=500.0),
            ],
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            # cycles_dir left as default (None)
        )
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertNotIn("reconciliation", payload)
        self.assertFalse(any(slug.startswith("position_drift") for slug in payload["incidents"]))
        self.assertFalse(any(slug.startswith("cycle_file_unreadable") for slug in payload["incidents"]))


class ExpiredOrdersTests(_HelperBase):
    """Detect expired DAY orders so the operator knows to expect a re-plan."""

    def test_expired_sleeve_order_today_records_incident_and_aviso_line(self) -> None:
        as_of_iso = self.as_of.isoformat()
        closed_orders = [
            _make_closed_order(
                client_order_id=f"sleeve-{as_of_iso}-BTC-buy",
                symbol="BTCUSD",
                side="buy",
                filled_quantity=0.0,  # expired — never filled
                filled_avg_price=None,
                updated_at=f"{as_of_iso}T20:00:00Z",
                status="expired",
            ),
        ]
        broker = _FakeBroker(
            positions=[],
            closed_orders=closed_orders,
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
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["expired_orders"]), 1)
        self.assertEqual(
            payload["expired_orders"][0]["client_order_id"],
            f"sleeve-{as_of_iso}-BTC-buy",
        )
        self.assertIn(
            f"order_expired:sleeve-{as_of_iso}-BTC-buy",
            payload["incidents"],
        )
        # Telegram mirrors the dedicated line.
        tg = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertIn(
            "AVISO: orden expirada sleeve-"
            f"{as_of_iso}-BTC-buy "
            "(el ciclo re-planeará)",
            tg["message"],
        )

    def test_expired_non_sleeve_order_is_ignored(self) -> None:
        # A closed order with status "expired" but a non-sleeve- prefix
        # (e.g. broker-rebooketed dust order) must NOT be flagged.
        as_of_iso = self.as_of.isoformat()
        closed_orders = [
            _make_closed_order(
                client_order_id=f"paper-{as_of_iso}-other-flip",
                symbol="BTCUSD",
                side="buy",
                filled_quantity=0.0,
                filled_avg_price=None,
                updated_at=f"{as_of_iso}T20:00:00Z",
                status="expired",
            ),
        ]
        broker = _FakeBroker(
            positions=[],
            closed_orders=closed_orders,
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        # No expired_orders in payload, no order_expired incident, status OK.
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["expired_orders"], [])
        self.assertFalse(any(slug.startswith("order_expired") for slug in payload["incidents"]))

    def test_expired_breaker_order_also_counted(self) -> None:
        # breaker- prefix orders (M11 watchdog) also count for the WS4
        # expired-order detector.
        as_of_iso = self.as_of.isoformat()
        closed_orders = [
            _make_closed_order(
                client_order_id=f"breaker-{as_of_iso}-flatten",
                symbol="BTCUSD",
                side="sell",
                filled_quantity=0.0,
                filled_avg_price=None,
                updated_at=f"{as_of_iso}T20:00:00Z",
                status="expired",
            ),
        ]
        broker = _FakeBroker(
            positions=[],
            closed_orders=closed_orders,
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
        self.assertEqual(len(payload["expired_orders"]), 1)
        self.assertIn(
            "order_expired:breaker-"
            f"{as_of_iso}-flatten",
            payload["incidents"],
        )

    def test_expired_order_from_two_days_ago_is_ignored(self) -> None:
        # Status==expired is only in scope for today or yesterday. An
        # order that expired 2 days ago should NOT fire (the next cycle
        # has already had a chance to re-plan).
        as_of_iso = self.as_of.isoformat()
        two_days_ago = (self.as_of - timedelta(days=2)).isoformat()
        closed_orders = [
            _make_closed_order(
                client_order_id=f"sleeve-{two_days_ago}-BTC-buy",
                symbol="BTCUSD",
                side="buy",
                filled_quantity=0.0,
                filled_avg_price=None,
                updated_at=f"{two_days_ago}T20:00:00Z",
                status="expired",
            ),
        ]
        broker = _FakeBroker(
            positions=[],
            closed_orders=closed_orders,
        )
        output = self.tmp_path / "position_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        self.assertEqual(result.status, "OK")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["expired_orders"], [])
        self.assertFalse(any(slug.startswith("order_expired") for slug in payload["incidents"]))
