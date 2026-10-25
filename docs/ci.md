# Integracao continua

O workflow [ci.yml](../.github/workflows/ci.yml) esta configurado para todo `push` e
`pull_request`, sem filtro de caminhos. Os jobs usam Ubuntu 24.04; as verificacoes Python usam
Python 3.12 e `uv` 0.13.4. As dependencias sao resolvidas pelo [uv.lock](../uv.lock) com
`uv sync --locked`; as actions
utilizadas estao fixadas por SHA de commit. O token do workflow tem somente leitura do conteudo
do repositorio.

## Verificacoes

| Job | Verificacao |
| --- | --- |
| `lint` | Ruff sobre `src`, `tests`, `scripts` e `dags`. |
| `tests` | Geracao do modelo sintetico, benchmark sklearn/ONNX, Pytest com cobertura e relatorio JUnit; inclui os testes de Airflow com as dependencias reais instaladas. |
| `dag` | Compilacao dos arquivos Python, importacao pelo `DagBag` real do Airflow e verificacao da estrutura de `triage_retrain`, sem executar tasks. |
| `openapi` | Validacao formal da especificacao e comparacao dos contratos existentes com o baseline [openapi-contract.json](openapi-contract.json). |
| `monitoring` | Validacao do Compose completo e regras com `promtool` 3.13.3, build da imagem opcional Airflow com lockfile e `check_compose.py` para API/Prometheus/Grafana 13.2.4, incluindo scrape, dashboard, consultas e recriacao do Grafana com persistencia. |
| `CI required` | Gate final: exige sucesso de `lint`, `tests`, `dag`, `openapi` e `monitoring`; falha se algum desses jobs falhar, for cancelado ou pulado. |

O nome literal do status obrigatorio e **`CI required`**. Mantenha esse nome estavel ao editar
o workflow, pois ele deve ser referenciado na protecao da branch. O gate usa os resultados dos
jobs; gerar um relatorio ou concluir um upload nao substitui as verificacoes.

## Reproduzir localmente

Para o conjunto completo, use Linux, WSL2 ou um container Linux com Python 3.12. O Airflow
nao deve ser instalado como parte destas instrucoes em Windows nativo.

```bash
uv sync --locked --extra dev --extra airflow
uv run --no-sync ruff check src tests scripts dags
uv run --no-sync python scripts/create_demo_model.py
uv run --no-sync pytest -q tests --cov=triage --cov=scripts --cov-report=term-missing --cov-report=xml:reports/ci/coverage.xml --cov-report=html:reports/ci/coverage-html --junitxml=reports/ci/junit.xml
uv run --no-sync python scripts/validate_dag.py --output reports/ci/dag-validation.json
uv run --no-sync python scripts/validate_openapi.py --output reports/ci/openapi.json
```

O extra `dev` inclui Ruff, Pytest, `pytest-cov` e `openapi-spec-validator`; o extra `airflow`
inclui as dependencias reais da DAG e da exportacao ONNX. `--no-sync` preserva o ambiente
completo preparado pelo primeiro comando.

Os jobs `lint` e `openapi` precisam somente de `uv sync --locked --extra dev`; podem ser
reproduzidos em Windows com Python suportado. Os jobs `tests` e `dag` usam ambos os extras
no runner Linux. O modelo sintetico precisa ser gerado antes de executar a suite de testes.

O job `monitoring` usa Docker/Compose e o `promtool` da imagem fixada para validar configuracao
de todos os perfis e cenarios de alerta. Constroi a imagem opcional Airflow com o lockfile,
sem iniciar seus servicos. Depois executa [scripts/check_compose.py](../scripts/check_compose.py),
que constroi a API com modelo sintetico, sobe API/Prometheus/Grafana em projeto proprio,
aguarda os healthchecks e verifica contrato HTTP, scrape, provisionamento e consultas. O script
recria o Grafana e repete o check para confirmar o estado persistido. Nao sobe Airflow nem acessa datasets.
Reproduza a partir da raiz do repositorio:

```powershell
docker compose --profile airflow config --quiet
docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus check config /etc/prometheus/prometheus.yml
docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus test rules /etc/prometheus/alerts.test.yml
uv sync --locked --extra dev
docker compose --profile airflow build airflow-init
uv run --no-sync python scripts/check_compose.py --timeout 900 --output reports/ci/compose-smoke.json
```

Os cenarios verificam disparo e recuperacao, limites, periodos `for`, falta de trafego,
readiness e isolamento por alvo/versao de modelo. A suite Pytest inclui testes dos checkers
Grafana e Compose com respostas simuladas. O smoke Compose usa projeto com nome aleatorio,
portas dinamicas em loopback e suas proprias credenciais de demonstracao, sem carregar o `.env`
do usuario. Seu check Grafana distingue a URL local do servidor da URL interna
`http://prometheus:9090` do datasource.
Operacao e verificacao da stack estao em [prometheus-local.md](prometheus-local.md) e
[grafana-local.md](grafana-local.md).

