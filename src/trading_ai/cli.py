"""Command-line interface for the trading AI research MVP."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from trading_ai.backtest.engine import BacktestConfig, run_momentum_vol_target_backtest
from trading_ai.config import load_risk_config, load_universe_config
from trading_ai.data.io import read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.alpaca_connection import build_alpaca_paper_client
from trading_ai.execution.alpaca_paper import (
    AlpacaPaperBroker,
    PaperOrder,
    PaperOrderSnapshot,
    PaperPosition,
    PaperPreflightDecision,
    evaluate_paper_preflight,
)
from trading_ai.features.engineering import build_features
from trading_ai.llm.evals import run_guardrail_evals
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    evaluate_classifier,
    load_model,
    save_model,
    temporal_train_test_split,
    train_logistic_baseline,
    walk_forward_evaluate,
)
from trading_ai.models.promotion import PromotionPolicy, evaluate_promotion
from trading_ai.models.signals import ModelSignal, generate_model_signals
from trading_ai.reports.markdown import render_backtest_report


PAPER_SIGNAL_ORDER_NOTIONAL = 1.0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--config", default="configs/universe.yml")
    ingest.add_argument("--from", dest="start", required=True)
    ingest.add_argument("--to", dest="end", required=True)
    ingest.add_argument("--output", default="data/raw/etfs.csv")
    ingest.add_argument("--source-csv")
    ingest.set_defaults(func=_ingest)

    validate = subparsers.add_parser("validate-data")
    validate.add_argument("--dataset", required=True)
    validate.set_defaults(func=_validate_data)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--dataset", required=True)
    manifest.add_argument("--output", required=True)
    manifest.set_defaults(func=_manifest)

    features = subparsers.add_parser("build-features")
    features.add_argument("--dataset", required=True)
    features.add_argument("--output", default="data/processed/features.csv")
    features.set_defaults(func=_build_features)

    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--strategy", default="momentum-vol-target")
    backtest.add_argument("--config", default="configs/risk.yml")
    backtest.add_argument("--dataset", default="data/raw/etfs.csv")
    backtest.add_argument("--output", default="reports/latest_backtest.json")
    backtest.add_argument("--report-output", default="reports/latest_backtest.md")
    backtest.set_defaults(func=_backtest)

    train = subparsers.add_parser("train")
    train.add_argument("--model", required=True)
    train.add_argument("--config", default="configs/model.yml")
    train.add_argument("--dataset", default="data/processed/features.csv")
    train.add_argument("--output", default="models/latest_model.json")
    train.add_argument("--run-output", default="reports/latest_model_run.json")
    train.set_defaults(func=_train)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--run-id", required=True)
    evaluate.add_argument("--output", default="reports/latest_model_eval.json")
    evaluate.set_defaults(func=_evaluate)

    promote = subparsers.add_parser("promote")
    promote.add_argument("--run-id", required=True)
    promote.add_argument("--baseline", required=True)
    promote.add_argument("--output", default="reports/latest_promotion_decision.json")
    promote.add_argument("--min-accuracy-lift", type=float, default=0.02)
    promote.add_argument("--min-test-samples", type=int, default=30)
    promote.set_defaults(func=_promote)

    llm_eval = subparsers.add_parser("llm-eval")
    llm_eval.add_argument("--output", default="reports/llm_guardrail_eval.json")
    llm_eval.set_defaults(func=_llm_eval)

    report = subparsers.add_parser("report")
    report.add_argument("--run-id", default="reports/latest_backtest.json")
    report.add_argument("--output", default="reports/report.md")
    report.set_defaults(func=_report)

    paper = subparsers.add_parser("paper")
    paper.add_argument("--broker", default="alpaca")
    paper.add_argument("--dry-run", action="store_true", default=True)
    paper.add_argument("--real-paper", action="store_true")
    paper.add_argument("--confirm-paper", action="store_true")
    paper.add_argument("--universe", default="configs/universe.yml")
    paper.add_argument("--risk", default="configs/risk.yml")
    paper.add_argument("--read-account", action="store_true")
    paper.add_argument("--read-positions", action="store_true")
    paper.add_argument("--kill-switch-test", action="store_true")
    paper.add_argument("--signal-model", default="models/latest_model.json")
    paper.add_argument("--features", default="data/processed/features.csv")
    paper.add_argument("--signal-threshold", type=float, default=0.5)
    paper.add_argument("--submit-signal-order", action="store_true")
    paper.add_argument("--max-feature-age-days", type=int, default=5)
    paper.add_argument("--as-of-date")
    paper.add_argument("--list-orders", action="store_true")
    paper.add_argument("--order-status", default="open")
    paper.add_argument("--get-order", action="store_true")
    paper.add_argument("--order-id")
    paper.add_argument("--client-order-id")
    paper.add_argument("--cancel-order", action="store_true")
    paper.add_argument("--confirm-cancel", action="store_true")
    paper.add_argument("--reconcile-order", action="store_true")
    paper.add_argument("--source-report")
    paper.add_argument("--output", default="reports/paper_kill_switch_test.json")
    paper.set_defaults(func=_paper)
    return parser


def _ingest(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if args.source_csv:
        records = read_records(args.source_csv)
    else:
        universe = load_universe_config(args.config)
        records = generate_sample_ohlcv(symbols=universe.symbols, start=args.start, end=args.end)
    write_records(records, output)
    print(f"wrote {len(records)} rows to {output}")
    return 0


def _validate_data(args: argparse.Namespace) -> int:
    records = read_records(args.dataset)
    result = validate_ohlcv_records(records)
    if result.valid:
        print(f"valid dataset: {result.row_count} rows, {len(result.symbols)} symbols")
        return 0
    for error in result.errors:
        print(error, file=sys.stderr)
    return 1


def _manifest(args: argparse.Namespace) -> int:
    records = read_records(args.dataset)
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1
    manifest = build_dataset_manifest(records, source=str(args.dataset))
    manifest["dataset_path"] = str(Path(args.dataset))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote manifest to {output}")
    return 0


def _build_features(args: argparse.Namespace) -> int:
    records = read_records(args.dataset)
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1
    features = build_features(records)
    write_records(features, args.output)
    print(f"wrote {len(features)} feature rows to {args.output}")
    return 0


def _backtest(args: argparse.Namespace) -> int:
    if args.strategy != "momentum-vol-target":
        print(f"unknown strategy: {args.strategy}", file=sys.stderr)
        return 2
    risk = load_risk_config(args.config)
    records = read_records(args.dataset)
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1
    result = run_momentum_vol_target_backtest(
        records,
        BacktestConfig(
            max_gross_exposure=risk.max_gross_exposure,
            max_single_position=risk.max_single_position,
        ),
    )
    metadata = build_dataset_manifest(records, source=str(args.dataset))
    metadata["dataset_path"] = str(Path(args.dataset))
    result = _with_metadata(result, metadata)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    report = Path(args.report_output)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_backtest_report(result), encoding="utf-8")
    print(f"wrote backtest to {output}")
    return 0


def _report(args: argparse.Namespace) -> int:
    run_path = Path(args.run_id)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    from trading_ai.backtest.engine import BacktestResult

    result = BacktestResult(
        config=BacktestConfig(**payload["config"]),
        daily_returns=tuple(payload["daily_returns"]),
        equity_curve=tuple(payload["equity_curve"]),
        positions=tuple(),
        trades=tuple(),
        metrics={key: float(value) for key, value in payload["metrics"].items()},
        metadata=payload.get("metadata", {}),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_backtest_report(result), encoding="utf-8")
    print(f"wrote report to {output}")
    return 0


def _paper(args: argparse.Namespace) -> int:
    if args.broker != "alpaca":
        print(f"unknown broker: {args.broker}", file=sys.stderr)
        return 2
    if args.real_paper and not args.confirm_paper:
        print("--real-paper requires --confirm-paper", file=sys.stderr)
        return 2
    if args.real_paper and args.kill_switch_test:
        print("--kill-switch-test is local dry-run only; omit --real-paper", file=sys.stderr)
        return 2
    if args.cancel_order and not args.confirm_cancel:
        print("--cancel-order requires --confirm-cancel", file=sys.stderr)
        return 2
    if (args.get_order or args.cancel_order) and not (args.order_id or args.client_order_id):
        print("--get-order/--cancel-order requires --order-id or --client-order-id", file=sys.stderr)
        return 2
    if args.order_id and args.client_order_id:
        print("provide only one of --order-id or --client-order-id", file=sys.stderr)
        return 2
    if args.reconcile_order and not args.source_report:
        print("--reconcile-order requires --source-report", file=sys.stderr)
        return 2
    universe = load_universe_config(args.universe)
    risk = load_risk_config(args.risk)
    dry_run = not args.real_paper
    client = None if dry_run else build_alpaca_paper_client()
    broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk, dry_run=dry_run)
    if args.kill_switch_test:
        broker.activate_kill_switch("cli_kill_switch_test")
        order_result = broker.submit_order(
            PaperOrder(
                symbol=universe.symbols[0],
                side="buy",
                quantity=1,
                client_order_id="kill-switch-test",
            )
        )
        cancel_result = broker.cancel_order("kill-switch-test")
        payload = {
            "mode": "dry-run",
            "broker": "alpaca",
            "kill_switch_active": True,
            "order_result": _paper_order_result_to_dict(order_result),
            "cancel_result": _paper_order_result_to_dict(cancel_result),
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote paper kill-switch test to {output}")
        return 0
    if args.list_orders:
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "order_status": args.order_status,
            "orders": [_paper_order_snapshot_to_dict(order) for order in broker.list_orders(status=args.order_status)],
        }
        _write_json_output(payload, args.output)
        print(f"wrote paper orders to {args.output}")
        return 0
    if args.get_order:
        order = _get_requested_order(broker, order_id=args.order_id, client_order_id=args.client_order_id)
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "order": _paper_order_snapshot_to_dict(order),
        }
        _write_json_output(payload, args.output)
        print(f"wrote paper order to {args.output}")
        return 0
    if args.cancel_order:
        resolved_order = None
        if args.client_order_id:
            resolved_order = broker.get_order_by_client_id(args.client_order_id) if not dry_run else None
            cancel_result = broker.cancel_order(client_order_id=args.client_order_id)
        else:
            resolved_order = broker.get_order(order_id=args.order_id) if not dry_run else None
            cancel_result = broker.cancel_order(order_id=args.order_id)
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "resolved_order": _paper_order_snapshot_to_dict(resolved_order) if resolved_order is not None else None,
            "cancel_result": _paper_order_result_to_dict(cancel_result),
        }
        _write_json_output(payload, args.output)
        print(f"wrote paper cancel report to {args.output}")
        return 0 if cancel_result.accepted else 1
    if args.reconcile_order:
        source_report = json.loads(Path(args.source_report).read_text(encoding="utf-8"))
        expected_order = source_report.get("order_intent") or {}
        client_order_id = str(expected_order.get("client_order_id", ""))
        if not client_order_id:
            print("source report does not contain order_intent.client_order_id", file=sys.stderr)
            return 2
        current_order = broker.get_order_by_client_id(client_order_id) if not dry_run else None
        account = broker.read_account()
        positions = broker.read_positions()
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "expected_order": expected_order,
            "current_order": _paper_order_snapshot_to_dict(current_order) if current_order is not None else None,
            "account": _paper_account_to_dict(account),
            "positions": [_paper_position_to_dict(position) for position in positions],
            "reconciliation": _reconcile_order(expected_order, current_order, positions),
        }
        _write_json_output(payload, args.output)
        print(f"wrote paper order reconciliation to {args.output}")
        return 0
    if args.submit_signal_order:
        model = load_model(args.signal_model)
        feature_rows = read_records(args.features)
        signals = generate_model_signals(
            feature_rows,
            model=model,
            allowlist=universe.symbols,
            threshold=args.signal_threshold,
        )
        selected_signal = _select_signal_to_submit(signals)
        order_intent = None
        order_result = None
        submitted = False
        order = None
        client_order_id = None
        if selected_signal is not None:
            client_order_id = _signal_client_order_id(selected_signal)
            order = PaperOrder(
                symbol=selected_signal.symbol,
                side="buy",
                notional=PAPER_SIGNAL_ORDER_NOTIONAL,
                client_order_id=client_order_id,
            )
            order_intent = _paper_order_intent_to_dict(order)
        open_orders = broker.list_orders(status="open")
        positions = broker.read_positions()
        preflight = evaluate_paper_preflight(
            signal=selected_signal,
            client_order_id=client_order_id,
            open_orders=open_orders,
            positions=positions,
            as_of_date=_parse_cli_date(args.as_of_date) if args.as_of_date else date.today(),
            max_feature_age_days=args.max_feature_age_days,
        )
        if order is not None and preflight.allowed:
            order_result = broker.submit_order(order)
            submitted = order_result.accepted
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "preflight": _paper_preflight_to_dict(preflight),
            "open_orders": [_paper_order_snapshot_to_dict(order) for order in open_orders],
            "positions": [_paper_position_to_dict(position) for position in positions],
            "submitted": submitted,
            "signals": [_model_signal_to_dict(signal) for signal in signals],
            "selected_signal": _model_signal_to_dict(selected_signal) if selected_signal is not None else None,
            "order_intent": order_intent,
            "order_result": _paper_order_result_to_dict(order_result) if order_result is not None else None,
            "account": _paper_account_to_dict(broker.read_account()),
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote paper signal order report to {output}")
        return 0 if order_result is None or order_result.accepted else 1
    if args.read_account or args.read_positions:
        payload: dict[str, object] = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
        }
        if args.read_account:
            payload["account"] = _paper_account_to_dict(broker.read_account())
        if args.read_positions:
            payload["positions"] = [_paper_position_to_dict(position) for position in broker.read_positions()]
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote paper status to {output}")
        return 0
    mode = "dry-run" if dry_run else "real-paper"
    print(f"alpaca paper broker initialized in {mode} mode")
    return 0


def _train(args: argparse.Namespace) -> int:
    if args.model != "logistic-baseline":
        print("only logistic-baseline is implemented without optional ML dependencies", file=sys.stderr)
        return 2
    records = read_records(args.dataset)
    manifest = build_dataset_manifest(records, source=str(args.dataset))
    feature_names = _default_feature_names(records)
    config = LogisticBaselineConfig(feature_names=feature_names)
    examples = build_supervised_examples(records, feature_names=config.feature_names)
    split = temporal_train_test_split(examples, test_fraction=config.test_fraction)
    model = train_logistic_baseline(split.train, config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_model(model, str(output))
    run_payload = {
        "model_type": args.model,
        "model_path": str(output),
        "dataset_path": str(Path(args.dataset)),
        "dataset_hash": manifest["dataset_hash"],
        "feature_names": list(config.feature_names),
        "train_range": [split.train[0].timestamp, split.train[-1].timestamp],
        "test_range": [split.test[0].timestamp, split.test[-1].timestamp],
        "metrics": {
            "train": evaluate_classifier(model, split.train),
            "test": evaluate_classifier(model, split.test),
            "walk_forward": walk_forward_evaluate(
                examples,
                config,
                min_train_size=max(2, len(split.train) // 2),
                test_size=max(1, len(split.test)),
            ),
        },
    }
    run_output = Path(args.run_output)
    run_output.parent.mkdir(parents=True, exist_ok=True)
    run_output.write_text(json.dumps(run_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote model to {output}")
    print(f"wrote training run to {run_output}")
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    run_path = Path(args.run_id)
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    records = read_records(run_payload["dataset_path"])
    feature_names = tuple(str(name) for name in run_payload["feature_names"])
    examples = build_supervised_examples(records, feature_names=feature_names)
    split = temporal_train_test_split(examples, test_fraction=0.25)
    model = load_model(run_payload["model_path"])
    eval_payload = {
        "run_id": str(run_path),
        "model_path": run_payload["model_path"],
        "dataset_hash": build_dataset_manifest(records, source=run_payload["dataset_path"])["dataset_hash"],
        "metrics": {
            "train": evaluate_classifier(model, split.train),
            "test": evaluate_classifier(model, split.test),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(eval_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote evaluation to {output}")
    return 0


def _promote(args: argparse.Namespace) -> int:
    run_payload = json.loads(Path(args.run_id).read_text(encoding="utf-8"))
    baseline_payload = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    challenger_metrics = run_payload.get("metrics", {}).get("test", {})
    decision = evaluate_promotion(
        challenger_metrics=challenger_metrics,
        baseline_metrics=baseline_payload,
        policy=PromotionPolicy(
            min_accuracy_lift=args.min_accuracy_lift,
            min_test_samples=args.min_test_samples,
        ),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(decision.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote promotion decision to {output}")
    return 0 if decision.approved else 1


def _llm_eval(args: argparse.Namespace) -> int:
    payload = run_guardrail_evals()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote LLM guardrail eval to {output}")
    return 0 if payload["failed"] == 0 else 1


def _not_implemented(message: str):
    def handler(_: argparse.Namespace) -> int:
        print(message, file=sys.stderr)
        return 2

    return handler


def _default_feature_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    first = records[0] if records else {}
    candidates = (
        "momentum_20",
        "momentum_2",
        "realized_volatility_20",
        "realized_volatility_3",
        "relative_volume_20",
        "relative_volume_2",
    )
    names = tuple(name for name in candidates if name in first)
    if not names:
        raise ValueError("dataset does not contain supported feature columns")
    return names


def _with_metadata(result, metadata: dict[str, object]):
    from trading_ai.backtest.engine import BacktestResult

    return BacktestResult(
        config=result.config,
        daily_returns=result.daily_returns,
        equity_curve=result.equity_curve,
        positions=result.positions,
        trades=result.trades,
        metrics=result.metrics,
        metadata=metadata,
    )


def _paper_order_result_to_dict(result) -> dict[str, object]:
    return {
        "accepted": result.accepted,
        "status": result.status,
        "reasons": list(result.reasons),
        "dry_run": result.dry_run,
        "broker_response": _broker_response_to_dict(result.broker_response),
    }


def _paper_order_snapshot_to_dict(order: PaperOrderSnapshot) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "time_in_force": order.time_in_force,
        "status": order.status,
        "notional": order.notional,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "filled_avg_price": order.filled_avg_price,
        "submitted_at": order.submitted_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "expires_at": order.expires_at,
    }


def _get_requested_order(
    broker: AlpacaPaperBroker,
    *,
    order_id: str | None,
    client_order_id: str | None,
) -> PaperOrderSnapshot:
    if order_id:
        return broker.get_order(order_id=order_id)
    if client_order_id:
        return broker.get_order_by_client_id(client_order_id)
    raise ValueError("order_id or client_order_id is required")


def _reconcile_order(
    expected_order: dict[str, object],
    current_order: PaperOrderSnapshot | None,
    positions: tuple[PaperPosition, ...],
) -> dict[str, object]:
    differences: list[str] = []
    expected_symbol = str(expected_order.get("symbol", "")).upper()
    if current_order is None:
        differences.append("order_missing")
        return {"matched": False, "differences": differences}

    status = current_order.status.lower()
    if current_order.symbol != expected_symbol:
        differences.append("unexpected_symbol")
    if status in {"canceled", "cancelled"}:
        differences.append("cancelled")
    elif status == "expired":
        differences.append("expired")
    elif current_order.filled_quantity <= 0:
        differences.append("not_filled_yet")
    elif not any(position.symbol == current_order.symbol for position in positions):
        differences.append("filled_without_position")

    return {"matched": not differences, "differences": differences}


def _paper_order_intent_to_dict(order: PaperOrder) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": order.symbol.upper(),
        "side": order.side.lower(),
        "client_order_id": order.client_order_id,
        "type": "market",
        "time_in_force": "day",
    }
    if order.quantity is not None:
        payload["quantity"] = order.quantity
    if order.notional is not None:
        payload["notional"] = order.notional
    return payload


def _paper_preflight_to_dict(decision: PaperPreflightDecision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "reasons": list(decision.reasons),
        "checked_at": decision.checked_at,
        "max_feature_age_days": decision.max_feature_age_days,
    }


def _model_signal_to_dict(signal: ModelSignal) -> dict[str, object]:
    return {
        "timestamp": signal.timestamp,
        "symbol": signal.symbol,
        "probability": signal.probability,
        "threshold": signal.threshold,
        "action": signal.action,
    }


def _select_signal_to_submit(signals: tuple[ModelSignal, ...]) -> ModelSignal | None:
    buy_signals = [signal for signal in signals if signal.action == "buy"]
    if not buy_signals:
        return None
    return max(buy_signals, key=lambda signal: (signal.probability, signal.symbol))


def _signal_client_order_id(signal: ModelSignal) -> str:
    compact_timestamp = "".join(character for character in signal.timestamp if character.isalnum())
    return f"signal-{signal.symbol.lower()}-{compact_timestamp[:16]}"


def _broker_response_to_dict(response) -> object:
    if response is None:
        return None
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    return {"repr": repr(response)}


def _write_json_output(payload: dict[str, object], output_path: str) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_cli_date(value: str) -> date:
    return date.fromisoformat(value)


def _paper_account_to_dict(account) -> dict[str, object]:
    return {
        "account_id": account.account_id,
        "status": account.status,
        "cash": account.cash,
        "equity": account.equity,
        "buying_power": account.buying_power,
    }


def _paper_position_to_dict(position) -> dict[str, object]:
    return {
        "symbol": position.symbol,
        "quantity": position.quantity,
        "market_value": position.market_value,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
