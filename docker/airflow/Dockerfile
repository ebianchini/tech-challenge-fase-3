FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIRFLOW_HOME=/opt/airflow

WORKDIR /opt/airflow

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY dags ./dags
COPY params.yaml ./params.yaml
COPY data ./data
COPY models ./models
COPY reports ./reports

RUN pip install --upgrade pip setuptools wheel \
    && pip install "apache-airflow==2.11.2" "psycopg2-binary" "onnx>=1.16,<2.0" "onnxconverter-common>=1.14,<2.0" "skl2onnx>=1.17,<2.0" "scikit-learn>=1.5,<1.7" "joblib>=1.4,<2.0" "pydantic>=2.9,<3.0" "PyYAML>=6.0,<7.0" \
    && mkdir -p /opt/airflow/artifacts/retraining /opt/airflow/models/registry

EXPOSE 8080
CMD ["airflow", "webserver"]
