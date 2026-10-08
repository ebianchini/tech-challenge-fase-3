# Sistema de Triagem Automatica de Laudos

Projeto iniciado conforme o [SDD](SDD.md). Esta entrega cobre a fundacao das Etapas 0 e 1: governanca
documentada, API FastAPI, contrato versionado, carregamento singleton no startup, health checks,
abstencao tecnica, container multi-stage e testes do dominio e da API.

O artefato atual e uma demonstracao tecnica treinada com texto sintetico. O Medical Abstracts TC
Corpus nao possui, por si so, labels de urgencia clinica. Nenhum resultado atual deve ser interpretado
como evidencia de seguranca ou desempenho clinico.

## Execucao local

Requer Python 3.11-3.13 e `uv`.

```powershell
uv sync --extra dev
uv run python scripts/create_demo_model.py
uv run python scripts/benchmark_baseline.py
uv run pytest -q tests
uv run uvicorn triage.api.app:app --host 0.0.0.0 --port 8000
```

A API fica em `http://localhost:8000`. O contrato completo esta em
`docs/inference-contract.md`. Nenhum texto de entrada e escrito em logs ou metricas por esta etapa.
O benchmark sintetico gera `reports/latency_baseline.csv` com p50, p95, p99 e memoria de pico.

O arquivo `.env.example` lista somente configuracoes locais sem segredos. O ambiente de dependencias
e fixado por `pyproject.toml` e `uv.lock`.

## Decisoes de arquitetura

A API usa inferencia real-time sincrona; treino e avaliacao serao batch offline. A arquitetura alvo e
AWS com ECR, ECS/Fargate, ALB, S3 e observabilidade gerenciada quando aplicavel. O Compose local e
somente desenvolvimento. TLS, identidade de servico, rate limiting e autorizacao devem ser aplicados
no gateway antes de qualquer deploy; a API valida limites de 10.000 caracteres e 32 instancias.

Detalhes operacionais: [docs/architecture.md](docs/architecture.md). Governanca: [docs/governance.md](docs/governance.md).
Anotacao clinica proposta: [docs/annotation-protocol.md](docs/annotation-protocol.md). Privacidade:
[docs/privacy-checklist.md](docs/privacy-checklist.md).

## Docker

```powershell
docker build -t triage-laudos:dev .
docker run --rm -p 8000:8000 triage-laudos:dev
# PowerShell: executa readiness e uma requisicao sintetica, removendo o container ao final
./scripts/docker_smoke.ps1
```

Endpoints: `GET /health/live`, `GET /health/ready` e `POST /v1/triage`.

## Proximos bloqueios

Antes de qualquer uso hospitalar, ainda sao necessarios governanca clinica, dataset rotulado por
especialistas, desidentificacao aprovada, pipeline batch, CI/CD, observabilidade Prometheus/Grafana,
DVC, Airflow e comparacao ONNX conforme [TODO](TODO.md).