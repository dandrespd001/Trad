"""Tests for the centralized recursive payload redactor in paper_common."""

import unittest

from trading_ai.execution.paper_common import redact_payload


class RedactPayloadTests(unittest.TestCase):
    def test_nested_dict_with_env_secret_is_redacted(self) -> None:
        payload = {
            "outer": {
                "broker": {
                    "api_key": "PUBLICID",
                    "secret_key": "SUPERSECRET",  # noqa: S105
                },
                "status": "OK",
            }
        }

        redacted = redact_payload(
            payload,
            env={
                "ALPACA_PAPER_API_KEY": "PUBLICID",
                "ALPACA_PAPER_SECRET_KEY": "SUPERSECRET",
            },
        )

        self.assertEqual(
            redacted,
            {
                "outer": {
                    "broker": {
                        "api_key": "[redacted-alpaca_paper_api_key]",
                        "secret_key": "[redacted-alpaca_paper_secret_key]",
                    },
                    "status": "OK",
                }
            },
        )
        self.assertNotIn("SUPERSECRET", repr(redacted))
        self.assertNotIn("PUBLICID", repr(redacted))

    def test_nested_list_and_tuple_are_redacted_and_tuple_becomes_list(self) -> None:
        payload = {
            "rows": [
                ("api_key=ABC", 1),
                ("plain", 2.5),
            ],
            "points": (
                ("token=XYZ", True),
            ),
        }

        redacted = redact_payload(payload, env={})

        self.assertIsInstance(redacted, dict)
        self.assertIsInstance(redacted["rows"], list)
        self.assertIsInstance(redacted["rows"][0], list)  # inner tuple -> list
        self.assertIsInstance(redacted["rows"][1], list)  # inner tuple -> list
        self.assertIsInstance(redacted["points"], list)  # outer tuple -> list
        self.assertIsInstance(redacted["points"][0], list)  # inner tuple -> list
        self.assertEqual(redacted["rows"][0][0], "api_key=[redacted]")
        self.assertEqual(redacted["rows"][0][1], 1)
        self.assertEqual(redacted["rows"][1][0], "plain")
        self.assertEqual(redacted["rows"][1][1], 2.5)
        self.assertEqual(redacted["points"][0][0], "token=[redacted]")
        self.assertIs(redacted["points"][0][1], True)

    def test_dict_keys_containing_secret_patterns_are_redacted(self) -> None:
        # Keys whose stringified form carries a redactable pattern are
        # redacted by ``redact_secrets``; env-driven redaction of keys is
        # exercised separately.
        payload = {
            "api_key=ABCDEFG": "v1",
            "token=SECRETTOKEN": "v2",  # noqa: S105
            "nested": {"secret=hush": "v3"},
        }

        redacted = redact_payload(payload, env={})

        self.assertEqual(
            redacted,
            {
                "api_key=[redacted]": "v1",
                "token=[redacted]": "v2",
                "nested": {"secret=[redacted]": "v3"},
            },
        )

    def test_jwt_and_slack_tokens_redacted_by_pattern_without_env(self) -> None:
        jwt = "eyJabcdefghijklmnop.qrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ._-abcdefghij"  # noqa: S105
        slack = "xoxb-111-222-abcdefghijklmnopqrstuvwxyz"  # noqa: S105
        payload = {"token": jwt, "slack": slack, "safe": "no secrets here"}

        redacted = redact_payload(payload, env={})

        self.assertEqual(redacted["token"], "[redacted-jwt]")
        self.assertEqual(redacted["slack"], "[redacted-slack-token]")
        self.assertEqual(redacted["safe"], "no secrets here")
        # Sanity: the original secret material is not preserved.
        self.assertNotIn("eyJabcdefghijklmnop", repr(redacted))
        self.assertNotIn("xoxb-111-222", repr(redacted))

    def test_non_str_scalars_are_returned_intact(self) -> None:
        payload = {"count": 7, "ratio": 0.5, "enabled": True, "missing": None}

        redacted = redact_payload(payload, env={})

        self.assertEqual(redacted, payload)
        self.assertIs(redacted["enabled"], True)
        self.assertIs(redacted["missing"], None)
        self.assertIs(redacted["count"], 7)
        self.assertIs(redacted["ratio"], 0.5)

    def test_non_collection_root_string_is_redacted(self) -> None:
        secret = "api_key=ABCDEF"  # noqa: S105

        redacted = redact_payload(secret, env={})

        self.assertIsInstance(redacted, str)
        self.assertNotIn("ABCDEF", redacted)
        self.assertEqual(redacted, "api_key=[redacted]")


if __name__ == "__main__":
    unittest.main()