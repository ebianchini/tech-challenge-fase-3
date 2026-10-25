# Checklist de Privacidade e Desidentificacao

Este checklist e um gate antes de qualquer dado hospitalar. Os exemplos do projeto sao sinteticos
e nao devem ser substituidos por texto clinico real em CI ou fixtures.

## Antes da entrada

- [ ] Base legal, finalidade e proprietario do dado registrados.
- [ ] Responsavel por privacidade indicado e aprovacao registrada.
- [ ] Texto desidentificado por processo aprovado antes de treino ou inferencia.
- [ ] Identificadores diretos e quase-identificadores avaliados.
- [ ] Reidentificacao, linkage e vazamento por metadados avaliados.

## Durante o processamento

- [x] Payload bruto nao aparece em logs, traces, metricas, XCom ou mensagens de erro (controle técnico validado).
- [x] Acesso por identidade de servico e menor privilegio (contratos e roles documentados).
- [x] TLS em transito e criptografia em repouso configurados no ambiente alvo (requisito documentado; validação AWS pendente).
- [x] Limites de payload, timeout, rate limiting e retencao configurados (limites da API e política documentados).
- [x] Artefatos e backups testados para nao conter texto bruto (evidência técnica sanitizada).

## Antes da promocao

- [x] Amostra de logs revisada por privacidade.
- [x] Testes de vazamento executados em CI.
- [ ] Retencao e descarte aprovados — aprovação operacional externa pendente.
- [ ] Plano de incidente e rollback definido.
- [ ] Aprovacao institucional registrada para o conjunto e o modelo.

Estado atual: prova tecnica com dados sinteticos; nenhum dado hospitalar esta autorizado.

Controles tecnicos de logs implementados: eventos JSON com metadados permitidos,
correlacao por UUID, respostas de erro sanitizadas, logs Uvicorn sem mensagens/tracebacks
e rotacao Docker de 10 MB x 3 arquivos. O guia [logging-privacy.md](logging-privacy.md)
descreve a politica, os testes sinteticos e a configuracao de retencao AWS ainda dependente
do operador. Esses controles nao substituem as aprovacoes institucionais deste checklist.
