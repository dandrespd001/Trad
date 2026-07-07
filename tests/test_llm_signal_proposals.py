import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main
from trading_ai.execution.llm_signal_proposals import _apply_indicator_evidence_gate, run_llm_signal_proposals
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
                "--ai-features",
                "ai_features.csv",
                "--forecast-features",
                "forecast_features.csv",
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
        self.assertEqual(args.ai_features, "ai_features.csv")
        self.assertEqual(args.forecast_features, "forecast_features.csv")
        self.assertEqual(args.output_dir, "/tmp/proposals")  # noqa: S108
        self.assertEqual(args.llm_model_alias, "llm_alias.json")
        self.assertFalse(args.use_openai)
        self.assertFalse(args.confirm_llm)

    def test_schema_requires_llm_authority_none(self) -> None:
        schema = schema_for("LLMSignalProposal")

        self.assertIn("proposal_kind", schema["required"])
        self.assertIn("llm_authority", schema["required"])
        self.assertIn("model_id", schema["required"])
        self.assertIn("prompt_version", schema["required"])
        self.assertIn("input_hashes", schema["required"])
        self.assertEqual(schema["properties"]["llm_authority"]["enum"], ["none"])
        self.assertEqual(
            schema["properties"]["action"]["enum"],
            ["buy", "hold", "close", "reduce", "tighten_stop", "update_take_profit", "no_action"],
        )

    def test_schema_validation_rejects_bad_action_and_confidence_range(self) -> None:
        with self.assertRaises(ValueError):
            validate_against_schema(
                "LLMSignalProposal",
                {
                    "proposal_kind": "entry",
                    "symbol": "SPY",
                    "action": "sell",
                    "confidence": 0.5,
                    "time_horizon": "1d",
                    "thesis": "bad action",
                    "risk_notes": ["paper only"],
                    "evidence_refs": ["model_signal:SPY:2026-06-16"],
                    "model_id": "deterministic-shadow",
                    "prompt_version": "signal_proposal_auditor:v1",
                    "input_hashes": {},
                    "llm_authority": "none",
                },
            )
        with self.assertRaises(ValueError):
            validate_against_schema(
                "LLMSignalProposal",
                {
                    "proposal_kind": "entry",
                    "symbol": "SPY",
                    "action": "buy",
                    "confidence": 1.5,
                    "time_horizon": "1d",
                    "thesis": "bad confidence",
                    "risk_notes": ["paper only"],
                    "evidence_refs": ["model_signal:SPY:2026-06-16"],
                    "model_id": "deterministic-shadow",
                    "prompt_version": "signal_proposal_auditor:v1",
                    "input_hashes": {},
                    "llm_authority": "none",
                },
            )

    def test_schema_validation_rejects_executable_order_fields(self) -> None:
        with self.assertRaises(ValueError):
            validate_against_schema(
                "LLMSignalProposal",
                {
                    "proposal_kind": "entry",
                    "symbol": "SPY",
                    "action": "buy",
                    "confidence": 0.7,
                    "time_horizon": "1d",
                    "thesis": "attempts to carry executable sizing",
                    "risk_notes": ["paper only"],
                    "evidence_refs": ["model_signal:SPY:2026-06-16"],
                    "model_id": "deterministic-shadow",
                    "prompt_version": "signal_proposal_auditor:v1",
                    "input_hashes": {},
                    "notional": 1000,
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
        self.assertEqual(proposals["SPY"]["proposal_kind"], "entry")
        self.assertEqual(proposals["SPY"]["time_horizon"], "1d")
        self.assertEqual(proposals["SPY"]["model_id"], "deterministic-shadow")
        self.assertEqual(proposals["SPY"]["prompt_version"], "signal_proposal_auditor:v1")
        self.assertEqual(proposals["SPY"]["input_hashes"], payload["input_hashes"])
        self.assertEqual(proposals["SPY"]["llm_authority"], "none")
        self.assertIn("model_signal:SPY:2026-06-16", proposals["SPY"]["evidence_refs"])
        self.assertIn("| `SPY` | `buy` |", markdown)

    def test_deterministic_proposals_include_ai_feature_provenance_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            ai_features = write_ai_features(root / "ai_features.csv")
            forecast_features = write_ai_features(
                root / "forecast_features.csv",
                body="timestamp,symbol,forecast_return_1d,forecast_confidence\n2026-06-16,SPY,0.02,0.6\n",
            )
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.81, "threshold": 0.5, "action": "buy"}],
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
                    "--ai-features",
                    str(ai_features),
                    "--forecast-features",
                    str(forecast_features),
                    "--model-signals",
                    str(model_signals),
                    "--output-dir",
                    str(root / "proposals"),
                ]
            )
            payload = read_json(root / "proposals" / "2026-06-16" / "llm_signal_proposals.json")

        self.assertEqual(exit_code, 0)
        self.assertRegex(payload["input_hashes"]["ai_features"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["input_hashes"]["forecast_features"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["sources"]["ai_features"], str(ai_features))
        self.assertEqual(payload["sources"]["forecast_features"], str(forecast_features))
        self.assertEqual(payload["proposals"][0]["input_hashes"], payload["input_hashes"])
        self.assertEqual(payload["authority"]["llm_authority"], "none")

    def test_deterministic_proposals_can_emit_position_management_actions(self) -> None:
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
                        "probability": 0.35,
                        "threshold": 0.5,
                        "action": "close",
                    }
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

        self.assertEqual(exit_code, 0)
        proposal = payload["proposals"][0]
        self.assertEqual(proposal["proposal_kind"], "position_management")
        self.assertEqual(proposal["action"], "close")
        self.assertEqual(proposal["llm_authority"], "none")

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

    def test_buy_with_valid_indicator_citations_passes_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.81, "threshold": 0.5, "action": "buy"}],
            )

            result = run_llm_signal_proposals(
                as_of_date="2026-06-16",
                readiness=readiness,
                features=features,
                model_signals=model_signals,
                output_dir=str(root / "proposals"),
                available_indicators=["momentum_20", "realized_volatility_20"],
            )

        self.assertEqual(result.status, "OK")
        proposal = result.payload["proposals"][0]
        self.assertEqual(proposal["action"], "buy")
        self.assertNotIn("degraded", proposal)
        self.assertEqual(sorted(proposal["indicator_evidence"]), ["momentum_20", "realized_volatility_20"])
        self.assertEqual(result.payload["indicator_vocabulary"], ["momentum_20", "realized_volatility_20"])

    def test_buy_without_matching_indicators_degrades_as_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.81, "threshold": 0.5, "action": "buy"}],
            )

            result = run_llm_signal_proposals(
                as_of_date="2026-06-16",
                readiness=readiness,
                features=features,
                model_signals=model_signals,
                output_dir=str(root / "proposals"),
                available_indicators=["rsi_14"],  # not present in the features artifact for SPY
            )

        self.assertEqual(result.status, "OK")
        proposal = result.payload["proposals"][0]
        self.assertEqual(proposal["action"], "no_action")
        self.assertTrue(proposal["degraded"])
        self.assertEqual(proposal["original_action"], "buy")
        self.assertEqual(proposal["degradation_reason"], "indicator_evidence_missing")
        self.assertEqual(proposal["indicator_evidence"], [])
        self.assertEqual(proposal["llm_authority"], "none")

    def test_hold_without_citations_is_not_degraded_when_vocabulary_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "QQQ", "probability": 0.42, "threshold": 0.5, "action": "hold"}],
            )

            result = run_llm_signal_proposals(
                as_of_date="2026-06-16",
                readiness=readiness,
                features=features,
                model_signals=model_signals,
                output_dir=str(root / "proposals"),
                available_indicators=["momentum_20", "realized_volatility_20"],
            )

        proposal = result.payload["proposals"][0]
        self.assertEqual(proposal["action"], "hold")
        self.assertNotIn("degraded", proposal)
        self.assertEqual(proposal["indicator_evidence"], [])

    def test_management_proposal_can_be_grounded_or_degraded_like_a_decision(self) -> None:
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
                        "probability": 0.35,
                        "threshold": 0.5,
                        "action": "close",
                    }
                ],
            )

            grounded = run_llm_signal_proposals(
                as_of_date="2026-06-16",
                readiness=readiness,
                features=features,
                model_signals=model_signals,
                output_dir=str(root / "proposals_grounded"),
                available_indicators=["momentum_20", "realized_volatility_20"],
            )
            missing = run_llm_signal_proposals(
                as_of_date="2026-06-16",
                readiness=readiness,
                features=features,
                model_signals=model_signals,
                output_dir=str(root / "proposals_missing"),
                available_indicators=["rsi_14"],
            )

        grounded_proposal = grounded.payload["proposals"][0]
        self.assertEqual(grounded_proposal["action"], "close")
        self.assertNotIn("degraded", grounded_proposal)
        self.assertEqual(sorted(grounded_proposal["indicator_evidence"]), ["momentum_20", "realized_volatility_20"])

        missing_proposal = missing.payload["proposals"][0]
        self.assertEqual(missing_proposal["action"], "no_action")
        self.assertTrue(missing_proposal["degraded"])
        self.assertEqual(missing_proposal["original_action"], "close")
        self.assertEqual(missing_proposal["degradation_reason"], "indicator_evidence_missing")

    def test_indicator_vocabulary_defaults_to_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root)
            features = write_features(root)
            model_signals = write_model_signals(root, [])

            result = run_llm_signal_proposals(
                as_of_date="2026-06-16",
                readiness=readiness,
                features=features,
                model_signals=model_signals,
                output_dir=str(root / "proposals"),
            )

        self.assertEqual(result.payload["indicator_vocabulary"], [])


