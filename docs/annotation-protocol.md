# Protocolo de Anotacao Clinica

## Escopo

Protocolo proposto para um futuro dataset institucional de `normal`, `atencao` e `urgente`. O
Medical Abstracts TC Corpus nao possui esses labels e nao deve ser anotado retroativamente como
urgencia sem governanca aprovada.

## Unidade e classes

- Uma unidade e um texto desidentificado completo recebido para triagem.
- `normal`: sem indicio de prioridade operacional imediata segundo o protocolo aprovado.
- `atencao`: requer avaliacao priorizada, mas sem criterio de urgencia imediata.
- `urgente`: atende a pelo menos um criterio de prioridade imediata.
- `indeterminado`: registro auxiliar para casos insuficientes ou fora do escopo; nao e classe de
  produto e deve ser revisado antes do fechamento do dataset.

Os criterios clinicos concretos devem ser preenchidos e aprovados pelo especialista clinico antes
 da coleta. Este repositorio nao inventa criterios medicos.

## Processo

1. Dois anotadores clinicos independentes avaliam cada unidade sem ver a anotacao do outro.
2. Cada anotacao registra classe, justificativa codificada, confianca e motivo de `indeterminado`.
3. Discordancias seguem para um terceiro avaliador ou reuniao de desempate documentada.
4. O gold label e a decisao do desempate, com trilha de auditoria e versao do protocolo.
5. O dataset de teste e congelado antes da avaliacao do modelo.

## Concordancia e qualidade

Reportar Cohen kappa para dois anotadores e Fleiss kappa quando houver mais de dois avaliadores,
alem de concordancia por classe e matriz de discordancias. O relatorio deve incluir cobertura,
casos indeterminados, taxa de desempate e intervalo de confianca quando aplicavel.

## Aprovacao pendente

O protocolo somente pode ser usado depois de aprovado pelo especialista clinico e pelo responsavel
por privacidade, com evidencias registradas em [governance.md](governance.md).
