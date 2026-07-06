import csv
import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class AiEventExtractionTests(unittest.TestCase):
    def test_manual_jsonl_writes_governed_events_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-15",
                        "symbol": "spy",
                        "source": "manual_note",
                        "event_type": "macro",
                        "sentiment_score": 0.35,
                        "event_risk_score": 0.20,
                        "confidence": 0.80,
                        "valid_until": "2026-06-17",
                        "model_id": "fixture-sentiment",
                        "thesis": "risk-on context",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            universe = write_universe(root / "universe.yml", ("SPY", "QQQ"))
            provider_config = write_provider_config(root / "ai_sources.yml")
            output_dir = root / "events"

            exit_code = main(
                [
                    "ai-event-extract",
                    "--as-of-date",
                    "2026-06-16",
                    "--input-jsonl",
                    str(source),
                    "--config",
                    str(universe),
                    "--provider-config",
                    str(provider_config),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            events_path = output_dir / "2026-06-16" / "events.jsonl"
            manifest_path = output_dir / "2026-06-16" / "events_manifest.json"
            payload = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["symbol"], "SPY")
        self.assertEqual(payload[0]["llm_authority"], "none")
        self.assertRegex(payload[0]["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["status"], "OK")
        self.assertEqual(manifest["event_count"], 1)
        self.assertEqual(manifest["authority"]["llm_authority"], "none")
        self.assertFalse(manifest["safety"]["orders_submitted"])
        self.assertFalse(manifest["safety"]["credentials_read"])

    def test_external_api_provider_is_blocked_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.jsonl"
            source.write_text("", encoding="utf-8")
            universe = write_universe(root / "universe.yml", ("SPY",))
            provider_config = write_provider_config(root / "ai_sources.yml")
            output_dir = root / "events"

            exit_code = main(
                [
                    "ai-event-extract",
                    "--as-of-date",
                    "2026-06-16",
                    "--input-jsonl",
                    str(source),
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
                (output_dir / "2026-06-16" / "events_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["status"], "BLOCKED")
        self.assertIn("external_api_disabled", manifest["blockers"])
        self.assertFalse(manifest["safety"]["external_api_used"])

    def test_invalid_authority_and_lookahead_events_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.jsonl"
            source.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-06-17",
                                "symbol": "SPY",
                                "source": "future_note",
                                "event_type": "macro",
                                "sentiment_score": 0.1,
                                "event_risk_score": 0.1,
                                "confidence": 0.7,
                                "valid_until": "2026-06-18",
                                "model_id": "fixture",
                                "llm_authority": "none",
                            },
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-06-15",
                                "symbol": "SPY",
                                "source": "bad_authority",
                                "event_type": "macro",
                                "sentiment_score": 0.1,
                                "event_risk_score": 0.1,
                                "confidence": 0.7,
                                "valid_until": "2026-06-16",
                                "model_id": "fixture",
                                "llm_authority": "trade",
                            },
                            sort_keys=True,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            universe = write_universe(root / "universe.yml", ("SPY",))
            provider_config = write_provider_config(root / "ai_sources.yml")
            output_dir = root / "events"

            exit_code = main(
                [
                    "ai-event-extract",
                    "--as-of-date",
                    "2026-06-16",
                    "--input-jsonl",
                    str(source),
                    "--config",
                    str(universe),
                    "--provider-config",
                    str(provider_config),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            manifest = json.loads(
                (output_dir / "2026-06-16" / "events_manifest.json").read_text(encoding="utf-8")
            )
            events_text = (output_dir / "2026-06-16" / "events.jsonl").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["status"], "BLOCKED")
        self.assertIn("event_timestamp_after_as_of_date:1", manifest["blockers"])
        self.assertIn("llm_authority_not_none:2", manifest["blockers"])
        self.assertEqual(events_text, "")


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
