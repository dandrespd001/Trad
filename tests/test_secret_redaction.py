import json
import unittest
from pathlib import Path

from trading_ai.execution.live_observability import redact_live_event


class SecretRedactionTests(unittest.TestCase):
    def test_live_event_redaction_recurses_nested_payloads(self) -> None:
        payload = {
            "message": "Authorization: Bearer sk-proj-live-secret",
            "nested": {
                "token": "token=SHOULD_NOT_LEAK",
                "api": "api_key=KEY secret_key=SECRET",
                "rows": ["github_pat_123456789012345678901234567890"],
            },
        }

        redacted = redact_live_event(payload)
        serialized = json.dumps(redacted, sort_keys=True)

        self.assertNotIn("sk-proj-live-secret", serialized)
        self.assertNotIn("SHOULD_NOT_LEAK", serialized)
        self.assertNotIn("KEY", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertIn("[redacted", serialized)

    def test_deploy_files_do_not_bake_secret_values(self) -> None:
        for path in (Path("Dockerfile"), Path("compose.yml")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("ALPACA_LIVE_API_KEY=", text)
            self.assertNotIn("ALPACA_LIVE_SECRET_KEY=", text)
            self.assertNotIn("sk-", text)
            self.assertNotIn("live-secret", text)


if __name__ == "__main__":
    unittest.main()
