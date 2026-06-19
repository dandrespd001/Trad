import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class LiveReadinessTests(unittest.TestCase):
    def test_parser_accepts_live_readiness_inputs(self) -> None:
        args = build_parser().parse_args(
            [
                "live-readiness-report",
                "--as-of-date",
                "2026-06-16",
                "--phase-review",
                "/tmp/phase.json",
                "--campaign-report",
                "/tmp/campaign.json",
                "--performance-report",
                "/tmp/performance.json",
                "--permissions",
                "/tmp/permissions.yml",
                "--reviewer",
                "ops",
                "--reason",
                "paper trial complete",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.reviewer, "ops")

    def test_live_readiness_ready_for_canary_without_authorizing_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            phase = write_json(root / "phase.json", phase_payload("READY_FOR_REVIEW"))
            campaign = write_json(root / "campaign.json", campaign_payload("PAPER_EVIDENCE_READY"))
            performance = write_json(root / "performance.json", stable_performance())
            permissions = write_text(root / "permissions.yml", "live_trading_allowed: false\n")

            exit_code = main(live_args(root, phase, campaign, performance, permissions))
            payload = read_json(root / "live" / "2026-06-16" / "live_readiness.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["live_readiness_state"], "READY_FOR_LIVE_CANARY")
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertFalse(payload["safety"]["live_execution_enabled"])
        self.assertFalse(payload["canary_plan"]["live_adapter_implemented"])
        self.assertFalse(payload["canary_plan"]["orders_enabled"])
        self.assertEqual(payload["canary_plan"]["max_orders_per_day"], 1)
        self.assertEqual(payload["canary_plan"]["max_notional_usd"], 1.0)
        self.assertEqual(payload["canary_plan"]["approval_required"], "human_canary_approval")
        self.assertEqual(payload["next_action"], "human_canary_approval_required")

    def test_live_readiness_blocks_without_paper_evidence_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            phase = write_json(root / "phase.json", phase_payload("ACCUMULATING"))
            campaign = write_json(root / "campaign.json", campaign_payload("ACCUMULATING"))
            performance = write_json(root / "performance.json", {"status": "OK", "blockers": [], "safety": safe()})
            permissions = write_text(root / "permissions.yml", "live_trading_allowed: false\n")

            exit_code = main(live_args(root, phase, campaign, performance, permissions))
            payload = read_json(root / "live" / "2026-06-16" / "live_readiness.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["live_readiness_state"], "BLOCKED")
        self.assertIn("phase_review_not_ready", payload["blockers"])
        self.assertIn("paper_evidence_not_ready", payload["blockers"])

    def test_live_readiness_blocks_if_permissions_enable_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            phase = write_json(root / "phase.json", phase_payload("READY_FOR_REVIEW"))
            campaign = write_json(root / "campaign.json", campaign_payload("PAPER_EVIDENCE_READY"))
            performance = write_json(root / "performance.json", {"status": "OK", "blockers": [], "safety": safe()})
            permissions = write_text(root / "permissions.yml", "live_trading_allowed: true\n")

            exit_code = main(live_args(root, phase, campaign, performance, permissions))
            payload = read_json(root / "live" / "2026-06-16" / "live_readiness.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["live_readiness_state"], "BLOCKED")
        self.assertIn("live_permissions_must_remain_disabled", payload["blockers"])

    def test_live_readiness_blocks_when_performance_lacks_sixty_stable_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            phase = write_json(root / "phase.json", phase_payload("READY_FOR_REVIEW"))
            campaign = write_json(root / "campaign.json", campaign_payload("PAPER_EVIDENCE_READY"))
            performance = write_json(
                root / "performance.json",
                {
                    "status": "OK",
                    "blockers": [],
                    "paper_metrics": {"complete_sessions": 12, "performance_stable": False, "pending_closeouts": 0},
                    "safety": safe(),
                },
            )
            permissions = write_text(root / "permissions.yml", "live_trading_allowed: false\n")

            exit_code = main(live_args(root, phase, campaign, performance, permissions))
            payload = read_json(root / "live" / "2026-06-16" / "live_readiness.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["live_readiness_state"], "BLOCKED")
        self.assertIn("paper_stability_sessions_insufficient", payload["blockers"])
        self.assertIn("paper_performance_not_stable", payload["blockers"])


def live_args(root: Path, phase: Path, campaign: Path, performance: Path, permissions: Path) -> list[str]:
    return [
        "live-readiness-report",
        "--as-of-date",
        "2026-06-16",
        "--phase-review",
        str(phase),
        "--campaign-report",
        str(campaign),
        "--performance-report",
        str(performance),
        "--permissions",
        str(permissions),
        "--reviewer",
        "ops",
        "--reason",
        "paper trial complete",
        "--output-dir",
        str(root / "live"),
    ]


def phase_payload(state: str) -> dict[str, object]:
    return {
        "status": "OK" if state == "READY_FOR_REVIEW" else "WARN",
        "phase_status": state,
        "real_money_consideration": {"state": "PAPER_EVIDENCE_READY" if state == "READY_FOR_REVIEW" else "ACCUMULATING"},
        "safety": safe(),
        "authority": {"llm_authority": "none", "live_trading_authorized": False},
    }


def campaign_payload(state: str) -> dict[str, object]:
    return {
        "status": "OK" if state == "PAPER_EVIDENCE_READY" else "WARN",
        "real_money_consideration": {"state": state, "live_trading_authorized": False},
        "safety": safe(),
    }


def safe() -> dict[str, object]:
    return {"paper_only": True, "broker_client_built": False, "credentials_read": False, "orders_submitted": False, "live_trading_authorized": False, "live_trading_allowed": False}


def stable_performance() -> dict[str, object]:
    return {
        "status": "OK",
        "blockers": [],
        "paper_metrics": {"complete_sessions": 60, "performance_stable": True, "pending_closeouts": 0},
        "safety": safe(),
    }


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
