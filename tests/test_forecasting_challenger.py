import csv
import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class ForecastingChallengerTests(unittest.TestCase):
    def test_forecasting_challenger_writes_local_forecast_features_without_lookahead(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            features = root / "features.csv"
            write_csv(
                features,
                [
                    {"timestamp": "2026-06-12", "symbol": "SPY", "close": "100"},
                    {"timestamp": "2026-06-15", "symbol": "SPY", "close": "101"},
                    {"timestamp": "2026-06-16", "symbol": "SPY", "close": "103"},
                    {"timestamp": "2026-06-17", "symbol": "SPY", "close": "99"},
                    {"timestamp": "2026-06-16", "symbol": "QQQ", "close": "200"},
                ],
            )
            universe = write_universe(root / "universe.yml", ("SPY", "QQQ"))
            output_dir = root / "forecast"

            exit_code = main(
                [
                    "forecasting-challenger-report",
                    "--as-of-date",
                    "2026-06-16",
                    "--features",
                    str(features),
                    "--config",
                    str(universe),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            run_dir = output_dir / "2026-06-16"
            rows = read_csv(run_dir / "forecast_features.csv")
            report = json.loads((run_dir / "forecast_report.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        timestamps = {row["timestamp"] for row in rows}
        self.assertNotIn("2026-06-17", timestamps)
        by_key = {(row["timestamp"], row["symbol"]): row for row in rows}
        spy = by_key[("2026-06-16", "SPY")]
        self.assertEqual(spy["forecast_model_id"], "local_return_forecaster_v1")
        self.assertGreater(float(spy["forecast_confidence"]), 0.0)
        self.assertIn("forecast_return_1d", spy)
        self.assertEqual(report["status"], "OK")
        self.assertIn("forecast_return_1d", report["feature_columns"])
        self.assertEqual(report["authority"]["llm_authority"], "none")
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertFalse(report["safety"]["external_api_used"])


def write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_universe(path: Path, symbols: tuple[str, ...]) -> Path:
    path.write_text(
        "universe:\n"
        "  name: test\n"
        "  asset_type: etf\n"
        "  market: us_equities\n"
        "  symbols:\n"
        + "".join(f"    - {symbol}\n" for symbol in symbols),
        encoding="utf-8",
    )
    return path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
