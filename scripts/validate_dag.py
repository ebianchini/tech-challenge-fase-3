"""Validate DAG parsing and structure without running tasks or accessing datasets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
TASK_ORDER = (
    "load_data",
    "validate_data",
    "prepare_splits",
    "train_model",
    "evaluate_model",
    "export_onnx",
    "benchmark_latency",
    "register_if_approved",
)


def validate_dag(dag_dir: Path = ROOT_DIR / "dags") -> dict[str, Any]:
    """Require real Airflow imports and the complete, correctly ordered pipeline."""
    sources = sorted(dag_dir.rglob("*.py"))
    if not sources:
        raise ValueError("Nenhum arquivo Python de DAG encontrado.")
    for source in sources:
        compile(source.read_bytes(), str(source), "exec")

    try:
        from airflow.models import DagBag
    except ImportError as exc:
        raise RuntimeError(
            "Validacao da DAG exige Airflow instalado: "
            "uv sync --locked --extra dev --extra airflow."
        ) from exc

    dag_bag = DagBag(
        dag_folder=str(dag_dir),
        include_examples=False,
        safe_mode=False,
        read_dags_from_db=False,
    )
    if dag_bag.import_errors:
        raise ValueError(f"Falha ao importar DAGs: {dag_bag.import_errors}")
    if set(dag_bag.dags) != {"triage_retrain"}:
        raise ValueError(f"DAGs inesperadas: {sorted(dag_bag.dags)}")

    # Access the parsed objects directly: get_dag() can query the metadata database.
    dag = dag_bag.dags["triage_retrain"]
    expected_dependencies = {
        task_id: list(TASK_ORDER[index + 1 : index + 2])
        for index, task_id in enumerate(TASK_ORDER)
    }
    dependencies = {
        task.task_id: sorted(task.downstream_task_ids)
        for task in dag.tasks
    }
    if dependencies != expected_dependencies:
        raise ValueError(f"Tarefas ou dependencias inesperadas: {dependencies}")
    if dag.catchup or dag.max_active_runs != 1:
        raise ValueError("A DAG deve usar catchup=False e max_active_runs=1.")
    if any(task.do_xcom_push for task in dag.tasks):
        raise ValueError("As tarefas nao devem transportar resultados de treino via XCom.")

    return {
        "status": "passed",
        "dag_id": dag.dag_id,
        "task_count": len(dag.tasks),
        "dependencies": dependencies,
        "catchup": dag.catchup,
        "max_active_runs": dag.max_active_runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Relatorio JSON sem dados de entrada.")
    args = parser.parse_args()

    with TemporaryDirectory(prefix="triage-airflow-validation-") as airflow_home:
        os.environ.setdefault("AIRFLOW_HOME", airflow_home)
        os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "false")
        report = validate_dag()

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"DAG {report['dag_id']} validada: {report['task_count']} tarefas, sem executar treino.")


if __name__ == "__main__":
    main()