class IndicatorEvidenceGateTests(unittest.TestCase):
    """Direct unit tests of the anti-hallucination gate for LLM-authored citations.

    These construct proposals the way an LLM response would look (post schema
    validation, pre-gate) to exercise rules that a rule-based, non-LLM caller can
    never trigger on its own (citing a name that was never real, or returning a
    malformed evidence list) — the deterministic baseline only ever cites names it
    already found present in the real feature row, so it cannot hallucinate.
    """

    def _buy_proposal(self, **overrides: Any) -> dict[str, Any]:
        proposal = {
            "proposal_kind": "entry",
            "symbol": "SPY",
            "action": "buy",
            "confidence": 0.7,
            "time_horizon": "1d",
            "thesis": "test",
            "risk_notes": ["paper-only shadow proposal"],
            "evidence_refs": ["model_signal:SPY:2026-06-16"],
            "model_id": "test-model",
            "prompt_version": "signal_proposal_auditor:v1",
            "input_hashes": {},
            "llm_authority": "none",
        }
        proposal.update(overrides)
        return proposal

    def test_hallucinated_indicator_name_degrades_whole_proposal(self) -> None:
        proposal = self._buy_proposal(indicator_evidence=["momentum_20", "rsi_999"])

        gated = _apply_indicator_evidence_gate(
            proposal, available_indicators=frozenset({"momentum_20", "realized_volatility_20"})
        )

        self.assertEqual(gated["action"], "no_action")
        self.assertEqual(gated["original_action"], "buy")
        self.assertTrue(gated["degraded"])
        self.assertEqual(gated["degradation_reason"], "indicator_evidence_unknown")
        self.assertEqual(gated["proposal_kind"], "entry")

    def test_five_citations_degrade_as_malformed(self) -> None:
        vocabulary = frozenset({"a", "b", "c", "d", "e"})
        proposal = self._buy_proposal(indicator_evidence=["a", "b", "c", "d", "e"])

        gated = _apply_indicator_evidence_gate(proposal, available_indicators=vocabulary)

        self.assertEqual(gated["action"], "no_action")
        self.assertTrue(gated["degraded"])
        self.assertEqual(gated["degradation_reason"], "indicator_evidence_malformed")

    def test_non_string_citation_degrades_as_malformed(self) -> None:
        proposal = self._buy_proposal(indicator_evidence=["momentum_20", 42])

        gated = _apply_indicator_evidence_gate(proposal, available_indicators=frozenset({"momentum_20"}))

        self.assertEqual(gated["action"], "no_action")
        self.assertEqual(gated["degradation_reason"], "indicator_evidence_malformed")

    def test_empty_vocabulary_with_citations_degrades_as_unverifiable(self) -> None:
        proposal = self._buy_proposal(indicator_evidence=["momentum_20"])

        gated = _apply_indicator_evidence_gate(proposal, available_indicators=frozenset())

        self.assertEqual(gated["action"], "no_action")
        self.assertTrue(gated["degraded"])
        self.assertEqual(gated["degradation_reason"], "indicator_evidence_unverifiable")

    def test_empty_vocabulary_without_citations_passes_intact(self) -> None:
        proposal = self._buy_proposal(indicator_evidence=[])

        gated = _apply_indicator_evidence_gate(proposal, available_indicators=frozenset())

        self.assertEqual(gated["action"], "buy")
        self.assertNotIn("degraded", gated)
        self.assertEqual(gated["indicator_evidence"], [])

    def test_hold_with_hallucinated_citation_is_still_degraded(self) -> None:
        proposal = self._buy_proposal(action="hold", indicator_evidence=["rsi_999"])

        gated = _apply_indicator_evidence_gate(proposal, available_indicators=frozenset({"momentum_20"}))

        self.assertEqual(gated["action"], "no_action")
        self.assertEqual(gated["original_action"], "hold")
        self.assertEqual(gated["degradation_reason"], "indicator_evidence_unknown")

    def test_hold_without_citations_never_requires_evidence(self) -> None:
        proposal = self._buy_proposal(action="hold", indicator_evidence=[])

        gated = _apply_indicator_evidence_gate(
            proposal, available_indicators=frozenset({"momentum_20", "realized_volatility_20"})
        )

        self.assertEqual(gated["action"], "hold")
        self.assertNotIn("degraded", gated)

    def test_degraded_proposal_never_reports_llm_authority_other_than_none(self) -> None:
        proposal = self._buy_proposal(indicator_evidence=["rsi_999"])

        gated = _apply_indicator_evidence_gate(proposal, available_indicators=frozenset({"momentum_20"}))

        self.assertEqual(gated["llm_authority"], "none")


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


def write_ai_features(
    path: Path,
    *,
    body: str = "timestamp,symbol,ai_sentiment_1d,ai_risk_1d,ai_confidence_1d\n2026-06-16,SPY,0.4,0.2,0.8\n",
) -> Path:
    path.write_text(body, encoding="utf-8")
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
