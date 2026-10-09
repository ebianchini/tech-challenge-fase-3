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

- [ ] Payload bruto nao aparece em logs, traces, metricas, XCom ou mensagens de erro.
- [ ] Acesso por identidade de servico e menor privilegio.
- [ ] TLS em transito e criptografia em repouso configurados no ambiente alvo.
- [ ] Limites de payload, timeout, rate limiting e retencao configurados.
- [ ] Artefatos e backups testados para nao conter texto bruto.

## Antes da promocao

- [ ] Amostra de logs revisada por privacidade.
- [ ] Testes de vazamento executados em CI.
- [ ] Retencao e descarte aprovados.
- [ ] Plano de incidente e rollback definido.
- [ ] Aprovacao institucional registrada para o conjunto e o modelo.

Estado atual: prova tecnica com dados sinteticos; nenhum dado hospitalar esta autorizado.
