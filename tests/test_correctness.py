import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("benchmark", ROOT / "benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class AggregationTests(unittest.TestCase):
    def test_empty_missing_and_duplicate_requests(self):
        self.assertEqual(benchmark.indexed_totals([], [1, 1]), [0, 0])
        orders = [(1, 100), (1, 50), (2, 700)]
        expected = [150, 0, 150, 700]
        self.assertEqual(benchmark.scan_totals(orders, [1, 99, 1, 2]), expected)
        self.assertEqual(benchmark.indexed_totals(orders, [1, 99, 1, 2]), expected)

    def test_methods_match_independent_reference(self):
        rng = random.Random(7)
        for _ in range(30):
            orders = [(rng.randrange(8), rng.randrange(-100, 101)) for _ in range(50)]
            wanted = [rng.randrange(10) for _ in range(15)]
            reference = []
            for customer in wanted:
                total = 0
                for order_customer, amount in orders:
                    if order_customer == customer:
                        total += amount
                reference.append(total)
            self.assertEqual(benchmark.scan_totals(orders, wanted), reference)
            self.assertEqual(benchmark.indexed_totals(orders, wanted), reference)

    def test_cli_produces_a_consistent_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            result = subprocess.run(
                [sys.executable, str(ROOT / "benchmark.py"), "--rows", "100",
                 "--customers", "10", "--requests", "4", "--repeats", "2",
                 "--output", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((output / "summary.json").read_text())
            self.assertGreater(summary["speedup"], 0)
            self.assertTrue((output / "REPORT.md").is_file())
            self.assertEqual(len((output / "timings.csv").read_text().splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
