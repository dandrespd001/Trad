# Bounded implementation: generic circular-block resampling

Modify only these allowlisted files:

- `src/trading_ai/research/block_bootstrap.py`
- `tests/test_research_block_bootstrap.py`

## Objective

Implement the dormant, generic `iter_circular_block_bootstrap_indices` helper
and its tests. The helper streams tuples of indices, preserves consecutive
indices inside each sampled block, wraps at the end, and is deterministic
through a local seeded generator.

## Required API

Keep this exact public signature:

```python
def iter_circular_block_bootstrap_indices(
    series_length: int,
    *,
    block_size: int,
    n_resamples: int,
    seed: int = 0,
) -> Iterator[tuple[int, ...]]:
```

Validate all four arguments immediately when the function is called, before
the returned iterator is consumed. Require `type(value) is int`, rejecting
bools, strings and floats with `TypeError` whose message names the parameter.
Require `series_length >= 1`, `n_resamples >= 1`, and
`1 <= block_size <= series_length`; reject violations with `ValueError` whose
message names the parameter.

## Algorithm and invariants

Use only a local `random.Random(seed)`. For every resample, start with an empty
index list. For each block, call `rng.randrange(series_length)` exactly once,
then append consecutive indices using modulo wraparound. Draw another start
only after the full block, until exactly `series_length` indices exist. Truncate
only the final block. Yield exactly `n_resamples` tuples in order without
materializing every resample; additional memory must be O(`series_length`).

- `block_size=1` must consume RNG identically to sequential calls to a fresh
  `random.Random(seed).randrange(series_length)`.
- `block_size=series_length` must produce a circular rotation containing each
  source index exactly once.
- The module must not read or alter the global random state.
- Add no dependency and no filesystem, environment, network, subprocess,
  dynamic-loading or credential capability.
- Do not import or reference operational packages, configuration, routing,
  activation, brokers, accounts or execution modules.
- Do not edit package initializers or create additional files.

## Acceptance tests

Complete the existing test stubs so they prove:

1. count, tuple length, range and within-block circular adjacency for length 7,
   block 3;
2. deterministic output for the same seed;
3. `block_size=1` exactly matches independent local sampling;
4. `block_size=series_length` yields circular permutations;
5. global RNG state is unchanged after consuming the iterator;
6. every invalid type/range above raises the specified error and names its
   parameter;
7. validation occurs on function call, without `next()`;
8. the result is an iterator consumable incrementally with the exact count.

Return only a unified Git patch beginning with `diff --git`, or `NO_CHANGE` if
the requirements cannot be met exactly.
