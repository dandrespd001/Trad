import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deploy" / "systemd"


class PaperExecutorPackagingTests(unittest.TestCase):
    def test_service_uses_fixed_identity_directories_and_encrypted_credentials(self) -> None:
        unit = (PACKAGE / "trading-ai-paper-executor.service").read_text(encoding="utf-8")

        required = (
            "Type=notify",
            "User=trading-ai-paper-executor",
            "Group=trading-ai-paper-ipc",
            "DynamicUser=no",
            "RuntimeDirectory=trading-ai-paper",
            "RuntimeDirectoryMode=0750",
            "RuntimeDirectoryPreserve=no",
            "StateDirectory=trading-ai-paper",
            "StateDirectoryMode=0700",
            "LoadCredentialEncrypted=alpaca-paper-api-key",
            "LoadCredentialEncrypted=alpaca-paper-secret-key",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
        )
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)

        forbidden = (
            "LoadCredential=",
            "EnvironmentFile=",
            "WatchdogSec=",
            "ExecStartPre=",
            "--socket-path",
            "--supervisor-root",
            "DynamicUser=yes",
        )
        for directive in forbidden:
            with self.subTest(directive=directive):
                self.assertNotIn(directive, unit)

    def test_accounts_and_authz_are_separate_and_open_is_absent(self) -> None:
        sysusers = (PACKAGE / "trading-ai-paper.conf").read_text(encoding="utf-8")
        authz = (PACKAGE / "executor-authz.yml.in").read_text(encoding="utf-8")

        for identity in (
            "trading-ai-paper-executor",
            "trading-ai-monitor",
            "trading-ai-safety",
            "trading-ai-paper-ipc",
        ):
            with self.subTest(identity=identity):
                self.assertIn(identity, sysusers)
        self.assertIn("capabilities: [health, observe]", authz)
        self.assertIn("capabilities: [health, observe, reduce, cancel, kill]", authz)
        self.assertNotIn("open", authz)
        self.assertNotIn("increase", authz)


if __name__ == "__main__":
    unittest.main()
