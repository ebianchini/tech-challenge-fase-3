# TODO - Sistema de Triagem Automatica de Laudos

## Como usar este TODO

- [ ] Itens marcados como P0 bloqueiam demonstracao de producao.
- [ ] Cada item deve gerar codigo, teste ou documentacao verificavel.
- [ ] Toda entrega deve atualizar o README, o SDD e os artefatos versionados quando houver mudanca de contrato.
- [ ] Nao usar texto clinico real em exemplos, fixtures, logs ou CI.

## Etapa 0 - Fundacao e governanca

- [ ] Confirmar patrocinador tecnico, especialista clinico e responsavel por privacidade.
- [x] Registrar no README que o Medical Abstracts TC Corpus nao possui, por si so, labels de urgencia clinica.
- [x] Definir o corpus atual como proxy tecnico; dataset rotulado para `normal`, `atencao` e `urgente` continua pendente.
- [x] Definir protocolo de anotacao clinica, dupla anotacao, desempate e medida de concordancia.
- [x] Definir processo de desidentificacao e checklist de compliance antes de qualquer dado hospitalar.
- [x] Fixar Python, dependencias e ferramenta de ambiente em `pyproject.toml`/lockfile.
- [x] Criar `.env.example` sem segredos e documentar configuracoes locais.
- [x] Criar estrutura `src/triage`, `tests`, `dags`, `monitoring`, `grafana`, `data` e `models`.
- [x] Definir versao inicial do contrato HTTP: `1.0`.
- [x] Adicionar politica de nao armazenamento de texto bruto em logs, metricas e traces.

## Etapa 1 - Decisao arquitetural e API inicial

### Arquitetura

- [x] Documentar no README a decisao por inferencia real-time e treino batch.
- [x] Documentar AWS como alvo inicial: ECR, ECS/Fargate, ALB, S3 e observabilidade gerenciada quando aplicavel.
- [x] Documentar diferencas entre Compose local e ambiente AWS.
- [x] Definir modelo de identidade de servico, TLS, rate limiting e limites de payload.
- [x] Definir politica de health check, startup, readiness e rollback.

### Dominio e contrato

- [x] Criar enum `TriageClass` com `normal`, `atencao` e `urgente`.
- [x] Criar caso de uso independente de FastAPI para classificar um ou varios textos.
- [x] Criar modelos Pydantic para request/response com `extra="forbid"`.
- [x] Validar texto nao vazio, tamanho maximo de 10.000 caracteres e lote maximo de 32.
- [x] Gerar `request_id` e `prediction_id` unicos sem registrar o texto.
- [x] Retornar `model_version`, probabilidades, classe e `abstained`.
- [x] Definir excecoes de dominio para entrada invalida, modelo indisponivel e falha de inferencia.
- [x] Mapear excecoes para respostas estruturadas sem caminhos locais ou stack trace.
- [x] Implementar `GET /health/live`.
- [x] Implementar `GET /health/ready` validando modelo e preprocessador carregados.
- [x] Implementar `POST /v1/triage`.
- [x] Criar `docs/inference-contract.md` com exemplos sanitizados.

### Container e baseline

- [x] Criar Dockerfile multi-stage com usuario nao-root.
- [x] Fixar imagem base por versao/digest quando possivel.
- [x] Criar `.dockerignore` para nao copiar dados sensiveis, caches ou segredos.
- [x] Garantir que o modelo seja carregado uma vez no startup.
- [x] Criar smoke test Docker para `/health/ready` e `/v1/triage`.
- [x] Criar benchmark baseline com warm-up, p50, p95, p99 e memoria.
- [x] Salvar baseline em `reports/latency_baseline.csv`.
- [x] Atualizar README com comando de execucao e resultado baseline.

### Testes Etapa 1

- [x] Testar payload valido.
- [x] Testar campo extra, texto vazio, texto acima do limite e lote acima do limite.
- [x] Testar ordem das predicoes e ids unicos entre requisicoes.
- [x] Testar indisponibilidade do modelo e retorno de readiness degradado.
- [x] Testar que logs nao contem o texto do laudo.

## Etapa 2 - CI/CD e pipeline automatizado

### Pipeline de dados e modelo

