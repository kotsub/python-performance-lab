"""Compare repeated scans with one-pass aggregation; standard library only."""

import argparse
import cProfile
import csv
import hashlib
import json
import platform
import pstats
import random
import statistics
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path


def scan_totals(orders, requested_customers):
    """One full scan per requested customer: O(rows * requests)."""
    return [
        sum(amount for customer, amount in orders if customer == wanted)
        for wanted in requested_customers
    ]


def indexed_totals(orders, requested_customers):
    """Build totals and answer requests: O(rows + requests), including the build."""
    totals = {}
    for customer, amount in orders:
        totals[customer] = totals.get(customer, 0) + amount
    return [totals.get(customer, 0) for customer in requested_customers]


def make_data(rows, customers, requests, seed):
    rng = random.Random(seed)
    orders = [(rng.randrange(customers), rng.randrange(1, 100_001)) for _ in range(rows)]
    wanted = [rng.randrange(customers) for _ in range(requests)]
    return orders, wanted


def positive_int(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def measure(function, orders, wanted):
    cpu_start = time.process_time_ns()
    start = time.perf_counter_ns()
    result = function(orders, wanted)
    wall_ms = (time.perf_counter_ns() - start) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_start) / 1_000_000
    return result, wall_ms, cpu_ms


def write_report(directory, metadata, summary):
    lines = [
        "# Результаты локального запуска",
        "",
        "Значения получены при выполнении benchmark.py; это не прогноз для другого ПК.",
        "",
        f"- Дата UTC: {metadata['created_at_utc']}",
        f"- Python: {metadata['python']}",
        f"- Строк: {metadata['rows']:,}; клиентов: {metadata['customers']:,}; "
        f"запросов: {metadata['requests']}; повторов: {metadata['repeats']}.",
        "- Данные генерируются один раз вне измеряемого участка.",
        "- Построение словаря включено во время indexed.",
        "- Порядок методов чередуется; перед замерами есть прогрев.",
        "- Память и профиль измеряются отдельно от основных замеров.",
        "",
        "| Метод | Медиана, мс | Минимум, мс | Максимум, мс | Пик дополнительных Python-аллокаций, КиБ |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, values in summary["methods"].items():
        lines.append(
            f"| {name} | {values['median_wall_ms']:.3f} | {values['min_wall_ms']:.3f} | "
            f"{values['max_wall_ms']:.3f} | {values['peak_traced_bytes'] / 1024:.1f} |"
        )
    lines += [
        "",
        f"Отношение медиан scan / indexed: **{summary['speedup']:.2f}**.",
        "",
        "## Наблюдение",
        "",
        "scan повторно просматривает все строки для каждого запроса. indexed один раз "
        "строит агрегаты и затем ищет результаты в словаре. Результаты методов сверены "
        "для каждого повтора. Дополнительная память словаря - цена ускорения.",
        "",
        "## Ограничения",
        "",
        "Это синтетическая однопоточная задача в памяти. Измерение включает Python, "
        "но не SQL, сеть и дисковый ввод-вывод. Нагрузки с одним запросом или маленьким "
        "объемом данных могут дать другую картину. Пик tracemalloc не равен RSS процесса. "
        "cProfile добавляет накладные расходы и используется для поиска горячих функций, "
        "а не для расчета приведенного ускорения.",
        "",
        "Сырые замеры: timings.csv. Контекст запуска: environment.json. "
        "Профили: profile_scan.txt и profile_indexed.txt.",
    ]
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100_000)
    parser.add_argument("--customers", type=positive_int, default=10_000)
    parser.add_argument("--requests", type=positive_int, default=100)
    parser.add_argument("--repeats", type=positive_int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("results/local"))
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    orders, wanted = make_data(args.rows, args.customers, args.requests, args.seed)
    methods = {"scan": scan_totals, "indexed": indexed_totals}
    expected = scan_totals(orders, wanted)
    for function in methods.values():
        if function(orders, wanted) != expected:
            raise AssertionError("methods produced different results")

    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "rows": args.rows,
        "customers": args.customers,
        "requests": args.requests,
        "repeats": args.repeats,
        "seed": args.seed,
        "result_sha256": hashlib.sha256(json.dumps(expected).encode()).hexdigest(),
        "warmup_runs_per_method": 1,
        "timing_includes_index_build": True,
    }
    quota = Path("/sys/fs/cgroup/cpu.max")
    if quota.exists():
        metadata["container_cpu_max"] = quota.read_text().strip()
    records = []
    for repeat in range(args.repeats):
        names = list(methods) if repeat % 2 == 0 else list(reversed(methods))
        for name in names:
            result, wall_ms, cpu_ms = measure(methods[name], orders, wanted)
            if result != expected:
                raise AssertionError(f"incorrect result: {name}, repeat {repeat + 1}")
            records.append(
                {"method": name, "repeat": repeat + 1, "wall_ms": wall_ms, "cpu_ms": cpu_ms}
            )

    summary = {"methods": {}}
    for name, function in methods.items():
        tracemalloc.start()
        result = function(orders, wanted)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if result != expected:
            raise AssertionError("incorrect result during memory measurement")
        profiler = cProfile.Profile()
        profiler.runcall(function, orders, wanted)
        with (args.output / f"profile_{name}.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(25)
        times = [row["wall_ms"] for row in records if row["method"] == name]
        summary["methods"][name] = {
            "median_wall_ms": statistics.median(times),
            "min_wall_ms": min(times),
            "max_wall_ms": max(times),
            "peak_traced_bytes": peak,
        }
    summary["speedup"] = (
        summary["methods"]["scan"]["median_wall_ms"]
        / summary["methods"]["indexed"]["median_wall_ms"]
    )
    with (args.output / "timings.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["method", "repeat", "wall_ms", "cpu_ms"])
        writer.writeheader()
        writer.writerows(records)
    for filename, value in [("environment.json", metadata), ("summary.json", summary)]:
        (args.output / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    write_report(args.output, metadata, summary)
    print(f"Verified equal results. Report: {args.output / 'REPORT.md'}")
    print(f"Median scan / indexed: {summary['speedup']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
