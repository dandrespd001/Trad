import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperChallengerShadowPlanTests(unittest.TestCase):
    def test_parser_defaults_for_shadow_plan(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-challenger-shadow-plan",
                "--challenger-report",
                "challenger.json",
                "--review-decision",
                "decision.json",
                "--latest-model",
                "models/latest_model.json",
                "--approved-manifest",
                "manifest.json",
                "--feature-schema",
                "schema.json",
            ]
        )

        self.assertEqual(args.challenger_report, "challenger.json")
        self.assertEqual(args.review_decision, "decision.json")
        self.assertEqual(args.latest_model, "models/latest_model.json")
        self.assertEqual(args.approved_manifest, "manifest.json")
        self.assertEqual(args.feature_schema, "schema.json")
        self.assertEqual(args.output_dir, "reports/tmp/paper_challenger_shadow")

    def test_reviewable_challenger_and_defer_decision_are_ready_for_shadow_without_broker_execution(self) -> None:
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            challenger = write_challenger(root / "challenger.json", status="REVIEWABLE")
            decision = write_decision(root / "decision.json", status="RECORDED", decision="DEFER")
            manifest = write_manifest(root / "manifest.json")
            schema = write_schema(root / "schema.json")

            exit_code = main(
                shadow_args(root, challenger=challenger, decision=decision, manifest=manifest, schema=schema)
            )
            payload = read_json(root / "shadow" / "shadow_plan.json")
            markdown = (root / "shadow" / "shadow_plan.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["shadow_state"], "READY_FOR_SHADOW")
        self.assertEqual(payload["champion"]["path"], "models/latest_model.json")
        self.assertTrue(payload["challenger"]["shadow_only"])
        self.assertFalse(payload["challenger"]["promotes_model"])
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertIn("Shadow state: **READY_FOR_SHADOW**", markdown)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)

    def test_shadow_plan_blocks_non_reviewable_challenger_or_approve_decision(self) -> None:
        cases = (
            ("BLOCKED", "DEFER", "challenger_not_reviewable"),
            ("REVIEWABLE", "APPROVE", "review_decision_not_shadow"),
        )
        for status, decision_value, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                challenger = write_challenger(root / "challenger.json", status=status)
                decision = write_decision(root / "decision.json", status="RECORDED", decision=decision_value)
                manifest = write_manifest(root / "manifest.json")
                schema = write_schema(root / "schema.json")

                exit_code = main(
                    shadow_args(root, challenger=challenger, decision=decision, manifest=manifest, schema=schema)
                )
                payload = read_json(root / "shadow" / "shadow_plan.json")

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["shadow_state"], "BLOCKED")
            self.assertIn(expected, blocker_codes(payload))


def shadow_args(root: Path, *, challenger: Path, decision: Path, manifest: Path, schema: Path) -> list[str]:
    return [
        "paper-challenger-shadow-plan",
        "--challenger-report",
        str(challenger),
        "--review-decision",
        str(decision),
        "--latest-model",
        "models/latest_model.json",
        "--approved-manifest",
        str(manifest),
        "--feature-schema",
        str(schema),
        "--output-dir",
        str(root / "shadow"),
    ]


def write_challenger(path: Path, *, status: str) -> Path:
    return write_json(
        path,
        {
            "status": status,
            "candidate_quality": {"paper_compatibility": "PASS"},
            "authority": {"mutates_latest_model": False, "automatic_champion_replacement": False},
            "safety": {"paper_only": True, "live_trading_authorized": False},
        },
    )


def write_decision(path: Path, *, status: str, decision: str) -> Path:
    return write_json(
        path, {"status": status, "decision": decision, "reviewer": "human", "reason": "shadow paper only"}
    )


def write_manifest(path: Path) -> Path:
    return write_json(
        path, {"dataset_hash": "dataset-a", "symbols": ["SPY"], "columns": ["timestamp", "symbol", "close"]}
    )


def write_schema(path: Path) -> Path:
    return write_json(path, {"feature_names": ["momentum_20", "realized_volatility_20", "relative_volume_20"]})


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def blocker_codes(payload: dict[str, Any]) -> set[str]:
    return {str(blocker["code"]) for blocker in payload["blockers"]}


if __name__ == "__main__":
    unittest.main()
