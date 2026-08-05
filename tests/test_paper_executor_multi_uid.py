from __future__ import annotations

import errno
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "support" / "paper_executor_multi_uid_probe.py"
SUBID_COUNT = 32


class PaperExecutorMultiUidTests(unittest.TestCase):
    def test_real_kernel_uids_enforce_dac_peer_credentials_and_capabilities(self) -> None:
        prerequisite = self._prerequisite()
        command_prefix = self._command_prefix(prerequisite)
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        preflight = subprocess.run(  # noqa: S603 - fixed local namespace probe
            [*command_prefix, shutil.which("true") or "/usr/bin/true"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if preflight.returncode != 0:
            detail = _last_bounded_line(preflight.stderr or preflight.stdout)
            self.skipTest(f"real multi-UID user namespace is unavailable before READY: {detail}")

        try:
            completed = subprocess.run(  # noqa: S603 - fixed audited local probe
                [*command_prefix, sys.executable, "-I", "-B", str(PROBE), str(ROOT)],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(f"real multi-UID probe timed out after preflight: {_bounded_output(exc.stdout)}")

        output_lines = tuple(line for line in completed.stdout.splitlines() if line)
        self.assertIn("READY", output_lines, msg=_failure_detail(completed))
        self.assertEqual(completed.returncode, 0, msg=_failure_detail(completed))
        self.assertGreaterEqual(len(output_lines), 2, msg=_failure_detail(completed))
        payload = json.loads(output_lines[-1])

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["runtime"], {"uid": 1, "gid": 10, "mode": "0750"})
        self.assertEqual(payload["socket"], {"uid": 1, "gid": 10, "mode": "0660"})
        self.assertEqual(payload["results"]["monitor_health"]["value"]["peer_uid"], 2)
        self.assertEqual(
            payload["results"]["monitor_kill"],
            {"code": "authorization_denied", "kind": "PaperExecutorRemoteError"},
        )
        self.assertEqual(payload["results"]["safety_kill"]["value"]["peer_uid"], 3)
        self.assertNotEqual(payload["results"]["unknown_group_member"]["kind"], "ok")
        self.assertEqual(
            payload["results"]["outsider"],
            {"errno": errno.EACCES, "kind": "OSError"},
        )
        self.assertEqual(
            payload["handler_audit"],
            ["2:health", "2:latch_kill_switch", "3:latch_kill_switch"],
        )
        self.assertEqual(payload["server_status"], 0)
        self.assertTrue(payload["cleanup_complete"])

    def _prerequisite(self) -> dict[str, int | str]:
        if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
            self.skipTest("real multi-UID probe requires Linux SO_PEERCRED")
        if os.getuid() != os.geteuid() or os.getgid() != os.getegid():
            self.skipTest("real multi-UID probe rejects an existing host identity transition")
        missing = tuple(
            executable for executable in ("unshare", "newuidmap", "newgidmap") if shutil.which(executable) is None
        )
        if missing:
            self.skipTest(f"real multi-UID probe prerequisites are missing: {', '.join(missing)}")
        try:
            username = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            self.skipTest("invoking UID has no NSS username for subordinate-ID lookup")
        subordinate_uid = _subordinate_range(Path("/etc/subuid"), username, os.getuid())
        subordinate_gid = _subordinate_range(Path("/etc/subgid"), username, os.getgid())
        if subordinate_uid is None or subordinate_gid is None:
            self.skipTest("invoking user has no subordinate UID/GID range of at least 32 IDs")
        return {
            "unshare": shutil.which("unshare") or "/usr/bin/unshare",
            "uid": os.getuid(),
            "gid": os.getgid(),
            "subuid": subordinate_uid,
            "subgid": subordinate_gid,
        }

    @staticmethod
    def _command_prefix(prerequisite: dict[str, int | str]) -> list[str]:
        return [
            str(prerequisite["unshare"]),
            "--user",
            "--net",
            "--map-users",
            f"0:{prerequisite['uid']}:1",
            "--map-users",
            f"1:{prerequisite['subuid']}:{SUBID_COUNT}",
            "--map-groups",
            f"0:{prerequisite['gid']}:1",
            "--map-groups",
            f"1:{prerequisite['subgid']}:{SUBID_COUNT}",
            "--setuid",
            "0",
            "--setgid",
            "0",
            "--fork",
            "--kill-child=SIGKILL",
        ]


def _subordinate_range(path: Path, username: str, current_id: int) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split(":")
        if len(fields) != 3 or fields[0] not in {username, str(current_id)}:
            continue
        try:
            start = int(fields[1])
            count = int(fields[2])
        except ValueError:
            continue
        if start > 0 and count >= SUBID_COUNT and not start <= current_id < start + SUBID_COUNT:
            return start
    return None


def _last_bounded_line(value: str) -> str:
    lines = tuple(line.strip() for line in value.splitlines() if line.strip())
    return (lines[-1] if lines else "unshare exited without diagnostics")[:240]


def _bounded_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "").strip()[-1_000:]


def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return (
        f"returncode={completed.returncode} "
        f"stdout={_bounded_output(completed.stdout)!r} "
        f"stderr={_bounded_output(completed.stderr)!r}"
    )


if __name__ == "__main__":
    unittest.main()