- [x] Criar adaptador para ler o CSV do Medical Abstracts TC Corpus.
- [x] Normalizar colunas para `text` e `label_original` sem assumir labels de urgencia inexistentes.
- [x] Criar validacao de schema, nulos, classes e tamanho minimo por classe.
- [x] Criar estrategia explicita de mapeamento/rotulagem para `urgency_label`.
- [x] Bloquear treino clinico quando `urgency_label` nao estiver presente e aprovado.
- [x] Implementar split estratificado e split temporal quando houver timestamp.
- [x] Ajustar TF-IDF somente no treino.
- [x] Criar pipeline `TfidfVectorizer -> LogisticRegression`.
- [x] Persistir pipeline original, labels, parametros e fingerprint.
- [x] Implementar metricas por classe, macro F1, recall de urgente, matriz de confusao e calibracao.
- [x] Definir gates de qualidade para promocao.
- [x] Criar comando `train` reproduzivel e idempotente.

### DVC

- [x] Configurar remote DVC seguro para dataset e artefatos.
- [x] Declarar `params.yaml` no `dvc.yaml` usando `params:`.
- [x] Declarar `data/raw`, dados processados, modelos e relatorios como deps/outs apropriados.
- [x] Versionar o split e o fingerprint do dataset.
- [ ] Validar `dvc repro` em ambiente limpo com Git.
- [x] Documentar `dvc pull` e `dvc push` sem expor credenciais.

### Airflow

- [x] Criar `dags/triage_retrain.py`.
- [x] Implementar task `load_data`.
- [x] Implementar task `validate_data`.
- [x] Implementar task `prepare_splits`.
- [x] Implementar task `train_model`.
- [x] Implementar task `evaluate_model`.
- [x] Implementar task `export_onnx`.
- [x] Implementar task `benchmark_latency`.
- [x] Implementar task `register_if_approved`.
- [x] Configurar retries, timeout, `catchup=False` e schedule parametrizavel.
- [x] Evitar transportar datasets/modelos via XCom.
- [x] Garantir que tasks possam ser repetidas sem corromper artefatos.
- [x] Adicionar teste de import e teste de carregamento da DAG.
- [x] Subir Airflow localmente para validar scheduler e execucao manual.

### CI

- [x] Criar `.github/workflows/ci.yml` acionado por push e pull request.
- [x] Adicionar job de lint com Ruff.
- [x] Adicionar job de testes com Pytest e cobertura.
- [x] Adicionar validacao de sintaxe/import da DAG.
- [x] Adicionar validacao de contrato OpenAPI.
- [x] Publicar relatorios como artefatos sem dados clinicos.
- [ ] Configurar branch protection exigindo CI verde.

A implementacao da CI e os comandos locais estao em `docs/ci.md`. Os itens marcados acima
indicam configuracao versionada; a primeira execucao no GitHub Actions e a exigencia do gate
na branch ainda dependem de verificacao remota. O item de protecao de branch permanece aberto
ate essa configuracao ser confirmada.

### CD

- [ ] Criar `.github/workflows/cd.yml` dependente do CI.
- [ ] Construir imagem Docker em tag imutavel de commit.
- [ ] Executar smoke test da imagem.
- [ ] Executar scan de vulnerabilidades.
- [ ] Publicar no ECR via OIDC, sem chave AWS fixa.
- [ ] Implantar em ECS/Fargate ou ambiente equivalente.
- [ ] Aguardar `/health/ready` apos deploy.
- [ ] Executar teste de contrato pos-deploy.
- [ ] Implementar rollback por digest anterior.
- [ ] Separar ambientes dev, staging e production.

## Etapa 3 - Monitoramento e observabilidade

### Prometheus

- [ ] Adicionar dependencia `prometheus-client`.
- [ ] Criar contador de requisicoes por rota, metodo e status.
- [ ] Criar histograma de latencia HTTP.
- [ ] Criar histograma de latencia de inferencia.
- [ ] Criar contador de predicoes por classe e versao do modelo.
- [ ] Criar contador de erros por tipo seguro.
- [ ] Criar contador de abstencoes.
- [ ] Criar gauge de readiness/modelo carregado.
- [ ] Criar histograma de tamanho do texto sem o conteudo.
- [ ] Expor `GET /metrics`.
- [ ] Revisar labels para impedir alta cardinalidade e vazamento de PHI.

### Prometheus local

- [ ] Criar `monitoring/prometheus/prometheus.yml`.
- [ ] Configurar scrape da API.
- [ ] Criar regras de alerta para erro, latencia, indisponibilidade e taxa de abstencao.
- [ ] Definir retencao e limites de armazenamento local.

### Grafana

