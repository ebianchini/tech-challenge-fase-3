# Contrato de Inferencia

## Escopo

Contrato HTTP `1.0` para uma demonstracao tecnica de classificacao de textos. O Medical Abstracts
TC Corpus nao possui, por si so, labels de urgencia clinica; portanto o modelo incluido e apenas
um artefato sintetico para validar a cadeia de software. Este servico nao substitui avaliacao medica.

## Request

`POST /v1/triage`

```json
{
  "contract_version": "1.0",
  "request_id": "optional-client-id",
  "instances": [{"report_text": "synthetic signal alpha"}]
}
```

`report_text` e obrigatorio, nao pode ser vazio apos trim e tem limite de 10.000 caracteres. O lote
aceita de 1 a 32 instancias. Campos extras sao rejeitados.

## Response

```json
{
  "contract_version": "1.0",
  "request_id": "generated-or-client-id",
  "model_version": "model",
  "results": [{
    "prediction_id": "unique-id",
    "triage_class": "atencao",
    "probabilities": {"normal": 0.12, "atencao": 0.77, "urgente": 0.11},
    "abstained": false
  }]
}
```

As classes sao `normal`, `atencao` e `urgente`. `abstained=true` indica confianca abaixo do limiar
tecnico e deve encaminhar o caso para revisao humana.

## Health checks e erros

- `GET /health/live` verifica se o processo responde.
- `GET /health/ready` retorna `ok` somente quando modelo e preprocessador foram carregados no startup.
- Erros de schema usam `INVALID_INPUT_SCHEMA`; indisponibilidade de artefatos usa
  `MODEL_UNAVAILABLE`; falha de inferencia usa `INFERENCE_FAILED`.
- Respostas de erro nao incluem payload, texto, stack trace ou caminho local.