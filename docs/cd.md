# CD: imagem imutavel e ECS/Fargate

O workflow [cd.yml](../.github/workflows/cd.yml) implementa build, smoke test, scan,
publicacao no ECR com OIDC e deploy com verificacao e rollback. A configuracao esta no
repositorio; criar os recursos AWS, configurar os GitHub Environments e executar o primeiro
deploy sao etapas operacionais separadas. Os exemplos em `infra/aws/` sao entradas JSON
parametrizaveis, nao um provisionador de VPC, ALB ou contas.

O workflow gera e embarca somente o modelo de demonstracao sintetico em todos os ambientes,
inclusive `production`. Esta fase nao distribui modelos clinicos. A pipeline valida a entrega
tecnica e nao representa aprovacao clinica ou autorizacao para receber dados assistenciais.

## Acionamento e promocao

- A conclusao bem-sucedida de `CI` para um push na `main` pode iniciar CD para `dev`.
  O automatico fica desativado ate a variavel **do repositorio** `CD_ENABLED` ser exatamente
  `true`. A conclusao de CI de pull request nao inicia deploy.
- `workflow_dispatch` permite escolher `environment` (`dev`, `staging` ou `production`) e
  `commit_sha` completo, com 40 caracteres hexadecimais. O commit precisa pertencer ao
  historico da `main`; sua execucao mais recente de `ci.yml` para push na `main` deve estar
  concluida com sucesso e ter o gate `CI required` aprovado. O dispatch continua disponivel
  quando `CD_ENABLED` estiver desativado.
- A promocao para `production` exige uma tag `vMAJOR.MINOR.PATCH`, com prerelease opcional,
  correspondente ao mesmo commit, e a aprovacao configurada no GitHub Environment. Execute
  o dispatch a partir da `main` para usar o workflow confiavel; informe a tag em `release_tag`.
- Cada ambiente possui seu proprio servico ECS, familia de task definition, role OIDC,
  endpoint e repositorio ECR. Uma conta AWS separada para producao reforca essa divisao.
  Nao reutilize a role ou o servico de producao em `dev`. As entregas para o mesmo ambiente
  sao serializadas e nao cancelam automaticamente o deploy que ja estiver em andamento.

O verificador e obtido explicitamente de `refs/heads/main` e captura seu `controller_sha`.
O job de entrega usa essa revisao fixa para os scripts de verificacao, publicacao e deploy.
O codigo da aplicacao aprovado pela CI fica em um checkout separado, `.release-source`,
usado como contexto do build. Assim, uma versao anterior da aplicacao pode ser entregue
mesmo quando ainda nao possuia scripts de CD, desde que tenha CI verde e cumpra os
requisitos de build e contrato atuais.

O commit identifica o codigo. A tag ECR usa `git-<SHA completo>`; a task definition referencia
`<registry>/<repository>@sha256:<digest>`. Tags como `latest` nao participam da promocao.
Se essa tag ja existir no ECR do ambiente, o workflow baixa a imagem pelo digest e repete
smoke e scan, sem rebuild ou novo push. Se ela nao existir, gera o modelo sintetico e faz
build `linux/amd64`. O push so acontece depois dos gates aprovados; uma repeticao nunca
sobrescreve a tag existente.

O scan usa `anchore/scan-action` 7.4.2 com Grype 0.118.0 e bloqueia vulnerabilidades
**High/Critical**, inclusive quando ainda nao existe correcao (`only-fixed: false`).
`infra/grype.yaml` nao contem excecoes de CVE. Falha de scan impede publicacao e deploy.

## Recursos AWS que devem existir

Prepare os recursos abaixo para cada ambiente antes de habilitar o workflow:

1. Repositorio ECR privado com `imageTagMutability=IMMUTABLE`, sem exclusoes mutaveis;
   [ecr-repository.json](../infra/aws/ecr-repository.json) e uma entrada para
   `aws ecr create-repository --cli-input-json file://...`. Mantenha os digests e as
   revisoes anteriores durante a janela de rollback; a politica de lifecycle nao pode
   remove-los nesse periodo.
2. Cluster ECS e servico **Fargate Linux X86_64**, controlador de deploy `ECS`, rolling
   update e pelo menos uma task ativa e saudavel. O CD atualiza um servico existente.
   O bootstrap inicial deve ser feito pelo operador com uma imagem conhecida e verificada.
