import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main
from trading_ai.llm.schemas import schema_for, validate_against_schema


class LlmSignalProposalTests(unittest.TestCase):
    def test_parser_defaults_for_llm_signal_proposals(self) -> None:
        args = build_parser().parse_args(
            [
                "llm-signal-proposals",
                "--as-of-date",
                "2026-06-16",
                "--readiness",
                "readiness.json",
                "--features",
                "features.csv",
                "--model-signals",
                "signals.json",
                "--output-dir",
                "/tmp/proposals",  # noqa: S108
                "--llm-model-alias",
                "llm_alias.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness, "readiness.json")
        self.assertEqual(args.features, "features.csv")
        self.assertEqual(args.model_signals, "signals.json")
        self.assertEqual(args.output_dir, "/tmp/proposals")  # noqa: S108
        self.assertEqual(args.llm_model_alias, "llm_alias.json")
        self.assertFalse(args.use_openai)
        self.assertFalse(args.confirm_llm)

    def test_schema_requires_llm_authority_none(self) -> None:
        schema = schema_for("LLMSignalProposal")

        self.assertIn("llm_authority", schema["required"])
        self.assertEqual(schema["properties"]["llm_authority"]["enum"], ["none"])
        self.assertEqual(schema["properties"]["action"]["enum"], ["buy", "hold"])

    def test_schema_validation_rejects_bad_action_and_confidence_range(self) -> None:
        with self.assertRaises(ValueError):
            validate_against_schema(
                "LLMSignalProposal",
                {
                    "symbol": "SPY",
                    "action": "sell",
                    "confidence": 0.5,
                    "thesis": "bad action",
                    "risk_notes": ["paper only"],
                    "evidence_refs": ["model_signal:SPY:2026-06-16"],
                    "llm_authority": "none",
                },
            )
        with self.assertRaises(ValueError):
            validate_against_schema(
                "LLMSignalProposal",
                {
                    "symbol": "SPY",
                    "action": "buy",
                    "confidence": 1.5,
                    "thesis": "bad confidence",
                    "risk_notes": ["paper only"],
                    "evidence_refs": ["model_signal:SPY:2026-06-16"],
                    "llm_authority": "none",
                },
            )

    def test_deterministic_proposals_shadow_model_signals_without_openai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(
                root,
                [
                    {
                        "timestamp": "2026-06-16",
                        "symbol": "SPY",
                        "probability": 0.81,
                        "threshold": 0.5,
                        "action": "buy",
                    },
                    {
                        "timestamp": "2026-06-16",
                        "symbol": "QQQ",
                        "probability": 0.42,
                        "threshold": 0.5,
                        "action": "hold",
                    },
                ],
            )

            exit_code = main(
                [
                    "llm-signal-proposals",
                    "--as-of-date",
                    "2026-06-16",
                    "--readiness",
                    str(readiness),
                    "--features",
                    str(features),
                    "--model-signals",
                    str(model_signals),
                    "--output-dir",
                    str(root / "proposals"),
                ]
            )
            payload = read_json(root / "proposals" / "2026-06-16" / "llm_signal_proposals.json")
            markdown = (root / "proposals" / "2026-06-16" / "llm_signal_proposals.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertFalse(payload["use_openai"])
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertRegex(payload["input_hashes"]["readiness"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["input_hashes"]["features"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["input_hashes"]["model_signals"], r"^[0-9a-f]{64}$")
        proposals = {item["symbol"]: item for item in payload["proposals"]}
        self.assertEqual(proposals["SPY"]["action"], "buy")
        self.assertEqual(proposals["QQQ"]["action"], "hold")
        self.assertEqual(proposals["SPY"]["llm_authority"], "none")
        self.assertIn("model_signal:SPY:2026-06-16", proposals["SPY"]["evidence_refs"])
        self.assertIn("| `SPY` | `buy` |", markdown)

    def test_openai_mode_requires_explicit_confirmation_before_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(root, [])

            exit_code = main(
                [
                    "llm-signal-proposals",
                    "--as-of-date",
                    "2026-06-16",
                    "--readiness",
                    str(readiness),
                    "--features",
                    str(features),
                    "--model-signals",
                    str(model_signals),
                    "--output-dir",
                    str(root / "proposals"),
                    "--use-openai",
                ]
            )
            payload = read_json(root / "proposals" / "2026-06-16" / "llm_signal_proposals.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("missing_confirm_llm", [error["code"] for error in payload["errors"]])
        self.assertTrue(payload["external_llm_requested"])
        self.assertFalse(payload["external_llm_used"])
        self.assertFalse(payload["safety"]["credentials_read"])
        self.assertFalse(payload["safety"]["broker_client_built"])

    def test_confirmed_openai_mode_is_blocked_without_api_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(root, [])

            exit_code = main(
                [
                    "llm-signal-proposals",
                    "--as-of-date",
                    "2026-06-16",
                    "--readiness",
                    str(readiness),
                    "--features",
                    str(features),
                    "--model-signals",
                    str(model_signals),
                    "--output-dir",
                    str(root / "proposals"),
                    "--use-openai",
                    "--confirm-llm",
                ]
            )
            payload = read_json(root / "proposals" / "2026-06-16" / "llm_signal_proposals.json")
            markdown = (root / "proposals" / "2026-06-16" / "llm_signal_proposals.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("external_llm_api_disabled", [error["code"] for error in payload["errors"]])
        self.assertTrue(payload["external_llm_requested"])
        self.assertFalse(payload["external_llm_used"])
        self.assertFalse(payload["use_openai"])
        self.assertIsNone(payload["model"])
        self.assertIn("OpenAI used: `False`", markdown)

    def test_active_llm_model_alias_is_reported_for_signal_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.7, "threshold": 0.5, "action": "buy"}],
            )
            alias = write_json(
                root / "llm_alias.json",
                {
                    "alias_state": "ACTIVE_LLM_ALIAS",
                    "role_id": "signal_proposal_auditor",
                    "active_model": "gpt-5.5-ft-shadow",
                    "alias_hash": "b" * 64,
                    "expires_on": "2026-07-16",
                    "safety": {"paper_only": True},
                },
            )

            exit_code = main(
                [
                    "llm-signal-proposals",
                    "--as-of-date",
                    "2026-06-16",
                    "--readiness",
                    str(readiness),
                    "--features",
                    str(features),
                    "--model-signals",
                    str(model_signals),
                    "--llm-model-alias",
                    str(alias),
                    "--output-dir",
                    str(root / "proposals"),
                ]
            )
            payload = read_json(root / "proposals" / "2026-06-16" / "llm_signal_proposals.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["llm_model_route"]["route_state"], "PAPER_ALIAS")
        self.assertEqual(payload["llm_model_route"]["active_model"], "gpt-5.5-ft-shadow")

    def test_openai_model_default_can_be_resolved_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(root, [])
            old_value = os.environ.get("TRADING_AI_OPENAI_MODEL")
            os.environ["TRADING_AI_OPENAI_MODEL"] = "gpt-env-test"
            try:
                exit_code = main(
                    [
                        "llm-signal-proposals",
                        "--as-of-date",
                        "2026-06-16",
                        "--readiness",
                        str(readiness),
                        "--features",
                        str(features),
                        "--model-signals",
                        str(model_signals),
                        "--output-dir",
                        str(root / "proposals"),
                        "--use-openai",
                    ]
                )
            finally:
                if old_value is None:
                    os.environ.pop("TRADING_AI_OPENAI_MODEL", None)
                else:
                    os.environ["TRADING_AI_OPENAI_MODEL"] = old_value
            payload = read_json(root / "proposals" / "2026-06-16" / "llm_signal_proposals.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["model_policy"]["model"], "gpt-env-test")
        self.assertEqual(payload["model_policy"]["source"], "env")


def write_readiness(root: Path) -> Path:
    return write_json(
        root / "readiness.json",
        {
            "status": "READY",
            "ready_for_paper_daily": True,
            "as_of_date": "2026-06-16",
            "approved_dataset": {"symbols": ["SPY", "QQQ"], "end": "2026-06-16"},
            "safety": {"credentials_read": False, "live_trading_allowed": False},
        },
    )


def write_features(root: Path) -> Path:
    path = root / "features.csv"
    path.write_text(
        "timestamp,symbol,momentum_20,realized_volatility_20\n2026-06-16,SPY,0.10,0.20\n2026-06-16,QQQ,-0.02,0.15\n",
        encoding="utf-8",
    )
    return path


def write_model_signals(root: Path, signals: list[dict[str, Any]]) -> Path:
    return write_json(
        root / "model_signals.json", {"signals": signals, "selected_signal": signals[0] if signals else None}
    )


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
