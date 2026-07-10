"""Tests for the governed sleeve circuit breaker (Sprint M11, §33)."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trading_ai.cli import main
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
    ) -> None:
        self._positions = list(positions or [])
        self._equity = equity
        self._last_equity = last_equity
        self._submit_accepted = submit_accepted
        self._submit_status = submit_status
        self._submit_reasons = submit_reasons
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
        return ()

    def submit_order(self, order: Any) -> Any:
        self.submitted.append(order)
        return SimpleNamespace(
            accepted=self._submit_accepted,
            status=self._submit_status,
            reasons=self._submit_reasons,
            dry_run=False,
            broker_response={"id": f"order-{len(self.submitted)}"},
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
# Scenario 1: no breach, no prior state → OK, no actions, state untouched.
# ---------------------------------------------------------------------------


class NoBreachCleanStartTests(_HelperBase):
    def test_no_breach_no_state_returns_ok_no_actions_state_untouched(self) -> None:
        broker = _FakeBroker(
            positions=[
                _make_position(symbol="BTCUSD", quantity=0.1, market_value=6000.0, avg_entry_price=60000.0),
                _make_position(symbol="ETHUSD", quantity=1.0, market_value=3000.0, avg_entry_price=3000.0),
            ],
            equity=100_000.0,
            last_equity=100_000.0,
        )
        state_path = self._state_path()
        output = self.tmp_path / "breaker.json"
        result = run_sleeve_circuit_breaker(
            risk_config="configs/risk.yml",
            output=output,
            broker=broker,
            state_path=state_path,
            equity_highwater_path=self._highwater_path(None),
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
        # No transition → state file is NOT written.
        self.assertFalse(state_path.exists())


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
        state_path = self._state_path()
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

        # State file written with the partial_done stage.
        self.assertTrue(state_path.exists())
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "partial_done")
        self.assertFalse(saved["paused"])

        # Telegram artifact carries the BREAKER prefix.
        tg = json.loads(telegram.read_text(encoding="utf-8"))
        self.assertEqual(tg["status"], "WARN")
        self.assertIn("BREAKER:", tg["message"])
        self.assertIn("partial_triggered", tg["message"])
        self.assertEqual(tg["schema_version"], SCHEMA_VERSION)


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
                _make_position(symbol="ETHUSD", quantity=1.0, market_value=3000.0, avg_entry_price=3000.0),
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
        self.assertIn("flatten_triggered", payload["events"])

        # Two sells, each 100% of current qty with `-all` client_order_id.
        self.assertEqual(len(broker.submitted), 2)
        ids = sorted(o.client_order_id for o in broker.submitted)
        self.assertTrue(all("-all" in i for i in ids), ids)
        by_id = {o.client_order_id: o for o in broker.submitted}
        self.assertAlmostEqual(by_id[f"breaker-{self.as_of.isoformat()}-BTCUSD-all"].quantity, 0.05)
        self.assertAlmostEqual(by_id[f"breaker-{self.as_of.isoformat()}-ETHUSD-all"].quantity, 1.0)

        # State file mutated: stage=flattened, paused=true.
        self.assertTrue(state_path.exists())
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "flattened")
        self.assertTrue(saved["paused"])
        self.assertEqual(result.status, "WARN")


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
            for symbol in ("BTC/USD", "ETH/USD"):
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
        return path

    def test_paused_state_blocks_sleeve_rebalance_without_submitting(self) -> None:
        # Build a paused state file.
        state_path = self._write_state(stage="flattened", paused=True)
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
            equity_highwater_path=self._highwater_path(None),
            breaker_state_path=state_path,
        )
        self.assertEqual(result.status, "BLOCKED")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("circuit_breaker_paused", payload["blockers"])
        # The breaker paused before any submit could happen.
        self.assertEqual(broker.submitted, [])

    def test_missing_state_file_does_not_block_sleeve_rebalance(self) -> None:
        # Sanity: a missing/unreadable state file must NOT block the cycle
        # (the fail-closed posture is: corrupted state ⇒ ignore, not lock).
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
            equity_highwater_path=self._highwater_path(None),
            breaker_state_path=self._state_path("missing.json"),  # does not exist
        )
        # Missing state file: cycle continues (not BLOCKED on circuit_breaker_paused).
        self.assertNotEqual(result.status, "BLOCKED")


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
        # Stage_after IS reflected (for the next run to reason about), but
        # the persistence is conditional on confirm_actions=True.
        self.assertEqual(payload["stage_after"], "partial_done")
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
        payload = json.loads(output.read_text(encoding="utf-8"))
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()