3. VPC, duas sub-redes privadas, security group permitindo a porta 8000 somente a partir do
   ALB, target group de tipo `ip` e ALB com HTTPS. Provisione saida via NAT ou endpoints
   privados necessarios para ECR, S3 e CloudWatch Logs. Um runner GitHub precisa alcancar
   `APP_BASE_URL`. O workflow usa runner GitHub `ubuntu-24.04`; endpoints privados exigem
   adaptar `runs-on` para um runner com acesso a essa rede antes de executar o CD.
4. Grupo CloudWatch Logs `/ecs/<ambiente>/triage`, retencao de **14 dias** aplicada com
   [cloudwatch-retention-policy.json](../infra/aws/cloudwatch-retention-policy.json) e
   politicas IAM descritas abaixo. A execution role nao cria grupos nem altera retencao.
   O procedimento e a verificacao estao no [guia de logs e privacidade](logging-privacy.md).
5. Provedor OIDC IAM `https://token.actions.githubusercontent.com`, audience
   `sts.amazonaws.com`, e role assumida exclusivamente pelo GitHub Environment do ambiente.

[task-definition.json](../infra/aws/task-definition.json) define porta 8000, usuario `app`
do Dockerfile, filesystem de leitura, capacidades removidas, artefatos embarcados e health
check que inspeciona o JSON de readiness. [ecs-service.json](../infra/aws/ecs-service.json)
exemplifica rede privada, duas replicas e circuit breaker com rollback. Substitua **todos**
os marcadores `__...__`, inclusive o digest de 64 caracteres hexadecimais, antes de usar
esses arquivos com AWS CLI. Eles nao sao consumidos automaticamente pelo workflow: o CD
deriva a nova revisao da task definition do servico existente, alterando a imagem da API.

Configure o ALB para `GET /health/ready`, porta de trafego, HTTP 200, intervalo 15 s e timeout
5 s. O endpoint atual tambem retorna HTTP 200 com `status=degraded`: por isso o health check
de container da task definition e o verificador pos-deploy precisam confirmar
`status=ok`, `model_loaded=true` e `preprocessor_loaded=true`. Somente testar o status HTTP
nao e suficiente. O ECS executa os health checks declarados na task definition; nao basta
o `HEALTHCHECK` que esta no Dockerfile. Consulte a [documentacao de health checks do
ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/healthcheck.html).

## Roles e menor privilegio

Ha tres identidades distintas:

| Identidade | Trust e permissoes |
| --- | --- |
| Role GitHub de deploy | [github-oidc-trust.json](../infra/aws/github-oidc-trust.json) e [github-deploy-policy.json](../infra/aws/github-deploy-policy.json), uma copia por ambiente. Publica em um ECR e atualiza um servico ECS. |
| Execution role ECS | [ecs-task-trust.json](../infra/aws/ecs-task-trust.json) e [ecs-execution-policy.json](../infra/aws/ecs-execution-policy.json). O agente baixa a imagem e grava logs no grupo definido. |
| Task role da API | Usa o mesmo trust de tasks, sem policies AWS anexadas enquanto a API somente le os artefatos embarcados. Adicione acesso a recursos especificos somente se o codigo precisar. |

Substituicoes para cada copia dos templates:

| Marcador | Exemplo ficticio ou origem |
| --- | --- |
| `__AWS_ACCOUNT_ID__`, `__AWS_REGION__` | ID da conta de 12 digitos e regiao real do ambiente. |
| `__ENVIRONMENT__` | Exatamente `dev`, `staging` ou `production`. |
| `__ECR_REPOSITORY__` | Nome, por exemplo `triage-dev`; nao e a URI completa. |
| `__ECS_CLUSTER__`, `__ECS_SERVICE__` | Nomes usados nos ARNs de cluster/servico. |
| `__TASK_FAMILY__` | Familia exclusiva do ambiente, por exemplo `triage-dev`. |
| `__EXECUTION_ROLE_NAME__`, `__TASK_ROLE_NAME__` | Nomes das duas roles ECS; inclua o path se a role usar um. |
| `__ECS_CONTAINER_NAME__` | Nome da API na task definition, por exemplo `api`. |
| Demais marcadores | Digest da imagem, revisao inicial, sub-redes, security group e target group ja provisionados. |

