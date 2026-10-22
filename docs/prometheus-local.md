# Prometheus local

O item **Prometheus local** da Etapa 3 configura coleta da API, cinco regras de alerta e retencao
em disco. A configuracao fica em [prometheus.yml](../monitoring/prometheus/prometheus.yml), as
regras em [alerts.yml](../monitoring/prometheus/alerts.yml) e os cenarios sinteticos de teste em
[alerts.test.yml](../monitoring/prometheus/alerts.test.yml). A semantica das metricas da aplicacao
esta em [metrics.md](metrics.md).

## Subir a stack

Requer Docker com suporte a containers Linux e Compose v2, alem de Python 3.11-3.13 e `uv` para
gerar o modelo de demonstracao. Na raiz do repositorio:

```powershell
uv sync --locked --extra dev
uv run --no-sync python scripts/create_demo_model.py
docker compose config --quiet
docker compose up -d --build api prometheus
docker compose ps api prometheus
```

O Dockerfile copia `models/model.joblib` e `models/preprocessor.joblib`; a geracao precisa ocorrer
antes do build. Esses artefatos sao sinteticos e nao constituem um modelo clinico aprovado.
O comando explicita os servicos `api` e `prometheus`, sem iniciar os servicos Airflow/Postgres
existentes no mesmo Compose. A API usa um worker por container.

Para incluir o dashboard Grafana ja provisionado, acrescente `grafana` ao comando:
`docker compose up -d --build api prometheus grafana`. O acesso fica em
`http://127.0.0.1:3000`; credenciais locais e detalhes estao em [grafana-local.md](grafana-local.md).

| Componente | Acesso local | Configuracao |
| --- | --- | --- |
| API | `http://127.0.0.1:8000` | Build pelo Dockerfile da raiz; readiness em `/health/ready`. |
| Prometheus | `http://127.0.0.1:9090` | Imagem `prom/prometheus:v3.13.3`; dados em `/prometheus`. |
| Scrape | Interno ao Compose | Job `triage-api`, alvo `api:8000`, caminho `/metrics`, HTTP. |

API, Prometheus e o Grafana opcional compartilham a rede bridge dedicada `observability`, isolada da rede usada
pelo Airflow. As portas publicadas escutam somente em `127.0.0.1`. A API nao autentica `/metrics`,
que continua acessivel ao usuario local pela porta 8000; essa configuracao e de desenvolvimento.
O coletor nao usa `localhost:8000`, pois isso apontaria para o proprio container Prometheus.

Scrapes e avaliacao das regras ocorrem a cada **15 segundos**, com timeout de scrape de **5
segundos**. Os arquivos de configuracao sao montados somente para leitura.
O Prometheus inicia apos o processo da API, sem exigir readiness positivo: assim tambem pode
observar falhas no carregamento do modelo. O healthcheck da API vem do Dockerfile; o do coletor
usa `promtool check ready`.

## Verificar coleta e consultas

Confira readiness e envie exclusivamente dados sinteticos:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready
$syntheticBody = '{"instances":[{"report_text":"exemplo sintetico sem dados de paciente"}]}'
Invoke-RestMethod http://127.0.0.1:8000/v1/triage -Method Post -ContentType application/json -Body $syntheticBody
```

Em `http://127.0.0.1:9090/targets`, o alvo `api:8000` deve ficar **UP** apos o primeiro scrape.
A consulta abaixo deve retornar uma serie com valor `1`:

```powershell
$query = [uri]::EscapeDataString('up{job="triage-api"}')
(Invoke-RestMethod "http://127.0.0.1:9090/api/v1/query?query=$query").data.result
```

No explorador de expressoes do Prometheus, use:

```promql
up{job="triage-api"}
max by (job, instance) (triage_model_ready{job="triage-api"})
sum by (job, instance) (rate(triage_http_requests_total{job="triage-api",method="POST",route="/v1/triage"}[5m]))
histogram_quantile(0.95, sum by (job, instance, le) (rate(triage_http_request_duration_seconds_bucket{job="triage-api",method="POST",route="/v1/triage"}[5m])))
```

`rate()` precisa de pelo menos duas amostras. Gere requisicoes entre scrapes para observar
trafego e aguarde alguns intervalos antes de avaliar uma janela. Ausencia de trafego nao deve
produzir os alertas de erro, latencia ou abstencao. Um gauge `model_version="unknown"` com valor
zero pode coexistir com a versao carregada com valor um; use o `max` por alvo para readiness.

## Alertas

Os limites sao iniciais para operacao do prototipo sintetico. O periodo `for` exige que a condicao
continue verdadeira pelo tempo indicado; o estado passa de `pending` para `firing` nas avaliacoes.

| Regra | Condicao | Janela / `for` | Severidade |
| --- | --- | --- | --- |
| `TriageAPIHighErrorRate` | Mais de 5% de respostas HTTP 5xx em `POST /v1/triage`. | 5m / 2m | `warning` |
| `TriageAPIHighLatency` | p95 da latencia HTTP de `POST /v1/triage` acima de 0,1s. | 5m / 2m | `warning` |
| `TriageAPIDown` | `up{job="triage-api"} == 0`: falha de coleta. | Scrape atual / 1m | `critical` |
| `TriageModelNotReady` | Alvo UP, mas nenhuma versao do modelo indica readiness igual a um, incluindo familia ausente. | Scrape atual / 1m | `critical` |
| `TriageHighAbstentionRate` | Mais de 20% dos resultados com abstencao, por versao de modelo. | 5m / 5m | `warning` |

