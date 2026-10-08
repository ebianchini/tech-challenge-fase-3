# Decisoes Operacionais da Etapa 1

## Execucao

A API usa inferencia real-time sincrona: o modelo e carregado uma vez no startup e cada requisicao
recebe resposta imediata. Treino e avaliacao pertencem ao fluxo batch offline e nao participam do
caminho HTTP.

## AWS alvo

A topologia alvo usa ECR para imagens por digest, ECS/Fargate para a API, ALB para TLS, identidade
e health checks, S3 para artefatos e DVC, e observabilidade gerenciada quando aplicavel. O Compose
local, quando criado nas etapas seguintes, e somente ambiente de desenvolvimento.

## Identidade, transporte e limites

- Em producao, o ALB/gateway termina TLS e encaminha identidade de servico autenticada.
- A API nao aceita texto de origem desconhecida; o gateway deve aplicar autenticacao, autorizacao,
  rate limiting e allowlist de rotas.
- O contrato da API limita cada texto a 10.000 caracteres e cada lote a 32 instancias.
- O ambiente local nao e uma fronteira de seguranca e nao deve receber dados hospitalares.
- Timeouts, rate limits e cabecalhos de identidade devem ser configurados no gateway antes do deploy.

## Health, startup e rollback

`/health/live` somente verifica o processo. `/health/ready` confirma modelo e preprocessador
carregados no startup. Falha de artefato deixa readiness degradada e retorna `MODEL_UNAVAILABLE`.
Deploy deve aguardar readiness antes de receber trafego e manter o digest anterior para rollback.

## Configuracao

Caminhos de artefatos podem ser definidos por `TRIAGE_ROOT_DIR`, `TRIAGE_MODEL_PATH` e
`TRIAGE_PREPROCESSOR_PATH`. Nenhuma dessas configuracoes contem segredo.
