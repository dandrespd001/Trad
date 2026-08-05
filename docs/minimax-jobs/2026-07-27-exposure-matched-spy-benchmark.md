# MiniMax bounded task: exposure-matched SPY benchmark

## Objective

Add one pure deterministic helper to
`src/trading_ai/research/exposure_benchmark.py` and focused unit tests in
`tests/test_research_exposure_benchmark.py`.

The helper must be named `exposure_matched_spy_returns` and construct the
already-aligned per-session return series for
`SPY_EXPOSURE_MATCHED_NEXT_OPEN_V1`. Scheduling and price alignment happen
outside this helper; its inputs are the effective SPY returns and effective
strategy gross exposures for the same ordered sessions.

## Required interface and semantics

```python
def exposure_matched_spy_returns(
    spy_returns: Iterable[float],
    gross_exposures: Iterable[float],
    *,
    one_way_cost_bps: float,
) -> tuple[float, ...]:
    ...
```

For each session `i`:

- `spy_returns[i]` is an already-aligned SPY return for that session under the
  frozen next-open timing contract.
- `gross_exposures[i]` is the effective long-only target exposure held for
  that return period, in `[0.0, 1.0]`.
- Start from cash with previous exposure `0.0`.
- Charge cost only on absolute exposure turnover:
  `abs(exposure_i - exposure_{i-1}) * one_way_cost_bps / 10_000`.
- Apply the cost once before the session return, using the multiplicative
  factor:
  `(1.0 - turnover_cost) * (1.0 + exposure_i * spy_return_i) - 1.0`.
- Do not add an artificial terminal liquidation or terminal cost.
- Return an immutable tuple.

## Validation and fail-closed behavior

- Materialize/copy both iterables before calculation.
- Reject empty input.
- Reject different lengths.
- Reject strings/bytes as iterables.
- Reject booleans and non-numeric values.
- Reject non-finite returns, exposures, and cost.
- Reject SPY returns `<= -1.0`.
- Reject exposures outside `[0.0, 1.0]`.
- Reject `one_way_cost_bps` outside `[0.0, 10_000.0)`.
- Every produced return must be finite and greater than `-1.0`; fail closed
  otherwise.

Keep validation local to this small module.

## Tests

Add focused `unittest` coverage for:

1. constant full exposure matches SPY after one initial entry cost and has no
   repeated/terminal cost;
2. zero exposure stays in cash with zero return and zero cost;
3. changing exposure charges absolute turnover in both directions;
4. input iterables are copied and the returned value is a tuple;
5. empty/misaligned/string inputs fail;
6. booleans, non-numeric, non-finite, out-of-range exposures/returns/costs
   fail;
7. a static AST test permits only `__future__`, `collections.abc`, `math`, and
   `typing` imports in the production module and rejects filesystem, network,
   process, execution, risk, broker, environment, and dynamic-loading
   references.

Acceptance test:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_research_exposure_benchmark -v
```

## Exclusions

- No files other than the two allowlisted source/test files.
- No CLI, configuration, package initializer, manifest, script, execution,
  broker, risk, live, paper, deployment, credential, network, subprocess,
  filesystem I/O, dynamic loading, or environment access.
- No data files, reports, notebooks, financial observations, or claims of
  profitability.
- Do not implement the target-schedule runner in this job.
- Do not edit the preregistration or authorize campaign execution.
- Keep `live_trading_allowed=false`.
