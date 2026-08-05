"""Tests for the continuous sleeve re-validation envelope (Sprint M14, WS5)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.execution import sleeve_revalidation as revalidation
from trading_ai.execution.sleeve_revalidation import (
    COST_INPUT_SEMANTICS,
    ENVELOPE_MC_P95_FRACTION,
    ENVELOPE_REFERENCE_EVIDENCE_STATUS,
    ENVELOPE_SCALE_UP_BLOCKER,
    PROMOTION_BLOCKER_NO_TRADE_LEDGER,
    PROMOTION_BLOCKER_NO_TRIAL_REGISTRY,
    SCHEMA_VERSION,
    run_sleeve_revalidation,
)


def _flat_ohlcv_row(*, timestamp: str, symbol: str, close: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def _increasing_ohlcv_row(*, timestamp: str, symbol: str, close: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def _build_etf_records(
    symbols_to_close: dict[str, list[float]],
    start: str = "2026-06-01",
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    start_date = date.fromisoformat(start)
    n_dates = len(next(iter(symbols_to_close.values())))
    for offset in range(n_dates):
        day = (start_date + timedelta(days=offset)).isoformat()
        for symbol, closes in symbols_to_close.items():
            close = closes[offset]
            rows.append(_increasing_ohlcv_row(timestamp=day, symbol=symbol, close=close))
    return rows


def _build_crypto_records(
    symbols_to_close: dict[str, list[float]],
    start: str = "2026-06-01",
) -> list[dict[str, object]]:
    return _build_etf_records(symbols_to_close, start=start)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_records(rows, path)


class _FakeBroker:
    """Duck-typed broker that exposes just the risk-context surface."""

    def __init__(
        self,
        *,
        equity: float = 100000.0,
        last_equity: float = 100000.0,
        raise_account: bool = False,
    ) -> None:
        self._equity = equity
        self._last_equity = last_equity
        self._raise_account = raise_account

    def read_account(self) -> SimpleNamespace:
        if self._raise_account:
            raise RuntimeError("account unavailable")
        return SimpleNamespace(equity=self._equity, last_equity=self._last_equity)

    def read_positions(self) -> tuple[SimpleNamespace, ...]:
        return ()


class SleeveRevalidationCostSemanticsTests(unittest.TestCase):
    """The all-in one-way sleeve cost is mapped to the engine exactly once."""

    def test_total_one_way_cost_is_not_duplicated_as_slippage(self) -> None:
        records = [
            _flat_ohlcv_row(timestamp="2026-06-01", symbol="SPY", close=100.0),
        ]
        backtest_payload = {
            "positions": [{"timestamp": "2026-06-01"}],
            "daily_returns": [0.0],
        }
        backtest_result = SimpleNamespace(to_dict=lambda: backtest_payload)
        with (
            patch.object(revalidation, "read_records", return_value=records),
            patch.object(
                revalidation,
                "validate_ohlcv_records",
                return_value=SimpleNamespace(valid=True, errors=[]),
            ),
            patch.object(
                revalidation,
                "run_momentum_vol_target_backtest",
                return_value=backtest_result,
            ) as run_backtest,
        ):
            returns, incidents = revalidation._run_sleeve_backtest_for_revalidation(
                dataset_path="synthetic.csv",
                cost_bps=25.0,
                momentum_window=20,
                periods_per_year=365,
            )

        self.assertEqual(returns, {"2026-06-01": 0.0})
        self.assertEqual(incidents, [])
        run_backtest.assert_called_once()
        config = run_backtest.call_args.args[1]
        self.assertEqual(config.cost_bps, 25.0)
        self.assertEqual(config.slippage_bps, 0.0)


class SleeveRevalidationNoBrokerTests(unittest.TestCase):
    """Strategy-check only — no broker → no envelope decision, scale stays 1.0."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        # ETF sleeve spec: momentum_window=20, periods_per_year=252 → need ≥ 21 bars
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        # Crypto sleeve spec: momentum_window=120, periods_per_year=365 → need ≥ 121 bars
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_no_broker_strategy_check_only_scale_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            result = run_sleeve_revalidation(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=1000.0,
                output=output,
                state_path=state_path,
                equity_track_path=equity_track_path,
                broker=None,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "OK")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(SCHEMA_VERSION, "2.0")
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["envelope"]["exposure_scale_before"], 1.0)
            self.assertEqual(payload["envelope"]["exposure_scale_after"], 1.0)
            self.assertIsNone(payload["envelope"]["threshold_dd"])
            self.assertIsNone(payload["envelope"]["current_drawdown_pct"])
            metrics = payload["strategy_check"]["metrics"]
            self.assertIn("sharpe_full", metrics)
            self.assertIn("sharpe_rolling_60d", metrics)
            self.assertIn("return_gain_loss_ratio", metrics)
            self.assertIsNone(metrics["profit_factor"])
            self.assertEqual(
                metrics["profit_factor_status"],
                "UNAVAILABLE_NO_TRADE_LEDGER",
            )
            self.assertIn("maxdd", metrics)
            for source in payload["strategy_check"]["sleeves"]:
                self.assertIn("total_one_way_cost_bps", source)
                self.assertEqual(source["cost_input_semantics"], COST_INPUT_SEMANTICS)
                self.assertEqual(
                    source["backtest_cost_bps"],
                    source["total_one_way_cost_bps"],
                )
                self.assertEqual(source["backtest_slippage_bps"], 0.0)
                self.assertEqual(source["cost_bps"], source["total_one_way_cost_bps"])
                self.assertEqual(source["cost_bps_alias_of"], "total_one_way_cost_bps")
            evidence = payload["promotion_evidence"]
            self.assertEqual(evidence["status"], "BLOCKED")
            self.assertFalse(evidence["promotion_eligible"])
            self.assertFalse(evidence["edge_promotable"])
            self.assertIsNone(evidence["deflated_sharpe"])
            self.assertFalse(evidence["trial_ledger_available"])
            self.assertEqual(
                evidence["blockers"],
                [
                    PROMOTION_BLOCKER_NO_TRIAL_REGISTRY,
                    PROMOTION_BLOCKER_NO_TRADE_LEDGER,
                ],
            )
            self.assertTrue(evidence["report_only"])
            self.assertFalse(evidence["affects_operational_status"])
            self.assertFalse(evidence["affects_exposure_scale"])
            self.assertEqual(payload["incidents"], [])
            self.assertTrue(payload["safety"]["read_only"])
            self.assertFalse(payload["safety"]["orders_submitted"])
            self.assertFalse(payload["safety"]["promotion_authorized"])
            self.assertFalse(payload["safety"]["live_trading_allowed"])
            # No state should have been written because scale did not change
            self.assertFalse(state_path.exists())
            # No equity track should have been written without a broker
            self.assertFalse(equity_track_path.exists())


