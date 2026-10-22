# Sistema de Triagem Automatica de Laudos

Projeto iniciado conforme o [SDD](SDD.md). Esta entrega cobre a fundacao das Etapas 0 e 1: governanca
documentada, API FastAPI, contrato versionado, carregamento singleton no startup, health checks,
abstencao tecnica, container multi-stage e testes do dominio e da API. A Etapa 2 inclui o pipeline
offline, DVC, DAG Airflow e a configuracao de CI descrita abaixo.

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

## Pipeline de dados, modelo e DVC

O pipeline offline esta em [`src/triage/training/`](src/triage/training). O adaptador aceita
`text`/`abstract`/`report_text` e preserva o rótulo original em `label_original`; ele nunca
converte classes temáticas em urgência. O treino técnico é executado com:

```powershell
uv run train --data data/raw/medical_abstracts.csv --output-dir models/training
```

O modelo é `TfidfVectorizer -> LogisticRegression`, com o vocabulário ajustado somente no
conjunto de treino. São persistidos o pipeline completo, componentes compatíveis, split,
fingerprint, parâmetros, métricas por classe, matriz de confusão e erro de calibração.
`--clinical` exige uma coluna `urgency_label` e `--clinical-approval`; sem ambos o treino
clínico falha explicitamente.

O fluxo DVC em [`dvc.yaml`](dvc.yaml) normaliza o CSV e treina usando [`params.yaml`](params.yaml):

```powershell
$env:DVC_REMOTE_URL = "s3://bucket-privado/triage-dvc"
dvc remote add --local -d default $env:DVC_REMOTE_URL
dvc repro
dvc pull
dvc push
```

Configure permissões por identidade (por exemplo, OIDC/role na CI ou perfil local), nunca
credenciais no repositório. O arquivo [`.dvc/config.example`](.dvc/config.example) mostra o
formato; o remote efetivo deve ser gravado somente na configuração local (`--local`). O corpus
precisa ser fornecido por `dvc pull` ou colocado em `data/raw/medical_abstracts.csv`.

## Airflow

A DAG [`dags/triage_retrain.py`](dags/triage_retrain.py) executa, em ordem,
`load_data`, `validate_data`, `prepare_splits`, `train_model`, `evaluate_model`, `export_onnx`,
`benchmark_latency` e `register_if_approved`. As tasks trocam somente manifestos e artefatos em
`TRIAGE_RUN_ROOT`; datasets e modelos não são enviados por XCom (`do_xcom_push=False`).

Instale as dependências da DAG com `uv sync --extra airflow` e aponte o Airflow para `dags/`.
O agendamento é manual por padrão; `TRIAGE_RETRAIN_SCHEDULE`, `TRIAGE_TASK_RETRIES`,
`TRIAGE_TASK_TIMEOUT_MINUTES`, `TRIAGE_DATA_PATH`, `TRIAGE_RUN_ROOT` e `TRIAGE_REGISTRY_ROOT`
permitem parametrizar o ambiente. O registro usa o fingerprint do artefato como diretório
imutável e só ocorre após o quality gate. Para modelo clínico, também exige
`TRIAGE_CLINICAL_APPROVAL=approved`.

## Integracao continua

O workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) executa em todo push e pull request,
com Python 3.12 e dependencias de `uv.lock`. Os jobs verificam Ruff, Pytest com cobertura, sintaxe e
carregamento da DAG pelo Airflow real, e contrato OpenAPI contra um baseline versionado. O gate
final **`CI required`** exige sucesso de todas essas verificacoes.

A CI usa somente fixtures e modelos sinteticos, sem baixar datasets ou publicar modelos. Os uploads
sao limitados aos relatorios explicitamente listados no workflow. Comandos locais, artefatos,
atualizacao do contrato e passos para exigir o gate na branch estao em [docs/ci.md](docs/ci.md).

A configuracao esta versionada; uma execucao verde no GitHub Actions e a protecao de branch ainda
precisam ser confirmadas no repositorio remoto. O checklist [TODO.md](TODO.md) distingue essas
pendencias da implementacao local.

## Proximos bloqueios

Antes de qualquer uso hospitalar, ainda sao necessarios governanca clinica, dataset rotulado por
especialistas, desidentificacao aprovada, protecao de branch com CI obrigatoria, CD,
observabilidade Prometheus/Grafana e comparacao de inferencia ONNX conforme [TODO](TODO.md).
