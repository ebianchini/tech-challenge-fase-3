# syntax=docker/dockerfile:1

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /opt/airflow
RUN pip install --no-cache-dir uv==0.13.4
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --extra airflow --no-dev --no-editable

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIRFLOW_HOME=/opt/airflow \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/opt/airflow:/opt/airflow/src"

RUN groupadd --gid 50000 airflow \
    && useradd --uid 50000 --gid airflow --home /home/airflow --create-home airflow \
    && mkdir -p /opt/airflow/logs /opt/airflow/artifacts/retraining /opt/airflow/models/registry \
    && chown -R airflow:airflow /opt/airflow

WORKDIR /opt/airflow
COPY --from=builder /opt/venv /opt/venv
COPY --chown=airflow:airflow src ./src
COPY --chown=airflow:airflow dags ./dags
COPY --chown=airflow:airflow params.yaml ./params.yaml
COPY --chown=airflow:airflow scripts/init_airflow.py ./scripts/init_airflow.py

USER airflow
EXPOSE 8080
CMD ["airflow", "webserver"]
