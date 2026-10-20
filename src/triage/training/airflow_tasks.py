from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import joblib

from triage.training.dataset import load_csv
from triage.training.pipeline import split_dataset
from triage.training.train import train


def load_data(
    source_path: str | Path,
    run_dir: str | Path,
) -> dict[str, Any]:
    """Verifica a entrada e grava apenas um manifesto pequeno do dataset."""
    source = Path(source_path)
    destination = Path(run_dir)
    destination.mkdir(parents=True, exist_ok=True)
    dataset = load_csv(source, min_samples_per_class=2)
    manifest = {
        "source": str(source),
        "rows": len(dataset.texts),
        "classes": list(dataset.classes),
        "label_column": dataset.label_column,
        "dataset_fingerprint": dataset.fingerprint,
    }
    _write_json(destination / "load_manifest.json", manifest)
    return manifest


def validate_data(
    source_path: str | Path,
    run_dir: str | Path,
    *,
    clinical: bool = False,
    clinical_approval: bool = False,
) -> dict[str, Any]:
    dataset = load_csv(
        source_path,
        min_samples_per_class=2,
        clinical=clinical,
        clinical_approval=clinical_approval,
        required_label_column="urgency_label" if clinical else None,
    )
    report = {
        "valid": True,
        "rows": len(dataset.texts),
        "classes": list(dataset.classes),
        "label_column": dataset.label_column,
        "dataset_fingerprint": dataset.fingerprint,
        "label_semantics": "clinical" if clinical else "technical-demo",
    }
    _write_json(Path(run_dir) / "validation.json", report)
    return report


def prepare_splits(
    source_path: str | Path,
    run_dir: str | Path,
    *,
    test_size: float = 0.2,
    random_state: int = 42,
    temporal_split: bool = False,
) -> dict[str, Any]:
    dataset = load_csv(source_path, min_samples_per_class=2)
    split = split_dataset(
        dataset,
        test_size=test_size,
        random_state=random_state,
        temporal=temporal_split,
    )
    result = {
        "dataset_fingerprint": dataset.fingerprint,
        "train_indices": split.train_indices,
        "test_indices": split.test_indices,
        "test_size": test_size,
        "random_state": random_state,
        "temporal_split": temporal_split,
    }
    _write_json(Path(run_dir) / "split.json", result)
    return result


def train_model(
    source_path: str | Path,
    run_dir: str | Path,
    *,
    params_file: str | Path = "params.yaml",
    clinical: bool = False,
    clinical_approval: bool = False,
) -> dict[str, Any]:
    output_dir = Path(run_dir) / "model"
    metadata = train(
        source_path,
        output_dir,
        params_file=params_file,
        clinical=clinical,
        clinical_approval=clinical_approval,
    )
    return {
        "output_dir": str(output_dir),
        "artifact_fingerprint": metadata["artifact_fingerprint"],
        "dataset_fingerprint": metadata["dataset_fingerprint"],
    }


def evaluate_model_task(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    metadata = _read_json(root / "model" / "metadata.json")
    if not metadata["metrics"] or "quality_gate" not in metadata:
        raise ValueError("Artefato de treino sem metricas ou quality_gate.")
    evaluation = metadata["metrics"] | {"quality_gate": metadata["quality_gate"]}
    _write_json(root / "evaluation.json", evaluation)
    return metadata["quality_gate"]


def export_onnx(run_dir: str | Path) -> dict[str, Any]:
    """Exporta o pipeline completo; a ausencia das dependencias falha explicitamente."""
    try:
        from skl2onnx import convert_sklearn
        from skl2onnx.common.data_types import StringTensorType
    except ImportError as exc:
        raise RuntimeError(
            "Exportacao ONNX requer o extra de dependencias do Airflow."
        ) from exc

    root = Path(run_dir)
    model = joblib.load(root / "model" / "pipeline.joblib")
    onnx_model = convert_sklearn(
        model,
        initial_types=[("text", StringTensorType([None, 1]))],
        target_opset=15,
    )
    output = root / "model" / "pipeline.onnx"
    output.write_bytes(onnx_model.SerializeToString())
    return {
        "path": str(output),
        "sha256": _sha256(output),
        "size_bytes": output.stat().st_size,
    }


def benchmark_latency(run_dir: str | Path, *, iterations: int = 100) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations deve ser positivo.")
    model = joblib.load(Path(run_dir) / "model" / "pipeline.joblib")
    sample = ["synthetic technical benchmark input"]
    for _ in range(min(10, iterations)):
        model.predict(sample)
    durations = []
    for _ in range(iterations):
        started = time.perf_counter()
        model.predict(sample)
        durations.append((time.perf_counter() - started) * 1000)
    durations.sort()
    result = {
        "iterations": iterations,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
    }
    _write_json(Path(run_dir) / "benchmark.json", result)
    return result


def register_if_approved(
    run_dir: str | Path,
    registry_dir: str | Path,
    *,
    require_clinical_approval: bool = True,
) -> dict[str, Any]:
    root = Path(run_dir)
    metadata = _read_json(root / "model" / "metadata.json")
    gate = metadata["quality_gate"]
    if not gate["passed"]:
        raise ValueError("Registro bloqueado: quality gate reprovado.")
    if require_clinical_approval and metadata["label_semantics"] == "clinical":
        if os.getenv("TRIAGE_CLINICAL_APPROVAL") != "approved":
            raise PermissionError("Registro clinico exige TRIAGE_CLINICAL_APPROVAL=approved.")

    version = metadata["artifact_fingerprint"]
    destination = Path(registry_dir) / version
    if destination.is_dir():
        existing = _read_json(destination / "registry.json")
        if existing["artifact_fingerprint"] == version:
            return existing
        raise FileExistsError(f"Registro conflitante para a versao {version}.")
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(root / "model", destination / "model")
    shutil.copy2(root / "evaluation.json", destination / "evaluation.json")
    shutil.copy2(root / "benchmark.json", destination / "benchmark.json")
    registry_entry = {
        "version": version,
        "path": str(destination),
        "dataset_fingerprint": metadata["dataset_fingerprint"],
        "artifact_fingerprint": version,
    }
    _write_json(destination / "registry.json", registry_entry)
    return registry_entry


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    index = min(len(values) - 1, round((len(values) - 1) * percentile))
    return round(values[index], 4)
