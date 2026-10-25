# Matriz de riscos e rollback

Esta matriz cobre a entrega tecnica atual. O corpus Medical Abstracts TC continua sendo
somente uma prova tecnica; nenhum risco abaixo autoriza uso clinico.

| Risco | Impacto | Probabilidade | Controle preventivo | Gatilho | Resposta |
|---|---|---:|---|---|---|
| Artefato sem equivalencia sklearn/ONNX | Alto | Baixa | Gate de classes e tolerancia de probabilidade `1e-5` no benchmark | Delta acima de `1e-5` ou classe divergente | Bloquear registro e servir a versao anterior |
| Degradacao de latencia | Medio | Media | Benchmark single/batch com p50/p95/p99 | ONNX sem melhoria de p95 | Bloquear promocao otimizada e investigar |
| Quality gate reprovado | Alto | Alta no corpus tecnico | Gate de macro F1 e recall de urgente | `quality_gate.passed=false` | Nao registrar artefato |
| Modelo indisponivel no startup | Alto | Baixa | Readiness, healthcheck e rollback por digest | `/health/ready` diferente de `status=ok` | Remover instancia do trafego e restaurar digest |
| Vazamento de texto em logs/metricas | Critico | Baixa | Redaction, labels limitados e testes de privacidade | Texto detectado em saída operacional | Interromper promoção, preservar evidência e revisar logs |
| Falha de deploy | Alto | Media | Smoke test, healthcheck ECS e circuit breaker | Deploy sem readiness ou contrato | Rollback para digest anterior |
| Drift ou viés clínico | Critico | Desconhecida | Dataset institucional, avaliação por subgrupo e aprovação humana | Mudança de distribuição ou métricas clínicas abaixo do limite | Suspender uso e iniciar revisão clínica |

## Procedimento de rollback

1. Identificar o digest anterior saudável no serviço ECS e nos registros de deploy.
2. Atualizar a task definition para o digest anterior, nunca para `latest`.
3. Aguardar todas as réplicas passarem o healthcheck de `/health/ready`.
4. Executar o teste de contrato pós-deploy.
5. Registrar motivo, digest removido, digest restaurado e evidências.
6. Manter o artefato rejeitado imutável para investigação; não sobrescrever registros.

O workflow [cd.yml](../.github/workflows/cd.yml) automatiza essa sequência para falhas durante a
implantação. A decisão de suspensão por risco clínico permanece humana e institucional.
