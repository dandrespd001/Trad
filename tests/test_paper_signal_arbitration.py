import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperSignalArbitrationTests(unittest.TestCase):
    def test_parser_defaults_for_signal_arbitration(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-signal-arbitration",
                "--as-of-date",
                "2026-06-16",
                "--model-signals",
                "signals.json",
                "--llm-proposals",
                "proposals.json",
                "--readiness",
                "readiness.json",
                "--output-dir",
                "/tmp/arbitration",  # noqa: S108
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.model_signals, "signals.json")
        self.assertEqual(args.llm_proposals, "proposals.json")
        self.assertEqual(args.readiness, "readiness.json")
        self.assertIsNone(args.shadow_plan)
        self.assertEqual(args.output_dir, "/tmp/arbitration")  # noqa: S108

    def test_baseline_buy_and_llm_buy_is_eligible_for_paper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(root, [{"symbol": "SPY", "action": "buy", "confidence": 0.77}])

            exit_code = main(
                arbitration_args(root, readiness=readiness, model_signals=model_signals, proposals=proposals)
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")
            markdown = (root / "arbitration" / "2026-06-16" / "signal_plan.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["decision"], "ELIGIBLE_FOR_PAPER")
        self.assertTrue(payload["eligible_for_paper"])
        self.assertEqual(payload["selected_symbol"], "SPY")
        self.assertEqual(payload["selected_signal"]["action"], "buy")
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertIn("Decision: **ELIGIBLE_FOR_PAPER**", markdown)

    def test_llm_buy_and_baseline_hold_requires_review_without_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.44, "threshold": 0.5, "action": "hold"}],
            )
            proposals = write_llm_proposals(root, [{"symbol": "SPY", "action": "buy", "confidence": 0.64}])

            exit_code = main(
                arbitration_args(root, readiness=readiness, model_signals=model_signals, proposals=proposals)
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["decision"], "NO_TRADE_REVIEW")
        self.assertFalse(payload["eligible_for_paper"])
        self.assertIsNone(payload["selected_signal"])
        self.assertIn("baseline_llm_disagree", reason_codes(payload))

    def test_stale_readiness_blocks_signal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-15")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(root, [{"symbol": "SPY", "action": "buy", "confidence": 0.77}])

            exit_code = main(
                arbitration_args(root, readiness=readiness, model_signals=model_signals, proposals=proposals)
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["decision"], "BLOCKED")
        self.assertFalse(payload["eligible_for_paper"])
        self.assertIn("dataset_stale", reason_codes(payload))

    def test_invalid_llm_proposal_schema_blocks_signal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(root, [{"symbol": "SPY", "action": "sell", "confidence": 1.25}])

            exit_code = main(
                arbitration_args(root, readiness=readiness, model_signals=model_signals, proposals=proposals)
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["decision"], "BLOCKED")
        self.assertFalse(payload["eligible_for_paper"])
        self.assertIn("invalid_llm_proposal_schema", reason_codes(payload))

    def test_llm_proposal_input_hash_mismatch_blocks_signal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            features = write_features(root)
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(
                root,
                [{"symbol": "SPY", "action": "buy", "confidence": 0.77}],
                input_hashes={
                    "readiness": "0" * 64,
                    "features": sha256_file(features),
                    "model_signals": sha256_file(model_signals),
                },
            )

            exit_code = main(
                arbitration_args(
                    root, readiness=readiness, features=features, model_signals=model_signals, proposals=proposals
                )
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["decision"], "BLOCKED")
        self.assertIn("readiness_hash_mismatch", reason_codes(payload))

    def test_llm_proposal_feature_hash_mismatch_blocks_signal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            features = write_features(root, body="timestamp,symbol\n2026-06-16,SPY\n")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(
                root,
                [{"symbol": "SPY", "action": "buy", "confidence": 0.77}],
                input_hashes={
                    "readiness": sha256_file(readiness),
                    "features": "0" * 64,
                    "model_signals": sha256_file(model_signals),
                },
            )

            exit_code = main(
                arbitration_args(
                    root, readiness=readiness, features=features, model_signals=model_signals, proposals=proposals
                )
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["decision"], "BLOCKED")
        self.assertIn("features_hash_mismatch", reason_codes(payload))

    def test_conflicting_duplicate_llm_symbol_blocks_signal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(
                root,
                [
                    {"symbol": "SPY", "action": "buy", "confidence": 0.77},
                    {"symbol": "SPY", "action": "hold", "confidence": 0.40},
                ],
            )

            exit_code = main(
                arbitration_args(root, readiness=readiness, model_signals=model_signals, proposals=proposals)
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["decision"], "BLOCKED")
        self.assertIn("duplicate_llm_proposal_symbol_conflict", reason_codes(payload))
        self.assertEqual(payload["collisions"][0]["symbol"], "SPY")

    def test_ready_shadow_plan_records_challenger_shadow_signals_without_affecting_paper_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_readiness(root, end="2026-06-16")
            model_signals = write_model_signals(
                root,
                [{"timestamp": "2026-06-16", "symbol": "SPY", "probability": 0.77, "threshold": 0.5, "action": "buy"}],
            )
            proposals = write_llm_proposals(root, [{"symbol": "SPY", "action": "buy", "confidence": 0.77}])
            shadow = write_shadow_plan(root / "shadow_plan.json", state="READY_FOR_SHADOW")

            exit_code = main(
                arbitration_args(
                    root, readiness=readiness, model_signals=model_signals, proposals=proposals, shadow_plan=shadow
                )
            )
            payload = read_json(root / "arbitration" / "2026-06-16" / "signal_plan.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["decision"], "ELIGIBLE_FOR_PAPER")
        self.assertTrue(payload["eligible_for_paper"])
        self.assertEqual(payload["shadow"]["state"], "READY_FOR_SHADOW")
        self.assertEqual(payload["shadow"]["selected_symbol"], "SPY")
        self.assertTrue(payload["shadow"]["shadow_only"])
        self.assertFalse(payload["shadow"]["affects_paper_order"])
        self.assertFalse(payload["safety"]["orders_submitted"])


def arbitration_args(
    root: Path,
    *,
    readiness: Path,
    model_signals: Path,
    proposals: Path,
    features: Path | None = None,
    shadow_plan: Path | None = None,
) -> list[str]:
    args = [
        "paper-signal-arbitration",
        "--as-of-date",
        "2026-06-16",
        "--model-signals",
        str(model_signals),
        "--llm-proposals",
        str(proposals),
        "--readiness",
        str(readiness),
        "--output-dir",
        str(root / "arbitration"),
    ]
    if features is not None:
        args.extend(["--features", str(features)])
    if shadow_plan is not None:
        args.extend(["--shadow-plan", str(shadow_plan)])
    return args


def write_readiness(root: Path, *, end: str) -> Path:
    return write_json(
        root / "readiness.json",
        {
            "status": "READY",
            "ready_for_paper_daily": True,
            "as_of_date": "2026-06-16",
            "approved_dataset": {"symbols": ["SPY"], "end": end},
            "safety": {"credentials_read": False, "live_trading_allowed": False},
        },
    )


def write_model_signals(root: Path, signals: list[dict[str, Any]]) -> Path:
    return write_json(root / "model_signals.json", {"signals": signals})


def write_features(root: Path, *, body: str = "timestamp,symbol,momentum_20\n2026-06-16,SPY,0.1\n") -> Path:
    path = root / "features.csv"
    path.write_text(body, encoding="utf-8")
    return path


def write_llm_proposals(
    root: Path,
    proposals: list[dict[str, Any]],
    *,
    input_hashes: dict[str, Any] | None = None,
) -> Path:
    normalized = []
    for proposal in proposals:
        item = {
            "thesis": "deterministic shadow proposal",
            "risk_notes": ["paper only"],
            "evidence_refs": [f"model_signal:{proposal['symbol']}:2026-06-16"],
            "llm_authority": "none",
            **proposal,
        }
        normalized.append(item)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "status": "OK",
        "proposals": normalized,
    }
    if input_hashes is not None:
        payload["input_hashes"] = input_hashes
    return write_json(root / "llm_signal_proposals.json", payload)


def write_shadow_plan(path: Path, *, state: str) -> Path:
    return write_json(
        path,
        {
            "shadow_state": state,
            "challenger": {"shadow_only": True, "promotes_model": False},
            "safety": {"broker_client_built": False, "orders_submitted": False, "live_trading_authorized": False},
        },
    )


def reason_codes(payload: dict[str, Any]) -> set[str]:
    return {str(reason.get("code")) for reason in payload.get("reasons", [])}


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