`iam:PassRole` permite somente as duas roles explicitadas e somente para
`ecs-tasks.amazonaws.com`. `ecs:TagResource` permite preservar tags somente ao registrar
uma revisao da familia autorizada; nao permite alterar tags de recursos existentes.
O CD nao recebe permissoes para criar rede, roles, servicos ou
alterar mutabilidade do ECR. `GetAuthorizationToken` e `DescribeTaskDefinition` usam
`Resource: "*"` pelas limitacoes de autorizacao dessas operacoes. `ListTasks` usa `*` com
condicao para o cluster; `DescribeTasks`, `UpdateService` e registro de task definition ficam
restritos aos ARNs do ambiente. Veja a [referencia IAM do
ECS](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html) e as
[permissoes de push ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html).

Nao anexe a execution policy a role GitHub nem entregue credenciais da role GitHub ao
container. A [execution role](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html)
e usada pelo agente; a [task role](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)
fornece as permissoes do codigo da aplicacao.

O trust OIDC usa `StringEquals` com audience `sts.amazonaws.com` e subject exato
`repo:ebianchini/tech-challenge-fase-3:environment:<ambiente>`. Nao troque por wildcard.
O job que assume a role deve declarar o mesmo GitHub Environment. Repositorios criados
apos 15/07/2026, ou que ativaram subjects imutaveis, podem usar
`repo:ebianchini@<OWNER_ID>/tech-challenge-fase-3@<REPOSITORY_ID>:environment:<ambiente>`;
nesse caso, ajuste somente para o subject exato aplicavel ao repositorio, mantendo os IDs.
Nao imprima tokens OIDC para investigar a configuracao. A [documentacao GitHub de OIDC na
AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
descreve os formatos e a protecao de environments.

## Configuracao no GitHub

Em **Settings > Environments**, crie os environments com estes nomes exatos:
`dev`, `staging`, `production`. Em cada um, defina as seguintes **variables**:

| Variavel | Valor |
| --- | --- |
| `AWS_ACCOUNT_ID` | ID de 12 digitos da conta autorizada; a sessao OIDC valida essa conta. |
| `AWS_REGION` | Regiao do ECR e do ECS. |
| `AWS_ROLE_ARN` | ARN da role GitHub do ambiente com o trust OIDC configurado. |
| `ECR_REPOSITORY` | Nome do repositorio, sem registry ou tag. |
| `ECS_CLUSTER` | Nome ou ARN do cluster. |
| `ECS_SERVICE` | Nome do servico existente. |
| `ECS_CONTAINER_NAME` | Nome exato do container API na task definition. |
| `APP_BASE_URL` | URL HTTPS do ambiente, sem credenciais, query string ou fragmento. |

Nao crie secrets `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`. A autenticacao usa OIDC
com sessao temporaria. A configuracao assume ECR/ECS na mesma conta e regiao por ambiente.

Se o gateway exigir Bearer token, crie o **secret do Environment** `TRIAGE_API_TOKEN`.
Ele e passado somente ao passo de deploy/verificacao remota, incluindo a recuperacao por
rollback; nao e usado no smoke local, no build ou no scan. Permita que a identidade de
verificacao acesse `/health/ready`, `/health/live` e `/v1/triage`. Os verificadores nao seguem
redirects nem registram o token ou o corpo das respostas.

Em `staging` e `production`, configure **required reviewers**, impeça autoaprovacao e
desabilite bypass administrativo conforme as politicas da equipe. Em **todos os tres
environments, inclusive `dev`**, restrinja as deployment branches exclusivamente a `main`.
O dispatch confiavel roda na `main`; a tag de release e verificada como input e nao precisa
ser liberada como ref de execucao do environment. Essa restricao complementa o trust OIDC
por environment e impede que um workflow de outra branch assuma a role de deploy.
A disponibilidade de protecoes depende do plano e da visibilidade do repositorio;
a mera existencia do YAML nao configura aprovadores ou regras de branches.

Somente depois de concluir AWS, IAM e environments, defina `CD_ENABLED=true` nas variables
**do repositorio** para habilitar o automatico em `dev`. Desativar essa variavel impede novos
deploys automaticos e nao reverte uma versao que ja esta implantada.

## Health, contrato e rollback

Antes de publicar, o workflow sobe a imagem localmente e usa somente texto sintetico no
smoke test. Apos atualizar o ECS, aguarda estabilizacao, confirma que o servico concluiu a
nova revisao da task definition, cuja imagem esta fixada por digest, e repete readiness e
contrato na URL do ambiente. O contrato inclui inferencia valida e rejeicao de entrada
invalida. O checker pode ser executado isoladamente:

```powershell
uv run --locked --extra dev python -m scripts.check_api --base-url https://triage-dev.example.invalid --timeout 120 --output reports/cd/post-deploy.json
```

O dominio `example.invalid` e ilustrativo; use o endpoint real acessivel pelo runner.
O checker nao imprime corpo HTTP, texto enviado ou headers de autenticacao.

Antes da alteracao, o deploy captura a task definition e o digest anterior. Se o deploy,
readiness ou contrato falhar, a rotina restaura o digest anterior e aguarda a estabilizacao
e readiness da revisao de recuperacao. O teste completo de contrato e executado na revisao
candidata; a recuperacao automatica verifica estabilidade e readiness. O workflow continua
com falha mesmo quando o rollback funciona, para preservar a sinalizacao da tentativa
malsucedida. O [circuit breaker do
ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html)
e uma protecao adicional e deve estar habilitado no servico; ele nao substitui o teste
de contrato HTTP.

Antes do primeiro CD automatizado, mantenha uma revisao anterior ativa, por digest e
conhecida como saudavel. Sem essa base nao ha rollback confiavel. Nao exclua do ECR as
imagens apontadas por revisoes ativas ou candidatas a retorno. A imutabilidade configurada
no ECR impede sobrescrita da tag de commit; veja a [documentacao de
imutabilidade](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-tag-mutability.html).

O workflow chama `scripts/deploy_ecs.py` com `--timeout-seconds 300`, `--poll-seconds 5`
e `--report reports/cd/deployment.json`. Os 300 segundos sao o orcamento global da tentativa
de deploy, compartilhado entre chamadas AWS, estabilizacao, readiness e contrato. Em caso
de falha, o rollback recebe um novo orcamento global independente de 300 segundos.

O job possui limite total de 60 minutos. Antes de tocar no ECS, um checkpoint aborta a
entrega se a preparacao ja ultrapassou 25 minutos; uma imagem pode ter sido publicada no
ECR, mas o servico permanece intacto. A sessao OIDC e renovada imediatamente antes do
deploy. O passo que executa deploy e eventual rollback possui limite de 15 minutos para
acomodar os dois orcamentos e a geracao dos relatorios.
Se o rollback tambem falhar, o relatorio registra a falha e o operador deve restaurar a
revisao conhecida pelo procedimento abaixo.

Para um retorno operacional posterior a um deploy bem-sucedido, use o digest anterior
registrado nas evidencias para criar uma nova revisao da familia, restaurando essa imagem,
e atualize o mesmo servico com a role autorizada. Aguarde estabilizacao e execute novamente
o checker; registre quem realizou a operacao e o resultado, sem incluir payloads clinicos.
Esse procedimento usa o digest preservado, nao uma tag `latest` ou um rebuild do commit.

## Evidencias e validacao

Os uploads do workflow listam somente os caminhos explicitamente produzidos pelo CD:
`reports/cd/smoke.json`, `reports/cd/vulnerabilities.json` e `reports/cd/deployment.json`.
Os artefatos sao retidos por 14 dias; relatorios de etapas ainda nao executadas podem nao
existir em uma tentativa interrompida. Nao publique o workspace inteiro, arquivos de credenciais,
task definitions com secrets, logs de containers, modelos ou datasets. Os relatorios
registram status tecnicos, SHA/digest e metadados operacionais sem texto de laudos.

As verificacoes locais incluem testes de contrato com servidor HTTP e simulacoes das
operacoes AWS, sem credenciais. Em **22/10/2026**, a suite teve **160 testes aprovados**,
sem skips, com **81% de cobertura da aplicacao e scripts**. Ruff e actionlint tambem passaram.
O checker foi validado contra a API FastAPI real iniciada localmente com modelo sintetico:
readiness, liveness, inferencia e contrato aprovados, sem usar Docker.
Os sete templates JSON AWS foram validados offline
contra os schemas de entrada do SDK, e os links locais desta documentacao foram verificados.

Nesta entrega nao foram executados Docker/build/scan nem deploy real, pois Docker e AWS
nao estavam disponiveis localmente. Evidencia de deploy real exige uma execucao no GitHub
Actions com os recursos do ambiente configurados, a role assumida, a revisao ECS executada
e os checks aprovados.
Nao confunda templates versionados com recursos efetivamente criados ou uma execucao local
de smoke com uma implantacao concluida na AWS.
