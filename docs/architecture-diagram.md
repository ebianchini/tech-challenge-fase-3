# Diagrama da arquitetura

```mermaid
flowchart LR
    Client[HIS/EMR ou cliente autorizado] --> Gateway[ALB/API Gateway<br/>TLS, identidade e rate limit]
    Gateway --> API[FastAPI stateless<br/>POST /v1/triage]
    API --> Model[ONNX Runtime<br/>warm-up no startup]
    API --> Metrics[Prometheus metrics]
    Metrics --> Grafana[Grafana provisionado]
    API --> Audit[Logs JSON minimizados<br/>sem texto bruto]

    DVC[DVC/S3 remote] --> Airflow[Airflow DAG]
    Airflow --> Normalize[Normalização e validação]
    Normalize --> Train[TF-IDF + Logistic Regression]
    Train --> Evaluate[Quality gate]
    Evaluate --> Export[Exportação ONNX + benchmark]
    Export --> Registry[Registry imutável por fingerprint]
    Registry --> Image[Imagem Docker por digest]
    Image --> API

    CI[GitHub Actions CI/CD] --> Image
    CI --> Scan[Scan de vulnerabilidades]
    Scan --> Image
```

## Limites da entrega

- O Compose local substitui ALB, AWS, S3 e observabilidade gerenciada somente para
  desenvolvimento.
- O corpus Medical Abstracts TC é técnico e temático; não representa `normal`, `atencao` ou
  `urgente`.
- Promoção clínica exige `urgency_label`, quality gate aprovado e aprovações institucionais.
- Rollback aponta para digest anterior; nenhum fluxo usa `latest`.
