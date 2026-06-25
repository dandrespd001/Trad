"""Shared pytest fixtures for the trading-ai test suite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def tmp_reports_dir(tmp_path: Path) -> Path:
    """Temporary directory tree mirroring the reports/ structure."""
    (tmp_path / "reports" / "paper").mkdir(parents=True)
    (tmp_path / "reports" / "registry").mkdir(parents=True)
    (tmp_path / "reports" / "tmp").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "approved" / "core_etfs" / "1d").mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def sample_ohlcv_df() -> Any:
    """120-row synthetic OHLCV DataFrame for SPY (daily, realistic prices).

    Requires the 'research' extras: pip install -e '.[research]'
    """
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(42)
    n = 120
    dates = pd.date_range("2023-01-03", periods=n, freq="B")
    close = 420.0 + np.cumsum(rng.normal(0, 1.5, n))
    high = close + rng.uniform(1, 5, n)
    low = close - rng.uniform(1, 5, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(40_000_000, 60_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


@pytest.fixture()
def dry_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject dry-run paper trading environment variables."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "TEST_KEY")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "TEST_SECRET")
    monkeypatch.setenv("PAPER_DRY_RUN", "true")


@pytest.fixture()
def minimal_risk_config() -> dict:
    """Minimal valid RiskLimits configuration dict."""
    return {
        "max_daily_loss_pct": 2.0,
        "max_drawdown_pct": 10.0,
        "max_single_position": 0.30,
        "max_gross_exposure": 1.0,
        "max_consecutive_error_days": 0,
    }


# ---------------------------------------------------------------------------
# LLM mock infrastructure
# ---------------------------------------------------------------------------

_SCHEMA_DEFAULT_RESPONSES: dict[str, dict[str, Any]] = {
    "LLMSignalProposal": {
        "symbol": "SPY",
        "action": "hold",
        "confidence": 0.5,
        "thesis": "Insufficient evidence to recommend a directional position.",
        "risk_notes": ["paper-only; no live execution"],
        "evidence_refs": [],
        "llm_authority": "none",
    },
    "BacktestSummary": {
        "summary": "Mock backtest summary for testing.",
        "key_metrics": {"sharpe": 0.8, "max_drawdown": 0.05},
        "risks": ["data quality", "regime change"],
        "requires_human_review": True,
    },
    "RiskReview": {
        "status": "pass",
        "limit_breaches": [],
        "recommended_actions": ["monitor position sizing"],
        "human_review_required": False,
    },
    "PaperOpsReview": {
        "operational_status": "OK",
        "risks": [],
        "blockers": [],
        "recommendation": "CONTINUE_OFFLINE",
        "reasoning": "All checks passed in mock environment.",
        "human_review_required": False,
        "llm_authority": "none",
    },
}


class MockOpenAIResponse:
    """Minimal stand-in for the openai SDK response object."""

    def __init__(self, schema_name: str, overrides: dict[str, Any] | None = None) -> None:
        payload = dict(_SCHEMA_DEFAULT_RESPONSES.get(schema_name, {"llm_authority": "none"}))
        if overrides:
            payload.update(overrides)
        self.output_text = json.dumps(payload)
        self.usage = MagicMock(input_tokens=100, output_tokens=50)


class MockOpenAIRawClient:
    """Injectable client that replaces the openai SDK in unit tests."""

    def __init__(
        self,
        *,
        schema_name: str = "LLMSignalProposal",
        overrides: dict[str, Any] | None = None,
        raise_on_call: Exception | None = None,
        fail_first_n: int = 0,
    ) -> None:
        self._schema_name = schema_name
        self._overrides = overrides
        self._raise_on_call = raise_on_call
        self._fail_first_n = fail_first_n
        self._call_count = 0
        self.responses = self

    def create(self, **_kwargs: Any) -> MockOpenAIResponse:
        self._call_count += 1
        if self._raise_on_call is not None and self._call_count <= self._fail_first_n:
            raise self._raise_on_call
        if self._raise_on_call is not None and self._fail_first_n == 0:
            raise self._raise_on_call
        return MockOpenAIResponse(self._schema_name, self._overrides)


@pytest.fixture()
def mock_openai_client() -> MockOpenAIRawClient:
    """Injectable OpenAI client that returns valid LLMSignalProposal without network calls."""
    return MockOpenAIRawClient(schema_name="LLMSignalProposal")


@pytest.fixture()
def mock_openai_client_factory():
    """Factory to create a MockOpenAIRawClient with custom parameters."""
    def _factory(
        schema_name: str = "LLMSignalProposal",
        overrides: dict[str, Any] | None = None,
        raise_on_call: Exception | None = None,
        fail_first_n: int = 0,
    ) -> MockOpenAIRawClient:
        return MockOpenAIRawClient(
            schema_name=schema_name,
            overrides=overrides,
            raise_on_call=raise_on_call,
            fail_first_n=fail_first_n,
        )
    return _factory
