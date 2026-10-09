from __future__ import annotations

import csv
import time
import tracemalloc
from pathlib import Path

from triage.domain.service import TriageService
from triage.infrastructure.model_loader import SklearnPredictor

ROOT_DIR = Path(__file__).resolve().parents[1]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def main() -> None:
    predictor = SklearnPredictor(
        ROOT_DIR / "models" / "model.joblib",
        ROOT_DIR / "models" / "preprocessor.joblib",
    )
    service = TriageService(predictor)
    text = "synthetic signal alpha"

    for _ in range(20):
        service.classify([text])

    durations: list[float] = []
    tracemalloc.start()
    for _ in range(100):
        started = time.perf_counter()
        service.classify([text])
        durations.append((time.perf_counter() - started) * 1000)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    output_path = ROOT_DIR / "reports" / "latency_baseline.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=["model_version", "samples", "p50_ms", "p95_ms", "p99_ms", "peak_memory_mb"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "model_version": predictor.model_version,
                "samples": len(durations),
                "p50_ms": round(percentile(durations, 0.50), 3),
                "p95_ms": round(percentile(durations, 0.95), 3),
                "p99_ms": round(percentile(durations, 0.99), 3),
                "peak_memory_mb": round(peak_bytes / 1024 / 1024, 3),
            }
        )


if __name__ == "__main__":
    main()