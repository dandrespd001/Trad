import csv
import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class AiFeaturePipelineTests(unittest.TestCase):
    def test_ai_feature_build_aggregates_only_valid_past_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            features = root / "features.csv"
            write_csv(
                features,
                [
                    {
                        "timestamp": "2026-06-15",
                        "symbol": "SPY",
                        "close": "100",
                        "momentum_20": "0.1",
                    },
                    {
                        "timestamp": "2026-06-16",
                        "symbol": "SPY",
                        "close": "101",
                        "momentum_20": "0.2",
                    },
                    {
                        "timestamp": "2026-06-16",
                        "symbol": "QQQ",
                        "close": "200",
                        "momentum_20": "0.3",
                    },
                    {
                        "timestamp": "2026-06-18",
                        "symbol": "SPY",
                        "close": "102",
                        "momentum_20": "0.4",
                    },
                ],
            )
            events = root / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-06-15",
                                "symbol": "SPY",
                                "source": "manual",
                                "event_type": "macro",
                                "sentiment_score": 0.5,
                                "event_risk_score": 0.2,
                                "confidence": 0.8,
                                "valid_until": "2026-06-17",
                                "model_id": "fixture",
                                "source_sha256": "a" * 64,
                                "llm_authority": "none",
                            },
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-06-17",
                                "symbol": "SPY",
                                "source": "future",
                                "event_type": "macro",
                                "sentiment_score": -0.9,
                                "event_risk_score": 0.9,
                                "confidence": 1.0,
                                "valid_until": "2026-06-18",
                                "model_id": "future",
                                "source_sha256": "b" * 64,
                                "llm_authority": "none",
                            },
                            sort_keys=True,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            universe = write_universe(root / "universe.yml", ("SPY", "QQQ"))
            provider_config = write_provider_config(root / "ai_sources.yml")
            output_dir = root / "ai_features"

            exit_code = main(
                [
                    "ai-feature-build",
                    "--as-of-date",
                    "2026-06-16",
                    "--features",
                    str(features),
                    "--events",
                    str(events),
                    "--config",
                    str(universe),
                    "--provider-config",
                    str(provider_config),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            rows = read_csv(output_dir / "2026-06-16" / "ai_features.csv")
            manifest = json.loads(
                (output_dir / "2026-06-16" / "ai_features_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        by_key = {(row["timestamp"], row["symbol"]): row for row in rows}
        self.assertEqual(by_key[("2026-06-16", "SPY")]["ai_event_count_1d"], "1")
        self.assertEqual(by_key[("2026-06-16", "SPY")]["ai_sentiment_1d"], "0.5")
        self.assertEqual(by_key[("2026-06-16", "SPY")]["ai_risk_1d"], "0.2")
        self.assertEqual(by_key[("2026-06-16", "SPY")]["ai_confidence_1d"], "0.8")
        self.assertEqual(by_key[("2026-06-16", "QQQ")]["ai_event_count_1d"], "0")
        self.assertEqual(by_key[("2026-06-18", "SPY")]["ai_event_count_1d"], "0")
        self.assertEqual(manifest["status"], "OK")
        self.assertEqual(manifest["row_count"], 4)
        self.assertRegex(manifest["dataset_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["authority"]["llm_authority"], "none")
        self.assertFalse(manifest["safety"]["orders_submitted"])

    def test_ai_feature_build_blocks_external_api_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            features = root / "features.csv"
            write_csv(features, [{"timestamp": "2026-06-16", "symbol": "SPY", "close": "100"}])
            events = root / "events.jsonl"
            events.write_text("", encoding="utf-8")
            universe = write_universe(root / "universe.yml", ("SPY",))
            provider_config = write_provider_config(root / "ai_sources.yml")
            output_dir = root / "ai_features"

            exit_code = main(
                [
                    "ai-feature-build",
                    "--as-of-date",
                    "2026-06-16",
                    "--features",
                    str(features),
                    "--events",
                    str(events),
                    "--config",
                    str(universe),
                    "--provider-config",
                    str(provider_config),
                    "--provider",
                    "external_api",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            manifest = json.loads(
                (output_dir / "2026-06-16" / "ai_features_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["status"], "BLOCKED")
        self.assertIn("external_api_disabled", manifest["blockers"])
        self.assertFalse(manifest["safety"]["external_api_used"])


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


def write_provider_config(path: Path) -> Path:
    path.write_text(
        "manual_jsonl:\n"
        "  enabled: true\n"
        "external_api:\n"
        "  enabled: false\n",
        encoding="utf-8",
    )
    return path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
