import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder, PaperPosition
from trading_ai.execution.paper_risk_state import (
    RiskState,
    compute_order_risk_inputs,
    evaluate_kill_switch,
    load_risk_state,
    reset_kill_switch,
    roll_daily_equity,
    save_risk_state,
)
from trading_ai.risk.policy import RiskLimits


class RollDailyEquityTests(unittest.TestCase):
    def test_first_observation_sets_opening_and_peak(self) -> None:
        state = roll_daily_equity(RiskState(), equity=10000.0, as_of_date="2026-06-24")
        self.assertEqual(state.opening_equity, 10000.0)
        self.assertEqual(state.peak_equity, 10000.0)
        self.assertEqual(state.as_of_date, "2026-06-24")

    def test_same_day_keeps_opening_and_tracks_peak(self) -> None:
        state = roll_daily_equity(RiskState(), equity=10000.0, as_of_date="2026-06-24")
        state = roll_daily_equity(state, equity=10500.0, as_of_date="2026-06-24")
        self.assertEqual(state.opening_equity, 10000.0)
        self.assertEqual(state.peak_equity, 10500.0)

    def test_new_day_resets_opening_but_keeps_peak(self) -> None:
        state = roll_daily_equity(RiskState(), equity=10500.0, as_of_date="2026-06-24")
        state = roll_daily_equity(state, equity=10000.0, as_of_date="2026-06-25")
        self.assertEqual(state.opening_equity, 10000.0)
        self.assertEqual(state.peak_equity, 10500.0)

    def test_dry_run_zero_equity_does_not_corrupt_baselines(self) -> None:
        state = roll_daily_equity(RiskState(), equity=0.0, as_of_date="2026-06-24")
        self.assertIsNone(state.opening_equity)
        self.assertEqual(state.last_equity, 0.0)


class ComputeOrderRiskInputsTests(unittest.TestCase):
    def test_sell_orders_are_always_benign(self) -> None:
        state = RiskState(opening_equity=10000.0, peak_equity=20000.0)
        inputs = compute_order_risk_inputs(
            side="sell",
            symbol="SPY",
            notional=None,
            quantity=3.0,
            account_equity=9000.0,
            positions=[PaperPosition(symbol="SPY", quantity=3.0, market_value=1500.0)],
            state=state,
        )
        self.assertEqual(inputs.daily_pnl_pct, 0.0)
        self.assertEqual(inputs.current_drawdown_pct, 0.0)
        self.assertEqual(inputs.projected_gross_exposure, 0.0)
        self.assertEqual(inputs.estimated_position_weight, 0.0)

    def test_buy_computes_drawdown_and_daily_loss(self) -> None:
        state = RiskState(opening_equity=10000.0, peak_equity=10000.0)
        inputs = compute_order_risk_inputs(
            side="buy",
            symbol="SPY",
            notional=1.0,
            quantity=None,
            account_equity=9000.0,
            positions=[],
            state=state,
        )
        self.assertAlmostEqual(inputs.daily_pnl_pct, -0.10)
        self.assertAlmostEqual(inputs.current_drawdown_pct, 0.10)

    def test_buy_projects_gross_exposure_with_existing_positions(self) -> None:
        state = RiskState(opening_equity=10000.0, peak_equity=10000.0)
        inputs = compute_order_risk_inputs(
            side="buy",
            symbol="QQQ",
            notional=500.0,
            quantity=None,
            account_equity=10000.0,
            positions=[PaperPosition(symbol="SPY", quantity=3.0, market_value=1500.0)],
            state=state,
        )
        # (1500 existing + 500 new) / 10000 = 0.20
        self.assertAlmostEqual(inputs.projected_gross_exposure, 0.20)
        self.assertAlmostEqual(inputs.estimated_position_weight, 0.05)


