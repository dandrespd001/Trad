import importlib.util
import unittest
from pathlib import Path


class PytestShimTests(unittest.TestCase):
    def test_repo_root_does_not_shadow_real_pytest_package(self) -> None:
        root_pytest = Path("pytest.py").resolve()
        spec = importlib.util.find_spec("pytest")

        self.assertFalse(root_pytest.exists())
        if spec is not None and spec.origin is not None:
            self.assertNotEqual(Path(spec.origin).resolve(), root_pytest)


if __name__ == "__main__":
    unittest.main()
