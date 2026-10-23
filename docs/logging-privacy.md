# Logs e privacidade

A API emite auditoria tecnica em JSON em `stdout`, sem criar arquivos de log ou volume
proprio. O coletor do ambiente controla armazenamento, acesso e descarte. O conteudo dos
laudos, headers, credenciais, tokens, query strings, URLs recebidas e mensagens de excecao
nao devem ser armazenados. Use somente dados sinteticos para verificar esta politica.

## Eventos HTTP e correlacao

O middleware ASGI externo registra exatamente um evento `http_request` por requisicao,
incluindo health checks, `/metrics`, 404, 405, 422, 500 e 503. A lista de campos e fechada:

| Campo | Origem e limite |
| --- | --- |
| `timestamp`, `level`, `event` | Horario UTC, severidade e evento fixo. |
| `request_id` | UUID canonico de `X-Request-ID`, ou UUID gerado pelo servidor. |
| `method` | Metodo HTTP conhecido ou `OTHER`. |
| `route` | Template registrado, sem parametros; caminhos desconhecidos viram `unmatched`. |
| `status_code` | Status efetivamente enviado. |
| `latency_ms` | Milissegundos ate o envio do ultimo bloco do corpo, sem trabalho posterior. |
| `model_version` | Versao normalizada dos metadados, com a mesma regra das metricas; antes da carga e `unknown`. |
| `error_type` | Opcional: `INVALID_INPUT_SCHEMA`, `MODEL_UNAVAILABLE` ou `INFERENCE_FAILED`. |

O log nao contem texto, corpo, predicoes, probabilidades, query string, IP, usuario,
headers de autenticacao/cookie, ID opaco do corpo, mensagem de excecao ou traceback.
Nao ha leitura/copia do corpo para produzir logs. A minimizacao usa uma lista permitida
de metadados, sem tentar identificar dados clinicos por regex. A normalizacao de versao
nao autoriza incluir identificadores pessoais em nomes de artefatos.

Um unico `X-Request-ID` no formato UUID canonico e aceito; valores invalidos, nao ASCII
ou headers duplicados recebem um UUID novo. A resposta e o log usam esse UUID inclusive
em erros. O `request_id` opcional no corpo continua sendo ecoado somente no corpo, para
compatibilidade. Quando ele e omitido, corpo/header/log coincidem. Nao enviar dados
pessoais ou credenciais em nenhum identificador, mesmo com formato UUID.

Erros de validacao usam mensagens e tipos conhecidos; a localizacao preserva somente
nomes do schema e indices numericos, trocando campos desconhecidos por `extra`.
Valores, contexto Pydantic e detalhes internos de excecoes sao descartados. O contrato
HTTP segue na versao `1.0`, com os mesmos schemas. Excecoes inesperadas recebem o mesmo
envelope generico 500 e a propagacao ASGI padrao; o formatter do servidor impede a
impressao posterior da excecao.

## Servidor HTTP

O [Dockerfile](../Dockerfile) e o comando `just api` iniciam Uvicorn com
`--no-access-log --log-config logging/uvicorn.json`. A imagem usa o caminho absoluto
`/app/logging/uvicorn.json`. O arquivo [uvicorn.json](../logging/uvicorn.json) substitui
os formatters do servidor por `SafeServerFormatter`: eventos de servidor em `stderr`
contem somente timestamp, nivel e o evento fixo `server_log`. Mensagem, argumentos e
traceback sao descartados, inclusive em falhas de protocolo ou startup. O logger
`uvicorn.access` tambem usa `NullHandler`, evitando uma segunda copia da URL com query
string, endereco remoto ou tokens. O middleware da API fornece os campos uteis para
auditoria de requisicoes sem depender do access log do servidor.

Ao iniciar Uvicorn diretamente a partir da raiz do repositorio, preserve as duas flags:

```powershell
uv run --no-sync uvicorn triage.api.app:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --log-config logging/uvicorn.json
```

Omitir `--log-config`, habilitar access logs ou instalar um handler adicional pode
reintroduzir mensagens e dados brutos. Os eventos genericos de servidor identificam
nivel e horario; use readiness, metricas e auditoria HTTP para investigar operacao.

## Retencao e rotacao local

