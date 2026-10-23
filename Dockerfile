# syntax=docker/dockerfile:1

ARG TRIAGE_MODEL_SOURCE=provided

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.4
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM builder AS artifacts-provided
COPY models/model.joblib models/preprocessor.joblib ./models/

FROM builder AS artifacts-demo
COPY scripts/create_demo_model.py ./scripts/create_demo_model.py
RUN /opt/venv/bin/python scripts/create_demo_model.py

FROM artifacts-${TRIAGE_MODEL_SOURCE} AS artifacts

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app"

WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --home /home/app --create-home app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
COPY --from=artifacts /app/models /app/models
RUN chown -R app:app /app /opt/venv && chmod -R u=rwX,go=rX /app /opt/venv

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)); assert r.get('status')=='ok' and r.get('model_loaded') is True and r.get('preprocessor_loaded') is True"

CMD ["uvicorn", "triage.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
