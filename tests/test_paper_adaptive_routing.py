import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_model_alias import resolve_paper_model_route


class PaperAdaptiveRoutingTests(unittest.TestCase):
    def test_challenger_signals_emit_shadow_only_model_signal_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_run = write_json(
                root / "model_run.json",
                {
                    "run_id": "candidate-1",
                    "model": {
                        "feature_names": ["momentum_20"],
                        "intercept": 0.0,
                        "coefficients": [3.0],
                    },
                },
            )
            features = write_text(root / "features.csv", "timestamp,symbol,momentum_20\n2026-06-16,SPY,1.0\n")
            readiness = write_json(
                root / "readiness.json",
                {
                    "status": "READY",
                    "ready_for_paper_daily": True,
                    "approved_dataset": {"symbols": ["SPY"], "end": "2026-06-16"},
                    "safety": {"credentials_read": False, "live_trading_allowed": False},
                },
            )

            exit_code = main(
                [
                    "paper-challenger-signals",
                    "--as-of-date",
                    "2026-06-16",
                    "--model-run",
                    str(model_run),
                    "--features",
                    str(features),
                    "--readiness",
                    str(readiness),
                    "--output-dir",
                    str(root / "out"),
                ]
            )
            payload = read_json(root / "out" / "2026-06-16" / "challenger_signals.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["signals"][0]["symbol"], "SPY")
        self.assertEqual(payload["signals"][0]["action"], "buy")
        self.assertTrue(payload["shadow_only"])
        self.assertFalse(payload["affects_paper_order"])

    def test_shadow_outcome_scorecard_and_alias_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = write_json(
                root / "signal_plan.json",
                {
                    "as_of_date": "2026-06-16",
                    "shadow": {
                        "selected_signal": {
                            "timestamp": "2026-06-16",
                            "symbol": "SPY",
                            "action": "buy",
                            "probability": 0.8,
                        },
                        "shadow_only": True,
                        "affects_paper_order": False,
                    },
                },
            )
            approved = root / "approved"
            approved.mkdir()
            write_text(
                approved / "ohlcv.csv",
                "timestamp,symbol,close\n2026-06-16,SPY,100\n2026-06-17,SPY,102\n",
            )
            ledger = root / "shadow_ledger.jsonl"

            outcome_exit = main(
                [
                    "paper-shadow-outcome-report",
                    "--as-of-date",
                    "2026-06-16",
                    "--signal-plan",
                    str(plan),
                    "--approved-dir",
                    str(approved),
                    "--ledger-output",
                    str(ledger),
                    "--output-dir",
                    str(root / "outcome"),
                ]
            )
            scorecard_exit = main(
                [
                    "paper-shadow-scorecard",
                    "--ledger-input",
                    str(ledger),
                    "--phase-review",
                    str(write_json(root / "phase.json", {"phase_status": "READY_FOR_REVIEW"})),
                    "--paper-performance",
                    str(write_json(root / "performance.json", {"status": "OK", "paper_metrics": {"rejections": 0}})),
                    "--min-shadow-trades",
                    "1",
                    "--output-dir",
                    str(root / "scorecard"),
                ]
            )
            candidate = write_json(
                root / "candidate_model_run.json",
                {
                    "run_id": "candidate-1",
                    "model": {"feature_names": ["momentum_20"], "intercept": 0.0, "coefficients": [1.0]},
                },
            )
            latest = write_json(
                root / "latest_model.json",
                {"feature_names": ["momentum_20"], "intercept": 0.0, "coefficients": [0.0]},
            )
            alias_exit = main(
                [
                    "paper-model-alias-decision",
                    "--shadow-scorecard",
                    str(root / "scorecard" / "shadow_scorecard.json"),
                    "--review-decision",
                    str(write_json(root / "review.json", {"decision": "APPROVE_FOR_NEXT_PAPER_CYCLE"})),
                    "--candidate-model-run",
                    str(candidate),
                    "--latest-model",
                    str(latest),
                    "--reviewer",
                    "human",
                    "--reason",
                    "shadow evidence ready",
                    "--output-dir",
                    str(root / "alias"),
                ]
            )
            outcome = read_json(root / "outcome" / "2026-06-16" / "shadow_outcome.json")
            scorecard = read_json(root / "scorecard" / "shadow_scorecard.json")
            alias = read_json(root / "alias" / "current.json")
            latest_payload = read_json(latest)

        self.assertEqual(outcome_exit, 0)
        self.assertEqual(scorecard_exit, 0)
        self.assertEqual(alias_exit, 0)
        self.assertEqual(outcome["state"], "RECORDED")
        self.assertEqual(scorecard["scorecard_state"], "READY_FOR_PAPER_ALIAS")
        self.assertEqual(alias["alias_state"], "ACTIVE_PAPER_ALIAS")
        self.assertNotEqual(latest_payload["coefficients"], [1.0])

    def test_shadow_ledger_records_no_signal_and_blocked_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved = root / "approved"
            approved.mkdir()
            write_text(
                approved / "ohlcv.csv",
                (
                    "timestamp,symbol,close\n"
                    "2026-06-16,SPY,100\n"
                    "2026-06-17,SPY,102\n"
                    "2026-06-16,QQQ,100\n"
                ),
            )
            ledger = root / "shadow_ledger.jsonl"
            recorded_plan = write_json(root / "recorded_plan.json", shadow_plan("2026-06-16", "SPY", "buy"))
            no_signal_plan = write_json(root / "no_signal_plan.json", shadow_plan("2026-06-17", "SPY", "hold"))
            blocked_plan = write_json(root / "blocked_plan.json", shadow_plan("2026-06-16", "QQQ", "buy"))

            recorded_exit = main(shadow_outcome_args("2026-06-16", recorded_plan, approved, ledger, root / "outcomes"))
            no_signal_exit = main(shadow_outcome_args("2026-06-17", no_signal_plan, approved, ledger, root / "outcomes"))
            blocked_exit = main(shadow_outcome_args("2026-06-16", blocked_plan, approved, ledger, root / "outcomes"))
            scorecard_exit = main(
                [
                    "paper-shadow-scorecard",
                    "--ledger-input",
                    str(ledger),
                    "--phase-review",
                    str(write_json(root / "phase.json", {"phase_status": "READY_FOR_REVIEW"})),
                    "--paper-performance",
                    str(write_json(root / "performance.json", {"status": "OK", "paper_metrics": {"rejections": 0}})),
                    "--min-shadow-trades",
                    "1",
                    "--max-missing-outcome-rate-pct",
                    "5",
                    "--output-dir",
                    str(root / "scorecard"),
                ]
            )
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            scorecard = read_json(root / "scorecard" / "shadow_scorecard.json")

        self.assertEqual(recorded_exit, 0)
        self.assertEqual(no_signal_exit, 0)
        self.assertEqual(blocked_exit, 1)
        self.assertEqual(scorecard_exit, 1)
        self.assertEqual([record["state"] for record in records], ["RECORDED", "NO_SHADOW_SIGNAL", "BLOCKED"])
        self.assertEqual(scorecard["metrics"]["record_count"], 3.0)
        self.assertEqual(scorecard["metrics"]["no_shadow_signal_count"], 1.0)
        self.assertEqual(scorecard["metrics"]["blocked_outcome_count"], 1.0)
        self.assertEqual(scorecard["scorecard_state"], "BLOCKED")
        self.assertIn("missing_outcomes", scorecard["blockers"])

    def test_prepare_parser_accepts_paper_model_alias(self) -> None:
        args = build_parser().parse_args(
            [
                "prepare-paper-daily",
                "--approved-dir",
                "/tmp/approved",
                "--from",
                "2026-06-01",
                "--to",
                "2026-06-16",
                "--as-of-date",
                "2026-06-16",
                "--paper-model-alias",
                "/tmp/current.json",
            ]
        )

        self.assertEqual(args.paper_model_alias, "/tmp/current.json")

    def test_paper_alias_blocks_invalid_model_payload_at_route_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model = write_json(root / "paper_model.json", {"feature_names": ["momentum_20"], "intercept": 0.0})
            alias = write_json(root / "current.json", active_alias_payload(model, reviewer="human", reason="approved"))

            route = resolve_paper_model_route(
                signal_model=root / "latest_model.json",
                paper_model_alias=alias,
                as_of_date="2026-06-16",
            )

        self.assertEqual(route["route_state"], "BLOCKED")
        self.assertEqual(route["reason"], "alias_model_invalid")

    def test_paper_alias_blocks_missing_human_governance_at_route_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model = write_json(
                root / "paper_model.json",
                {"feature_names": ["momentum_20"], "intercept": 0.0, "coefficients": [1.0]},
            )
            alias = write_json(root / "current.json", active_alias_payload(model, reviewer="", reason=""))

            route = resolve_paper_model_route(
                signal_model=root / "latest_model.json",
                paper_model_alias=alias,
                as_of_date="2026-06-16",
            )

        self.assertEqual(route["route_state"], "BLOCKED")
        self.assertEqual(route["reason"], "alias_governance_invalid")


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def shadow_plan(as_of_date: str, symbol: str, action: str) -> dict[str, object]:
    return {
        "as_of_date": as_of_date,
        "shadow": {
            "selected_signal": {
                "timestamp": as_of_date,
                "symbol": symbol,
                "action": action,
                "probability": 0.8,
            },
            "shadow_only": True,
            "affects_paper_order": False,
        },
    }


def shadow_outcome_args(as_of_date: str, plan: Path, approved: Path, ledger: Path, output_dir: Path) -> list[str]:
    return [
        "paper-shadow-outcome-report",
        "--as-of-date",
        as_of_date,
        "--signal-plan",
        str(plan),
        "--approved-dir",
        str(approved),
        "--ledger-output",
        str(ledger),
        "--output-dir",
        str(output_dir),
    ]


def active_alias_payload(model: Path, *, reviewer: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "alias_state": "ACTIVE_PAPER_ALIAS",
        "active_model_path": str(model),
        "active_model_sha256": sha256(model),
        "candidate_model_run": str(model.parent / "candidate_model_run.json"),
        "created_on": "2026-06-16",
        "expires_on": "2026-07-16",
        "reviewer": reviewer,
        "reason": reason,
        "latest_model": {"path": str(model.parent / "latest_model.json"), "sha256": "abc", "mutated": False},
        "authority": {"human_review_required": True, "mutates_latest_model": False, "llm_authority": "none"},
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
