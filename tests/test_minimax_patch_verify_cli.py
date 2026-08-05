from __future__ import annotations

import argparse
import fcntl
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
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "skills" / "delegate-minimax-api" / "scripts" / "minimax_patch_verify.py"
WORKER = ROOT / "skills" / "delegate-minimax-api" / "scripts" / "minimax_api_worker.py"

HAS_LINUX_MEMFD_SEALS = hasattr(os, "memfd_create") and all(
    hasattr(fcntl, name)
    for name in (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_GROW",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_WRITE",
    )
)


def load_verifier_module() -> object:
    spec = importlib.util.spec_from_file_location("minimax_patch_verify_test_module", VERIFIER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load MiniMax patch verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_worker_module() -> object:
    spec = importlib.util.spec_from_file_location("minimax_api_worker_verifier_test_module", WORKER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load MiniMax API worker")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    HAS_LINUX_MEMFD_SEALS,
    "verifier production tests require Linux memfd sealing via /usr/bin/python3",
)
class MiniMaxPatchVerifyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "stable.txt").write_text("committed\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.name", "Verifier Test")
        self._git("config", "user.email", "verifier@localhost")
        self._git("add", "--all")
        self._git("commit", "-q", "-m", "baseline")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        self.module = load_verifier_module()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - controlled local Git fixture
            ["/usr/bin/git", *arguments],
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
            env={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(self.root),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
        )

    def _candidate(self, patch_path: Path) -> object:
        return self.module.FrozenCandidate(
            job_id="a" * 20,
            patch_path=patch_path,
            patch_sha256=self.module.sha256_bytes(patch_path.read_bytes()),
            source_head_commit=self.head,
            source_snapshot_sha256="b" * 64,
            files=(),
            worker_sha256="c" * 64,
        )

    def _result(self, phase: str, returncode: int) -> object:
        return self.module.CommandResult(
            phase=phase,
            argv_sha256="d" * 64,
            returncode=returncode,
            duration_seconds=0.01,
            timed_out=False,
            output_limited=False,
            output_bytes=0,
            output_sha256=self.module.sha256_bytes(b""),
            output_tail="",
        )

    def _resource_limits(self) -> object:
        return self.module.SandboxResourceLimits(
            address_space_bytes=4 * 1024 * 1024 * 1024,
            cpu_seconds=3,
            file_size_bytes=4096,
            open_files=1024,
            user_tasks=4096,
        )

    def test_command_json_is_direct_argv_and_never_shell_parsed(self) -> None:
        raw = json.dumps(["/usr/bin/python3", "-c", "print('; touch /tmp/not-a-command')"])

        argv = self.module.parse_command_json(raw, "focused")

        self.assertEqual(argv[2], "print('; touch /tmp/not-a-command')")
        with self.assertRaises(self.module.VerifyError):
            self.module.parse_command_json(json.dumps("/usr/bin/python3 -V"), "focused")
        with self.assertRaises(self.module.VerifyError):
            self.module.parse_command_json(json.dumps(["python3", "-V"]), "focused")
        with self.assertRaises(self.module.VerifyError):
            self.module.parse_command_json(json.dumps(["/usr/bin/python3", "bad\x00arg"]), "focused")
        for denied in (
            ["/bin/sh", "-c", "true"],
            ["/usr/bin/env", "bash", "-c", "true"],
            ["/usr/bin/pytest", "-q"],
            ["/usr/bin/pytest-3", "-q"],
            ["/usr/bin/keyctl", "show"],
            ["/usr/bin/perl", "-e", "print 1"],
            ["/work/run-tests", "--all"],
        ):
            with self.subTest(denied=denied), self.assertRaises(self.module.VerifyError):
                self.module.parse_command_json(json.dumps(denied), "focused")

    def test_projection_uses_head_plus_selected_snapshot_and_ignores_dirty_tree(self) -> None:
        sentinel = self.root / "git-hook-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
        hook.chmod(0o700)
        selected = self.repo / "src" / "module.py"
        selected.write_text("VALUE = 7\n", encoding="utf-8")
        (self.repo / "stable.txt").write_text("unrelated dirty\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("must stay outside\n", encoding="utf-8")
        data = selected.read_bytes()
        manifest = (
            {
                "path": "src/module.py",
                "bytes": len(data),
                "sha256": self.module.sha256_bytes(data),
                "source_executable": False,
            },
        )
        projection = self.root / "projection"
        projection.mkdir()

        real_popen = subprocess.Popen
        with mock.patch.object(self.module.subprocess, "Popen", side_effect=real_popen) as popen:
            self.module.materialize_tracked_head(self.repo, self.head, projection)
        self.module.overlay_selected_snapshot(self.repo, manifest, projection)

        self.assertEqual((projection / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 7\n")
        self.assertEqual((projection / "stable.txt").read_text(encoding="utf-8"), "committed\n")
        self.assertFalse((projection / "untracked.txt").exists())
        self.assertFalse(sentinel.exists())
        batch_calls = [call for call in popen.call_args_list if "cat-file" in call.args[0]]
        self.assertEqual(len(batch_calls), 1)
        self.assertIn("--batch", batch_calls[0].args[0])

    def test_frozen_patch_applies_only_to_private_projection(self) -> None:
        projection = self.root / "projection"
        projection.mkdir()
        self.module.materialize_tracked_head(self.repo, self.head, projection)
        patch = self.root / "candidate.patch"
        patch.write_text(
            "diff --git a/src/module.py b/src/module.py\n"
            "--- a/src/module.py\n"
            "+++ b/src/module.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n",
            encoding="utf-8",
        )

        self.module.apply_frozen_patch(projection, self._candidate(patch))

        self.assertEqual((projection / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 2\n")
        self.assertEqual((self.repo / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    def test_bwrap_profile_has_no_real_home_or_writable_checkout_mount(self) -> None:
        projection = self.root / "projection"
        projection.mkdir()
        command = ["/usr/bin/python3", "-S", "-c", "print('ok')"]

        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "must-not-enter",
                "ALPACA_PAPER_API_KEY": "must-not-enter",
                "HOME": "/home/host-user",
            },
            clear=False,
        ):
            argv = self.module.build_bwrap_argv(
                projection,
                command,
                venv=None,
                tmpfs_bytes=64 * 1024 * 1024,
                seccomp_fd=42,
            )

        joined = "\n".join(argv)
        self.assertIn("--unshare-all", argv)
        self.assertIn("--disable-userns", argv)
        self.assertIn("--clearenv", argv)
        self.assertEqual(argv[argv.index("--seccomp") + 1], "42")
        for masked in self.module.MASKED_PROC_FILES:
            index = argv.index(masked)
            self.assertEqual(argv[index - 2 : index], ["--ro-bind", "/dev/null"])
        self.assertIn(str(projection), argv)
        self.assertIn("--ro-bind", argv)
        self.assertNotIn("--bind", argv)
        self.assertNotIn("must-not-enter", joined)
        self.assertNotIn("/home/host-user", joined)
        self.assertIn("GIT_NO_LAZY_FETCH", argv)
        self.assertEqual(self.module.clean_env()["GIT_NO_LAZY_FETCH"], "1")
        work_index = argv.index(str(projection))
        self.assertEqual(argv[work_index - 1], "--ro-bind")
        self.assertEqual(argv[work_index + 1], "/work")
        self.assertEqual(argv[-len(command) :], command)

    def test_user_task_limit_uses_current_threads_plus_bounded_margin(self) -> None:
        infinity = self.module.resource.RLIM_INFINITY

        self.assertEqual(self.module.calculate_user_task_limit(2661, infinity), 2917)
        self.assertEqual(self.module.calculate_user_task_limit(4000, infinity), 4096)
        self.assertEqual(self.module.calculate_user_task_limit(2661, 2800), 2800)
        with self.assertRaises(self.module.VerifyError) as raised:
            self.module.calculate_user_task_limit(4096, infinity)
        self.assertEqual(raised.exception.code, "sandbox_user_task_budget_unavailable")

    def test_child_applies_calculated_task_and_address_space_limits(self) -> None:
        infinity = self.module.resource.RLIM_INFINITY
        address_space_hard = 2 * 1024 * 1024 * 1024
        hard_limits = {
            self.module.resource.RLIMIT_AS: address_space_hard,
            self.module.resource.RLIMIT_CPU: infinity,
            self.module.resource.RLIMIT_FSIZE: infinity,
            self.module.resource.RLIMIT_NOFILE: infinity,
            self.module.resource.RLIMIT_NPROC: infinity,
        }
        with (
            mock.patch.object(
                self.module.resource,
                "getrlimit",
                side_effect=lambda kind: (0, hard_limits[kind]),
            ),
            mock.patch.object(self.module, "current_host_uid_tasks", return_value=2661),
        ):
            limits = self.module.sandbox_resource_limits(4096, 1.0)

        self.assertEqual(limits.address_space_bytes, address_space_hard)
        self.assertEqual(limits.user_tasks, 2917)

        with mock.patch.object(self.module.resource, "setrlimit") as set_limit:
            self.module._child_limits(limits)

        self.assertEqual(set_limit.call_count, 6)
        set_limit.assert_any_call(self.module.resource.RLIMIT_CORE, (0, 0))
        set_limit.assert_any_call(
            self.module.resource.RLIMIT_AS,
            (limits.address_space_bytes, limits.address_space_bytes),
        )
        set_limit.assert_any_call(
            self.module.resource.RLIMIT_NPROC,
            (limits.user_tasks, limits.user_tasks),
        )

    def test_focused_failure_prevents_release(self) -> None:
        patch = self.root / "candidate.patch"
        patch.write_text("placeholder\n", encoding="utf-8")
        candidate = self._candidate(patch)
        verify_root = self.root / "verify"
        verify_root.mkdir(mode=0o700)
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=None,
            job_id=candidate.job_id,
            expect_patch_sha256=candidate.patch_sha256,
            venv=None,
            focused_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            release_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            focused_timeout_seconds=1.0,
            release_timeout_seconds=1.0,
            max_output_bytes=4096,
            tmpfs_bytes=16 * 1024 * 1024,
        )
        calls = [self._result("preflight", 0), self._result("focused-1", 7)]
        with (
            mock.patch.object(self.module, "resolve_repo", return_value=self.repo),
            mock.patch.object(self.module, "load_frozen_candidate", return_value=candidate),
            mock.patch.object(self.module, "verifier_temp_root", return_value=verify_root),
            mock.patch.object(self.module, "materialize_tracked_head"),
            mock.patch.object(self.module, "overlay_selected_snapshot"),
            mock.patch.object(self.module, "apply_frozen_patch"),
            mock.patch.object(self.module, "run_sandbox_command", side_effect=calls) as sandbox,
        ):
            passed, payload = self.module.verify_candidate(args)

        self.assertFalse(passed)
        self.assertEqual(payload["outcome"], "FOCUSED_FAILED")
        self.assertEqual(sandbox.call_count, 2)
        self.assertFalse(any(call.args[0].startswith("release") for call in sandbox.call_args_list))

    def test_bwrap_preflight_failure_is_fail_closed(self) -> None:
        patch = self.root / "candidate.patch"
        patch.write_text("placeholder\n", encoding="utf-8")
        candidate = self._candidate(patch)
        verify_root = self.root / "verify"
        verify_root.mkdir(mode=0o700)
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=None,
            job_id=candidate.job_id,
            expect_patch_sha256=candidate.patch_sha256,
            venv=None,
            focused_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            release_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            focused_timeout_seconds=1.0,
            release_timeout_seconds=1.0,
            max_output_bytes=4096,
            tmpfs_bytes=16 * 1024 * 1024,
        )
        unavailable = self.module.CommandResult(
            phase="preflight",
            argv_sha256="d" * 64,
            returncode=1,
            duration_seconds=0.01,
            timed_out=False,
            output_limited=False,
            output_bytes=32,
            output_sha256="e" * 64,
            output_tail="bwrap: Operation not permitted",
        )
        with (
            mock.patch.object(self.module, "resolve_repo", return_value=self.repo),
            mock.patch.object(self.module, "load_frozen_candidate", return_value=candidate),
            mock.patch.object(self.module, "verifier_temp_root", return_value=verify_root),
            mock.patch.object(self.module, "materialize_tracked_head"),
            mock.patch.object(self.module, "overlay_selected_snapshot"),
            mock.patch.object(self.module, "apply_frozen_patch"),
            mock.patch.object(self.module, "run_sandbox_command", return_value=unavailable) as sandbox,
            self.assertRaises(self.module.VerifyError) as raised,
        ):
            self.module.verify_candidate(args)

        self.assertEqual(raised.exception.code, "sandbox_unavailable")
        self.assertEqual(sandbox.call_count, 1)

    def test_timeout_kills_sandbox_process_group_and_returns_failure(self) -> None:
        projection = self.root / "projection"
        projection.mkdir()
        temporary = self.root / "run"
        temporary.mkdir()
        fake_process = ["/usr/bin/python3", "-c", "import time; time.sleep(10)"]

        with (
            mock.patch.object(self.module, "build_bwrap_argv", return_value=fake_process),
            mock.patch.object(self.module, "sandbox_resource_limits", return_value=self._resource_limits()),
        ):
            result = self.module.run_sandbox_command(
                "focused-1",
                projection,
                ["/usr/bin/python3", "-V"],
                venv=None,
                timeout_seconds=0.05,
                max_output_bytes=4096,
                tmpfs_bytes=16 * 1024 * 1024,
                temporary=temporary,
            )

        self.assertTrue(result.timed_out)
        self.assertFalse(result.passed)

    def test_command_result_never_exports_raw_output_for_repair(self) -> None:
        result = self.module.CommandResult(
            phase="focused-1",
            argv_sha256="d" * 64,
            returncode=1,
            duration_seconds=0.01,
            timed_out=False,
            output_limited=False,
            output_bytes=12,
            output_sha256="e" * 64,
            output_tail="sensitive candidate output",
        )

        payload = result.as_dict()

        self.assertNotIn("output_tail", payload)
        self.assertNotIn("sensitive candidate output", json.dumps(payload))
        self.assertEqual(payload["output_content"], "omitted_not_eligible_for_automatic_repair_egress")

    def test_venv_site_disable_must_precede_module_or_script(self) -> None:
        self.assertTrue(
            self.module.venv_python_is_isolated(
                ["/runtime/venv/bin/python", "-S", "-P", "-m", "unittest"]
            )
        )

    def test_command_policy_denies_shells_console_launchers_and_unisolated_python(self) -> None:
        denied = (
            (["/bin/sh", "-c", "python3 -V"], False, "command_executable_denied"),
            (["/usr/bin/env", "bash", "-c", "true"], False, "command_executable_denied"),
            (["/usr/bin/python3", "-I", "-S", "-P", "-V"], True, "command_executable_denied"),
            (["/usr/bin/pytest", "-q"], True, "command_executable_denied"),
            (["/usr/bin/keyctl", "show"], False, "command_executable_denied"),
            (["/usr/bin/perl", "-e", "print 1"], False, "command_executable_denied"),
            (["/work/run-tests", "--all"], False, "command_executable_denied"),
            (
                ["/runtime/venv/bin/python", "-P", "-m", "unittest"],
                True,
                "python_site_hook_denied",
            ),
            (
                ["/runtime/venv/bin/python", "-S", "-m", "unittest"],
                True,
                "python_site_hook_denied",
            ),
            (["/usr/bin/python3", "-S", "-P", "-c", "pass"], False, "python_site_hook_denied"),
        )
        for command, venv_enabled, expected_code in denied:
            with self.subTest(command=command, venv_enabled=venv_enabled):
                with self.assertRaises(self.module.VerifyError) as raised:
                    self.module.validate_verification_command(
                        command,
                        "focused-1",
                        venv_enabled=venv_enabled,
                    )
                self.assertEqual(raised.exception.code, expected_code)

        self.module.validate_verification_command(
            ["/runtime/venv/bin/python", "-S", "-P", "-m", "unittest"],
            "focused-1",
            venv_enabled=True,
        )
        self.module.validate_verification_command(
            ["/usr/bin/python3", "-I", "-S", "-P", "-c", "pass"],
            "focused-1",
            venv_enabled=False,
        )

    def test_invalid_venv_python_command_is_rejected_before_bwrap(self) -> None:
        patch = self.root / "candidate.patch"
        patch.write_text("placeholder\n", encoding="utf-8")
        candidate = self._candidate(patch)
        verify_root = self.root / "verify"
        verify_root.mkdir(mode=0o700)
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=None,
            job_id=candidate.job_id,
            expect_patch_sha256=candidate.patch_sha256,
            venv=".venv312",
            focused_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            release_argv_json=[
                json.dumps(["/runtime/venv/bin/python", "-S", "-P", "-m", "unittest"])
            ],
            focused_timeout_seconds=1.0,
            release_timeout_seconds=1.0,
            max_output_bytes=4096,
            tmpfs_bytes=16 * 1024 * 1024,
        )
        with (
            mock.patch.object(self.module, "resolve_repo", return_value=self.repo),
            mock.patch.object(self.module, "load_frozen_candidate", return_value=candidate),
            mock.patch.object(self.module, "verifier_temp_root", return_value=verify_root),
            mock.patch.object(self.module, "materialize_tracked_head"),
            mock.patch.object(self.module, "overlay_selected_snapshot"),
            mock.patch.object(self.module, "apply_frozen_patch"),
            mock.patch.object(self.module, "prepare_venv_runtime") as prepare_venv,
            mock.patch.object(self.module, "run_sandbox_command") as sandbox,
            self.assertRaises(self.module.VerifyError) as raised,
        ):
            self.module.verify_candidate(args)

        self.assertEqual(raised.exception.code, "command_executable_denied")
        prepare_venv.assert_not_called()
        sandbox.assert_not_called()

    def test_sandbox_environment_disables_pytest_plugin_autoload(self) -> None:
        self.assertEqual(self.module.sandbox_environment(None)["PYTEST_DISABLE_PLUGIN_AUTOLOAD"], "1")

    def test_selected_source_manifest_and_mode_are_revalidated(self) -> None:
        source = self.repo / "src" / "module.py"
        data = source.read_bytes()
        files = [
            {
                "path": "src/module.py",
                "bytes": len(data),
                "sha256": self.module.sha256_bytes(data),
                "source_executable": False,
            }
        ]
        snapshot = self.module.sha256_bytes(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        validated = self.module.validate_source_manifest(files, snapshot)
        self.assertEqual(validated[0]["source_executable"], False)

        invalid = [dict(files[0], source_executable=True)]
        invalid_snapshot = self.module.sha256_bytes(
            json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        with self.assertRaises(self.module.VerifyError) as invalid_raised:
            self.module.validate_source_manifest(invalid, invalid_snapshot)
        self.assertEqual(invalid_raised.exception.code, "source_manifest_invalid")

        source.chmod(0o700)
        projection = self.root / "mode-projection"
        projection.mkdir()
        with self.assertRaises(self.module.VerifyError) as mode_raised:
            self.module.overlay_selected_snapshot(self.repo, tuple(files), projection)
        self.assertEqual(mode_raised.exception.code, "source_executable_changed")

    def test_fifo_export_source_and_venv_config_fail_without_blocking(self) -> None:
        export_fifo = self.root / "export.patch"
        os.mkfifo(export_fifo)
        started = time.monotonic()
        with self.assertRaises(self.module.VerifyError) as export_raised:
            self.module.read_regular(export_fifo, 1024, "frozen patch export")
        self.assertEqual(export_raised.exception.code, "file_type_denied")
        self.assertLess(time.monotonic() - started, 1.0)

        source_fifo = self.repo / "src" / "fifo.py"
        os.mkfifo(source_fifo)
        projection = self.root / "fifo-projection"
        projection.mkdir()
        source_manifest = (
            {
                "path": "src/fifo.py",
                "bytes": 0,
                "sha256": self.module.sha256_bytes(b""),
                "source_executable": False,
            },
        )
        started = time.monotonic()
        with self.assertRaises(self.module.VerifyError) as source_raised:
            self.module.overlay_selected_snapshot(self.repo, source_manifest, projection)
        self.assertEqual(source_raised.exception.code, "file_type_denied")
        self.assertLess(time.monotonic() - started, 1.0)

        venv = self.repo / ".venv-fifo"
        venv.mkdir()
        os.mkfifo(venv / "pyvenv.cfg")
        runtime = self.root / "fifo-runtime"
        runtime.mkdir()
        started = time.monotonic()
        with self.assertRaises(self.module.VerifyError) as venv_raised:
            self.module.prepare_venv_runtime(self.repo, ".venv-fifo", runtime)
        self.assertEqual(venv_raised.exception.code, "file_type_denied")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_venv_dependency_tree_rejects_links_special_files_and_startup_hooks(self) -> None:
        cases = ("symlink", "fifo", "socket", "sitecustomize.py", "usercustomize.py")
        for case in cases:
            with self.subTest(case=case):
                site = self.root / f"site-{case}"
                site.mkdir()
                lstat_context = nullcontext()
                if case == "symlink":
                    os.symlink(self.repo / "stable.txt", site / "linked.py")
                elif case == "fifo":
                    os.mkfifo(site / "channel")
                elif case == "socket":
                    endpoint = site / "endpoint.sock"
                    endpoint.write_bytes(b"")

                    def socket_lstat(path: Path) -> os.stat_result:
                        metadata = os.lstat(path)
                        if Path(path).name == "endpoint.sock":
                            values = list(metadata)
                            values[0] = self.module.stat.S_IFSOCK | 0o600
                            return os.stat_result(values)
                        return metadata

                    lstat_context = mock.patch.object(
                        self.module.Path,
                        "lstat",
                        autospec=True,
                        side_effect=socket_lstat,
                    )
                else:
                    (site / case).write_text("VALUE = 1\n", encoding="utf-8")
                with lstat_context, self.assertRaises(self.module.VerifyError) as raised:
                    self.module.validate_site_packages_tree(site)
                self.assertIn(raised.exception.code, {"venv_tree_invalid", "venv_startup_hook_denied"})

    @unittest.skipUnless((ROOT / ".venv312").is_dir(), "repository venv is unavailable")
    def test_repository_venv_dependency_tree_passes_bounded_static_validation(self) -> None:
        temporary = self.root / "runtime-static"
        temporary.mkdir()
        runtime = self.module.prepare_venv_runtime(ROOT, ".venv312", temporary)
        self.assertIsNotNone(runtime)
        self.assertFalse(
            self.module.venv_python_is_isolated(
                ["/runtime/venv/bin/python", "-m", "unittest", "-S"]
            )
        )

    def test_venv_symlinked_parent_is_rejected_before_mount(self) -> None:
        venv = self.repo / ".venv-test"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("version_info = 3.14.0\n", encoding="utf-8")
        os.symlink("/usr/bin/python3", venv / "bin" / "python")
        outside = self.root / "outside-site"
        (outside / "python3.14" / "site-packages").mkdir(parents=True)
        os.symlink(outside, venv / "lib")
        runtime = self.root / "runtime"
        runtime.mkdir()

        with self.assertRaises(self.module.VerifyError) as raised:
            self.module.prepare_venv_runtime(self.repo, ".venv-test", runtime)

        self.assertEqual(raised.exception.code, "path_component_symlink")

    def test_cleanup_failure_reports_retained_private_projection(self) -> None:
        base = self.root / "verify"
        retained = base / "job"
        retained.mkdir(parents=True)

        with (
            mock.patch.object(self.module, "verifier_temp_root", return_value=base),
            mock.patch.object(self.module.shutil, "rmtree", side_effect=OSError("denied")),
            self.assertRaises(self.module.VerifyError) as raised,
        ):
            self.module.purge_verification_tree(retained)

        self.assertEqual(raised.exception.code, "verification_cleanup_failed")
        self.assertIn(str(retained), str(raised.exception))

        with (
            mock.patch.object(self.module, "verifier_temp_root", return_value=base),
            self.assertRaises(self.module.VerifyError) as root_raised,
        ):
            self.module.purge_verification_tree(base)
        self.assertEqual(root_raised.exception.code, "verification_cleanup_path_invalid")

    def test_git_batch_reader_has_a_real_stream_deadline(self) -> None:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            reader = self.module.TimedBatchReader(stream, self.module.time.monotonic() - 1.0)
            with self.assertRaises(self.module.VerifyError) as raised:
                reader.line(64, "fixture")
        self.assertEqual(raised.exception.code, "git_batch_timeout")

    def test_cli_rejects_invalid_reviewed_hash_before_worker_access(self) -> None:
        result = subprocess.run(  # noqa: S603 - invokes the fixed verifier under test
            [
                "/usr/bin/python3",
                "-I",
                str(VERIFIER),
                "--json",
                "--repo",
                str(self.repo),
                "verify",
                "a" * 20,
                "--expect-patch-sha256",
                "not-a-hash",
                "--focused-argv-json",
                json.dumps(["/usr/bin/python3", "-V"]),
                "--release-argv-json",
                json.dumps(["/usr/bin/python3", "-V"]),
            ],
            check=False,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "expected_patch_hash_invalid")

    def test_cli_rejects_nonfinite_timeouts_with_sanitized_json(self) -> None:
        for option, value in (
            ("--focused-timeout-seconds", "nan"),
            ("--focused-timeout-seconds", "inf"),
            ("--release-timeout-seconds", "-inf"),
        ):
            with self.subTest(option=option, value=value):
                result = subprocess.run(  # noqa: S603 - invokes the fixed verifier under test
                    [
                        "/usr/bin/python3",
                        "-I",
                        str(VERIFIER),
                        "--json",
                        "--repo",
                        str(self.repo),
                        "verify",
                        "a" * 20,
                        "--expect-patch-sha256",
                        "b" * 64,
                        "--focused-argv-json",
                        json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"]),
                        "--release-argv-json",
                        json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"]),
                        f"{option}={value}",
                    ],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=5.0,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"]["code"], "timeout_invalid")
                self.assertNotIn("NaN", result.stdout)
                self.assertNotIn("Infinity", result.stdout)

    def test_frozen_candidate_requires_exact_reviewed_worker_and_export_hashes(self) -> None:
        job_id = "f" * 20
        patch_data = b"frozen patch\n"
        patch_hash = self.module.sha256_bytes(patch_data)
        worker_hash = self.module.sha256_bytes(WORKER.read_bytes())
        frozen_worker = self.module.FrozenWorker(
            fd=-1,
            sha256=worker_hash,
            device=1,
            inode=1,
            size=len(WORKER.read_bytes()),
        )
        export = (
            Path("/tmp")  # noqa: S108 - asserts the fixed private worker export contract
            / f"minimax-api-worker-{os.getuid()}"
            / "exports"
            / f"{job_id}.patch"
        )
        files = [
            {
                "path": "src/module.py",
                "bytes": 10,
                "sha256": "a" * 64,
                "source_executable": False,
            }
        ]
        source_snapshot = self.module.sha256_bytes(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        status = {
            "ok": True,
            "status": "PATCH_READY",
            "runner_version": self.module.EXPECTED_RUNNER_VERSION,
            "contract_version": self.module.EXPECTED_WORKER_CONTRACT,
            "endpoint": self.module.EXPECTED_ENDPOINT,
            "model": self.module.EXPECTED_MODEL,
            "runner_sha256": worker_hash,
            "result": {"patch_sha256": patch_hash},
            "files": files,
            "source_head_commit": self.head,
            "source_snapshot_sha256": source_snapshot,
        }
        diff = {"ok": True, "sha256": patch_hash, "patch": str(export)}

        with (
            mock.patch.object(self.module, "call_worker", side_effect=(status, diff)) as worker,
            mock.patch.object(self.module, "read_regular", return_value=patch_data),
        ):
            candidate = self.module.load_frozen_candidate(
                frozen_worker,
                self.repo,
                None,
                job_id,
                patch_hash,
            )

        self.assertEqual(candidate.patch_sha256, patch_hash)
        self.assertEqual(candidate.worker_sha256, worker_hash)
        self.assertEqual(worker.call_count, 2)

    def test_verifier_pins_the_exact_audited_adjacent_worker_hash(self) -> None:
        self.assertEqual(
            self.module.sha256_bytes(WORKER.read_bytes()),
            self.module.EXPECTED_WORKER_SHA256,
        )

    def test_worker_hash_mismatch_never_executes_adjacent_bytes(self) -> None:
        sentinel = self.root / "mismatched-worker-executed"
        adjacent = self.root / "untrusted-worker.py"
        adjacent.write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        adjacent.chmod(0o600)
        destination = self.root / "private-freeze"
        destination.mkdir(mode=0o700)
        with (
            mock.patch.object(self.module, "adjacent_worker_path", return_value=adjacent),
            mock.patch.object(self.module, "EXPECTED_WORKER_SHA256", "0" * 64),
            self.assertRaises(self.module.VerifyError) as raised,
        ):
            self.module.freeze_adjacent_worker(destination)
        self.assertEqual(raised.exception.code, "worker_binary_mismatch")
        self.assertFalse(sentinel.exists())

    def test_status_and_diff_execute_the_same_sealed_worker_fd_after_source_swap(self) -> None:
        sentinel = self.root / "swapped-worker-executed"
        adjacent = self.root / "audited-worker.py"
        adjacent.write_text(
            "import json\nprint(json.dumps({'ok': True, 'worker_file': __file__}))\n",
            encoding="utf-8",
        )
        adjacent.chmod(0o600)
        expected_hash = self.module.sha256_bytes(adjacent.read_bytes())
        destination = self.root / "private-freeze"
        destination.mkdir(mode=0o700)
        with (
            mock.patch.object(self.module, "adjacent_worker_path", return_value=adjacent),
            mock.patch.object(self.module, "EXPECTED_WORKER_SHA256", expected_hash),
        ):
            frozen = self.module.freeze_adjacent_worker(destination)
            try:
                adjacent.write_text(
                    f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
                    encoding="utf-8",
                )
                status = self.module.call_worker(frozen, self.repo, None, "status", "a" * 20)
                diff = self.module.call_worker(frozen, self.repo, None, "diff", "a" * 20)
                inherited = f"/proc/self/fd/{frozen.fd}"
                self.assertEqual(status["worker_file"], inherited)
                self.assertEqual(diff["worker_file"], inherited)
                with self.assertRaises(OSError) as sealed:
                    os.write(frozen.fd, b"mutation")
                self.assertEqual(sealed.exception.errno, self.module.errno.EPERM)
            finally:
                os.close(frozen.fd)
        self.assertFalse(sentinel.exists())

    def test_candidate_failure_closes_frozen_fd_and_purges_private_child(self) -> None:
        verify_root = self.root / "verify-cleanup"
        verify_root.mkdir(mode=0o700)
        args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=None,
            job_id="a" * 20,
            expect_patch_sha256="b" * 64,
            venv=None,
            focused_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            release_argv_json=[json.dumps(["/usr/bin/python3", "-I", "-S", "-P", "-V"])],
            focused_timeout_seconds=1.0,
            release_timeout_seconds=1.0,
            max_output_bytes=4096,
            tmpfs_bytes=16 * 1024 * 1024,
        )
        captured: list[object] = []
        real_freeze = self.module.freeze_adjacent_worker

        def capture(destination: Path) -> object:
            worker = real_freeze(destination)
            captured.append(worker)
            return worker

        with (
            mock.patch.object(self.module, "resolve_repo", return_value=self.repo),
            mock.patch.object(self.module, "verifier_temp_root", return_value=verify_root),
            mock.patch.object(self.module, "freeze_adjacent_worker", side_effect=capture),
            mock.patch.object(
                self.module,
                "load_frozen_candidate",
                side_effect=self.module.VerifyError("candidate_failed", "synthetic failure"),
            ),
            self.assertRaises(self.module.VerifyError),
        ):
            self.module.verify_candidate(args)
        self.assertEqual(list(verify_root.iterdir()), [])
        with self.assertRaises(OSError):
            os.fstat(captured[0].fd)

    def test_seccomp_policy_compiles_to_a_sealed_real_filter(self) -> None:
        descriptor = self.module.create_seccomp_policy_fd()
        try:
            metadata = os.fstat(descriptor)
            seals = self.module.fcntl.fcntl(descriptor, self.module.fcntl.F_GET_SEALS)
            required = (
                self.module.fcntl.F_SEAL_SEAL
                | self.module.fcntl.F_SEAL_SHRINK
                | self.module.fcntl.F_SEAL_GROW
                | self.module.fcntl.F_SEAL_WRITE
            )
            self.assertGreater(metadata.st_size, 0)
            self.assertEqual(seals & required, required)
        finally:
            os.close(descriptor)

    def test_seccomp_fd_is_closed_when_sandbox_setup_fails(self) -> None:
        descriptor = self.module.create_seccomp_policy_fd()
        projection = self.root / "seccomp-cleanup-projection"
        projection.mkdir()
        temporary = self.root / "seccomp-cleanup-run"
        temporary.mkdir()
        with (
            mock.patch.object(self.module, "create_seccomp_policy_fd", return_value=descriptor),
            mock.patch.object(
                self.module,
                "build_bwrap_argv",
                side_effect=self.module.VerifyError("sandbox_unavailable", "synthetic"),
            ),
            self.assertRaises(self.module.VerifyError),
        ):
            self.module.run_sandbox_command(
                "preflight",
                projection,
                ["/usr/bin/python3", "-I", "-S", "-P", "-V"],
                venv=None,
                timeout_seconds=1.0,
                max_output_bytes=4096,
                tmpfs_bytes=16 * 1024 * 1024,
                temporary=temporary,
            )
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    @unittest.skipUnless(
        os.environ.get("MINIMAX_VERIFY_REAL_BWRAP") == "1",
        "real user/network namespace test is opt-in outside the managed seccomp sandbox",
    )
    def test_real_bwrap_profile_denies_network_home_env_and_projection_writes(self) -> None:
        projection = self.root / "projection"
        (projection / "src").mkdir(parents=True)
        (projection / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        temporary = self.root / "run"
        temporary.mkdir()
        probe = (
            "import errno,os,socket,sys;sys.path[:0]=['/work/src','/work'];from pathlib import Path;"
            "from module import VALUE;assert VALUE==2;"
            "assert os.environ['HOME']=='/home/sandbox';"
            "assert 'ANTHROPIC_AUTH_TOKEN' not in os.environ;"
            "assert 'ALPACA_PAPER_API_KEY' not in os.environ;"
            f"assert not Path({str(self.repo)!r}).exists();"
            "assert not Path('/home/adquiod/.config/fish/config.fish').exists();"
            "interfaces={line.split(':',1)[0].strip() for line in "
            "Path('/proc/net/dev').read_text(encoding='ascii').splitlines()[2:] if ':' in line};"
            "assert interfaces=={'lo'};"
            "netlink_denied=False;"
            "\ntry:socket.socket(socket.AF_NETLINK,socket.SOCK_RAW)"
            "\nexcept OSError as e:netlink_denied=e.errno==errno.EPERM\n"
            "assert netlink_denied;"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);ok=False;"
            "\ntry:s.connect(('198.51.100.1',9))"
            "\nexcept OSError as e:ok=e.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EPERM)\n"
            "assert ok;"
            "\ntry:open('/work/src/module.py','wb')"
            "\nexcept OSError:pass"
            "\nelse:raise AssertionError('projection writable')\n"
        )
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "must-not-enter",
                "ALPACA_PAPER_API_KEY": "must-not-enter",
            },
            clear=False,
        ):
            result = self.module.run_sandbox_command(
                "real-profile",
                projection,
                ["/usr/bin/python3", "-I", "-S", "-P", "-c", probe],
                venv=None,
                timeout_seconds=15.0,
                max_output_bytes=64_000,
                tmpfs_bytes=64 * 1024 * 1024,
                temporary=temporary,
            )

        self.assertTrue(result.passed, result.output_tail)
        self.assertEqual((projection / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 2\n")

    @unittest.skipUnless(
        os.environ.get("MINIMAX_VERIFY_REAL_BWRAP") == "1",
        "real read-only venv mount test is opt-in outside the managed seccomp sandbox",
    )
    def test_real_venv_runtime_uses_sanitized_skeleton_without_pth_hooks(self) -> None:
        projection = self.root / "projection"
        projection.mkdir()
        temporary = self.root / "run"
        temporary.mkdir()
        runtime = self.module.prepare_venv_runtime(ROOT, ".venv312", temporary)

        result = self.module.run_sandbox_command(
            "real-venv",
            projection,
            [
                "/runtime/venv/bin/python",
                "-S",
                "-P",
                "-c",
                "import pandas;assert pandas.__version__=='2.3.3'",
            ],
            venv=runtime,
            timeout_seconds=30.0,
            max_output_bytes=64_000,
            tmpfs_bytes=64 * 1024 * 1024,
            temporary=temporary,
        )

        self.assertTrue(result.passed, result.output_tail)

    @unittest.skipUnless(
        os.environ.get("MINIMAX_VERIFY_REAL_BWRAP") == "1",
        "end-to-end frozen job verification is opt-in outside the managed seccomp sandbox",
    )
    def test_real_end_to_end_frozen_job_never_edits_source_checkout(self) -> None:
        worker = load_worker_module()
        spec_path = self.repo / "task.md"
        spec_path.write_text("Change VALUE from 1 to 2 and preserve all other files.\n", encoding="utf-8")
        fish = self.root / "config.fish"
        fish.write_text(
            'function claude-minimax\n set -lx ANTHROPIC_AUTH_TOKEN "sk-cp-test-verifier-0123456789abcdef"\nend\n',
            encoding="utf-8",
        )
        fish.chmod(0o600)
        state = worker.worker_temp_base() / "state" / f"verify-e2e-{self.root.name}"
        create_args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(state),
            endpoint=worker.DEFAULT_ENDPOINT,
            model=worker.DEFAULT_MODEL,
            cloud_approved=True,
            spec="task.md",
            allow_path=["src/module.py"],
            json=True,
        )
        created_output = io.StringIO()
        with redirect_stdout(created_output):
            self.assertEqual(worker.command_job_create(create_args), 0)
        created = json.loads(created_output.getvalue())
        job_id = str(created["job_id"])
        patch = (
            "diff --git a/src/module.py b/src/module.py\n"
            "--- a/src/module.py\n"
            "+++ b/src/module.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
        )
        response = {
            "response_id": "resp-local-verifier-test",
            "model": worker.DEFAULT_MODEL,
            "status": "completed",
            "output_text": patch,
            "usage": {
                "input_tokens": 100,
                "cached_tokens": 0,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "total_tokens": 120,
            },
        }
        run_args = argparse.Namespace(
            repo=str(self.repo),
            state_dir=str(state),
            job_id=job_id,
            fish_config=str(fish),
            reasoning="none",
            max_output_tokens=20_000,
            timeout_seconds=30.0,
            queue_timeout_seconds=0.0,
            json=True,
        )
        run_output = io.StringIO()
        try:
            with mock.patch.object(worker, "api_create_response", return_value=response), redirect_stdout(run_output):
                self.assertEqual(worker.command_job_run(run_args), 0)
            run = json.loads(run_output.getvalue())
            patch_hash = str(run["patch_sha256"])
            focused = json.dumps(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-P",
                    "-c",
                    "import sys;sys.path[:0]=['/work/src','/work'];from module import VALUE;assert VALUE==2",
                ]
            )
            release = json.dumps(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-P",
                    "-c",
                    "import sys;sys.path[:0]=['/work/src','/work'];"
                    "from module import VALUE;from pathlib import Path;"
                    "assert VALUE==2;assert Path('stable.txt').read_text()=='committed\\n'",
                ]
            )
            verify = subprocess.run(  # noqa: S603 - invokes the fixed verifier under test
                [
                    "/usr/bin/python3",
                    "-I",
                    str(VERIFIER),
                    "--json",
                    "--repo",
                    str(self.repo),
                    "--state-dir",
                    str(state),
                    "verify",
                    job_id,
                    "--expect-patch-sha256",
                    patch_hash,
                    "--focused-argv-json",
                    focused,
                    "--release-argv-json",
                    release,
                ],
                check=False,
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "ANTHROPIC_AUTH_TOKEN": "must-not-enter-sandbox",
                    "ALPACA_PAPER_API_KEY": "must-not-enter-sandbox",
                },
                timeout=60.0,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            verified = json.loads(verify.stdout)
            self.assertEqual(verified["outcome"], "PASS")
            self.assertNotIn("must-not-enter-sandbox", verify.stdout + verify.stderr)
            self.assertEqual((self.repo / "src" / "module.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        finally:
            export = worker.worker_temp_base() / "exports" / f"{job_id}.patch"
            if export.is_file() or export.is_symlink():
                export.unlink()
            if state.is_dir() and not state.is_symlink():
                shutil.rmtree(state)


if __name__ == "__main__":
    unittest.main()
