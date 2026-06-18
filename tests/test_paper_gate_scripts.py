import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-artifacts.sh"
ENVIRONMENT_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-environment.sh"
GATES_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-gates.sh"


class PaperGateScriptTests(unittest.TestCase):
    def test_environment_script_skip_research_reports_core_check(self) -> None:
        result = run_script(ENVIRONMENT_SCRIPT, "--skip-research")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("paper environment check passed", result.stdout)
        self.assertIn("python_version", result.stdout)
        self.assertIn("yaml", result.stdout)

    def test_artifact_gate_accepts_tmp_monitor_and_campaign_with_live_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_json(
                root / "reports" / "tmp" / "paper_monitor" / "latest.json",
                '{"stability": {"live_trading_authorized": false}}',
            )
            write_json(
                root / "reports" / "tmp" / "paper_campaign" / "latest.json",
                (
                    '{"progress": {"live_trading_authorized": false}, '
                    '"safety": {"live_trading_authorized": false}}'
                ),
            )

            result = run_script(ARTIFACT_SCRIPT, "--root", str(root))

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_artifact_gate_rejects_generated_reports_outside_reports_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_json(root / "reports" / "latest.json", '{"status": "old root output"}')

            result = run_script(ARTIFACT_SCRIPT, "--root", str(root))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reports/latest.json", result.stderr + result.stdout)
        self.assertIn("outside reports/tmp", result.stderr + result.stdout)

    def test_artifact_gate_rejects_live_authorized_monitor_or_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_json(
                root / "reports" / "tmp" / "paper_monitor" / "latest.json",
                '{"stability": {"live_trading_authorized": true}}',
            )

            result = run_script(ARTIFACT_SCRIPT, "--root", str(root))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live_trading_authorized", result.stderr + result.stdout)
        self.assertIn("paper_monitor/latest.json", result.stderr + result.stdout)

    def test_artifact_gate_rejects_live_allowed_true_in_any_report_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_json(
                root / "reports" / "tmp" / "paper_ops_check" / "latest.json",
                '{"safety": {"live_trading_allowed": true}}',
            )

            result = run_script(ARTIFACT_SCRIPT, "--root", str(root))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live_trading_allowed", result.stderr + result.stdout)
        self.assertIn("paper_ops_check/latest.json", result.stderr + result.stdout)

    def test_gate_wrapper_returns_zero_when_overridden_commands_pass(self) -> None:
        result = run_script(
            GATES_SCRIPT,
            env=gate_env(
                focused='python3 -c "raise SystemExit(0)"',
                full='python3 -c "raise SystemExit(0)"',
                diff='python3 -c "raise SystemExit(0)"',
                artifacts='python3 -c "raise SystemExit(0)"',
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("focused paper tests", result.stdout)
        self.assertIn("artifact policy", result.stdout)

    def test_gate_wrapper_returns_nonzero_when_a_gate_fails(self) -> None:
        result = run_script(
            GATES_SCRIPT,
            env=gate_env(
                focused='python3 -c "raise SystemExit(0)"',
                full='python3 -c "raise SystemExit(7)"',
                diff='python3 -c "raise SystemExit(0)"',
                artifacts='python3 -c "raise SystemExit(0)"',
            ),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full unittest suite", result.stdout)
        self.assertIn("FAILED", result.stdout)

    def test_docs_reference_versioned_gate_commands(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (REPO_ROOT / "docs" / "paper-real-runbook.md").read_text(encoding="utf-8")

        for command in (
            "scripts/verify-paper-environment.sh",
            "scripts/verify-paper-focused.sh",
            "scripts/verify-paper-artifacts.sh",
            "scripts/verify-paper-gates.sh",
        ):
            self.assertIn(command, readme)
            self.assertIn(command, runbook)

    def test_github_workflow_scans_live_true_assignment_and_mapping_forms(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "paper-gates.yml").read_text(encoding="utf-8")

        self.assertIn("live_trading_authorized", workflow)
        self.assertIn("live_trading_allowed", workflow)
        self.assertIn("([[:space:]]*[:=][[:space:]]*true)", workflow)
        self.assertIn("src configs scripts docs README.md .github", workflow)


def run_script(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(script), *args],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def gate_env(*, focused: str, full: str, diff: str, artifacts: str) -> dict[str, str]:
    return {
        "VERIFY_PAPER_FOCUSED_CMD": focused,
        "VERIFY_PAPER_FULL_CMD": full,
        "VERIFY_PAPER_DIFF_CMD": diff,
        "VERIFY_PAPER_ARTIFACT_CMD": artifacts,
    }


def write_json(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