- [ ] Criar datasource Prometheus provisionado por arquivo.
- [ ] Criar dashboard versionado `grafana/dashboards/triage-overview.json`.
- [ ] Criar painel de total de requisicoes.
- [ ] Criar painel de latencia p50/p95/p99.
- [ ] Criar painel de taxa de erro.
- [ ] Criar painel opcional de predicoes por classe.
- [ ] Criar painel opcional de abstencao e modelo ativo.
- [ ] Validar que o dashboard sobe automaticamente no Compose.
- [ ] Gerar screenshot do dashboard para o entregavel.

### Docker Compose

- [ ] Subir API, Prometheus e Grafana juntos.
- [ ] Adicionar healthchecks e dependencias de startup.
- [ ] Persistir somente dados de observabilidade necessarios.
- [ ] Separar rede interna da API e portas de acesso local.
- [ ] Documentar credenciais locais de demonstracao sem reutiliza-las em producao.
- [ ] Se Airflow fizer parte do Compose, separar seus volumes e metadata database.

### Logs e privacidade

- [ ] Usar logs estruturados com `request_id`, rota, status, latencia e versao.
- [ ] Redigir texto, headers sensiveis e tokens.
- [ ] Aplicar retencao e rotacao.
- [ ] Testar que excecoes nao retornam caminhos, payload ou stack trace.

## Etapa 4 - Otimizacao de latencia e entrega

### Exportacao ONNX

- [ ] Adicionar `skl2onnx` e `onnxruntime` com versoes fixadas.
- [ ] Exportar o pipeline TF-IDF + Logistic Regression.
- [ ] Salvar `models/triage.onnx` e metadata de entrada/saida.
- [ ] Criar loader ONNX com `InferenceSession` singleton por processo.
- [ ] Executar warm-up no startup antes do readiness.
- [ ] Validar equivalencia de classe entre sklearn e ONNX.
- [ ] Definir tolerancia numerica para probabilidades.
- [ ] Manter fallback controlado para o modelo original apenas em ambiente de desenvolvimento.

### Benchmark comparativo

- [ ] Criar conjunto fixo de textos sanitizados para benchmark.
- [ ] Medir cold start do processo.
- [ ] Medir warm-up.
- [ ] Medir p50, p95 e p99 para uma instancia.
- [ ] Medir p50, p95 e p99 para lotes.
- [ ] Medir memoria residente.
- [ ] Medir tamanho dos artefatos.
- [ ] Calcular speedup e variacao percentual.
- [ ] Salvar `reports/latency_comparison.csv`.
- [ ] Publicar comparativo no README e como artefato CI.
- [ ] Definir gate: nenhuma degradacao clinica acima do limite e melhoria de latencia/memoria comprovada.

## Integracao e entrega final

- [ ] Atualizar README com arquitetura, setup, comandos e decisao AWS.
- [ ] Atualizar SDD com decisoes implementadas e desvios.
- [ ] Adicionar contrato de inferencia e exemplos sanitizados.
- [ ] Adicionar diagrama da arquitetura.
- [ ] Adicionar JSON do dashboard Grafana.
- [ ] Adicionar print do dashboard ao entregavel.
- [ ] Adicionar evidencia do DAG executado com sucesso.
- [ ] Adicionar evidencia dos dois workflows CI/CD.
- [ ] Adicionar tabela de benchmark original versus ONNX.
- [ ] Adicionar matriz de riscos e plano de rollback.
- [ ] Executar checklist de seguranca e privacidade.
- [ ] Executar suite completa de testes.
- [ ] Executar build limpo do Docker Compose.
- [ ] Confirmar que nenhum artefato final contem texto clinico bruto.

## Definition of Done

- [ ] API FastAPI funciona em Docker com contrato versionado.
- [ ] Lint e testes rodam automaticamente no GitHub Actions.
- [ ] DAG Airflow e carregada e executa o fluxo de treinamento.
- [ ] DVC reproduz o pipeline com dataset/artefatos versionados.
- [ ] Prometheus coleta requisicoes, latencia e erros.
- [ ] Grafana inicia com dashboard de pelo menos tres paineis.
- [ ] Modelo ONNX e carregado e comparado ao sklearn.
- [ ] Relatorio mostra latencias original e otimizada.
- [ ] Deploy usa imagem imutavel, health check e rollback.
- [ ] Limitacoes do dataset e ausencia de validacao clinica estao explicitas.
- [ ] Privacidade, abstencao e revisao humana estao implementadas ou formalmente bloqueando a promocao.
