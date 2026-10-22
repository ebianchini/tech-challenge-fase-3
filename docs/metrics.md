# Metricas da API

O item Prometheus da Etapa 3 instrumenta a aplicacao com `prometheus-client` e expoe `GET /metrics`.
O endpoint esta descrito no OpenAPI e devolve somente metricas tecnicas no formato de exposicao
Prometheus; a inclusao aditiva preserva a versao `1.0` do contrato de inferencia. A coleta por um
servidor local, sua retencao e regras de alerta estao descritas em
[prometheus-local.md](prometheus-local.md). O dashboard provisionado e descrito em
[grafana-local.md](grafana-local.md).

## Execucao e verificacao local

Na raiz do repositorio, use o ambiente definido pelo lockfile:

```powershell
uv sync --locked --extra dev --extra airflow
uv run --no-sync python scripts/create_demo_model.py
uv run --no-sync uvicorn triage.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Em outro terminal, consulte readiness, envie somente um exemplo sintetico e leia as metricas:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready
$syntheticBody = '{"instances":[{"report_text":"exemplo sintetico sem dados de paciente"}]}'
Invoke-RestMethod http://127.0.0.1:8000/v1/triage -Method Post -ContentType application/json -Body $syntheticBody
(Invoke-WebRequest http://127.0.0.1:8000/metrics).Content
# Alternativa ao Invoke-WebRequest:
curl.exe --fail --silent http://127.0.0.1:8000/metrics
```

Nao use laudos reais nesse procedimento. A saida de `/metrics` contem nomes, labels tecnicas e
valores numericos; o texto enviado nao aparece nela. Classes, abstencoes e latencia do modelo sao
inicializadas com zero na carga; os tres tipos de erro existem desde a criacao da aplicacao.
As series HTTP sao criadas conforme as combinacoes de rota, metodo e status sao observadas.

## Familias e contagem

| Familia | Tipo e labels | O que mede |
| --- | --- | --- |
| `triage_http_requests_total` | Counter: `method`, `route`, `status` | Uma observacao por requisicao HTTP, incluindo erros. |
| `triage_http_request_duration_seconds` | Histogram: `method`, `route` | Segundos entre entrada no ASGI e envio do ultimo corpo da resposta, incluindo erros inesperados. |
| `triage_model_inference_duration_seconds` | Histogram: `model_version` | Segundos da chamada ao caso de uso, incluindo preprocessamento, predicao, abstencao e falhas de inferencia. |
| `triage_predictions_total` | Counter: `model_version`, `triage_class` | Um incremento por resultado de uma resposta de triagem valida. |
| `triage_prediction_errors_total` | Counter: `error_type` | Uma falha por requisicao tratada: `INVALID_INPUT_SCHEMA`, `MODEL_UNAVAILABLE` ou `INFERENCE_FAILED`. |
| `triage_abstentions_total` | Counter: `model_version` | Um incremento por resultado com `abstained=true` em uma resposta valida. |
| `triage_model_ready` | Gauge: `model_version` | `1` quando o modelo/preprocessador estao carregados, `0` antes do startup, em falha de carga e no shutdown. |
| `triage_input_length_characters` | Histogram sem labels | Numero de caracteres de cada texto apos `strip()`, sem conteudo. |

Um lote de 32 textos com sucesso gera uma requisicao HTTP, uma observacao de latencia de inferencia,
32 observacoes de tamanho e 32 incrementos de predicao; o contador de abstencoes cresce somente
para os resultados abstidos. A classe prevista continua sendo contada quando ha abstencao.

Validacao Pydantic rejeitada gera uma requisicao HTTP e um erro `INVALID_INPUT_SCHEMA`, mas nao
gera observacoes de inferencia, tamanho ou predicao. Modelo indisponivel gera `MODEL_UNAVAILABLE`
e nao observa tamanho ou inferencia. Se a inferencia falhar, o tempo da tentativa e o tamanho dos
textos validos sao observados, com um erro `INFERENCE_FAILED` e sem predicoes ou abstencoes parciais.
Falha na carga do modelo no startup e representada pelo gauge `0`, sem incrementar erros de
requisicao. Respostas 404/405 entram nas metricas HTTP e nao no contador de erros de predicao.

A latencia de inferencia exclui validacao HTTP/Pydantic e construcao da resposta. Ela mede uma
chamada de lote ao caso de uso, nao tempo por texto. Todas as latencias usam segundos; o histograma
de entrada usa caracteres Unicode de Python (`len(text.strip())`), nao bytes ou tokens.
Se uma resposta for interrompida sem enviar seu ultimo corpo ASGI, a latencia HTTP termina na
saida da aplicacao e o contador preserva o status enviado, ou `500` se nao houve inicio de resposta.

