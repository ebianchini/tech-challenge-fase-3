from __future__ import annotations

import csv
import json

import pytest

from triage.training.airflow_tasks import (
    benchmark_latency,
    evaluate_model_task,
    export_onnx,
    load_data,
    prepare_splits,
    register_if_approved,
    train_model,
    validate_data,
)


def _write_dataset(path) -> None:
    rows = [
        ("alpha normal", "normal"),
        ("beta normal", "normal"),
        ("alpha attention", "atencao"),
        ("beta attention", "atencao"),
        ("alpha urgent", "urgente"),
        ("beta urgent", "urgente"),
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["text", "label_original"])
        writer.writerows(rows)


def test_airflow_task_functions_exchange_files_not_datasets(tmp_path) -> None:
    source = tmp_path / "data.csv"
    run_dir = tmp_path / "run"
    _write_dataset(source)

    manifest = load_data(source, run_dir)
    validation = validate_data(source, run_dir)
    split = prepare_splits(source, run_dir, test_size=0.5)
    params = tmp_path / "params.yaml"
    params.write_text(
        "train:\n  test_size: 0.5\n  min_macro_f1: 0.0\n  min_urgent_recall: 0.0\n",
        encoding="utf-8",
    )
    trained = train_model(source, run_dir, params_file=params)
    gate = evaluate_model_task(run_dir)
    onnx = export_onnx(run_dir)
    benchmark = benchmark_latency(run_dir, iterations=3)
    registered = register_if_approved(run_dir, tmp_path / "registry")

    assert manifest["dataset_fingerprint"] == validation["dataset_fingerprint"]
    assert split["dataset_fingerprint"] == trained["dataset_fingerprint"]
    assert gate["passed"] is True
    assert benchmark["iterations"] == 3
    assert onnx["size_bytes"] > 0
    assert registered["artifact_fingerprint"] == trained["artifact_fingerprint"]
    assert json.loads((run_dir / "validation.json").read_text())["valid"] is True
    assert not (run_dir / "texts.json").exists()


def test_dag_loads_without_executing_tasks_when_airflow_is_available(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "false")
    monkeypatch.setenv("TRIAGE_DATA_PATH", str(tmp_path / "dataset-does-not-exist.csv"))
    monkeypatch.setenv("TRIAGE_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("TRIAGE_REGISTRY_ROOT", str(tmp_path / "registry"))
    pytest.importorskip("airflow")

    from scripts.validate_dag import validate_dag
    from triage.training import airflow_tasks

    def unexpected_execution(*args, **kwargs):
        raise AssertionError("O carregamento da DAG nao deve executar tarefas.")

    for name in (
        "load_data",
        "validate_data",
        "prepare_splits",
        "train_model",
        "evaluate_model_task",
        "export_onnx",
        "benchmark_latency",
        "register_if_approved",
    ):
        monkeypatch.setattr(airflow_tasks, name, unexpected_execution)

    report = validate_dag()

    assert report["status"] == "passed"
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "registry").exists()
