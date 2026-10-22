# Stack local com Docker Compose

O [docker-compose.yml](../docker-compose.yml) inicia API, Prometheus e Grafana por padrao.
Airflow e Postgres pertencem ao perfil opcional `airflow`. A stack usa somente o modelo de
demonstracao sintetico e configuracoes de desenvolvimento local.

## Subir a stack padrao

Requer Docker Engine para containers Linux e Compose com suporte a `--wait`. O build baixa
as imagens e dependencias; nao precisa de Python, `uv` ou modelos preparados no host.
Na raiz do repositorio:

```powershell
docker version
docker compose version
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
```

Sem `--profile` nem `COMPOSE_PROFILES`, somente os tres servicos padrao sao iniciados. O Compose
define `TRIAGE_MODEL_SOURCE=demo` no build da API, que gera os artefatos sinteticos dentro da
imagem. Um `docker build` direto preserva o modo `provided` do Dockerfile, usado pelo CD, e
continua exigindo os artefatos em `models/`.

| Servico | URL com os defaults | Verificacao |
| --- | --- | --- |
| API | `http://127.0.0.1:8000` | `/health/live`, `/health/ready`, `/metrics` e `POST /v1/triage`. |
| Prometheus | `http://127.0.0.1:9090` | `/targets` deve mostrar `api:8000` como UP; `/alerts` mostra as regras. |
| Grafana | `http://127.0.0.1:3000` | Login `admin` / `triage-local-demo`; pasta **Triage**, dashboard UID `triage-overview`. |

O datasource Prometheus e o dashboard sao carregados automaticamente dos arquivos em
`grafana/provisioning` e `grafana/dashboards`. Nao e necessario importar JSON pela UI.
[grafana-local.md](grafana-local.md) explica os nove paineis; [prometheus-local.md](prometheus-local.md)
detalha coleta, regras e retencao.

As dependencias controlam o startup: Prometheus aguarda o processo da API, podendo coletar
readiness degradada, e Grafana aguarda o healthcheck do Prometheus. `up --wait` tambem exige
os healthchecks da stack. O healthcheck da API confirma os dois artefatos carregados, o do
Prometheus usa `promtool check ready` e o do Grafana consulta `/api/health`.

## Configuracao local

