import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.backtest.engine import BacktestConfig, BacktestResult
from trading_ai.cli import build_parser, main
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder
from trading_ai.llm.openai_client import OpenAIResearchClient
from trading_ai.llm.schemas import schema_for
from trading_ai.reports.markdown import render_backtest_report
from trading_ai.risk.policy import RiskLimits


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)

        class Response:
            output_text = json.dumps(
                {
                    "summary": "Baseline positive after costs.",
                    "key_metrics": {"sharpe": 1.2},
                    "risks": ["sample size is small"],
                    "requires_human_review": False,
                }
            )
            usage = {"input_tokens": 10, "output_tokens": 20}

        return Response()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


class ReportsExecutionLlmCliTests(unittest.TestCase):
    def test_generated_report_defaults_use_reports_tmp(self) -> None:
        parser = build_parser()

        ingest = parser.parse_args(["ingest", "--from", "2024-01-01", "--to", "2024-01-31"])
        features = parser.parse_args(["build-features", "--dataset", "raw.csv"])
        backtest = parser.parse_args(["backtest"])
        train = parser.parse_args(["train", "--model", "logistic-baseline"])
        evaluate = parser.parse_args(["evaluate", "--run-id", "run.json"])
        promote = parser.parse_args(["promote", "--run-id", "run.json", "--baseline", "baseline.json"])
        llm_eval = parser.parse_args(["llm-eval"])
        report = parser.parse_args(["report"])

        self.assertEqual(ingest.output, "reports/tmp/ingest/latest.csv")
        self.assertEqual(features.output, "reports/tmp/build_features/latest.csv")
        self.assertEqual(backtest.output, "reports/tmp/backtest/latest.json")
        self.assertEqual(backtest.report_output, "reports/tmp/backtest/latest.md")
        self.assertEqual(train.output, "reports/tmp/train/latest_model.json")
        self.assertEqual(train.run_output, "reports/tmp/train/latest_run.json")
        self.assertEqual(evaluate.output, "reports/tmp/evaluate/latest.json")
        self.assertEqual(promote.output, "reports/tmp/promote/latest.json")
        self.assertEqual(llm_eval.output, "reports/tmp/llm_eval/latest.json")
        self.assertEqual(report.run_id, "reports/tmp/backtest/latest.json")
        self.assertEqual(report.output, "reports/tmp/report/latest.md")

    def test_report_includes_required_metrics(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            daily_returns=(0.01, -0.005),
            equity_curve=(1.01, 1.00495),
            positions=(),
            trades=(),
            metrics={
                "cumulative_return": 0.00495,
                "cagr": 0.25,
                "sharpe": 1.5,
                "sortino": 2.0,
                "max_drawdown": 0.005,
                "turnover": 0.1,
                "trade_count": 1,
                "average_exposure": 0.5,
                "estimated_costs": 0.0002,
            },
            metadata={"dataset_hash": "abc123", "dataset_path": "data/raw/etfs.csv"},
        )

        markdown = render_backtest_report(result, title="MVP Backtest")

        self.assertIn("# MVP Backtest", markdown)
        self.assertIn("Sharpe", markdown)
        self.assertIn("Max drawdown", markdown)
        self.assertIn("Estimated costs", markdown)
        self.assertIn("Dataset hash", markdown)
        self.assertIn("abc123", markdown)

    def test_paper_broker_dry_run_rejects_symbol_outside_allowlist(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
        )
        order = PaperOrder(symbol="TSLA", side="buy", quantity=1, client_order_id="x-1")

        result = broker.submit_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("symbol_not_allowlisted", result.reasons)

    def test_paper_broker_dry_run_accepts_idempotent_order_inside_risk_limits(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
        )
        order = PaperOrder(symbol="SPY", side="buy", quantity=1, client_order_id="x-1")

        first = broker.submit_order(order)
        second = broker.submit_order(order)

        self.assertTrue(first.accepted)
        self.assertTrue(first.dry_run)
        self.assertEqual(second.status, "duplicate_accepted")

    def test_openai_client_builds_structured_responses_request_without_storing_state(self) -> None:
        fake = FakeOpenAIClient()
        client = OpenAIResearchClient(client=fake, model="gpt-5.5")

        parsed = client.create_structured_output(
            schema_name="BacktestSummary",
            user_input="Summarize this run.",
            reasoning_effort="medium",
            verbosity="medium",
        )

        call = fake.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.5")
        self.assertFalse(call["store"])
        self.assertEqual(call["reasoning"], {"effort": "medium"})
        self.assertEqual(call["text"]["verbosity"], "medium")
        self.assertEqual(call["text"]["format"]["name"], "BacktestSummary")
        self.assertEqual(call["text"]["format"]["schema"], schema_for("BacktestSummary"))
        self.assertEqual(parsed.data["summary"], "Baseline positive after costs.")

    def test_cli_validate_data_returns_nonzero_for_invalid_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset = Path(temp_dir) / "bad.csv"
            dataset.write_text("timestamp,symbol,open,high,low,close\n2024-01-01,SPY,1,1,1,1\n", encoding="utf-8")

            exit_code = main(["validate-data", "--dataset", str(dataset)])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