Erro e latencia sao calculados por `(job, instance)`, sem misturar trafego de outros alvos ou
rotas. Erro usa requisicoes da mesma rota como denominador; status 4xx nao entram no numerador.
A abstencao usa `(job, instance, model_version)` e o total de **predicoes**, incluindo abstidas,
como denominador. Um lote conta uma requisicao HTTP e varias predicoes. As tres regras de taxa
ou latencia exigem trafego positivo, evitando divisao por zero ou alertas por ociosidade.
Readiness e abstencao sao sinais tecnicos; nao medem seguranca clinica.

Consulte as regras e seus estados em `http://127.0.0.1:9090/alerts` ou pela API:

```powershell
(Invoke-RestMethod http://127.0.0.1:9090/api/v1/rules).data.groups
(Invoke-RestMethod http://127.0.0.1:9090/api/v1/alerts).data.alerts
```

Nao ha Alertmanager nem envio de notificacoes nesta entrega. O dashboard Grafana consulta as
mesmas metricas por datasource provisionado; os alertas continuam definidos no Prometheus.
Para verificar indisponibilidade manualmente no ambiente local,
pare somente a API com `docker compose stop api`, aguarde o periodo da regra e confira
`TriageAPIDown`; depois recupere com `docker compose start api`. Erro, latencia, abstencao e
readiness possuem cenarios sinteticos no teste das regras, sem alterar o comportamento da API.

## Retencao e armazenamento

O volume nomeado `prometheus-data` persiste `/prometheus`, inclusive apos recriar o container.
As flags do servico definem `--storage.tsdb.retention.time=7d` e
`--storage.tsdb.retention.size=1GB`: a primeira politica atingida governa a remocao de blocos
antigos. A limpeza e assincrona; o limite de tamanho nao e uma quota rigida do volume.
WAL, head e compactacao tambem precisam de espaco. Veja a
[documentacao de armazenamento do Prometheus](https://prometheus.io/docs/prometheus/latest/storage/).

Reserve **pelo menos 2 GB livres** para esta demonstracao e acompanhe o consumo. Isso e um
orcamento inicial, dependente da carga e cardinalidade. O volume nomeado nao impoe quota: se
houver necessidade de um teto fisico, configure-o no storage/ambiente Docker e deixe margem
para WAL e compactacao. O Compose nao provisiona essa quota externa.

```powershell
docker compose exec prometheus du -sh /prometheus
docker system df -v
```

Para encerrar apenas esta stack sem apagar o historico:

```powershell
docker compose stop api prometheus
```

Se tambem iniciou o dashboard, inclua `grafana` no comando de parada. Evite
`docker compose down -v` como rotina: ele apaga os volumes deste projeto, inclusive o
historico Prometheus e os bancos do Grafana e do Postgres do Airflow.

## Validar e atualizar a configuracao

O `promtool` da mesma imagem verifica sintaxe e cenarios sinteticos sem subir a API ou Airflow:

```powershell
docker compose config --quiet
docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus check config /etc/prometheus/prometheus.yml
docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus test rules /etc/prometheus/alerts.test.yml
```

A CI executa essas verificacoes no job `monitoring`, incluido no gate `CI required`; veja
[ci.md](ci.md). Os testes de regras verificam estados antes/depois de `for`, limites, ausencia
de trafego e separacao entre alvos/modelos. Eles nao substituem a verificacao da rede e dos
containers na maquina de destino.

Apos editar e validar os arquivos montados, aplique-os com `docker compose restart prometheus`
e confira os alvos e regras novamente. Alteracoes no Compose, incluindo retencao, exigem
`docker compose up -d prometheus` para recriar o servico quando necessario.

## Validacao local de 22/10/2026

Validado com os executaveis oficiais do Prometheus/promtool 3.13.3 para Windows, conferidos por
SHA256, e Uvicorn real com modelo sintetico. A configuracao usada no smoke manteve intervalos e
regras versionados, alterando somente o alvo de `api:8000` para uma porta temporaria em loopback.

- `promtool check config`: configuracao valida, com as cinco regras carregadas.
- `promtool test rules`: onze cenarios aprovados.
- Scrape real: `up=1`, oito requisicoes validas em lotes de tres, 24 predicoes, um erro de
  validacao 422 e nove observacoes do histograma HTTP; `/metrics` nao se auto-observou.
- Cinco regras com estado de avaliacao saudavel; flags de retencao de 7d/1GB confirmadas pela API.
- Ao encerrar a API, o scrape passou a `up=0` e `TriageAPIDown` atingiu `firing` apos o periodo `for`.
- YAML do Compose validado com Compose standalone 5.6.1; workflow aprovado pelo actionlint 1.7.12.
- Regressao da aplicacao: 179 testes aprovados, sem skips, 83% de cobertura; Ruff, OpenAPI e
  lockfile aprovados.

O smoke gerou `reports/ci/prometheus-smoke.json`, ignorado pelo Git, e encerrou os processos
temporarios. Nenhum texto clinico real foi usado. Docker nao esta instalado nesta maquina:
build, rede entre containers, healthchecks e persistencia do volume apos recriacao ainda precisam
ser verificados no ambiente Docker. Os comandos deste guia permitem reproduzir essa verificacao.
