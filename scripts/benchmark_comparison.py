from __future__ import annotations

import csv
import json
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import psutil

from triage.domain.service import TriageService
from triage.infrastructure.model_loader import ONNXPredictor, SklearnPredictor

ROOT_DIR = Path(__file__).resolve().parents[1]
TEXTS = [
    "synthetic stable normal alpha",
    "synthetic review attention beta",
    "synthetic priority urgent gamma",
]
ITERATIONS = 100
WARMUP_ITERATIONS = 20


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 4)


def _measure(
    name: str,
    classify: Callable[[list[str]], object],
    batch_size: int,
) -> dict[str, object]:
    sample = TEXTS[:1] if batch_size == 1 else TEXTS
    warmup_started = time.perf_counter()
    for _ in range(WARMUP_ITERATIONS):
        classify(sample)
    warmup_ms = round((time.perf_counter() - warmup_started) * 1000, 4)

    tracemalloc.start()
    durations: list[float] = []
    peak_rss = _resident_memory_bytes()
    for _ in range(ITERATIONS):
        started = time.perf_counter()
        classify(sample)
        durations.append((time.perf_counter() - started) * 1000)
        peak_rss = max(peak_rss, _resident_memory_bytes())
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "backend": name,
        "scenario": "single" if batch_size == 1 else "batch",
        "batch_size": batch_size,
        "iterations": ITERATIONS,
        "warmup_ms": warmup_ms,
        "p50_ms": percentile(durations, 0.50),
        "p95_ms": percentile(durations, 0.95),
        "p99_ms": percentile(durations, 0.99),
        "peak_rss_mb": round(peak_rss / 1024 / 1024, 4),
        "peak_allocated_mb": round(peak_bytes / 1024 / 1024, 4),
    }


def _resident_memory_bytes() -> int:
    return int(psutil.Process().memory_info().rss)


def _cold_start(backend: str) -> float:
    started = time.perf_counter()
    if backend == "sklearn":
        predictor = SklearnPredictor(
            ROOT_DIR / "models" / "model.joblib",
            ROOT_DIR / "models" / "preprocessor.joblib",
        )
    else:
        predictor = ONNXPredictor(
            ROOT_DIR / "models" / "triage.onnx",
            ROOT_DIR / "models" / "model_metadata.json",
        )
    predictor.predict(TEXTS[:1])
    return round((time.perf_counter() - started) * 1000, 4)


def _artifact_size(backend: str) -> int:
    paths = (
        (ROOT_DIR / "models" / "model.joblib", ROOT_DIR / "models" / "preprocessor.joblib")
        if backend == "sklearn"
        else (ROOT_DIR / "models" / "triage.onnx",)
    )
    return sum(path.stat().st_size for path in paths)


def main() -> None:
    sklearn = TriageService(
        SklearnPredictor(
            ROOT_DIR / "models" / "model.joblib",
            ROOT_DIR / "models" / "preprocessor.joblib",
        )
    )
    onnx = TriageService(
        ONNXPredictor(
            ROOT_DIR / "models" / "triage.onnx",
            ROOT_DIR / "models" / "model_metadata.json",
        )
    )
    rows = [
        _measure("sklearn", sklearn.classify, 1),
        _measure("sklearn", sklearn.classify, len(TEXTS)),
        _measure("onnx", onnx.classify, 1),
        _measure("onnx", onnx.classify, len(TEXTS)),
    ]
    for row in rows:
        row["cold_start_ms"] = _cold_start(str(row["backend"]))
        row["artifact_size_bytes"] = _artifact_size(str(row["backend"]))
    by_scenario = {
        (str(row["backend"]), str(row["scenario"])): row for row in rows
    }
    for row in rows:
        baseline = by_scenario[("sklearn", str(row["scenario"]))]
        row["p95_speedup_vs_sklearn"] = round(
            float(baseline["p95_ms"]) / float(row["p95_ms"]), 4
        )
        row["p95_variation_percent_vs_sklearn"] = round(
            (float(row["p95_ms"]) / float(baseline["p95_ms"]) - 1) * 100, 4
        )
        row["rss_delta_mb_vs_sklearn"] = round(
            float(row["peak_rss_mb"]) - float(baseline["peak_rss_mb"]), 4
        )
    output = ROOT_DIR / "reports" / "latency_comparison.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "backend",
        "scenario",
        "batch_size",
        "iterations",
        "warmup_ms",
        "cold_start_ms",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "peak_rss_mb",
        "peak_allocated_mb",
        "artifact_size_bytes",
        "p95_speedup_vs_sklearn",
        "p95_variation_percent_vs_sklearn",
        "rss_delta_mb_vs_sklearn",
    ]
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    sklearn_predictions = sklearn.classify(TEXTS)
    onnx_predictions = onnx.classify(TEXTS)
    assert [prediction.triage_class for prediction in sklearn_predictions] == [
        prediction.triage_class for prediction in onnx_predictions
    ], "sklearn e ONNX produziram classes diferentes"
    max_delta = max(
        abs(
            sklearn_prediction.probabilities[triage_class]
            - onnx_prediction.probabilities[triage_class]
        )
        for sklearn_prediction, onnx_prediction in zip(
            sklearn_predictions, onnx_predictions, strict=True
        )
        for triage_class in sklearn_prediction.probabilities
    )
    if max_delta > 1e-5:
        raise RuntimeError(f"Probabilidades sklearn/ONNX divergentes: {max_delta}")
    onnx_rows = [row for row in rows if row["backend"] == "onnx"]
    if any(float(row["p95_speedup_vs_sklearn"]) <= 1.0 for row in onnx_rows):
        raise RuntimeError("ONNX nao comprovou melhoria de p95 em todos os cenarios.")
    summary = {
        "equivalence": {
            "class_match": True,
            "max_probability_delta": max_delta,
            "probability_tolerance": 1e-5,
        },
        "quality_gate": {
            "passed": True,
            "requirements": [
                "classes equivalentes",
                "delta de probabilidade <= 1e-5",
                "p95 ONNX melhor que sklearn em single e batch",
            ],
        },
        "rows": rows,
    }
    (ROOT_DIR / "reports" / "latency_comparison.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    print(f"Benchmark salvo em {output}")
    print(f"equivalence_max_probability_delta={max_delta:.8f}")


if __name__ == "__main__":
    main()
