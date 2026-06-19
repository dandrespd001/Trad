import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-artifacts.sh"
CLEAN_SCRIPT = REPO_ROOT / "scripts" / "clean-local-artifacts.sh"
ENVIRONMENT_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-environment.sh"
GATES_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-gates.sh"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "verify-release.sh"
SAFE_DAILY_SCRIPT = REPO_ROOT / "scripts" / "run-paper-daily-safe.sh"


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

    def test_release_gate_wrapper_lists_quality_security_and_safety_gates(self) -> None:
        result = run_script(
            RELEASE_SCRIPT,
            env=release_env('python3 -c "raise SystemExit(0)"'),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for gate in (
            "paper environment",
            "focused paper tests",
            "paper gates",
            "full unittest suite",
            "live authorization safety scan",
            "futures execution parser scan",
            "ruff static lint",
            "mypy static typing",
            "pip dependency audit",
            "bandit security scan",
        ):
            self.assertIn(gate, result.stdout)

    def test_release_gate_defaults_are_scoped_to_current_milestone(self) -> None:
        script = RELEASE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("ruff check src tests --select E9,F63,F7,F82", script)
        self.assertIn("src/trading_ai/execution/paper_auto_cycle.py", script)
        self.assertIn("src/trading_ai/execution/paper_execute_session.py", script)
        self.assertIn("src/trading_ai/llm/factory.py", script)
        self.assertIn("src/trading_ai/llm/local_registry.py", script)
        self.assertIn("src/trading_ai/execution/llm_paper_review.py", script)
        self.assertIn("src/trading_ai/execution/llm_signal_proposals.py", script)
        self.assertIn("pip_audit --dry-run --cache-dir /tmp/pip-audit-cache", script)
        self.assertIn("bandit -q -ll -r src/trading_ai", script)

    def test_clean_local_artifacts_dry_run_does_not_target_reports_or_models(self) -> None:
        result = run_script(CLEAN_SCRIPT)

        self.assertIn(result.returncode, {0}, result.stderr + result.stdout)
        output = result.stdout + result.stderr
        self.assertNotIn("reports/tmp", output)
        self.assertNotIn("data/raw/approved", output)
        self.assertNotIn("models/latest_model.json", output)

    def test_safe_daily_rejects_relative_dates_before_running_cycle(self) -> None:
        result = run_script(
            SAFE_DAILY_SCRIPT,
            "--as-of-date",
            "today",
            "--from",
            "2026-03-01",
            "--to",
            "2026-06-16",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("relative or invalid date rejected", result.stderr + result.stdout)

    def test_safe_daily_requires_clean_state_for_confirmed_auto(self) -> None:
        result = run_script(
            SAFE_DAILY_SCRIPT,
            "--as-of-date",
            "2026-06-16",
            "--from",
            "2026-03-01",
            "--to",
            "2026-06-16",
            "--confirm-paper-auto",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--confirm-paper-auto requires --require-clean-state", result.stderr + result.stdout)

    def test_docs_reference_release_gate_and_quickstart(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = (REPO_ROOT / "docs" / "paper-quickstart.md").read_text(encoding="utf-8")

        self.assertIn("scripts/verify-release.sh", readme)
        self.assertIn("docs/paper-quickstart.md", readme)
        self.assertIn("scripts/run-paper-daily-safe.sh", quickstart)
        self.assertIn("Live trading remains out of scope", quickstart)


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


def release_env(command: str) -> dict[str, str]:
    return {
        "VERIFY_RELEASE_ENVIRONMENT_CMD": command,
        "VERIFY_RELEASE_FOCUSED_CMD": command,
        "VERIFY_RELEASE_PAPER_GATES_CMD": command,
        "VERIFY_RELEASE_FULL_TEST_CMD": command,
        "VERIFY_RELEASE_DIFF_CMD": command,
        "VERIFY_RELEASE_MODEL_CMD": command,
        "VERIFY_RELEASE_LIVE_SCAN_CMD": command,
        "VERIFY_RELEASE_FUTURES_SCAN_CMD": command,
        "VERIFY_RELEASE_RUFF_CMD": command,
        "VERIFY_RELEASE_MYPY_CMD": command,
        "VERIFY_RELEASE_PIP_AUDIT_CMD": command,
        "VERIFY_RELEASE_BANDIT_CMD": command,
    }


def write_json(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
