import json
import tempfile
import unittest
from pathlib import Path

from trading_ai import config as config_module
from trading_ai.execution import paper_graduation
from trading_ai.execution.paper_graduation import evaluate_paper_graduation
from trading_ai.risk.policy import RiskLimits


class PaperGraduationTests(unittest.TestCase):
    def test_paper_stages_do_not_include_live_stages(self) -> None:
        expected = {"CANARY", "SCALE_UP", "READINESS"}

        self.assertEqual(config_module.PAPER_STAGES, expected)
        self.assertEqual(set(paper_graduation.PAPER_STAGES), expected)
        for stage in ("LIVE", "LIVE_CANARY", "LIVE_DRY_RUN", "LIVE_SCALE_UP"):
            self.assertNotIn(stage, config_module.PAPER_STAGES)
            self.assertNotIn(stage, paper_graduation.PAPER_STAGES)

    def test_canary_requires_one_dollar_notional(self) -> None:
        result = evaluate_paper_graduation(
            risk_limits=RiskLimits(paper_stage="CANARY", paper_notional_usd=2.0)
        )

        self.assertFalse(result["allowed"])
        self.assertIn("canary_notional_must_be_one", blocker_codes(result))

    def test_scale_up_requires_reviewer_reason_and_campaign_evidence(self) -> None:
        result = evaluate_paper_graduation(
            risk_limits=RiskLimits(paper_stage="SCALE_UP", paper_notional_usd=2.0)
        )

        self.assertFalse(result["allowed"])
        self.assertIn("paper_stage_reviewer_missing", blocker_codes(result))
        self.assertIn("paper_stage_reason_missing", blocker_codes(result))
        self.assertIn("campaign_report_missing", blocker_codes(result))

    def test_scale_up_records_campaign_evidence_hash_when_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            campaign_path = write_json(Path(temp_dir) / "campaign.json", campaign_payload())
            result = evaluate_paper_graduation(
                risk_limits=RiskLimits(
                    paper_stage="SCALE_UP",
                    paper_notional_usd=2.0,
                    paper_stage_reviewer="reviewer@example.com",
                    paper_stage_reason="30 clean paper trial days",
                ),
                campaign_report=read_json(campaign_path),
                campaign_report_path=campaign_path,
            )

        campaign = result["evidence"]["campaign_report"]  # type: ignore[index]
        self.assertTrue(result["allowed"])
        self.assertTrue(campaign["provided"])  # type: ignore[index]
        self.assertEqual(campaign["path"], str(campaign_path))  # type: ignore[index]
        self.assertIsNotNone(campaign["sha256"])  # type: ignore[index]

    def test_readiness_requires_phase_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            campaign_path = write_json(Path(temp_dir) / "campaign.json", campaign_payload())
            result = evaluate_paper_graduation(
                risk_limits=RiskLimits(
                    paper_stage="READINESS",
                    paper_notional_usd=2.0,
                    paper_stage_reviewer="reviewer@example.com",
                    paper_stage_reason="paper evidence ready for manual review",
                ),
                campaign_report=read_json(campaign_path),
                campaign_report_path=campaign_path,
            )

        self.assertFalse(result["allowed"])
        self.assertIn("phase_review_missing", blocker_codes(result))

    def test_readiness_records_campaign_and_phase_hashes_when_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            campaign_path = write_json(root / "campaign.json", campaign_payload())
            phase_path = write_json(root / "phase.json", phase_payload())
            result = evaluate_paper_graduation(
                risk_limits=RiskLimits(
                    paper_stage="READINESS",
                    paper_notional_usd=2.0,
                    paper_stage_reviewer="reviewer@example.com",
                    paper_stage_reason="paper evidence ready for manual review",
                ),
                campaign_report=read_json(campaign_path),
                campaign_report_path=campaign_path,
                phase_review=read_json(phase_path),
                phase_review_path=phase_path,
            )

        evidence = result["evidence"]  # type: ignore[assignment]
        campaign = evidence["campaign_report"]  # type: ignore[index]
        phase = evidence["phase_review"]  # type: ignore[index]
        self.assertTrue(result["allowed"])
        self.assertIsNotNone(campaign["sha256"])  # type: ignore[index]
        self.assertIsNotNone(phase["sha256"])  # type: ignore[index]
        self.assertEqual(phase["phase_status"], "READY_FOR_REVIEW")  # type: ignore[index]

    def test_campaign_live_authority_blocks_graduation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            campaign = campaign_payload()
            campaign["safety"] = {"live_trading_allowed": True}
            campaign_path = write_json(Path(temp_dir) / "campaign.json", campaign)
            result = evaluate_paper_graduation(
                risk_limits=RiskLimits(
                    paper_stage="SCALE_UP",
                    paper_notional_usd=2.0,
                    paper_stage_reviewer="reviewer@example.com",
                    paper_stage_reason="30 clean paper trial days",
                ),
                campaign_report=read_json(campaign_path),
                campaign_report_path=campaign_path,
            )

        self.assertFalse(result["allowed"])
        self.assertIn("campaign_live_trading_not_allowed", blocker_codes(result))


def campaign_payload() -> dict[str, object]:
    return {
        "status": "OK",
        "real_money_consideration": {
            "state": "PAPER_EVIDENCE_READY",
            "clean_trial_days": 30,
            "target_trial_days": 30,
            "recovery_days": 0,
            "error_days": 0,
        },
        "safety": {"live_trading_allowed": False, "live_trading_authorized": False},
    }


def phase_payload() -> dict[str, object]:
    return {
        "status": "OK",
        "phase_status": "READY_FOR_REVIEW",
        "review_only": True,
        "authority": {"live_trading_authorized": False},
        "safety": {"live_trading_allowed": False, "live_trading_authorized": False},
    }


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def blocker_codes(result: dict[str, object]) -> set[str]:
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        return set()
    return {str(item.get("code")) for item in blockers if isinstance(item, dict)}


if __name__ == "__main__":
    unittest.main()