O script encerra a stack temporaria e remove somente seus volumes ao final, inclusive em falha. Na
maquina de desenvolvimento, prefira `docker compose stop api prometheus grafana` para preservar
o historico. A CI do commit publicado `6f562a6` passou a criacao da stack e o check Grafana na
[execucao 39865208559](https://github.com/ebianchini/tech-challenge-fase-3/actions/runs/39865208559).
O [registro versionado](evidence/grafana-compose-ci.json) identifica o commit, job e artefato
desse baseline, junto das limitacoes do checker original.
O novo fluxo `check_compose.py` precisa de uma execucao do commit atualizado para comprovar as
alteracoes de build, perfis, redes e persistencia descritas em [compose-local.md](compose-local.md).
O job concede 900 segundos ao smoke; o build e a inicializacao real do perfil Airflow sao
verificacoes distintas, e seus servicos nao sao iniciados por esse smoke.

## Validacao local inicial da CI

Em 22/10/2026, com Python 3.12.15 e as dependencias do lockfile, foram verificados:

- Ruff: aprovado em `src`, `tests`, `scripts` e `dags`.
- Pytest: 42 testes aprovados, sem skips, com 85% de cobertura de `triage`.
- DAG: compilacao e carregamento pelo DagBag real aprovados, com oito tarefas.
- OpenAPI: especificacao formalmente valida e correspondente ao baseline versionado.
- Workflow: sintaxe e expressoes aprovadas pelo actionlint 1.7.12.
- Lockfile: `uv lock --check` aprovado.

Essas verificacoes da entrega inicial foram locais em Windows. A entrega posterior de CD
amplia a cobertura para os scripts; veja os resultados atuais em [cd.md](cd.md).
O baseline publicado possui execucao verde no runner Linux; as alteracoes posteriores devem ser
confirmadas em uma execucao do respectivo commit.

## Artefatos e privacidade

Os uploads usam somente os caminhos abaixo, com retencao de 14 dias:

| Job | Caminhos permitidos | Conteudo |
| --- | --- | --- |
| `tests` | `reports/ci/coverage.xml`, `reports/ci/coverage-html/`, `reports/ci/junit.xml`, `reports/latency_comparison.csv`, `reports/latency_comparison.json` | Cobertura, resultados dos testes e benchmark com fixtures sinteticas. |
| `dag` | `reports/ci/dag-validation.json` | Identificador da DAG, estrutura das tasks e resultado das validacoes. |
| `openapi` | `reports/ci/openapi.json` | Especificacao HTTP gerada, sem exemplos de dados reais. |
| `monitoring` | `reports/ci/compose-smoke.json` | Resultado tecnico dos servicos, scrape, provisionamento, consultas e recriacao, sem credenciais ou textos de entrada. |

A CI nao executa `dvc pull`, nao acessa corpus hospitalar e nao precisa de credenciais AWS
ou acesso a servicos externos de dados. Modelos, datasets, logs Airflow e o diretorio inteiro
`reports/` nao sao publicados. Os testes usam textos artificiais; fixtures, exemplos OpenAPI
e mensagens de erro devem continuar sem laudos reais.

## Alterar o contrato OpenAPI

[scripts/validate_openapi.py](../scripts/validate_openapi.py) chama `create_app().openapi()`
sem executar o lifespan da API. Nao precisa carregar modelos, iniciar servidor ou acessar dados.
O script valida formalmente tanto a especificacao gerada quanto o baseline versionado.

A comparacao protege as operacoes existentes: alteracoes de parametros, request, response,
restricoes dos schemas ou seguranca precisam de revisao do baseline. A verificacao e
conservadora, portanto tambem pode sinalizar mudancas compativeis. Anotacoes documentais e
novas operacoes sao permitidas se a especificacao continuar valida.

Para uma alteracao deliberada, revise o contrato e sua versao em
[inference-contract.md](inference-contract.md), ajuste a API e os testes e gere o novo baseline:

```bash
uv run --no-sync python -c 'import json; from pathlib import Path; from triage.api.app import create_app; Path("docs/openapi-contract.json").write_text(json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")'
uv run --no-sync python scripts/validate_openapi.py
```

Revise o diff do JSON junto com as mudancas de codigo. A CI compara com o baseline existente;
ela nao o regenera automaticamente.

## Exigir CI verde na branch principal

O workflow possui uma execucao verde no baseline publicado; novas alteracoes precisam de uma
execucao no commit correspondente. A protecao de `main` permanece pendente de verificacao remota.
Versionar o YAML nao configura
essa protecao. A conta conectada nesta entrega possui permissao de escrita, mas nao de
administracao/manutencao do repositorio; um administrador precisa aplicar a regra.

Depois de publicar a alteracao no GitHub:

1. Em **Actions > CI**, abra a execucao do commit publicado. Confira sucesso dos cinco jobs
   e de **CI required**; registre o link dessa execucao como evidencia.
2. Com permissao de administrar regras, abra **Settings > Branches > Add classic branch
   protection rule** (ou edite a regra existente de `main`).
3. Use `main` em **Branch name pattern**, marque **Require status checks to pass before
   merging** e adicione o status **CI required** emitido por GitHub Actions.
4. Salve a regra e confira em um pull request direcionado a `main` que o status e obrigatorio
   e que a mesclagem fica bloqueada quando ele falha.
5. So entao marque o item de branch protection no `TODO.md` de apoio. A evidencia de CI/CD
   e o Definition of Done continuam pendentes ate as verificacoes remotas correspondentes.

Esses passos seguem a [documentacao de protecao de branches do GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).
O workflow separado de CD, build/publicacao de imagem e deploy esta descrito em [cd.md](cd.md).
