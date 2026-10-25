set shell := ["powershell.exe", "-NoLogo", "-Command"]

default:
    @just --list

install:
    uv sync --extra dev

model:
    uv run python scripts/create_demo_model.py

benchmark:
    uv run python scripts/benchmark_baseline.py

benchmark-comparison:
    uv run python scripts/benchmark_comparison.py

train:
    uv run train --data data/interim/medical_tc_train_normalized.csv --output-dir models/training

dvc-repro:
    uv run dvc repro

lint:
    uv run ruff check src tests scripts

test:
    uv run pytest -q tests

api:
    uv run uvicorn triage.api.app:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log --log-config logging/uvicorn.json

docker-build:
    docker build -t triage-laudos:dev .
