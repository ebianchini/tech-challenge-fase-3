#!/bin/bash
set -euo pipefail
airflow dags trigger triage_retrain \
  --run-id local-manual-check \
  --conf '{"source_path":"/opt/airflow/data/raw/medical_abstracts.csv"}'