class SleeveRevalidationEnvelopeBreachTests(unittest.TestCase):
    """Broker with drawdown above envelope → scale 0.5, telegram, WARN."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_drawdown_above_envelope_triggers_breach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            telegram_path = tmp_path / "telegram.json"
            highwater_path = tmp_path / "equity_highwater.json"
            # High-water > equity by ~10% so current_dd ≈ 0.10 > envelope_dd ≈ 0.066
            # (total_notional / equity = 1/1 → envelope_dd = 0.066 * 1 = 0.066)
            highwater_path.write_text(
                json.dumps({"high_water_equity": 110000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            result = run_sleeve_revalidation(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                telegram_artifact=telegram_path,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["envelope"]["exposure_scale_before"], 1.0)
            self.assertEqual(payload["envelope"]["exposure_scale_after"], 0.5)
            self.assertIn("envelope_breached", payload["envelope"]["events"])
            self.assertEqual(payload["envelope"]["events"].count("envelope_breached"), 1)
            self.assertGreater(
                payload["envelope"]["current_drawdown_pct"],
                payload["envelope"]["threshold_dd"],
            )
            # State JSON written with new scale and timestamp
            self.assertTrue(state_path.exists())
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_payload["exposure_scale"], 0.5)
            self.assertEqual(state_payload["since"], "2026-09-28")
            # Telegram artifact (M9 shape) was written
            self.assertTrue(telegram_path.exists())
            telegram_payload = json.loads(telegram_path.read_text(encoding="utf-8"))
            self.assertIn("REVAL: envelope_breached", telegram_payload["message"])
            # Equity track was appended
            self.assertTrue(equity_track_path.exists())
            track_lines = equity_track_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(track_lines[0], "date,equity")
            self.assertEqual(track_lines[1].split(",")[0], "2026-09-28")


class SleeveRevalidationRecoveryTests(unittest.TestCase):
    """Invalidated P0-03 evidence can never authorize a scale increase."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_invalidated_envelope_cannot_increase_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            telegram_path = tmp_path / "telegram.json"
            highwater_path = tmp_path / "equity_highwater.json"
            # Seed prior state at 0.5
            state_path.write_text(
                json.dumps(
                    {
                        "exposure_scale": 0.5,
                        "since": "2026-09-27",
                        "updated_at": "2026-09-27T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            # High-water matches equity → drawdown 0 < envelope/2
            highwater_path.write_text(
                json.dumps({"high_water_equity": 100000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            result = run_sleeve_revalidation(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                telegram_artifact=telegram_path,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "OK")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["envelope"]["exposure_scale_before"], 0.5)
            self.assertEqual(payload["envelope"]["exposure_scale_after"], 0.5)
            self.assertEqual(payload["envelope"]["events"], [])
            self.assertEqual(
                payload["envelope"]["reference_evidence_status"],
                ENVELOPE_REFERENCE_EVIDENCE_STATUS,
            )
            self.assertFalse(payload["envelope"]["scale_up_allowed"])
            self.assertEqual(
                payload["envelope"]["scale_up_blockers"],
                [ENVELOPE_SCALE_UP_BLOCKER],
            )
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_payload["exposure_scale"], 0.5)
            self.assertEqual(state_payload["since"], "2026-09-27")
            self.assertFalse(telegram_path.exists())

    def test_intermediate_drawdown_keeps_scale_halved(self) -> None:
        # Even an intermediate drawdown cannot use invalidated evidence to scale up.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            state_path.write_text(
                json.dumps(
                    {
                        "exposure_scale": 0.5,
                        "since": "2026-09-27",
                        "updated_at": "2026-09-27T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            # current_dd = 0.04 (intermediate: envelope/2 = 0.033 < 0.04 < envelope = 0.066)
            highwater_path.write_text(
                json.dumps({"high_water_equity": 104000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            result = run_sleeve_revalidation(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "OK")  # no events → OK
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["envelope"]["exposure_scale_before"], 0.5)
            self.assertEqual(payload["envelope"]["exposure_scale_after"], 0.5)
            self.assertEqual(payload["envelope"]["events"], [])
            # State was not re-written because scale did not change
            state_after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_after["exposure_scale"], 0.5)
            self.assertEqual(state_after["since"], "2026-09-27")  # unchanged


class SleeveRevalidationEquityTrackTests(unittest.TestCase):
    """Same-date dedup and multi-day real_track."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_duplicate_as_of_dedupes_equity_track_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            highwater_path.write_text(
                json.dumps({"high_water_equity": 100000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            common_kwargs = dict(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            run_sleeve_revalidation(**common_kwargs)
            # Same as_of → no new row, no scale change → no telegram
            telegram_path = tmp_path / "telegram.json"
            run_sleeve_revalidation(
                **common_kwargs,
                telegram_artifact=telegram_path,
            )
            self.assertTrue(equity_track_path.exists())
            track_lines = equity_track_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(track_lines[0], "date,equity")
            self.assertEqual(len(track_lines), 2, track_lines)
            self.assertEqual(track_lines[1].split(",")[0], "2026-09-28")
            self.assertFalse(telegram_path.exists())

    def test_different_dates_produce_two_rows_and_real_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output1 = tmp_path / "rev1.json"
            output2 = tmp_path / "rev2.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            highwater_path.write_text(
                json.dumps({"high_water_equity": 100000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            base = dict(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                broker=_FakeBroker(equity=100000.0, last_equity=100000.0),
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
            )
            run_sleeve_revalidation(**base, output=output1, as_of_date=date(2026, 9, 28))
            run_sleeve_revalidation(**base, output=output2, as_of_date=date(2026, 9, 29))
            self.assertTrue(equity_track_path.exists())
            track_lines = equity_track_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(track_lines[0], "date,equity")
            self.assertEqual(len(track_lines), 3, track_lines)
            self.assertEqual(track_lines[1].split(",")[0], "2026-09-28")
            self.assertEqual(track_lines[2].split(",")[0], "2026-09-29")
            payload2 = json.loads(output2.read_text(encoding="utf-8"))
            real_track = payload2["real_track"]
            self.assertEqual(real_track["n_days"], 2)
            self.assertEqual(real_track["first"]["date"], "2026-09-28")
            self.assertEqual(real_track["last"]["date"], "2026-09-29")
            self.assertEqual(real_track["real_return_pct"], 0.0)


class SleeveRevalidationDatasetFailureTests(unittest.TestCase):
    """Corrupt dataset → incident, status WARN, rest of payload still surfaces."""

    def _build_good_crypto(self, tmp: Path) -> Path:
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return crypto_path

    def test_missing_etf_dataset_records_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            crypto_path = self._build_good_crypto(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            highwater_path.write_text(
                json.dumps({"high_water_equity": 100000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            result = run_sleeve_revalidation(
                etf_dataset=tmp_path / "missing.csv",
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsNone(payload["strategy_check"]["metrics"])
            self.assertTrue(
                any(
                    incident.startswith("dataset_unreadable:")
                    for incident in payload["incidents"]
                ),
                payload["incidents"],
            )
            # The envelope decision still ran (broker was provided and valid)
            self.assertEqual(payload["envelope"]["exposure_scale_before"], 1.0)
            self.assertEqual(payload["envelope"]["exposure_scale_after"], 1.0)
            self.assertIsNotNone(payload["envelope"]["threshold_dd"])

    def test_corrupt_crypto_dataset_records_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            crypto_path = tmp_path / "corrupt_crypto.csv"
            # Header-only CSV → zero rows → invalid for validation
            crypto_path.parent.mkdir(parents=True, exist_ok=True)
            crypto_path.write_text(
                "timestamp,symbol,open,high,low,close,volume\n",
                encoding="utf-8",
            )
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            highwater_path.write_text(
                json.dumps({"high_water_equity": 100000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            result = run_sleeve_revalidation(
                etf_dataset=tmp_path / "missing.csv",
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    incident.startswith("dataset_")
                    for incident in payload["incidents"]
                ),
                payload["incidents"],
            )


class SleeveRevalidationBrokerFailureTests(unittest.TestCase):
    """Broker supplied but ``read_account`` fails → incident, prior scale preserved."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_account_unavailable_records_incident_and_preserves_scale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            state_path.write_text(
                json.dumps(
                    {
                        "exposure_scale": 1.0,
                        "since": "2026-09-27",
                        "updated_at": "2026-09-27T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            broker = _FakeBroker(raise_account=True)
            result = run_sleeve_revalidation(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=100000.0,
                output=output,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "WARN")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("account_risk_context_unavailable", payload["incidents"])
            self.assertEqual(payload["envelope"]["exposure_scale_before"], 1.0)
            self.assertEqual(payload["envelope"]["exposure_scale_after"], 1.0)
            # No equity track appended because we had no risk context
            self.assertFalse(equity_track_path.exists())


class SleeveRevalidationEnvelopeMathTests(unittest.TestCase):
    """Sanity: ``threshold_dd`` == MC p95 fraction × (total_notional / equity)."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_threshold_dd_scales_with_total_notional_over_equity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "revalidation.json"
            state_path = tmp_path / "revalidation_state.json"
            equity_track_path = tmp_path / "equity_track.csv"
            highwater_path = tmp_path / "equity_highwater.json"
            highwater_path.write_text(
                json.dumps({"high_water_equity": 100000.0, "updated_at": "2026-07-01T00:00:00Z"}),
                encoding="utf-8",
            )
            # 0.5 budget ratio → threshold = 0.066 * 0.5 = 0.033
            broker = _FakeBroker(equity=100000.0, last_equity=100000.0)
            result = run_sleeve_revalidation(
                etf_dataset=etf_path,
                crypto_dataset=crypto_path,
                total_notional_usd=50000.0,
                output=output,
                broker=broker,
                state_path=state_path,
                equity_track_path=equity_track_path,
                equity_highwater_path=highwater_path,
                as_of_date=date(2026, 9, 28),
            )
            self.assertEqual(result.status, "OK")
            payload = json.loads(output.read_text(encoding="utf-8"))
            expected_threshold = round(ENVELOPE_MC_P95_FRACTION * (50000.0 / 100000.0), 6)
            self.assertEqual(
                payload["envelope"]["threshold_dd"], expected_threshold
            )


class SleeveRevalidateCliTests(unittest.TestCase):
    """CLI surface: required flags, default output paths."""

    def _build_datasets(self, tmp: Path) -> tuple[Path, Path]:
        n_etf = 30
        etf_path = tmp / "etf.csv"
        _write_csv(
            etf_path,
            _build_etf_records(
                {
                    "SPY": [400.0 + i * 1.0 for i in range(n_etf)],
                    "QQQ": [350.0 for _ in range(n_etf)],
                }
            ),
        )
        n_crypto = 130
        crypto_path = tmp / "crypto.csv"
        _write_csv(
            crypto_path,
            _build_crypto_records(
                {
                    "BTC/USD": [100.0 + i * 1.0 for i in range(n_crypto)],
                    "ETH/USD": [200.0 for _ in range(n_crypto)],
                }
            ),
        )
        return etf_path, crypto_path

    def test_cli_default_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "rev.json"
            argv = [
                "sleeve-revalidate",
                "--etf-dataset",
                str(etf_path),
                "--crypto-dataset",
                str(crypto_path),
                "--total-notional-usd",
                "1000",
                "--output",
                str(output),
            ]
            exit_code = main(argv)
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "OK")

    def test_cli_real_paper_without_confirm_returns_error(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "rev.json"
            argv = [
                "sleeve-revalidate",
                "--etf-dataset",
                str(etf_path),
                "--crypto-dataset",
                str(crypto_path),
                "--total-notional-usd",
                "1000",
                "--real-paper",
                "--output",
                str(output),
            ]
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(argv)
            self.assertEqual(exit_code, 2)

    def test_cli_real_paper_uses_executor_without_broker_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            etf_path, crypto_path = self._build_datasets(tmp_path)
            output = tmp_path / "rev.json"
            broker = object()
            result = SimpleNamespace(
                status="OK",
                exit_code=0,
                payload={
                    "envelope": {
                        "exposure_scale_after": 1.0,
                        "current_drawdown_pct": 0.0,
                    },
                    "strategy_check": {
                        "metrics": {"sharpe_rolling_60d": 1.0},
                    },
                },
            )
            argv = [
                "sleeve-revalidate",
                "--etf-dataset",
                str(etf_path),
                "--crypto-dataset",
                str(crypto_path),
                "--total-notional-usd",
                "1000",
                "--real-paper",
                "--confirm-paper",
                "--output",
                str(output),
            ]
            with (
                patch("trading_ai.cli.PaperExecutorBrokerClient", return_value=broker) as executor,
                patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker credentials must not be used"),
                ) as direct_broker,
                patch("trading_ai.cli.run_sleeve_revalidation", return_value=result) as run,
            ):
                exit_code = main(argv)

            self.assertEqual(exit_code, 0)
            executor.assert_called_once_with()
            direct_broker.assert_not_called()
            self.assertIs(run.call_args.kwargs["broker"], broker)


if __name__ == "__main__":
    unittest.main()
