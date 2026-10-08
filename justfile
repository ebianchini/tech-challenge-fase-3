set shell := ["powershell.exe", "-NoLogo", "-Command"]

default:
    @just --list

install:
    uv sync --extra dev

model:
    uv run python scripts/create_demo_model.py

benchmark:
    uv run python scripts/benchmark_baseline.py

lint:
    uv run ruff check src tests scripts

test:
    uv run pytest -q tests

api:
    uv run uvicorn triage.api.app:app --host 0.0.0.0 --port 8000

docker-build:
    docker build -t triage-laudos:dev .