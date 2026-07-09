"""Tests for multi-sleeve risk-parity combination (Sprint L1)."""

from __future__ import annotations

import statistics
import unittest

from trading_ai.backtest.portfolio import combine_risk_parity_sleeves


def _series(start_day: int, values: list[float]) -> dict[str, float]:
    return {f"2020-01-{start_day + i:02d}": v for i, v in enumerate(values)}


class RiskParitySleeveTests(unittest.TestCase):
    def test_single_sleeve_passthrough_after_warmup(self) -> None:
        # One sleeve, constant tiny returns; during warmup returns are raw.
        s = _series(1, [0.001] * 25)
        result = combine_risk_parity_sleeves({"a": s}, warmup=5, vol_window=5)
        self.assertEqual(len(result.daily_returns), 25)
        # First warmup values are unscaled.
        self.assertAlmostEqual(result.daily_returns[0], 0.001, places=12)

    def test_leverage_cap_limits_scale_up(self) -> None:
        # Low-vol series -> target/vol would be huge; cap must bound the scale.
        vals = [0.0001 * ((-1) ** i) for i in range(40)]  # tiny alternating
        s = {"a": _series(1, vals)}
        capped = combine_risk_parity_sleeves(s, leverage_cap=1.0, warmup=5, vol_window=10, target_daily_vol=0.01)
        uncapped = combine_risk_parity_sleeves(s, leverage_cap=5.0, warmup=5, vol_window=10, target_daily_vol=0.01)
        # With cap 1.0 no post-warmup value exceeds the raw magnitude.
        raw_max = max(abs(v) for v in vals)
        self.assertLessEqual(max(abs(v) for v in capped.daily_returns), raw_max + 1e-12)
        # A higher cap lets the scale grow, so its max magnitude is larger.
        self.assertGreater(
            max(abs(v) for v in uncapped.daily_returns),
            max(abs(v) for v in capped.daily_returns),
        )

    def test_only_available_sleeves_contribute_per_date(self) -> None:
        # Sleeve b trades on an extra date a does not (like crypto weekends).
        a = {"2020-01-01": 0.01, "2020-01-02": 0.02}
        b = {"2020-01-01": -0.01, "2020-01-02": 0.02, "2020-01-03": 0.05}
        result = combine_risk_parity_sleeves({"a": a, "b": b}, warmup=100)  # warmup>len -> raw
        by_date = dict(zip(result.timestamps, result.daily_returns))
        # Day 1: both present -> average of raw (warmup) values.
        self.assertAlmostEqual(by_date["2020-01-01"], (0.01 + -0.01) / 2, places=12)
        # Day 3: only b -> its value alone.
        self.assertAlmostEqual(by_date["2020-01-03"], 0.05, places=12)

    def test_start_date_filters(self) -> None:
        a = {"2019-12-31": 0.9, "2020-01-01": 0.01}
        result = combine_risk_parity_sleeves({"a": a}, start_date="2020-01-01", warmup=100)
        self.assertEqual(result.timestamps, ("2020-01-01",))

    def test_diversification_lowers_combined_volatility(self) -> None:
        # Two anti-correlated sleeves -> combined vol below each sleeve's vol.
        n = 60
        a = {f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}": (0.01 if i % 2 == 0 else -0.01) for i in range(n)}
        b = {f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}": (-0.01 if i % 2 == 0 else 0.01) for i in range(n)}
        result = combine_risk_parity_sleeves({"a": a, "b": b}, warmup=100)
        combined_vol = statistics.pstdev(result.daily_returns)
        self.assertLess(combined_vol, statistics.pstdev(list(a.values())))

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            combine_risk_parity_sleeves({})
        with self.assertRaises(ValueError):
            combine_risk_parity_sleeves({"a": {"2020-01-01": 0.0}}, target_daily_vol=0.0)
        with self.assertRaises(ValueError):
            combine_risk_parity_sleeves({"a": {"2020-01-01": 0.0}}, leverage_cap=0.0)


if __name__ == "__main__":
    unittest.main()


class SleeveBacktestCliTests(unittest.TestCase):
    def _write(self, path, symbols, days):
        import csv
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
            for s_i, sym in enumerate(symbols):
                for i in range(days):
                    m, d = 1 + i // 28, 1 + i % 28
                    c = 100.0 + i * (0.5 + 0.2 * s_i)
                    w.writerow([f"2021-{m:02d}-{d:02d}", sym, c - 0.2, c + 1, c - 1, c, 1_000_000])

    def test_cli_sleeve_backtest_runs_and_writes_metrics(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from trading_ai.cli import build_parser

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, b = root / "a.csv", root / "b.csv"
            self._write(a, ["SPY", "QQQ"], 80)
            self._write(b, ["BTCUSD", "ETHUSD"], 80)
            out = root / "sleeve.json"
            args = build_parser().parse_args([
                "sleeve-backtest",
                "--sleeve", f"etf={a},1,20,252",
                "--sleeve", f"crypto={b},25,20,365",
                "--output", str(out),
            ])
            rc = args.func(args)
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["strategy"], "risk-parity-sleeves")
            for key in ("sharpe_full", "profit_factor", "max_drawdown", "deflated_sharpe"):
                self.assertIn(key, payload["metrics"])

    def test_cli_rejects_bad_sleeve_spec(self) -> None:
        from trading_ai.cli import build_parser

        args = build_parser().parse_args(["sleeve-backtest", "--sleeve", "bogus_spec"])
        self.assertEqual(args.func(args), 2)