Os dois histogramas de latencia usam os buckets `0.001`, `0.005`, `0.01`, `0.02`, `0.05`, `0.1`,
`0.25`, `0.5`, `1`, `2.5`, `5` e `10` segundos, alem de `+Inf`. O tamanho de entrada usa `1`, `50`,
`100`, `250`, `500`, `1000`, `2500`, `5000`, `7500` e `10000` caracteres, alem de `+Inf`.
Histogramas expoem series `_bucket`, `_sum` e `_count`; o cliente tambem pode expor timestamps
`_created`. O bucket `le` e gerado pelo cliente, com esse conjunto fixo de limites.

Exemplos de consultas no Prometheus local:

```promql
sum(rate(triage_http_requests_total[5m]))
histogram_quantile(0.95, sum by (le) (rate(triage_http_request_duration_seconds_bucket{route="/v1/triage"}[5m])))
sum(rate(triage_prediction_errors_total[5m]))
sum(rate(triage_abstentions_total[5m])) / sum(rate(triage_predictions_total[5m]))
max(triage_model_ready)
```

A taxa de abstencao e definida por predicao, enquanto erros sao contados por requisicao. Nao
divida erros pelo total de predicoes ao comparar cargas com tamanhos de lote diferentes.

## Operacao e limites

Cada instancia criada por `create_app()` possui seu proprio `CollectorRegistry`. Os contadores
reiniciam com o processo; use `rate()` ou `increase()` no Prometheus para considerar esses resets.
Nao ha collectors padrao de processo/Python no registro exposto.

Execute **um worker por container**. Nao ha agregacao multiprocess nesta entrega: varios workers
atras da mesma porta produziriam amostras diferentes a cada scrape. Para escalar, execute containers
separados e configure a coleta individual de cada replica no trabalho de infraestrutura.
O comando padrao do Dockerfile explicita `--workers 1`.

`/metrics` nao tem autenticacao na aplicacao. Restrinja-o por rede/security group e por regras do
gateway: permita acesso somente ao coletor e mantenha essa rota fora da lista publica. Os comandos
acima escutam apenas em loopback. No Compose local, API e Prometheus compartilham a rede interna
`api-internal`; Grafana e Prometheus usam `observability`, separada da API e do Airflow.
As portas publicadas escutam em `127.0.0.1`:
`/metrics` continua acessivel localmente na porta da API. A segmentacao de rede do ambiente de
destino deve ser configurada nesse ambiente.

Esta instrumentacao descreve comportamento tecnico do prototipo sintetico. Readiness e taxa de
abstencao nao representam aprovacao ou seguranca clinica.

## Revisao de labels e privacidade

- [x] Usar rota registrada/template; caminhos inexistentes recebem `route="unmatched"`.
- [x] Limitar metodos HTTP e tipos de erro a conjuntos conhecidos; metodos desconhecidos usam `OTHER`.
- [x] Limitar a classe aos valores `normal`, `atencao` e `urgente`.
- [x] Derivar versao exclusivamente de metadados do modelo carregado, com ate 64 caracteres seguros;
  hashes, UUIDs, caminhos e formatos invalidos recebem `unknown`. A versao real da resposta HTTP
  nao e alterada por essa normalizacao de labels. Metadados de versao nao devem conter nomes de
  pacientes ou identificadores, mesmo quando a sintaxe for valida.
- [x] Nao usar texto, `request_id`, `prediction_id`, URL completa, query string, cabecalhos, tokens,
  paciente, usuario, hospital, fingerprint ou mensagem de excecao como label.
- [x] Observar somente o numero de caracteres do texto, sem registrar seu conteudo.
- [x] Manter os scrapes de `/metrics` fora das metricas HTTP para evitar auto-observacao.

Esta revisao cobre o endpoint e suas labels. A aprovacao institucional de privacidade e os demais
gates de [privacy-checklist.md](privacy-checklist.md) continuam pendentes para dados hospitalares.

## Evidencia local

Validacao de 22/10/2026: **179 testes aprovados, sem skips**, incluindo **19 novos testes de
metricas**, com **83% de cobertura da aplicacao e scripts**. Ruff, verificacao formal do contrato
OpenAPI contra o baseline atualizado e verificacao do lockfile passaram. Os testes cobrem lotes,
erros, abstencoes, readiness, isolamento de registros, labels limitadas, ausencia de conteudo
sensivel, scrapes sem auto-observacao e respostas ASGI com streaming/erros.

O smoke HTTP tambem passou contra Uvicorn real com modelo sintetico: liveness, readiness, contrato
de triagem e erro 422 foram verificados antes da leitura de `/metrics`. A exposicao foi parseada
com `prometheus-client` e confirmou tres predicoes, tres tamanhos, uma inferencia, um erro e modelo
ready. Texto sintetico e request id estavam ausentes, e `/metrics` nao apareceu como label de rota.
Essa verificacao executou a API diretamente, sem Docker ou servidor Prometheus.

Essa evidencia valida a instrumentacao local. A configuracao e os testes do coletor local sao
tratados em [prometheus-local.md](prometheus-local.md). A evidencia do dashboard publicado e os
passos para validar as alteracoes atuais da stack estao em [compose-local.md](compose-local.md).
