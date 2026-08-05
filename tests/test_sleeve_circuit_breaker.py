"""Tests for the governed sleeve circuit breaker (Sprint M11, §33)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from trading_ai.cli import main
from trading_ai.execution.alpaca_paper import PaperOrderSnapshot
from trading_ai.execution.sleeve_circuit_breaker import (
    SCHEMA_VERSION,
    run_sleeve_circuit_breaker,
)
from trading_ai.execution.sleeve_position_watch import run_sleeve_position_watch
from trading_ai.execution.sleeve_rebalance import run_sleeve_rebalance

# ---------------------------------------------------------------------------
# Fakes (mirror test_sleeve_rebalance's _FakeBroker pattern).
# ---------------------------------------------------------------------------


def _make_position(
    *,
    symbol: str,
    quantity: float,
    market_value: float,
    avg_entry_price: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        quantity=quantity,
        qty=quantity,
        market_value=market_value,
        avg_entry_price=avg_entry_price,
        current_price=avg_entry_price,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
    )


def _make_order_snapshot(
    *,
    symbol: str,
    client_order_id: str,
    status: str = "new",
    side: str = "buy",
    quantity: float = 1.0,
) -> PaperOrderSnapshot:
    return PaperOrderSnapshot(
        order_id=f"broker-{client_order_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type="market",
        time_in_force="day",
        status=status,
        notional=None,
        quantity=quantity,
        filled_quantity=0.0,
        filled_avg_price=None,
        submitted_at="2026-07-10T13:00:00+00:00",
        created_at="2026-07-10T13:00:00+00:00",
        updated_at="2026-07-10T13:00:01+00:00",
        expires_at="",
    )


class _FakeBroker:
    """Duck-typed broker for offline tests — no real broker dependency."""

    def __init__(
        self,
        *,
        positions: Iterable[SimpleNamespace] | None = None,
        equity: float = 100_000.0,
        last_equity: float = 100_000.0,
        submit_accepted: bool = True,
        submit_status: str = "accepted",
        submit_reasons: tuple[str, ...] = (),
        order_status_plans: dict[str, list[str]] | None = None,
        apply_fills: bool = True,
        open_orders: Iterable[PaperOrderSnapshot] | None = None,
    ) -> None:
        self._positions = list(positions or [])
        self._equity = equity
        self._last_equity = last_equity
        self._submit_accepted = submit_accepted
        self._submit_status = submit_status
        self._submit_reasons = submit_reasons
        self._order_status_plans = {
            key.upper().replace("/", ""): list(plan) for key, plan in (order_status_plans or {}).items()
        }
        self._apply_fills = apply_fills
        self._open_orders = tuple(open_orders or ())
        self._applied_fills: set[str] = set()
        self.submitted: list[Any] = []

    def read_account(self) -> SimpleNamespace:
        return SimpleNamespace(equity=self._equity, last_equity=self._last_equity)

    def read_positions(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self._positions)

    def list_orders(self, *, status: str = "open") -> tuple[Any, ...]:
        # Sleeve watch reads closed orders to compute fills_today. The
        # breaker itself does not call list_orders, but tests share this
        # fake across both modules; return an empty list so neither path
        # raises AttributeError.
        return self._open_orders if status == "open" else ()

    def submit_order(self, order: Any) -> Any:
        self.submitted.append(order)
        return SimpleNamespace(
            accepted=self._submit_accepted,
            status=self._submit_status,
            reasons=self._submit_reasons,
            dry_run=False,
            broker_response={"id": f"order-{len(self.submitted)}"},
        )

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        order = next(item for item in self.submitted if item.client_order_id == client_order_id)
        key = order.symbol.upper().replace("/", "")
        plan = self._order_status_plans.setdefault(key, ["filled"])
        status = plan.pop(0) if len(plan) > 1 else plan[0]
        quantity = float(order.quantity or 0.0)
        if status == "filled":
            filled_quantity = quantity
        elif status == "partially_filled":
            filled_quantity = quantity / 2.0
        else:
            filled_quantity = 0.0
        if status == "filled" and self._apply_fills and client_order_id not in self._applied_fills:
            compact = order.symbol.upper().replace("/", "")
            for position in self._positions:
                if position.symbol.upper().replace("/", "") != compact:
                    continue
                prior = float(position.quantity)
                after = prior - quantity if order.side == "sell" else prior + quantity
                position.quantity = after
                position.qty = after
                if abs(prior) > 1e-12:
                    position.market_value = float(position.market_value) * (after / prior)
                if abs(after) <= 1e-12:
                    self._positions.remove(position)
                self._applied_fills.add(client_order_id)
                break
        return PaperOrderSnapshot(
            order_id=f"order-{self.submitted.index(order) + 1}",
            client_order_id=client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type="market",
            time_in_force="day",
            status=status,
            notional=None,
            quantity=quantity,
            filled_quantity=filled_quantity,
            filled_avg_price=100.0 if filled_quantity else None,
            submitted_at="2026-07-10T14:00:00+00:00",
            created_at="2026-07-10T14:00:00+00:00",
            updated_at="2026-07-10T14:00:01+00:00",
            expires_at="",
        )


class _HelperBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.as_of = date(2026, 7, 10)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _state_path(self, name: str = "breaker_state.json") -> Path:
        return self.tmp_path / name

    def _highwater_path(self, value: float | None) -> Path:
        path = self.tmp_path / "equity_highwater.json"
        if value is not None:
            path.write_text(json.dumps({"high_water_equity": value}), encoding="utf-8")
        return path

    def _write_state(
        self,
        *,
        stage: str,
        paused: bool = False,
        first_breach_at: str | None = None,
    ) -> Path:
        path = self._state_path()
        payload = {
            "stage": stage,
            "paused": paused,
            "first_breach_at": first_breach_at,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# State integrity: confirmed mode never treats missing/corrupt as fresh state.
# ---------------------------------------------------------------------------


class StateIntegrityTests(_HelperBase):
    def test_missing_state_blocks_confirmed_execution_and_persists_safe_latch(self) -> None:
        state_path = self._state_path()
        broker = _FakeBroker(
            positions=[_make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0)],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=self.tmp_path / "missing-state.json",
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.payload["state_load_status"], "missing")
        self.assertIn("breaker_state_missing", result.payload["blockers"])
        self.assertEqual(broker.submitted, [])
        latched = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(latched["stage"], "none")
        self.assertTrue(latched["paused"])

        repeated = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=self.tmp_path / "latched-state.json",
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
        )
        self.assertEqual(repeated.status, "BLOCKED")
        self.assertIn("circuit_breaker_paused", repeated.payload["blockers"])
        self.assertEqual(broker.submitted, [])

    def test_corrupt_state_blocks_confirmed_execution_and_never_submits(self) -> None:
        state_path = self._state_path()
        state_path.write_text("{not-json", encoding="utf-8")
        broker = _FakeBroker(
            positions=[_make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0)],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=self.tmp_path / "corrupt-state.json",
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.payload["state_load_status"], "corrupt")
        self.assertIn("breaker_state_invalid_json", result.payload["blockers"])
        self.assertEqual(broker.submitted, [])
        self.assertTrue(json.loads(state_path.read_text(encoding="utf-8"))["paused"])


# ---------------------------------------------------------------------------
# Scenario 1: no breach, valid prior state → OK, no actions, state untouched.
# ---------------------------------------------------------------------------


class NoBreachCleanStartTests(_HelperBase):
    def test_no_breach_valid_state_returns_ok_no_actions_state_untouched(self) -> None:
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0, avg_entry_price=60000.0),
                _make_position(symbol="ETHUSD", quantity=1.0, market_value=3000.0, avg_entry_price=3000.0),
            ],
            equity=100_000.0,
            last_equity=100_000.0,
        )
        state_path = self._write_state(stage="none")
        state_before = state_path.read_text(encoding="utf-8")
        output = self.tmp_path / "breaker.json"
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "OK")
        self.assertFalse(payload["breached"])
        self.assertEqual(payload["stage_after"], "none")
        self.assertEqual(payload["actions"], [])
        self.assertEqual(payload["planned_actions"], [])
        self.assertEqual(broker.submitted, [])
        # No transition → the valid state file is not rewritten.
        self.assertEqual(state_path.read_text(encoding="utf-8"), state_before)


# ---------------------------------------------------------------------------
# Scenario 2: new breach + confirm → sell qty/2 of every sleeve position.
# ---------------------------------------------------------------------------


class NewBreachPartialTests(_HelperBase):
    def test_new_breach_triggers_partial_sells_with_half_ids(self) -> None:
        # equity=100k, last_equity=100.5k → daily_pnl_pct=-0.00498 ≈ -0.5% (not breached).
        # Push the loss past 2% (max_daily_loss_pct=0.02):
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0, avg_entry_price=60000.0),
                _make_position(symbol="ETHUSD", quantity=2.0, market_value=6000.0, avg_entry_price=3000.0),
            ],
            equity=97_500.0,
            last_equity=100_000.0,  # -2.5% → breached
        )
        state_path = self._write_state(stage="none")
        output = self.tmp_path / "breaker.json"
        telegram = self.tmp_path / "telegram.json"
        fixed_now = datetime(2026, 7, 10, 14, 0, 0, tzinfo=UTC)
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            telegram_artifact=telegram,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
            now=lambda: fixed_now,
        )
        self.assertEqual(result.status, "WARN")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["breached"])
        self.assertEqual(payload["stage_after"], "partial_done")
        self.assertEqual(payload["first_breach_at"], fixed_now.isoformat())
        self.assertEqual(payload["paused"], False)

        # Two positions → two sells, each qty/2 with a `-half` client_order_id.
        self.assertEqual(len(broker.submitted), 2)
        ids = sorted(o.client_order_id for o in broker.submitted)
        self.assertTrue(all("-half" in i for i in ids), ids)
        # Compact form (BTCUSD, ETHUSD).
        self.assertIn(f"breaker-{self.as_of.isoformat()}-BTCUSD-half", ids)
        self.assertIn(f"breaker-{self.as_of.isoformat()}-ETHUSD-half", ids)
        # Each sell is qty/2.
        by_id = {o.client_order_id: o for o in broker.submitted}
        self.assertAlmostEqual(by_id[f"breaker-{self.as_of.isoformat()}-BTCUSD-half"].quantity, 0.05)
        self.assertAlmostEqual(by_id[f"breaker-{self.as_of.isoformat()}-ETHUSD-half"].quantity, 1.0)
        # Risk kwargs propagated (M7 idiom).
        for order in broker.submitted:
            self.assertAlmostEqual(order.daily_pnl_pct, -0.025)
            self.assertEqual(order.side, "sell")
            self.assertEqual(order.position_intent, "reduce")

        # State file written with the partial_done stage.
        self.assertTrue(state_path.exists())
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "partial_done")
        self.assertFalse(saved["paused"])

        # Telegram artifact carries the BREAKER prefix.
        tg = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertEqual(tg["status"], "WARN")
        self.assertIn("BREAKER:", tg["message"])
        self.assertIn("partial_reconciled", tg["message"])
        self.assertEqual(tg["schema_version"], SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# Terminal execution and post-trade reconciliation are transition gates.
# ---------------------------------------------------------------------------


class ExecutionReconciliationTests(_HelperBase):
    def _run_breached(
        self,
        *,
        broker: _FakeBroker,
        output_name: str,
    ) -> tuple[object, dict[str, object], dict[str, object]]:
        state_path = self._write_state(stage="none")
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=self.tmp_path / output_name,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
            poll_attempts=2,
            poll_interval_seconds=0.0,
            sleep=lambda _seconds: None,
        )
        payload = result.payload
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        return result, payload, saved

    def test_pending_partial_and_rejected_orders_never_advance_stage(self) -> None:
        for terminal_status in ("new", "partially_filled", "rejected"):
            with self.subTest(terminal_status=terminal_status):
                broker = _FakeBroker(
                    positions=[
                        _make_position(
                            symbol="BTCUSD",
                            quantity=0.2,
                            market_value=12_000.0,
                            avg_entry_price=60_000.0,
                        )
                    ],
                    equity=97_500.0,
                    last_equity=100_000.0,
                    order_status_plans={"BTCUSD": [terminal_status]},
                )
                result, payload, saved = self._run_breached(
                    broker=broker,
                    output_name=f"{terminal_status}.json",
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(payload["stage_before"], "none")
                self.assertEqual(payload["stage_after"], "none")
                self.assertTrue(payload["paused"])
                self.assertFalse(payload["transition_reconciled"])
                self.assertIn("partial_failed", payload["events"])
                self.assertEqual(saved["stage"], "none")
                self.assertTrue(saved["paused"])

    def test_filled_order_without_observed_position_reduction_does_not_advance(self) -> None:
        broker = _FakeBroker(
            positions=[
                _make_position(
                    symbol="BTCUSD",
                    quantity=0.2,
                    market_value=12_000.0,
                    avg_entry_price=60_000.0,
                )
            ],
            equity=97_500.0,
            last_equity=100_000.0,
            apply_fills=False,
        )
        result, payload, saved = self._run_breached(
            broker=broker,
            output_name="stale-position.json",
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("partial_position_mismatch:BTC/USD", payload["blockers"])
        self.assertEqual(saved["stage"], "none")
        self.assertTrue(saved["paused"])

    def test_short_position_uses_buy_reduce_and_advances_only_after_fill(self) -> None:
        broker = _FakeBroker(
            positions=[
                _make_position(
                    symbol="BTCUSD",
                    quantity=-0.2,
                    market_value=-12_000.0,
                    avg_entry_price=60_000.0,
                )
            ],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        result, payload, saved = self._run_breached(
            broker=broker,
            output_name="short-reduce.json",
        )
        self.assertEqual(result.status, "WARN")
        self.assertTrue(payload["transition_reconciled"])
        self.assertEqual(payload["stage_after"], "partial_done")
        self.assertEqual(saved["stage"], "partial_done")
        self.assertFalse(saved["paused"])
        self.assertEqual(len(broker.submitted), 1)
        order = broker.submitted[0]
        self.assertEqual(order.side, "buy")
        self.assertEqual(order.position_intent, "reduce")
        self.assertAlmostEqual(order.quantity, 0.1)
        self.assertAlmostEqual(payload["reconciliation"]["position_quantities"]["BTC/USD"], -0.1)

    def test_unmapped_position_blocks_without_submitting(self) -> None:
        broker = _FakeBroker(
            positions=[
                _make_position(
                    symbol="NOTINUNIVERSE",
                    quantity=1.0,
                    market_value=1000.0,
                )
            ],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        result, payload, saved = self._run_breached(
            broker=broker,
            output_name="unmapped.json",
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("position_unmapped:NOTINUNIVERSE", payload["blockers"])
        self.assertEqual(broker.submitted, [])
        self.assertEqual(saved["stage"], "none")
        self.assertTrue(saved["paused"])


# ---------------------------------------------------------------------------
# Scenario 3: persistent breach <24h → awaiting_escalation, no new actions.
# ---------------------------------------------------------------------------


class AwaitingEscalationTests(_HelperBase):
    def test_breach_under_24h_keeps_partial_stage_with_no_new_actions(self) -> None:
        first_breach_at = datetime(2026, 7, 10, 8, 0, 0, tzinfo=UTC)
        state_path = self._write_state(
            stage="partial_done",
            paused=False,
            first_breach_at=first_breach_at.isoformat(),
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.05, market_value=3000.0, avg_entry_price=60000.0),
            ],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "breaker.json"
        # Now = 16:00 (8h after first breach) → <24h.
        fixed_now = datetime(2026, 7, 10, 16, 0, 0, tzinfo=UTC)
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
            now=lambda: fixed_now,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["stage_after"], "partial_done")
        self.assertEqual(payload["stage_before"], "partial_done")
        self.assertIn("awaiting_escalation", payload["events"])
        self.assertEqual(payload["actions"], [])
        self.assertEqual(payload["planned_actions"], [])
        self.assertEqual(broker.submitted, [])
        # State NOT mutated (no transition: stage + paused + first_breach_at all unchanged).
        reread = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(reread["stage"], "partial_done")
        self.assertFalse(reread["paused"])
        self.assertEqual(reread["first_breach_at"], first_breach_at.isoformat())
        # No-op on actions, but the breach is still active → WARN.
        self.assertEqual(result.status, "WARN")


# ---------------------------------------------------------------------------
# Scenario 4: persistent breach >=24h → flatten remaining, paused.
# ---------------------------------------------------------------------------


class FlattenEscalationTests(_HelperBase):
    def test_breach_past_24h_triggers_flatten_with_all_ids_and_pauses(self) -> None:
        first_breach_at = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)
        state_path = self._write_state(
            stage="partial_done",
            paused=False,
            first_breach_at=first_breach_at.isoformat(),
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.05, market_value=3000.0, avg_entry_price=60000.0),
                _make_position(symbol="ETHUSD", quantity=-1.0, market_value=-3000.0, avg_entry_price=3000.0),
            ],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "breaker.json"
        # Now = 2026-07-10 14:00 → ~28h since first breach.
        fixed_now = datetime(2026, 7, 10, 14, 0, 0, tzinfo=UTC)
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
            now=lambda: fixed_now,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["stage_after"], "flattened")
        self.assertTrue(payload["paused"])
        self.assertIn("flatten_reconciled", payload["events"])

        # Long and short both close 100% with `-all` client_order_ids.
        self.assertEqual(len(broker.submitted), 2)
        ids = sorted(o.client_order_id for o in broker.submitted)
        self.assertTrue(all("-all" in i for i in ids), ids)
        by_id = {o.client_order_id: o for o in broker.submitted}
        self.assertAlmostEqual(by_id[f"breaker-{self.as_of.isoformat()}-BTCUSD-all"].quantity, 0.05)
        self.assertAlmostEqual(by_id[f"breaker-{self.as_of.isoformat()}-ETHUSD-all"].quantity, 1.0)
        self.assertEqual(by_id[f"breaker-{self.as_of.isoformat()}-BTCUSD-all"].side, "sell")
        self.assertEqual(by_id[f"breaker-{self.as_of.isoformat()}-ETHUSD-all"].side, "buy")
        self.assertTrue(all(order.position_intent == "close" for order in broker.submitted))

        # State file mutated: stage=flattened, paused=true.
        self.assertTrue(state_path.exists())
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "flattened")
        self.assertTrue(saved["paused"])
        self.assertEqual(result.status, "WARN")

    def test_filled_closes_with_remaining_open_order_preserve_partial_stage(self) -> None:
        first_breach_at = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)
        state_path = self._write_state(
            stage="partial_done",
            paused=False,
            first_breach_at=first_breach_at.isoformat(),
        )
        broker = _FakeBroker(
            positions=[
                _make_position(
                    symbol="BTCUSD",
                    quantity=0.05,
                    market_value=3000.0,
                    avg_entry_price=60_000.0,
                )
            ],
            equity=97_500.0,
            last_equity=100_000.0,
            open_orders=[
                _make_order_snapshot(
                    symbol="ETH/USD",
                    client_order_id="unrelated-open-order",
                )
            ],
        )
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=self.tmp_path / "flatten-open-order.json",
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
            now=lambda: datetime(2026, 7, 10, 14, 0, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.payload["stage_after"], "partial_done")
        self.assertTrue(result.payload["paused"])
        self.assertIn("open_orders_remaining:1", result.payload["blockers"])
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "partial_done")
        self.assertTrue(saved["paused"])


# ---------------------------------------------------------------------------
# Scenario 5: paused state blocks sleeve-rebalance via circuit_breaker_paused.
# ---------------------------------------------------------------------------


class PausedBlocksRebalanceTests(_HelperBase):
    def _build_valid_dataset(self) -> Path:
        """Build an OHLCV dataset that passes validation (high >= close, etc.)."""
        from trading_ai.data.io import write_records

        start = self.as_of - timedelta(days=200)
        rows: list[dict[str, object]] = []
        for offset in range(200):
            day = (start + timedelta(days=offset)).isoformat()
            for symbol in (
                "BTC/USD",
                "ETH/USD",
                "LTC/USD",
                "BCH/USD",
                "DOGE/USD",
                "XRP/USD",
            ):
                close = 100.0 + offset * 1.0
                rows.append(
                    {
                        "timestamp": day,
                        "symbol": symbol,
                        "open": close,
                        "high": close,  # flat OHLC — high == open == close.
                        "low": close,
                        "close": close,
                        "volume": 1.0,
                    }
                )
        path = self.tmp_path / "crypto.csv"
        write_records(rows, path)
        symbols = ("BTC/USD", "ETH/USD", "LTC/USD", "BCH/USD", "DOGE/USD", "XRP/USD")
        first_day = str(rows[0]["timestamp"])
        last_day = str(rows[-1]["timestamp"])
        sidecar = {
            "schema_version": "1.1",
            "generated_at": "2026-07-10T00:00:00Z",
            "start": first_day,
            "end": last_day,
            "observed_start": first_day,
            "observed_end": last_day,
            "expected_latest_bar_date": last_day,
            "symbols": list(symbols),
            "row_count": len(rows),
            "per_symbol_row_counts": {symbol: 200 for symbol in symbols},
            "per_symbol_latest_dates": {symbol: last_day for symbol in symbols},
            "provider": "alpaca_crypto_data",
            "feed": "us",
            "status": "OK",
            "blockers": [],
            "published": True,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        path.with_name(path.name + ".fetch.json").write_text(json.dumps(sidecar, sort_keys=True), encoding="utf-8")
        return path

    def test_paused_state_blocks_sleeve_rebalance_without_submitting(self) -> None:
        # Build a paused state file.
        state_path = self._write_state(
            stage="flattened",
            paused=True,
            first_breach_at=datetime(2026, 7, 9, 10, 0, tzinfo=UTC).isoformat(),
        )
        dataset_path = self._build_valid_dataset()

        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0),
            ],
            equity=100_000.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "report.json"
        result = run_sleeve_rebalance(
            universe_config="configs/crypto_alpaca.yml",
            risk_config="configs/risk.yml",
            dataset=dataset_path,
            output=output,
            notional_usd=1000.0,
            broker=broker,
            confirm_submit=True,
            equity_highwater_path=self._highwater_path(100_000.0),
            breaker_state_path=state_path,
        )
        self.assertEqual(result.status, "BLOCKED")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("circuit_breaker_paused", payload["blockers"])
        # The breaker paused before any submit could happen.
        self.assertEqual(broker.submitted, [])

    def test_missing_state_file_blocks_sleeve_rebalance(self) -> None:
        # Confirmed mutation cannot infer a healthy latch from absence.
        dataset_path = self._build_valid_dataset()
        broker = _FakeBroker(
            positions=[],
            equity=100_000.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "report.json"
        result = run_sleeve_rebalance(
            universe_config="configs/crypto_alpaca.yml",
            risk_config="configs/risk.yml",
            dataset=dataset_path,
            output=output,
            notional_usd=1000.0,
            broker=broker,
            confirm_submit=True,
            equity_highwater_path=self._highwater_path(100_000.0),
            breaker_state_path=self._state_path("missing.json"),  # does not exist
            # Pin as_of to the fixture's date range: without it the cycle uses
            # date.today() and the dataset goes stale as real days pass (this
            # test broke silently 4 days after it was written).
            as_of_date=self.as_of,
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("circuit_breaker_state_missing", result.payload["blockers"])
        self.assertEqual(broker.submitted, [])


# ---------------------------------------------------------------------------
# Scenario 6: report-only (confirm_actions=False) with breach → planned
# actions present, broker never called, state never mutated.
# ---------------------------------------------------------------------------


class ReportOnlyTests(_HelperBase):
    def test_report_only_with_breach_lists_planned_actions_without_submitting(self) -> None:
        state_path = self._state_path()
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0, avg_entry_price=60000.0),
            ],
            equity=97_500.0,
            last_equity=100_000.0,
        )
        output = self.tmp_path / "breaker.json"
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=False,
            as_of_date=self.as_of,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["breached"])
        self.assertEqual(payload["state_load_status"], "missing")
        self.assertIn("breaker_state_missing", payload["state_diagnostics"])
        self.assertEqual(payload["reconciliation"]["status"], "report_only")
        # Plan is fully computed.
        self.assertEqual(len(payload["planned_actions"]), 1)
        planned = payload["planned_actions"][0]
        self.assertEqual(planned["pair"], "BTC/USD")
        self.assertAlmostEqual(planned["quantity"], 0.05)
        self.assertTrue(planned["client_order_id"].endswith("-half"))
        # But actions are marked report-only, broker never called.
        self.assertEqual(len(broker.submitted), 0)
        actions = payload["actions"]
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0]["skipped"])
        self.assertFalse(actions[0]["submitted"])
        self.assertEqual(actions[0]["status"], "report_only")
        # Safety block says read-only.
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["actions_executed"])
        # State file NOT written under report-only.
        self.assertFalse(state_path.exists())
        # The effective state remains unchanged; the hypothetical transition
        # is exposed separately and is explicitly unreconciled.
        self.assertEqual(payload["stage_after"], "none")
        self.assertEqual(payload["proposed_stage"], "partial_done")
        self.assertFalse(payload["transition_reconciled"])
        self.assertEqual(result.status, "WARN")


# ---------------------------------------------------------------------------
# Scenario 7: clean breach with partial_done (not paused) → reset to none.
# ---------------------------------------------------------------------------


class BreachClearedTests(_HelperBase):
    def test_clean_breach_with_partial_stage_resets_to_none(self) -> None:
        # Pretend the prior run entered PARTIAL but the account recovered.
        first_breach_at = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)
        state_path = self._write_state(
            stage="partial_done",
            paused=False,
            first_breach_at=first_breach_at.isoformat(),
        )
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.05, market_value=3000.0, avg_entry_price=60000.0),
            ],
            equity=100_000.0,
            last_equity=100_000.0,  # no loss today → not breached
        )
        output = self.tmp_path / "breaker.json"
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(100_000.0),
            confirm_actions=True,
            as_of_date=self.as_of,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(payload["breached"])
        self.assertEqual(payload["stage_before"], "partial_done")
        self.assertEqual(payload["stage_after"], "none")
        self.assertIsNone(payload["first_breach_at"])
        self.assertIn("breach_cleared", payload["events"])
        # No sells.
        self.assertEqual(broker.submitted, [])
        self.assertEqual(payload["actions"], [])
        self.assertEqual(result.status, "OK")
        # State file rewritten to "none" since this IS a transition.
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "none")
        self.assertFalse(saved["paused"])
        self.assertIsNone(saved["first_breach_at"])


# ---------------------------------------------------------------------------
# Scenario 8: watchdog — watch with cycles_dir flags missing daily cycle.
# ---------------------------------------------------------------------------


class DailyCycleWatchdogTests(_HelperBase):
    def _positions_broker(self) -> _FakeBroker:
        return _FakeBroker(
            positions=[],
            equity=100_000.0,
            last_equity=100_000.0,
        )

    def test_missing_cycle_in_dir_flags_daily_cycle_missing(self) -> None:
        broker = self._positions_broker()
        cycles_dir = self.tmp_path / "cycles"
        cycles_dir.mkdir()
        # Cycles from 5 days ago — older than as_of - 1 day.
        stale = cycles_dir / "cycle_crypto_2026-07-05.json"
        stale.write_text(json.dumps({"status": "OK"}), encoding="utf-8")
        output = self.tmp_path / "watch.json"
        telegram = self.tmp_path / "telegram_watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            telegram_artifact=telegram,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=cycles_dir,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "WARN")
        self.assertIn("daily_cycle_missing:2026-07-05", payload["incidents"])
        self.assertIn("daily_cycle_missing:2026-07-05", payload["blockers"])
        tg = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertEqual(tg["status"], "WARN")
        self.assertIn("ciclo diario ausente desde 2026-07-05", tg["message"])

    def test_empty_dir_flags_daily_cycle_missing_none(self) -> None:
        broker = self._positions_broker()
        cycles_dir = self.tmp_path / "empty_cycles"
        cycles_dir.mkdir()
        output = self.tmp_path / "watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=cycles_dir,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "WARN")
        self.assertIn("daily_cycle_missing:none", payload["incidents"])

    def test_cycle_from_today_does_not_flag_incident(self) -> None:
        broker = self._positions_broker()
        cycles_dir = self.tmp_path / "cycles"
        cycles_dir.mkdir()
        fresh = cycles_dir / f"cycle_crypto_{self.as_of.isoformat()}.json"
        fresh.write_text(json.dumps({"status": "OK"}), encoding="utf-8")
        output = self.tmp_path / "watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=cycles_dir,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "OK")
        self.assertNotIn("daily_cycle_missing", " ".join(payload["incidents"]))

    def test_cycle_from_yesterday_does_not_flag_incident(self) -> None:
        broker = self._positions_broker()
        cycles_dir = self.tmp_path / "cycles"
        cycles_dir.mkdir()
        yesterday = self.as_of - timedelta(days=1)
        fresh = cycles_dir / f"cycle_crypto_{yesterday.isoformat()}.json"
        fresh.write_text(json.dumps({"status": "OK"}), encoding="utf-8")
        output = self.tmp_path / "watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
            cycles_dir=cycles_dir,
        )
        self.assertEqual(result.status, "OK")

    def test_no_cycles_dir_kwarg_keeps_existing_behavior(self) -> None:
        broker = self._positions_broker()
        output = self.tmp_path / "watch.json"
        result = run_sleeve_position_watch(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            as_of_date=self.as_of,
            equity_highwater_path=self._highwater_path(None),
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        # No cycles_dir -> no daily_cycle_missing incident (even if the
        # dir on disk happens to be empty).
        self.assertEqual(result.status, "OK")
        for incident in payload["incidents"]:
            self.assertFalse(incident.startswith("daily_cycle_missing"))


# ---------------------------------------------------------------------------
# CLI handler tests (lifecycle check that the parser + handler are wired).
# ---------------------------------------------------------------------------


class CircuitBreakerCliTests(_HelperBase):
    def test_cli_real_paper_without_confirm_paper_returns_error(self) -> None:
        stderr = StringIO()
        argv = ["sleeve-circuit-breaker", "--real-paper"]
        with redirect_stderr(stderr):
            exit_code = main(argv)
        self.assertEqual(exit_code, 2)
        self.assertIn("--real-paper requires --confirm-paper", stderr.getvalue())

    def test_cli_without_real_paper_returns_error(self) -> None:
        stderr = StringIO()
        argv = ["sleeve-circuit-breaker"]
        with redirect_stderr(stderr):
            exit_code = main(argv)
        self.assertEqual(exit_code, 2)
        self.assertIn("--real-paper is required for sleeve-circuit-breaker", stderr.getvalue())

    def test_cli_report_only_routes_observation_through_executor(self) -> None:
        broker = object()
        result = SimpleNamespace(
            status="OK",
            exit_code=0,
            payload={"breached": False, "stage_after": "none", "actions": []},
        )
        with (
            patch("trading_ai.cli.PaperExecutorBrokerClient", return_value=broker) as executor,
            patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                side_effect=AssertionError("direct broker credentials must not be used"),
            ) as direct_client,
            patch(
                "trading_ai.cli.AlpacaPaperBroker",
                side_effect=AssertionError("direct broker facade must not be constructed"),
            ) as direct_broker,
            patch(
                "trading_ai.cli.load_risk_config",
                side_effect=AssertionError("the CLI must not construct broker risk state"),
            ) as broker_risk,
            patch("trading_ai.cli.run_sleeve_circuit_breaker", return_value=result) as run,
        ):
            exit_code = main(
                [
                    "sleeve-circuit-breaker",
                    "--real-paper",
                    "--confirm-paper",
                ]
            )

        self.assertEqual(exit_code, 0)
        executor.assert_called_once_with()
        direct_client.assert_not_called()
        direct_broker.assert_not_called()
        broker_risk.assert_not_called()
        self.assertIs(run.call_args.kwargs["broker"], broker)
        self.assertFalse(run.call_args.kwargs["confirm_actions"])

    def test_cli_confirmed_actions_use_executor_without_fallback(self) -> None:
        broker = object()
        result = SimpleNamespace(
            status="WARN",
            exit_code=1,
            payload={"breached": True, "stage_after": "partial_done", "actions": [{}]},
        )
        with (
            patch("trading_ai.cli.PaperExecutorBrokerClient", return_value=broker) as executor,
            patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                side_effect=AssertionError("direct broker credentials must not be used"),
            ) as direct_client,
            patch(
                "trading_ai.cli.AlpacaPaperBroker",
                side_effect=AssertionError("direct broker facade must not be constructed"),
            ) as direct_broker,
            patch(
                "trading_ai.cli.load_risk_config",
                side_effect=AssertionError("the CLI must not construct broker risk state"),
            ) as broker_risk,
            patch("trading_ai.cli.run_sleeve_circuit_breaker", return_value=result) as run,
        ):
            exit_code = main(
                [
                    "sleeve-circuit-breaker",
                    "--real-paper",
                    "--confirm-paper",
                    "--confirm-actions",
                ]
            )

        self.assertEqual(exit_code, 1)
        executor.assert_called_once_with()
        direct_client.assert_not_called()
        direct_broker.assert_not_called()
        broker_risk.assert_not_called()
        self.assertIs(run.call_args.kwargs["broker"], broker)
        self.assertTrue(run.call_args.kwargs["confirm_actions"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