Todos os valores possuem defaults. Para personalizar, crie `.env` a partir de
[.env.example](../.env.example) se ainda nao tiver um arquivo local; preserve seus valores
existentes. Variaveis exportadas no terminal prevalecem sobre `.env`, conforme a
[interpolacao do Docker Compose](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

| Variaveis | Default / finalidade |
| --- | --- |
| `API_PORT`, `PROMETHEUS_PORT`, `GRAFANA_PORT` | `8000`, `9090`, `3000`; somente a porta do host muda. |
| `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD` | `admin`, `triage-local-demo`; credenciais iniciais da demonstracao. |
| `AIRFLOW_PORT` | `8080`, usado somente pelo perfil `airflow`. |
| `AIRFLOW_ADMIN_USER`, `AIRFLOW_ADMIN_PASSWORD`, `AIRFLOW_ADMIN_EMAIL` | `admin`, `airflow-local-demo`, `admin@example.com`. |
| `AIRFLOW_DB_PASSWORD`, `AIRFLOW_WEBSERVER_SECRET_KEY` | Valores locais de demonstracao; a senha do banco deve usar caracteres seguros para URL. |
| `TRIAGE_RETRAIN_SCHEDULE` | Vazio: execucao manual da DAG. |

Todas as portas publicadas escutam em `127.0.0.1`. As credenciais de exemplo atendem somente
a demonstracao local. Grafana e Airflow preservam usuarios existentes no banco; mudar uma
variavel de senha inicial nao altera automaticamente uma conta ja criada.

## Redes e persistencia

| Rede | Servicos conectados | Finalidade |
| --- | --- | --- |
| `api-internal` | API e Prometheus | Rede interna para scrape de `http://api:8000/metrics`. |
| `api-access` | API | Rede bridge para acesso pela porta local publicada. |
| `observability` | Prometheus e Grafana | Consultas do datasource a `http://prometheus:9090` e acesso local aos dois servicos. |
| `airflow-backend` | Postgres, init, scheduler e webserver | Rede interna exclusiva do perfil Airflow. |
| `airflow-access` | Webserver Airflow | Rede bridge para a UI local do Airflow. |

Grafana nao compartilha rede com a API. O perfil Airflow nao compartilha redes nem volumes de
artefatos com o servico de inferencia. Os nomes de servico resolvem dentro das redes correspondentes;
`localhost` dentro de um container aponta para o proprio container. Os detalhes dessa separacao
estao em [architecture.md](architecture.md#topologia-local).

| Volume | Conteudo |
| --- | --- |
| `prometheus-data` | Series temporais em `/prometheus`; retencao de 7 dias ou 1 GB, conforme o primeiro limite atingido. |
| `grafana-data` | Banco e estado do Grafana em `/var/lib/grafana`. |
| `airflow-metadata` | Banco Postgres do perfil Airflow. |
| `airflow-logs` | Logs das tarefas Airflow. |
| `airflow-artifacts` | Outputs das execucoes de retreino. |
| `airflow-model-registry` | Modelos aprovados pelo quality gate do pipeline. |

Os volumes persistem apos recriar containers. A API recebe o modelo pela imagem e nao monta o
registro do Airflow. Configuracoes Prometheus/Grafana sao bind mounts somente leitura. Reserve
pelo menos 2 GB para os dados do Prometheus: sua retencao nao e uma quota fisica de disco,
como detalhado no [guia de armazenamento](prometheus-local.md#retencao-e-armazenamento).

Se usou a versao anterior do Compose, o antigo volume `postgres-db-volume` nao e reutilizado
automaticamente: o perfil agora usa `airflow-metadata`. O volume antigo e preservado; migre
ou restaure seus metadados explicitamente se precisar desse historico no novo banco.

Todos os servicos limitam os logs do driver Docker a tres arquivos de 10 MB por container.
Essa rotacao nao limita o volume `airflow-logs`; o consumo dos volumes deve ser acompanhado
se o perfil for usado por periodos prolongados.

## Airflow opcional

O perfil acrescenta Postgres, inicializacao, scheduler e webserver, mantendo a stack padrao:

```powershell
docker compose --profile airflow config --quiet
docker compose --profile airflow up -d --build --wait --wait-timeout 300
docker compose --profile airflow ps -a
```

Abra `http://127.0.0.1:8080` com `admin` / `airflow-local-demo`, ou os valores de `.env`.
`airflow-init` termina com codigo zero quando conclui: isso e esperado para esse servico.
O init aplica migracoes e cria o usuario apenas quando necessario. Falhas encerram a
inicializacao e bloqueiam scheduler/webserver, sem ocultar erros com `|| true`.

A DAG e criada pausada, sem agendamento automatico. Para executar retreino, forneca o CSV
esperado em `data/raw/medical_abstracts.csv` e use as configuracoes do pipeline. O perfil nao
baixa datasets nem concede `TRIAGE_CLINICAL_APPROVAL` automaticamente. `dags/`, `src/`,
`params.yaml` e `data/` sao montados somente para leitura; outputs ficam nos volumes dedicados.
O acesso ao treinamento nao altera o modelo que a API ja serve.

Os perfis ativam somente os servicos associados, junto dos servicos sem perfil. Veja a
[documentacao de perfis do Compose](https://docs.docker.com/compose/how-tos/profiles/).

## Validacao integrada

Com Docker ativo, Python 3.11-3.13 e `uv`, execute:

```powershell
uv sync --locked --extra dev
uv run --no-sync python scripts/check_compose.py --output reports/ci/compose-smoke.json
```

[scripts/check_compose.py](../scripts/check_compose.py) cria um projeto `triage-smoke-<id>`
proprio, usa portas dinamicas em loopback e nao carrega o `.env` do usuario nem ativa Airflow.
Ele constroi e sobe os tres servicos, aguarda healthchecks, valida a API com requisicoes
sinteticas, confirma scrape/metricas no Prometheus e executa o checker Grafana. Em seguida,
recria somente o Grafana, preservando seus volumes, e repete as verificacoes de dashboard e
consultas para confirmar o provisionamento automatico e a persistencia.

O script remove somente os containers, redes e volumes do projeto temporario ao final,
inclusive quando uma verificacao falha. Ele nao recria nem apaga os recursos da stack local
ja existente. O relatorio registra resultados tecnicos e codigos de falha, sem credenciais,
respostas de inferencia ou texto enviado. Retorna codigo zero somente quando as verificacoes
e a limpeza concluem com sucesso.

O orcamento de execucao padrao e 600 segundos; ajuste com `--timeout`. A limpeza possui
um prazo adicional de 30 segundos. `--no-build` reutiliza a imagem local `triage-laudos:local`,
sem construir a API. A CI chama o mesmo script no job `monitoring` e publica
somente `reports/ci/compose-smoke.json`; veja [ci.md](ci.md).

## Operacao e diagnostico

```powershell
docker compose ps -a
docker compose logs --tail 100 api prometheus grafana
docker compose restart grafana
docker compose stop
```

`stop` preserva containers e dados; para retomar, use `docker compose start`. Alteracoes no
Compose exigem `docker compose up -d --build --wait` para aplicar build e recriacao quando
necessario. Para encerrar tambem os servicos opcionais, use `docker compose --profile airflow stop`.

`docker compose down` remove os containers/redes ativos da stack padrao e preserva volumes.
`docker compose --profile airflow down` inclui o perfil opcional. Acrescentar `--volumes`
apaga os dados desses volumes; essa opcao e usada pelo smoke somente no projeto temporario.

Se uma porta estiver ocupada, ajuste a variavel correspondente em `.env` e atualize a URL no
navegador. Se `up --wait` falhar, confira o servico marcado como unhealthy e seus logs. Um
datasource com erro exige verificar Prometheus e `api:8000` em `/targets`; nao troque a URL
interna do datasource pela URL do host.

## Evidencia e limite da validacao

O baseline publicado `6f562a6` iniciou a stack Compose e passou o checker do dashboard no
[GitHub Actions, execucao 39865208559](https://github.com/ebianchini/tech-challenge-fase-3/actions/runs/39865208559).
Essa evidencia confirma o provisionamento automatico na versao publicada.

Validacao local desta entrega em 22/10/2026: **257 testes aprovados**, sem skips, com **85% de
cobertura** da aplicacao e scripts; inclui 21 casos do smoke Compose e seis do init Airflow.
Ruff, contrato OpenAPI, lockfile e actionlint aprovados. O Compose standalone 5.6.1 validou os
dois perfis, portas dinamicas e a stack padrao com somente os volumes Grafana/Prometheus.
O preflight real do novo checker retornou `docker_unavailable`, sem criar ou remover stacks.
O [registro versionado da evidencia](evidence/grafana-compose-ci.json) inclui commit completo,
job, hash do artefato e relatorio dos nove paineis/onze consultas. Esse checker aceita consultas
sem amostras; o artefato publicado nao comprova incrementos de contadores nem persistencia.

A maquina local nao possui Docker/Podman/WSL operacionais. Validacao de YAML, testes simulados
e a captura Grafana de processos nativos nao comprovam a execucao das novas redes, perfis e
build no runtime Docker. Essas mudancas e o novo smoke de recriacao precisam de uma execucao
do commit atualizado. O perfil Airflow tambem requer verificacao real de inicializacao e
healthchecks; o smoke padrao cobre API, Prometheus e Grafana.
