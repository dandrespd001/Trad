import csv
import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperStatementValidateTests(unittest.TestCase):
    def test_parser_defaults_for_statement_validate(self) -> None:
        args = build_parser().parse_args(
            ["paper-statement-validate", "--statement", "statement.json", "--as-of-date", "2026-06-16"]
        )

        self.assertEqual(args.statement, "statement.json")
        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.output_dir, "reports/tmp/paper_statements")

    def test_valid_json_normalizes_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.json"
            write_raw_statement(statement, extra_secret="api_key=KEY")

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")
            markdown = (root / "out" / "2026-06-16" / "statement.normalized.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["as_of_date"], "2026-06-16")
        self.assertEqual(payload["fills"][0]["client_order_id"], "signal-spy-20260616")
        self.assertEqual(payload["fills"][0]["quantity"], 0.002)
        self.assertEqual(payload["fills"][0]["raw"]["extra_note"], "[redacted]")
        self.assertFalse(payload["safety"]["credentials_read"])
        self.assertNotIn("KEY", json.dumps(payload))
        self.assertNotIn("KEY", markdown)

    def test_valid_csv_normalizes_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.csv"
            with statement.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "client_order_id",
                        "symbol",
                        "side",
                        "quantity",
                        "filled_avg_price",
                        "filled_at",
                        "realized_pnl",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "client_order_id": "signal-spy-20260616",
                        "symbol": "SPY",
                        "side": "buy",
                        "quantity": "0.002",
                        "filled_avg_price": "500",
                        "filled_at": "2026-06-16T00:03:00+00:00",
                        "realized_pnl": "0.03",
                    }
                )

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["fills"][0]["symbol"], "SPY")

    def test_csv_common_broker_aliases_normalize_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.csv"
            with statement.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "Client Order ID",
                        "Ticker",
                        "Action",
                        "Filled Qty",
                        "Average Fill Price",
                        "Fill Time",
                        "Realized P&L",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "Client Order ID": "signal-spy-20260616",
                        "Ticker": "spy",
                        "Action": "BUY",
                        "Filled Qty": "0.002",
                        "Average Fill Price": "500",
                        "Fill Time": "2026-06-16T00:03:00+00:00",
                        "Realized P&L": "0.03",
                    }
                )

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["fills"][0]["client_order_id"], "signal-spy-20260616")
        self.assertEqual(payload["fills"][0]["symbol"], "SPY")
        self.assertEqual(payload["fills"][0]["side"], "buy")
        self.assertEqual(payload["fills"][0]["quantity"], 0.002)

    def test_fill_date_outside_as_of_date_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.json"
            fill = raw_fill()
            fill["filled_at"] = "2026-06-15T23:59:00+00:00"
            write_json(statement, {"fills": [fill]})

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("filled_at_outside_as_of_date", warning_codes(payload))

    def test_invalid_numeric_field_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.json"
            fill = raw_fill()
            fill["quantity"] = "not-a-number"
            write_json(statement, {"fills": [fill]})

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_quantity", error_codes(payload))

    def test_missing_timezone_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.json"
            fill = raw_fill()
            fill["filled_at"] = "2026-06-16T00:03:00"
            write_json(statement, {"fills": [fill]})

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("filled_at_missing_timezone", warning_codes(payload))

    def test_duplicate_client_order_id_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.json"
            write_json(statement, {"fills": [raw_fill(), raw_fill()]})

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("duplicate_client_order_id", error_codes(payload))

    def test_missing_required_field_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            statement = root / "statement.json"
            fill = raw_fill()
            fill.pop("filled_avg_price")
            write_json(statement, {"fills": [fill]})

            exit_code = main(statement_args(statement, root / "out"))
            payload = read_json(root / "out" / "2026-06-16" / "statement.normalized.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("missing_filled_avg_price", error_codes(payload))

    def test_performance_accepts_normalized_statement_for_broker_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "2026-06-16")
            raw = root / "statement.json"
            write_raw_statement(raw)
            normalized_dir = root / "normalized"

            self.assertEqual(main(statement_args(raw, normalized_dir)), 0)
            normalized = normalized_dir / "2026-06-16" / "statement.normalized.json"
            output = root / "performance.json"
            exit_code = main(
                [
                    "paper-performance-report",
                    "--sessions-root",
                    str(root / "sessions"),
                    "--broker-statement",
                    str(normalized),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(root / "performance.md"),
                ]
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["paper_metrics"]["pnl"]["source"], "broker_statement")
        self.assertEqual(payload["paper_metrics"]["pnl"]["realized_pnl"], 0.03)


def statement_args(statement: Path, output_dir: Path) -> list[str]:
    return [
        "paper-statement-validate",
        "--statement",
        str(statement),
        "--as-of-date",
        "2026-06-16",
        "--output-dir",
        str(output_dir),
    ]


def write_raw_statement(path: Path, *, extra_secret: str = "operator_reviewed") -> None:
    fill = raw_fill()
    fill["extra_note"] = extra_secret
    write_json(path, {"fills": [fill]})


def raw_fill() -> dict[str, object]:
    return {
        "client_order_id": "signal-spy-20260616",
        "symbol": "SPY",
        "side": "buy",
        "quantity": 0.002,
        "filled_avg_price": 500.0,
        "filled_at": "2026-06-16T00:03:00+00:00",
        "realized_pnl": 0.03,
    }


def write_performance_session(session_dir: Path) -> None:
    expected_order = {
        "symbol": "SPY",
        "side": "buy",
        "client_order_id": "signal-spy-20260616",
        "notional": 1.0,
    }
    write_json(session_dir / "session.json", {"ready_for_paper_review": True, "as_of_date": "2026-06-16"})
    write_json(
        session_dir / "closeout" / "paper_closeout.json",
        {
            "status": "CLOSED",
            "session": {"as_of_date": "2026-06-16"},
            "expected_order": expected_order,
            "broker_order": {
                "client_order_id": "signal-spy-20260616",
                "symbol": "SPY",
                "side": "buy",
                "status": "filled",
                "filled_quantity": 0.002,
                "filled_avg_price": 500.0,
            },
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def error_codes(payload: dict[str, object]) -> set[str]:
    return {str(error["code"]) for error in payload["errors"]}


def warning_codes(payload: dict[str, object]) -> set[str]:
    return {str(warning["code"]) for warning in payload["warnings"]}


if __name__ == "__main__":
    unittest.main()
