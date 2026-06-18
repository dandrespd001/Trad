import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class LlmRoleRegistryTests(unittest.TestCase):
    def test_role_registry_cli_writes_governed_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            args = build_parser().parse_args(["llm-role-registry", "--output-dir", str(root / "roles")])
            exit_code = main(["llm-role-registry", "--output-dir", str(root / "roles")])
            payload = read_json(root / "roles" / "roles.json")
            markdown = (root / "roles" / "roles.md").read_text(encoding="utf-8")

        self.assertEqual(args.output_dir, str(root / "roles"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertFalse(payload["safety"]["orders_submitted"])
        roles = {item["role_id"]: item for item in payload["roles"]}
        self.assertEqual(roles["paper_ops_reviewer"]["schema_name"], "PaperOpsReview")
        self.assertEqual(roles["signal_proposal_auditor"]["schema_name"], "LLMSignalProposal")
        self.assertIn("broker_access", roles["paper_ops_reviewer"]["forbidden_capabilities"])
        self.assertIn("paper_ops_reviewer", markdown)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