class RiskStatePersistenceTests(unittest.TestCase):
    def test_round_trip_preserves_state_and_passes_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            state = RiskState(opening_equity=10000.0, peak_equity=12000.0, kill_switch_active=True)
            save_risk_state(state, path)
            loaded = load_risk_state(path)
            self.assertEqual(loaded.opening_equity, 10000.0)
            self.assertEqual(loaded.peak_equity, 12000.0)
            self.assertTrue(loaded.kill_switch_active)

    def test_atomic_write_leaves_no_residual_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            save_risk_state(RiskState(), path)
            self.assertEqual([entry.name for entry in Path(tmp).iterdir()], [path.name])

    def test_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = load_risk_state(Path(tmp) / "absent.json")
            self.assertTrue(state.kill_switch_active)
            self.assertEqual(state.kill_switch_reason, "state_missing_fail_closed")

    def test_corrupt_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            path.write_text("{not valid json", encoding="utf-8")
            state = load_risk_state(path)
            self.assertTrue(state.kill_switch_active)
            self.assertEqual(state.kill_switch_reason, "state_corrupt_fail_closed")

    def test_non_mapping_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            state = load_risk_state(path)
            self.assertTrue(state.kill_switch_active)
            self.assertEqual(state.kill_switch_reason, "state_corrupt_fail_closed")

    def test_legacy_file_without_checksum_loads_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            legacy_payload = RiskState(opening_equity=5000.0, peak_equity=6000.0).to_dict()
            path.write_text(json.dumps(legacy_payload), encoding="utf-8")
            state = load_risk_state(path)
            self.assertFalse(state.kill_switch_active)
            self.assertEqual(state.opening_equity, 5000.0)

    def test_tampered_checksum_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            save_risk_state(RiskState(opening_equity=10000.0), path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["opening_equity"] = 999999.0
            path.write_text(json.dumps(payload), encoding="utf-8")
            state = load_risk_state(path)
            self.assertTrue(state.kill_switch_active)
            self.assertEqual(state.kill_switch_reason, "state_integrity_mismatch_fail_closed")


class KillSwitchTests(unittest.TestCase):
    def test_drawdown_breach_trips_and_latches(self) -> None:
        state = RiskState(peak_equity=20000.0, last_equity=17000.0)  # 15% drawdown
        tripped = evaluate_kill_switch(state, max_drawdown_pct=0.10)
        self.assertTrue(tripped.kill_switch_active)
        self.assertIsNotNone(tripped.kill_switch_reason)

    def test_within_drawdown_does_not_trip(self) -> None:
        state = RiskState(peak_equity=20000.0, last_equity=19000.0)  # 5% drawdown
        result = evaluate_kill_switch(state, max_drawdown_pct=0.10)
        self.assertFalse(result.kill_switch_active)

    def test_error_streak_trips(self) -> None:
        state = RiskState(consecutive_error_days=3)
        result = evaluate_kill_switch(state, max_drawdown_pct=0.10, max_consecutive_error_days=3)
        self.assertTrue(result.kill_switch_active)

    def test_already_active_is_idempotent(self) -> None:
        state = RiskState(kill_switch_active=True, kill_switch_reason="prior", peak_equity=20000.0, last_equity=100.0)
        result = evaluate_kill_switch(state, max_drawdown_pct=0.10)
        self.assertEqual(result.kill_switch_reason, "prior")

    def test_reset_clears_flags(self) -> None:
        state = RiskState(kill_switch_active=True, kill_switch_reason="x", consecutive_error_days=4)
        cleared = reset_kill_switch(state)
        self.assertFalse(cleared.kill_switch_active)
        self.assertIsNone(cleared.kill_switch_reason)
        self.assertEqual(cleared.consecutive_error_days, 0)


class RiskGateEndToEndTests(unittest.TestCase):
    """The previously-dormant drawdown gate must now reject new buys."""

    def _broker(self) -> AlpacaPaperBroker:
        return AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(max_drawdown_pct=0.10),
            dry_run=True,
        )

    def test_drawdown_breach_rejects_buy(self) -> None:
        state = RiskState(opening_equity=20000.0, peak_equity=20000.0)
        inputs = compute_order_risk_inputs(
            side="buy",
            symbol="SPY",
            notional=1.0,
            quantity=None,
            account_equity=17000.0,  # 15% drawdown vs peak
            positions=[],
            state=state,
        )
        order = replace(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1"),
            **inputs.as_order_kwargs(),
        )
        result = self._broker().submit_order(order)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "risk_rejected")
        self.assertIn("drawdown_limit_breached", result.reasons)

    def test_healthy_account_allows_buy(self) -> None:
        state = RiskState(opening_equity=20000.0, peak_equity=20000.0)
        inputs = compute_order_risk_inputs(
            side="buy",
            symbol="SPY",
            notional=1.0,
            quantity=None,
            account_equity=20100.0,
            positions=[],
            state=state,
        )
        order = replace(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-2"),
            **inputs.as_order_kwargs(),
        )
        result = self._broker().submit_order(order)
        self.assertTrue(result.accepted)


if __name__ == "__main__":
    unittest.main()
