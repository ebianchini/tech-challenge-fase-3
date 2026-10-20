from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.operators.python import PythonOperator

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

ROOT_DIR = Path(os.getenv("TRIAGE_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
DATA_PATH = Path(os.getenv("TRIAGE_DATA_PATH", ROOT_DIR / "data/raw/medical_abstracts.csv"))
RUN_ROOT = Path(os.getenv("TRIAGE_RUN_ROOT", ROOT_DIR / "artifacts/retraining"))
REGISTRY_ROOT = Path(os.getenv("TRIAGE_REGISTRY_ROOT", ROOT_DIR / "models/registry"))
PARAMS_PATH = Path(os.getenv("TRIAGE_PARAMS_PATH", ROOT_DIR / "params.yaml"))
SCHEDULE = os.getenv("TRIAGE_RETRAIN_SCHEDULE") or None
TASK_TIMEOUT = timedelta(
    minutes=int(os.getenv("TRIAGE_TASK_TIMEOUT_MINUTES", "30"))
)
RETRIES = int(os.getenv("TRIAGE_TASK_RETRIES", "2"))


def _run_dir(**context: object) -> str:
    run_id = str(context["run_id"]).replace(":", "_").replace("/", "_")
    path = RUN_ROOT / run_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _task_kwargs() -> dict[str, object]:
    return {
        "retries": RETRIES,
        "retry_delay": timedelta(minutes=2),
        "execution_timeout": TASK_TIMEOUT,
        "do_xcom_push": False,
    }


with DAG(
    dag_id="triage_retrain",
    description="Retreinamento batch auditavel do pipeline de triagem.",
    start_date=datetime(2026, 1, 1),
    schedule=SCHEDULE,
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "triage-ml"},
    tags=["triage", "ml", "retraining"],
) as dag:
    load_data_task = PythonOperator(
        task_id="load_data",
        python_callable=load_data,
        op_kwargs={"source_path": str(DATA_PATH), "run_dir": str(RUN_ROOT / "{{ run_id }}")},
        **_task_kwargs(),
    )
    validate_data_task = PythonOperator(
        task_id="validate_data",
        python_callable=validate_data,
        op_kwargs={"source_path": str(DATA_PATH), "run_dir": str(RUN_ROOT / "{{ run_id }}")},
        **_task_kwargs(),
    )
    prepare_splits_task = PythonOperator(
        task_id="prepare_splits",
        python_callable=prepare_splits,
        op_kwargs={"source_path": str(DATA_PATH), "run_dir": str(RUN_ROOT / "{{ run_id }}")},
        **_task_kwargs(),
    )
    train_model_task = PythonOperator(
        task_id="train_model",
        python_callable=train_model,
        op_kwargs={
            "source_path": str(DATA_PATH),
            "run_dir": str(RUN_ROOT / "{{ run_id }}"),
            "params_file": str(PARAMS_PATH),
        },
        **_task_kwargs(),
    )
    evaluate_model_task_operator = PythonOperator(
        task_id="evaluate_model",
        python_callable=evaluate_model_task,
        op_kwargs={"run_dir": str(RUN_ROOT / "{{ run_id }}")},
        **_task_kwargs(),
    )
    export_onnx_task = PythonOperator(
        task_id="export_onnx",
        python_callable=export_onnx,
        op_kwargs={"run_dir": str(RUN_ROOT / "{{ run_id }}")},
        **_task_kwargs(),
    )
    benchmark_latency_task = PythonOperator(
        task_id="benchmark_latency",
        python_callable=benchmark_latency,
        op_kwargs={"run_dir": str(RUN_ROOT / "{{ run_id }}")},
        **_task_kwargs(),
    )
    register_task = PythonOperator(
        task_id="register_if_approved",
        python_callable=register_if_approved,
        op_kwargs={
            "run_dir": str(RUN_ROOT / "{{ run_id }}"),
            "registry_dir": str(REGISTRY_ROOT),
        },
        **_task_kwargs(),
    )

    (
        load_data_task
        >> validate_data_task
        >> prepare_splits_task
        >> train_model_task
        >> evaluate_model_task_operator
        >> export_onnx_task
        >> benchmark_latency_task
        >> register_task
    )
