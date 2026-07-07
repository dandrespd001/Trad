import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.paper_swing_declarations import (
    active_swing_symbols,
    load_swing_declarations,
    record_swing_declaration,
)

VALID_THESIS = "Breakout above 200d MA with strong volume confirmation and sector tailwind."
VALID_PLAN_HASH = "abc12345def67890"


class RecordSwingDeclarationTests(unittest.TestCase):
    def test_happy_path_declaration_is_recorded_with_verifiable_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir) / "swing"
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="spy",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.5,
                expires_on="2026-06-20",
                registry_dir=registry_dir,
            )
            self.assertEqual(decision.status, "OK")
            self.assertEqual(decision.exit_code, 0)
            self.assertEqual(decision.payload["symbol"], "SPY")

            registry_path = registry_dir / "2026-06-16" / "registry.json"
            self.assertTrue(registry_path.exists())
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertIn("integrity_sha256", raw)
            self.assertEqual(len(raw["records"]), 1)
            self.assertEqual(raw["records"][0]["symbol"], "SPY")

            # A second, different declaration should append and still verify.
            decision_two = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="QQQ",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=1.0,
                expires_on="2026-06-25",
                registry_dir=registry_dir,
            )
            self.assertEqual(decision_two.status, "OK")
            loaded = load_swing_declarations("2026-06-16", registry_dir=registry_dir)
            self.assertFalse(loaded["fail_closed"])
            self.assertEqual(len(loaded["records"]), 2)
            symbols = {record["symbol"] for record in loaded["records"]}
            self.assertEqual(symbols, {"SPY", "QQQ"})

    def test_thesis_too_short_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis="too short",
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("thesis_too_short", decision.payload["blockers"])

    def test_overnight_loss_pct_zero_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=0,
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("overnight_loss_pct_invalid", decision.payload["blockers"])

    def test_overnight_loss_pct_negative_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=-1.0,
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("overnight_loss_pct_invalid", decision.payload["blockers"])

    def test_overnight_loss_pct_above_limit_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=5.1,
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("overnight_loss_pct_invalid", decision.payload["blockers"])

    def test_overnight_loss_pct_non_numeric_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct="not-a-number",
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("overnight_loss_pct_invalid", decision.payload["blockers"])

    def test_expires_on_in_the_past_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-10",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("expires_on_invalid", decision.payload["blockers"])

    def test_expires_on_equal_to_as_of_date_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-16",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("expires_on_invalid", decision.payload["blockers"])

    def test_expires_on_not_parseable_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="not-a-date",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("expires_on_invalid", decision.payload["blockers"])

    def test_plan_hash_too_short_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash="ab12",
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("plan_hash_invalid", decision.payload["blockers"])

    def test_plan_hash_empty_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash="",
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-20",
                registry_dir=Path(temp_dir),
            )
            self.assertIn("plan_hash_invalid", decision.payload["blockers"])

    def test_duplicate_declaration_same_day_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir)
            first = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-20",
                registry_dir=registry_dir,
            )
            self.assertEqual(first.status, "OK")

            second = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=3.0,
                expires_on="2026-06-25",
                registry_dir=registry_dir,
            )
            self.assertEqual(second.status, "BLOCKED")
            self.assertIn("duplicate_declaration", second.payload["blockers"])
            # Registry must not have been mutated by the blocked attempt.
            loaded = load_swing_declarations("2026-06-16", registry_dir=registry_dir)
            self.assertEqual(len(loaded["records"]), 1)

    def test_corrupt_registry_fails_closed_and_blocks_new_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir)
            registry_path = registry_dir / "2026-06-16" / "registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text("{not valid json", encoding="utf-8")

            loaded = load_swing_declarations("2026-06-16", registry_dir=registry_dir)
            self.assertTrue(loaded["fail_closed"])
            self.assertEqual(loaded["records"], [])

            decision = record_swing_declaration(
                as_of_date="2026-06-16",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.0,
                expires_on="2026-06-20",
                registry_dir=registry_dir,
            )
            self.assertEqual(decision.status, "BLOCKED")
            self.assertIn("registry_fail_closed", decision.payload["blockers"])

    def test_tampered_checksum_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_dir = Path(temp_dir)
            registry_path = registry_dir / "2026-06-16" / "registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "as_of_date": "2026-06-16",
                        "records": [],
                        "integrity_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_swing_declarations("2026-06-16", registry_dir=registry_dir)
            self.assertTrue(loaded["fail_closed"])

    def test_missing_registry_is_normal_empty_not_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = load_swing_declarations("2026-06-16", registry_dir=Path(temp_dir))
            self.assertFalse(loaded["fail_closed"])
            self.assertEqual(loaded["records"], [])


class ActiveSwingSymbolsTests(unittest.TestCase):
    def test_expires_before_as_of_date_is_excluded(self) -> None:
        registry = {
            "fail_closed": False,
            "records": [{"symbol": "SPY", "expires_on": "2026-06-15"}],
        }
        active = active_swing_symbols(registry, as_of_date="2026-06-16")
        self.assertEqual(active, {})

    def test_expires_equal_to_as_of_date_is_included(self) -> None:
        registry = {
            "fail_closed": False,
            "records": [{"symbol": "SPY", "expires_on": "2026-06-16"}],
        }
        active = active_swing_symbols(registry, as_of_date="2026-06-16")
        self.assertIn("SPY", active)

    def test_expires_after_as_of_date_is_included(self) -> None:
        registry = {
            "fail_closed": False,
            "records": [{"symbol": "SPY", "expires_on": "2026-06-20"}],
        }
        active = active_swing_symbols(registry, as_of_date="2026-06-16")
        self.assertIn("SPY", active)

    def test_fail_closed_registry_yields_no_active_symbols_even_with_valid_records(self) -> None:
        registry = {
            "fail_closed": True,
            "records": [{"symbol": "SPY", "expires_on": "2026-06-20"}],
        }
        active = active_swing_symbols(registry, as_of_date="2026-06-16")
        self.assertEqual(active, {})

    def test_unparseable_expires_on_is_excluded(self) -> None:
        registry = {
            "fail_closed": False,
            "records": [{"symbol": "SPY", "expires_on": "not-a-date"}],
        }
        active = active_swing_symbols(registry, as_of_date="2026-06-16")
        self.assertEqual(active, {})


if __name__ == "__main__":
    unittest.main()