O [Compose](../docker-compose.yml) aplica a todos os servicos o driver Docker `json-file`
com `max-size: "10m"` e `max-file: "3"`. Quando um arquivo atinge 10 MB, o Docker faz a
rotacao; ao exceder tres arquivos, elimina o mais antigo. O orcamento nominal e de
aproximadamente 30 MB por container, sujeito ao tamanho do ultimo registro e aos
metadados do driver. A retencao e por volume de logs, sem prazo minimo de dias. Esse
limite ja esta aplicado na configuracao versionada, sem depender de defaults do daemon.
Consulte a [documentacao do driver json-file](https://docs.docker.com/engine/logging/drivers/json-file/).

Depois de mudar a configuracao de logs, recrie os containers e confira a configuracao
efetiva; `restart` isolado preserva a configuracao anterior:

```powershell
docker compose up -d --build --force-recreate api prometheus grafana
docker inspect --format '{{json .HostConfig.LogConfig}}' (docker compose ps -q api)
docker compose logs --no-log-prefix --tail 50 api
```

O `inspect` da API deve mostrar `Type=json-file` e `Config` com `max-size=10m`,
`max-file=3`. Um `docker run` fora do Compose precisa das opcoes equivalentes:

```powershell
docker run --rm --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 -p 127.0.0.1:8000:8000 triage-laudos:local
```

O processo Python executado diretamente escreve somente no console; redirecionar a
saida para um arquivo cria armazenamento fora da politica Docker. Use Compose quando
precisar de persistencia local com rotacao. A aplicacao nao duplica logs em arquivos.
O volume opcional `airflow-logs` pertence ao Airflow e nao recebe a auditoria HTTP da
API; o limite do driver Docker nao controla arquivos gravados nesse volume. Nao enviar
laudos a tarefas ou dados reais a demonstracao.

## Retencao AWS

O driver `awslogs` da [task definition](../infra/aws/task-definition.json) envia a saida
do container para `/ecs/<ambiente>/triage`, com stream por task. A politica definida
para esse grupo e de **14 dias**, em todos os ambientes tecnicos. CloudWatch gerencia
armazenamento e descarte; a API nao faz rotacao de arquivos dentro da task Fargate.

Antes de aceitar trafego, o operador deve criar o grupo do ambiente e aplicar o
template [cloudwatch-retention-policy.json](../infra/aws/cloudwatch-retention-policy.json).
Substitua `__ENVIRONMENT__` por `dev`, `staging` ou `production` em uma copia local do
arquivo e use a conta e a regiao correspondentes ao grupo:

```powershell
aws logs put-retention-policy --cli-input-json file://cloudwatch-retention-policy.rendered.json --region <regiao>
aws logs describe-log-groups --log-group-name-prefix /ecs/<ambiente>/triage --region <regiao> --query 'logGroups[].{name:logGroupName,retention:retentionInDays}'
```

Confirme o nome exato do grupo e `retention=14` na resposta. Esses comandos sao
procedimentos operacionais documentados; nenhuma chamada AWS e feita por esta entrega.
Os templates nao provisionam recursos, e o CD apenas substitui a imagem de uma task
existente. A execucao remota e a evidencia da retencao aplicada dependem do ambiente
AWS configurado. A execution role e a role de CD mantem suas permissoes restritas;
aplicar `logs:PutRetentionPolicy` pertence a identidade operacional de infraestrutura.

O CloudWatch pode levar ate 72 horas, e raramente mais, para eliminar eventos apos o
fim da retencao. O prazo de 14 dias e a configuracao de expiracao, nao uma garantia de
apagamento fisico instantaneo. Veja a [referencia PutRetentionPolicy](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html).

Logs do ALB, gateway, WAF ou agentes externos possuem coletores proprios e precisam
de politica de minimizacao, acesso e retencao antes de receber dados assistenciais.
A configuracao da API nao sanitiza registros produzidos fora do seu processo.

## Validacao

Em 22/10/2026, a suite completa passou com **283 testes**, sem skips, incluindo **26 novos**
casos de logs/privacidade, e **86% de cobertura** da aplicacao e scripts. Ruff, contrato OpenAPI
contra baseline e verificacao do lockfile tambem passaram. Execute a regressao com:

```powershell
uv run --no-sync pytest -q tests/test_logging_privacy.py
uv run --no-sync pytest -q tests --cov=triage --cov=scripts --cov-report=term-missing
uv run --no-sync ruff check src tests scripts dags
uv run --no-sync python scripts/validate_openapi.py
uv lock --check
```

Os testes cobrem sucesso, erros 400/404/405/422/500/503, JSON malformado, nomes de campos
extras, mensagens de ValueError, headers sensiveis, tokens, IDs duplicados/com controles,
versoes inseguras, templates de rota e a configuracao real de logging em subprocesso.
O smoke nativo Uvicorn incluiu protocolo malformado e uma excecao inesperada apos a resposta
generica, verificando ausencia de marcadores sinteticos e traceback em corpo/stdout/stderr.
Docker nao esta disponivel nesta maquina; rotacao fisica e build da imagem aguardam o runtime
Docker. O template AWS foi validado offline, sem aplicar a politica em uma conta remota.

## Evidencia local

A [evidencia sanitizada de 22/10/2026](evidence/logging-privacy-local.json) registra
testes com Uvicorn real para health, rota desconhecida, HTTP malformado e excecao
inesperada em endpoint sintetico temporario. Corpo HTTP e streams de logs foram
verificados sem armazenar sua saida bruta. A configuracao de rotacao do Compose e o
schema do template CloudWatch foram validados offline. Docker e AWS nao foram
executados nessa verificacao; o arquivo explicita esses limites.
