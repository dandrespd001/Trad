import os
import subprocess
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-artifacts.sh"
CLEAN_SCRIPT = REPO_ROOT / "scripts" / "clean-local-artifacts.sh"
ENVIRONMENT_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-environment.sh"
GATES_SCRIPT = REPO_ROOT / "scripts" / "verify-paper-gates.sh"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "verify-release.sh"
MINIMAL_RELEASE_SCRIPT = REPO_ROOT / "scripts" / "verify-release-minimal.sh"
SAFE_DAILY_SCRIPT = REPO_ROOT / "scripts" / "run-paper-daily-safe.sh"
AUTO_CYCLE_SCRIPT = REPO_ROOT / "scripts" / "run-paper-auto-cycle.sh"
TRAIN_LLM_SCRIPT = REPO_ROOT / "scripts" / "run-llm-local-training.sh"
PYTHON_RESOLVER_SCRIPT = REPO_ROOT / "scripts" / "lib" / "python-bin.sh"
SAFETY_PATTERN_SCRIPT = REPO_ROOT / "scripts" / "verify-safety-patterns.py"


class PaperGateScriptTests(unittest.TestCase):
    def test_environment_script_skip_research_reports_core_check(self) -> None:
        result = run_script(ENVIRONMENT_SCRIPT, "--skip-research")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("paper environment check passed", result.stdout)
        self.assertIn("python_version", result.stdout)
        self.assertIn("yaml", result.stdout)

    def test_python_resolver_prefers_project_venv312_over_path_python3(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            project_python = root / ".venv312" / "bin" / "python"
            project_python.parent.mkdir(parents=True)
            project_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            project_python.chmod(0o755)

            bin_dir = Path(temp_dir) / "path-bin"
            bin_dir.mkdir()
            fake_python = bin_dir / "python3"
            fake_python.write_text("#!/usr/bin/env bash\necho path-python3-used\n", encoding="utf-8")
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env.pop("PYTHON_BIN", None)
            env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'source "{PYTHON_RESOLVER_SCRIPT}"; resolve_python_bin "$1"',
                    "bash",
                    str(root),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(str(project_python), result.stdout.strip())
        self.assertNotIn("path-python3-used", result.stderr + result.stdout)

    def test_environment_script_respects_explicit_python_bin_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_python = Path(temp_dir) / "custom-python"
            payload = '{"status":"OK","checks":[{"name":"python_bin","ok":true,"detail":"override-python"}]}'
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                f"echo '{payload}'\n"
                "echo 'paper environment check passed: override-python'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            result = run_script(ENVIRONMENT_SCRIPT, "--skip-research", env={"PYTHON_BIN": str(fake_python)})

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("override-python", result.stdout)

    def test_artifact_gate_accepts_tmp_monitor_and_campaign_with_live_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_json(
                root / "reports" / "tmp" / "paper_monitor" / "latest.json",
                '{"stability": {"live_trading_authorized": false}}',
            )
            write_json(
                root / "reports" / "tmp" / "paper_campaign" / "latest.json",
                ('{"progress": {"live_trading_authorized": false}, "safety": {"live_trading_authorized": false}}'),
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
                focused="git-diff-check",
                full="git-diff-check",
                diff="git-diff-check",
                artifacts="git-diff-check",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("focused paper tests", result.stdout)
        self.assertIn("artifact policy", result.stdout)

    def test_gate_wrapper_returns_nonzero_when_a_gate_fails(self) -> None:
        result = run_script(
            GATES_SCRIPT,
            env=gate_env(
                focused="git-diff-check",
                full="totally-invalid-token",
                diff="git-diff-check",
                artifacts="git-diff-check",
            ),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full unittest suite", result.stdout)
        self.assertIn("invalid command token", result.stdout)

    def test_docs_reference_versioned_gate_commands(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (REPO_ROOT / "docs" / "paper-real-runbook.md").read_text(encoding="utf-8")

        for command in (
            "scripts/verify-paper-environment.sh",
            "scripts/verify-release-minimal.sh",
            "scripts/verify-paper-focused.sh",
            "scripts/verify-paper-artifacts.sh",
            "scripts/verify-paper-gates.sh",
        ):
            self.assertIn(command, readme)
            self.assertIn(command, runbook)

    def test_focused_gate_includes_forex_readiness_tests(self) -> None:
        script = (REPO_ROOT / "scripts" / "verify-paper-focused.sh").read_text(encoding="utf-8")

        self.assertIn("tests.test_futures_readiness_report", script)
        self.assertIn("tests.test_forex_readiness_report", script)
        self.assertIn("tests.test_cross_asset_session_plan", script)
        self.assertIn("tests.test_paper_telegram_status", script)
        self.assertIn("tests.test_paper_telegram_history", script)
        self.assertIn("tests.test_paper_telegram_send", script)
        self.assertIn("tests.test_paper_telegram_notify", script)

    def test_github_workflow_scans_live_true_assignment_and_mapping_forms(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "paper-gates.yml").read_text(encoding="utf-8")

        self.assertIn("verify-safety-patterns.py --mode live", workflow)
        self.assertIn("verify-safety-patterns.py --mode futures", workflow)
        self.assertNotIn("grep -R -n -E", workflow)

    def test_safety_pattern_scan_includes_json_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "configs") as temp_dir:
            fixture = Path(temp_dir) / "unsafe.json"
            fixture.write_text('{"live_trading_allowed": true}\n', encoding="utf-8")

            result = run_script(SAFETY_PATTERN_SCRIPT, "--mode", "live")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live_trading_allowed", result.stderr + result.stdout)
        self.assertIn("unsafe.json", result.stderr + result.stdout)

    def test_safety_pattern_scan_blocks_forex_execution_parsers(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "src") as temp_dir:
            fixture = Path(temp_dir) / "unsafe_forex_parser.py"
            command = "forex-" + "submit"
            fixture.write_text(f'subparsers.add_parser("{command}")\n', encoding="utf-8")

            result = run_script(SAFETY_PATTERN_SCRIPT, "--mode", "futures")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forex-submit", result.stderr + result.stdout)
        self.assertIn("futures/forex execution parser found", result.stderr + result.stdout)

    def test_release_gate_wrapper_lists_quality_security_and_safety_gates(self) -> None:
        result = run_script(
            RELEASE_SCRIPT,
            env=release_env(),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for gate in (
            "paper environment",
            "focused paper tests",
            "paper gates",
            "full unittest suite",
            "live authorization safety scan",
            "futures execution parser scan",
            "ruff critical lint",
            "coverage gate",
            "mypy scoped typing",
            "pip dependency audit",
            "bandit security scan",
        ):
            self.assertIn(gate, result.stdout)

    def test_release_gate_wrapper_rejects_invalid_override_token(self) -> None:
        result = run_script(
            RELEASE_SCRIPT,
            env=release_env(full="totally-invalid-token"),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid command token", result.stdout)

    def test_minimal_release_gate_uses_unittest_and_safety_scans_without_pytest_or_network(self) -> None:
        script = MINIMAL_RELEASE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("minimal full unittest suite", script)
        self.assertIn("minimal-focused-paper-tests", script)
        self.assertIn("verify-safety-patterns.py --mode live", script)
        self.assertIn("verify-safety-patterns.py --mode futures", script)
        self.assertIn("latest model unchanged", script)
        self.assertIn("-m unittest", script)
        self.assertIn("tests.test_live_readiness", script)
        self.assertIn("tests.test_config_loading", script)
        self.assertIn("tests.test_alpaca_paper_connection", script)
        self.assertIn("tests.test_alpaca_paper_execution", script)
        self.assertNotIn("unittest discover", script)
        self.assertNotIn("-m pytest", script)
        self.assertNotIn("pip_audit", script)
        self.assertNotIn("pip-audit", script)

    def test_minimal_release_gate_wrapper_accepts_safe_overrides(self) -> None:
        result = run_script(
            MINIMAL_RELEASE_SCRIPT,
            env=minimal_release_env(
                environment="git-diff-check",
                focused="git-diff-check",
                full="git-diff-check",
                diff="git-diff-check",
                model="git-diff-check",
                live_scan="git-diff-check",
                futures_scan="git-diff-check",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for gate in (
            "minimal paper environment",
            "minimal focused paper tests",
            "minimal full unittest suite",
            "minimal live authorization safety scan",
            "minimal futures execution parser scan",
        ):
            self.assertIn(gate, result.stdout)

    def test_minimal_release_gate_wrapper_rejects_invalid_override_token(self) -> None:
        result = run_script(
            MINIMAL_RELEASE_SCRIPT,
            env=minimal_release_env(full="totally-invalid-token"),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid command token", result.stdout)

    def test_release_gate_defaults_are_scoped_to_current_milestone(self) -> None:
        script = RELEASE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("ruff check src tests --select E9,F63,F7,F82", script)
        self.assertIn("ruff critical lint", script)
        self.assertIn("mypy scoped typing", script)
        self.assertIn("src/trading_ai/execution/paper_auto_cycle.py", script)
        self.assertIn("src/trading_ai/execution/paper_execute_session.py", script)
        self.assertIn("src/trading_ai/llm/factory.py", script)
        self.assertIn("src/trading_ai/llm/local_registry.py", script)
        self.assertIn("src/trading_ai/execution/llm_paper_review.py", script)
        self.assertIn("src/trading_ai/execution/llm_signal_proposals.py", script)
        self.assertIn("pip_audit --dry-run --cache-dir /tmp/pip-audit-cache", script)
        self.assertIn("pip-audit-network", script)
        self.assertIn("bandit -q -ll -r src/trading_ai", script)

    def test_release_coverage_gate_uses_real_coverage_not_pytest_shim(self) -> None:
        script = RELEASE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("COVERAGE_PYTHON_BIN", script)
        self.assertIn("-m coverage run -m unittest discover -s tests", script)
        self.assertIn("-m coverage report --fail-under=75", script)
        self.assertNotIn("-m pytest --cov", script)

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

    def test_paper_auto_cycle_wrapper_runs_environment_preflight_with_project_python(self) -> None:
        script = AUTO_CYCLE_SCRIPT.read_text(encoding="utf-8")
        preflight_index = script.index("scripts/verify-paper-environment.sh")
        cycle_index = script.index('"$PYTHON_BIN" -m trading_ai.cli paper-auto-cycle')

        self.assertLess(preflight_index, cycle_index)
        self.assertIn('source "$ROOT/scripts/lib/python-bin.sh"', script)
        self.assertIn('"$PYTHON_BIN" -m trading_ai.cli paper-auto-cycle', script)
        self.assertNotIn("PYTHONPATH=\"${PYTHONPATH:-src}\" python3 -m trading_ai.cli paper-auto-cycle", script)

    def test_paper_auto_cycle_wrapper_rejects_relative_dates_before_preflight(self) -> None:
        result = run_script(
            AUTO_CYCLE_SCRIPT,
            "--as-of-date",
            "today",
            "--from",
            "2026-03-01",
            "--to",
            "2026-06-23",
        )

        self.assertEqual(result.returncode, 2)
        output = result.stderr + result.stdout
        self.assertIn("relative or invalid date rejected", output)
        self.assertNotIn("paper environment check passed", output)

    def test_paper_auto_cycle_wrapper_requires_clean_state_for_confirmed_auto_before_preflight(self) -> None:
        result = run_script(
            AUTO_CYCLE_SCRIPT,
            "--as-of-date",
            "2026-06-23",
            "--from",
            "2026-03-01",
            "--to",
            "2026-06-23",
            "--confirm-paper-auto",
        )

        self.assertEqual(result.returncode, 2)
        output = result.stderr + result.stdout
        self.assertIn("--confirm-paper-auto requires --require-clean-state", output)
        self.assertNotIn("paper environment check passed", output)

    def test_paper_auto_cycle_wrapper_requires_operational_evidence_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = run_script(
                AUTO_CYCLE_SCRIPT,
                "--as-of-date",
                "2026-06-23",
                "--from",
                "2026-03-01",
                "--to",
                "2026-06-23",
                "--source",
                str(root / "fresh.csv"),
                "--require-operational-evidence",
                env={
                    "PAPER_AUTO_POSITION_WATCH": str(root / "missing_position_watch.json"),
                    "PAPER_AUTO_EOD_POSITION_PLAN": str(root / "missing_eod_position_plan.json"),
                    "PAPER_AUTO_CROSS_ASSET_SESSION_PLAN": str(root / "missing_cross_asset_session_plan.json"),
                    "PAPER_AUTO_TELEGRAM_DISPATCH": str(root / "missing_telegram_dispatch.json"),
                    "PAPER_AUTO_RISK_STATE": str(root / "missing_risk_state.json"),
                },
            )

        self.assertEqual(result.returncode, 2)
        output = result.stderr + result.stdout
        self.assertIn("required operational evidence missing", output)
        self.assertIn("missing_position_watch.json", output)
        self.assertIn("missing_risk_state.json", output)
        self.assertNotIn("paper environment check passed", output)

    def test_paper_auto_cycle_wrapper_rejects_invalid_autonomy_incident_sync_value(self) -> None:
        result = run_script(
            AUTO_CYCLE_SCRIPT,
            "--as-of-date",
            "2026-06-23",
            "--from",
            "2026-03-01",
            "--to",
            "2026-06-23",
            env={"AUTONOMY_INCIDENT_SYNC": "bogus"},
        )

        self.assertEqual(result.returncode, 2)
        output = result.stderr + result.stdout
        self.assertIn("invalid AUTONOMY_INCIDENT_SYNC value: bogus", output)
        self.assertNotIn("paper environment check passed", output)

    def test_paper_auto_cycle_wrapper_accepts_autonomy_incident_sync_falsy_values(self) -> None:
        script = AUTO_CYCLE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("AUTONOMY_INCIDENT_SYNC", script)
        self.assertIn("autonomy-incident-sync", script)
        self.assertIn("AUTONOMY_MARKET", script)
        for case in ("1|true|yes)", '0|false|no|"")'):
            self.assertIn(case, script)

    def test_paper_auto_cycle_wrapper_does_not_mask_cycle_exit_code(self) -> None:
        script = AUTO_CYCLE_SCRIPT.read_text(encoding="utf-8")

        cycle_index = script.index("cycle_exit=$?")
        exit_index = script.index('exit "$exit_code"')
        self.assertLess(cycle_index, exit_index)
        self.assertIn("if [ \"$sync_exit\" -gt \"$exit_code\" ]; then", script)

    def test_llm_local_training_script_blocks_missing_cache_without_download_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "registry.json"
            registry.write_text(
                '{"models":[{"model_id":"Qwen/Qwen3-0.6B","local_dir":"qwen3-0.6b"}]}',
                encoding="utf-8",
            )

            result = run_script(
                TRAIN_LLM_SCRIPT,
                "--role",
                "paper_ops_reviewer",
                "--model-id",
                "Qwen/Qwen3-0.6B",
                "--as-of-date",
                "2026-06-16",
                "--confirm-train",
                env={
                    "LLM_LOCAL_REGISTRY": str(registry),
                    "LLM_LOCAL_CACHE_ROOT": str(root / "weights"),
                    "PYTHON_BIN": None,
                },
            )

        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("--confirm-download", result.stderr + result.stdout)
        self.assertFalse((root / "weights" / "qwen3-0.6b").exists())

    def test_local_llm_extra_avoids_heavy_forecasting_packages(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        extras = pyproject["project"]["optional-dependencies"]
        local_llm = set(extras["local-llm"])

        for requirement in (
            "torch>=2.4,<3",
            "transformers>=4.45,<6",
            "accelerate>=1,<2",
            "peft>=0.13,<1",
            "trl>=0.12,<1",
            "datasets>=3,<5",
            "huggingface_hub>=0.23,<2",
            "safetensors>=0.4,<1",
        ):
            self.assertIn(requirement, local_llm)
        self.assertFalse(any(item.startswith("timesfm") for item in local_llm))
        self.assertFalse(any(item.startswith("chronos-forecasting") for item in local_llm))

    def test_training_script_suggests_local_llm_extra_for_download_dependency(self) -> None:
        script = TRAIN_LLM_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('pip install -e ".[local-llm]"', script)
        self.assertNotIn('pip install -e ".[forecasting]"', script)

    def test_training_script_passes_schema_specific_smoke_prompt(self) -> None:
        script = TRAIN_LLM_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("SMOKE_PROMPT=", script)
        self.assertIn('--prompt "$SMOKE_PROMPT"', script)
        self.assertIn("PaperOpsReview", script)
        self.assertIn("llm_authority", script)
        self.assertIn("READY_FOR_PAPER_CONFIRMATION", script)

    def test_docs_reference_release_gate_and_quickstart(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = (REPO_ROOT / "docs" / "paper-quickstart.md").read_text(encoding="utf-8")

        self.assertIn("scripts/verify-release.sh", readme)
        self.assertIn("scripts/verify-release-minimal.sh", readme)
        self.assertIn("scripts/verify-release-minimal.sh", quickstart)
        self.assertIn("docs/paper-quickstart.md", readme)
        self.assertIn("scripts/run-paper-daily-safe.sh", quickstart)
        self.assertIn("Live trading remains out of scope", quickstart)


def run_script(
    script: Path,
    *args: str,
    env: Mapping[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        for key, value in env.items():
            if value is None:
                merged_env.pop(key, None)
            else:
                merged_env[key] = value
    return subprocess.run(
        [str(script), *args],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def gate_env(*, focused: str, full: str, diff: str, artifacts: str) -> dict[str, str]:
    return {
        "VERIFY_PAPER_FOCUSED_CMD": focused,
        "VERIFY_PAPER_FULL_CMD": full,
        "VERIFY_PAPER_DIFF_CMD": diff,
        "VERIFY_PAPER_ARTIFACT_CMD": artifacts,
    }


def release_env(
    *,
    environment: str = "git-diff-check",
    focused: str = "git-diff-check",
    paper_gates: str = "git-diff-check",
    full: str = "git-diff-check",
    diff: str = "git-diff-check",
    model: str = "git-diff-check",
    live_scan: str = "git-diff-check",
    futures_scan: str = "git-diff-check",
    ruff: str = "git-diff-check",
    coverage: str = "git-diff-check",
    mypy: str = "git-diff-check",
    pip_audit: str = "git-diff-check",
    bandit: str = "git-diff-check",
) -> dict[str, str]:
    return {
        "VERIFY_RELEASE_ENVIRONMENT_CMD": environment,
        "VERIFY_RELEASE_FOCUSED_CMD": focused,
        "VERIFY_RELEASE_PAPER_GATES_CMD": paper_gates,
        "VERIFY_RELEASE_FULL_TEST_CMD": full,
        "VERIFY_RELEASE_DIFF_CMD": diff,
        "VERIFY_RELEASE_MODEL_CMD": model,
        "VERIFY_RELEASE_LIVE_SCAN_CMD": live_scan,
        "VERIFY_RELEASE_FUTURES_SCAN_CMD": futures_scan,
        "VERIFY_RELEASE_RUFF_CMD": ruff,
        "VERIFY_RELEASE_COVERAGE_CMD": coverage,
        "VERIFY_RELEASE_MYPY_CMD": mypy,
        "VERIFY_RELEASE_PIP_AUDIT_CMD": pip_audit,
        "VERIFY_RELEASE_BANDIT_CMD": bandit,
    }


def minimal_release_env(
    *,
    environment: str = "git-diff-check",
    focused: str = "git-diff-check",
    full: str = "git-diff-check",
    diff: str = "git-diff-check",
    model: str = "git-diff-check",
    live_scan: str = "git-diff-check",
    futures_scan: str = "git-diff-check",
) -> dict[str, str]:
    return {
        "VERIFY_RELEASE_MINIMAL_ENVIRONMENT_CMD": environment,
        "VERIFY_RELEASE_MINIMAL_FOCUSED_CMD": focused,
        "VERIFY_RELEASE_MINIMAL_FULL_TEST_CMD": full,
        "VERIFY_RELEASE_MINIMAL_DIFF_CMD": diff,
        "VERIFY_RELEASE_MINIMAL_MODEL_CMD": model,
        "VERIFY_RELEASE_MINIMAL_LIVE_SCAN_CMD": live_scan,
        "VERIFY_RELEASE_MINIMAL_FUTURES_SCAN_CMD": futures_scan,
    }


def write_json(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
