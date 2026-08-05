import random
import unittest
from collections.abc import Iterator

from trading_ai.research.block_bootstrap import iter_circular_block_bootstrap_indices


class CircularBlockBootstrapIndexTests(unittest.TestCase):
    def test_shape_range_and_block_adjacency(self) -> None:
        samples = list(
            iter_circular_block_bootstrap_indices(
                7,
                block_size=3,
                n_resamples=5,
                seed=17,
            )
        )

        self.assertEqual(len(samples), 5)
        for sample in samples:
            self.assertIsInstance(sample, tuple)
            self.assertEqual(len(sample), 7)
            self.assertTrue(all(0 <= index < 7 for index in sample))
            for position in range(1, len(sample)):
                if position % 3 != 0:
                    self.assertEqual(sample[position], (sample[position - 1] + 1) % 7)

    def test_output_is_deterministic_for_the_same_seed(self) -> None:
        kwargs = {"block_size": 3, "n_resamples": 4, "seed": 29}

        first = list(iter_circular_block_bootstrap_indices(8, **kwargs))
        second = list(iter_circular_block_bootstrap_indices(8, **kwargs))

        self.assertEqual(first, second)

    def test_unit_blocks_match_local_independent_sampling(self) -> None:
        series_length = 5
        n_resamples = 3
        seed = 41
        expected_rng = random.Random(seed)  # noqa: S311 - mirrors the research helper
        expected = [
            tuple(expected_rng.randrange(series_length) for _ in range(series_length))
            for _ in range(n_resamples)
        ]

        actual = list(
            iter_circular_block_bootstrap_indices(
                series_length,
                block_size=1,
                n_resamples=n_resamples,
                seed=seed,
            )
        )

        self.assertEqual(actual, expected)

    def test_full_blocks_are_circular_permutations(self) -> None:
        samples = list(
            iter_circular_block_bootstrap_indices(
                9,
                block_size=9,
                n_resamples=6,
                seed=53,
            )
        )

        for sample in samples:
            self.assertEqual(set(sample), set(range(9)))
            for position in range(1, len(sample)):
                self.assertEqual(sample[position], (sample[position - 1] + 1) % 9)

    def test_global_random_state_is_unchanged(self) -> None:
        state_before = random.getstate()

        list(
            iter_circular_block_bootstrap_indices(
                7,
                block_size=3,
                n_resamples=4,
                seed=67,
            )
        )

        self.assertEqual(random.getstate(), state_before)

    def test_invalid_types_name_the_parameter(self) -> None:
        valid = {
            "series_length": 7,
            "block_size": 3,
            "n_resamples": 2,
            "seed": 0,
        }
        for parameter in valid:
            for invalid in (True, "1", 1.0):
                kwargs = dict(valid)
                kwargs[parameter] = invalid
                with self.subTest(parameter=parameter, invalid=invalid), self.assertRaisesRegex(
                    TypeError, parameter
                ):
                    iter_circular_block_bootstrap_indices(**kwargs)

    def test_invalid_ranges_name_the_parameter(self) -> None:
        cases = (
            ({"series_length": 0, "block_size": 1, "n_resamples": 1}, "series_length"),
            ({"series_length": 7, "block_size": 0, "n_resamples": 1}, "block_size"),
            ({"series_length": 7, "block_size": 8, "n_resamples": 1}, "block_size"),
            ({"series_length": 7, "block_size": 1, "n_resamples": 0}, "n_resamples"),
        )
        for kwargs, parameter in cases:
            with self.subTest(parameter=parameter, kwargs=kwargs), self.assertRaisesRegex(
                ValueError, parameter
            ):
                iter_circular_block_bootstrap_indices(**kwargs)

    def test_validation_is_eager(self) -> None:
        with self.assertRaisesRegex(TypeError, "seed"):
            iter_circular_block_bootstrap_indices(
                7,
                block_size=3,
                n_resamples=2,
                seed=False,
            )

    def test_result_is_an_incremental_iterator_with_exact_count(self) -> None:
        stream = iter_circular_block_bootstrap_indices(
            7,
            block_size=3,
            n_resamples=4,
            seed=79,
        )

        self.assertIsInstance(stream, Iterator)
        self.assertIs(iter(stream), stream)
        first = next(stream)
        self.assertEqual(len(first), 7)
        self.assertEqual(len(list(stream)), 3)
        with self.assertRaises(StopIteration):
            next(stream)


if __name__ == "__main__":
    unittest.main()
