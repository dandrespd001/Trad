"""Generic circular-block resampling primitive for offline research."""

from __future__ import annotations

import random
from collections.abc import Iterator


def iter_circular_block_bootstrap_indices(
    series_length: int,
    *,
    block_size: int,
    n_resamples: int,
    seed: int = 0,
) -> Iterator[tuple[int, ...]]:
    """Stream deterministic circular moving-block index samples."""

    for name, value in (
        ("series_length", series_length),
        ("block_size", block_size),
        ("n_resamples", n_resamples),
        ("seed", seed),
    ):
        if type(value) is not int:
            raise TypeError(f"{name} must be an int")

    if series_length < 1:
        raise ValueError("series_length must be at least 1")
    if block_size < 1 or block_size > series_length:
        raise ValueError("block_size must be between 1 and series_length")
    if n_resamples < 1:
        raise ValueError("n_resamples must be at least 1")

    def _iter_samples() -> Iterator[tuple[int, ...]]:
        rng = random.Random(seed)  # noqa: S311 - deterministic research resampling
        for _ in range(n_resamples):
            sample: list[int] = []
            while len(sample) < series_length:
                start = rng.randrange(series_length)
                remaining = series_length - len(sample)
                for offset in range(min(block_size, remaining)):
                    sample.append((start + offset) % series_length)
            yield tuple(sample)

    return _iter_samples()
