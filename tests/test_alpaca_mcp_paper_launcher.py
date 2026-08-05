from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "alpaca-mcp-paper.sh"


class AlpacaMcpPaperLauncherTests(unittest.TestCase):
    def test_launcher_is_fail_closed_without_reading_or_forwarding_credentials(self) -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "ALPACA_PAPER_API_KEY": "must-not-be-used",
            "ALPACA_PAPER_SECRET_KEY": "must-not-be-used",
            "ALPACA_MCP_ENV_FILE": "/does/not/exist",
        }
        completed = subprocess.run(  # noqa: S603 - fixed audited local script
            ["/usr/bin/bash", str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 64)
        self.assertIn("disabled", completed.stderr)
        self.assertIn("single paper executor", completed.stderr)
        self.assertNotIn("must-not-be-used", completed.stdout + completed.stderr)

    def test_launcher_contains_no_credential_or_env_file_ingestion(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        for forbidden in (
            "ALPACA_PAPER_API_KEY",
            "ALPACA_PAPER_SECRET_KEY",
            "ALPACA_MCP_ENV_FILE",
            "ALPACA_PAPER_TRADE",
            "source ",
            ". \"$",
            "exec uvx",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
