from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from triage.domain.exceptions import InferenceError
from triage.domain.models import TriageClass, TriageRequest
from triage.domain.service import TriageService
from triage.infrastructure.logging import HTTPLoggingMiddleware
from triage.infrastructure.metrics import HTTPMetricsMiddleware, TriageMetrics
from triage.infrastructure.model_loader import ModelLoadError, SklearnPredictor

CONTRACT_VERSION = "1.0"
MAX_BATCH_SIZE = 32
ROOT_DIR = Path(os.getenv("TRIAGE_ROOT_DIR", Path.cwd()))
MODEL_PATH = Path(os.getenv("TRIAGE_MODEL_PATH", ROOT_DIR / "models" / "model.joblib"))
PREPROCESSOR_PATH = Path(
    os.getenv("TRIAGE_PREPROCESSOR_PATH", ROOT_DIR / "models" / "preprocessor.joblib")
)


class TriageBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = Field(default=CONTRACT_VERSION, pattern=r"^1\.0$")
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    instances: list[TriageRequest] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prediction_id: str
    triage_class: TriageClass
    probabilities: dict[TriageClass, float]
    abstained: bool


class TriageBatchResponse(BaseModel):
    contract_version: str
    request_id: str
    model_version: str
    results: list[TriageResult]


class HealthResponse(BaseModel):
    status: str
    contract_version: str
    model_loaded: bool
    preprocessor_loaded: bool


class ValidationErrorDetail(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ErrorBody(BaseModel):
    code: Literal["INVALID_INPUT_SCHEMA", "MODEL_UNAVAILABLE", "INFERENCE_FAILED"]
    message: str
    details: list[ValidationErrorDetail]


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_response(code: str, message: str, http_status: int, details: list[object] | None = None):
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message, "details": details or []}},
    )


def validation_details(exc: RequestValidationError) -> list[dict[str, object]]:
    # Pydantic's msg/ctx may contain exception text, and loc can contain arbitrary
    # extra-field names. Only schema vocabulary and fixed messages may be returned.
    messages = {
        "missing": "Campo obrigatorio.",
        "extra_forbidden": "Campo extra nao permitido.",
        "string_type": "Esperada uma string.",
        "string_too_short": "String abaixo do tamanho minimo.",
        "string_too_long": "String acima do tamanho maximo.",
        "string_pattern_mismatch": "Formato de string invalido.",
        "list_type": "Esperada uma lista.",
        "too_short": "Quantidade de itens abaixo do minimo.",
        "too_long": "Quantidade de itens acima do maximo.",
        "model_attributes_type": "Esperado um objeto.",
        "model_type": "Esperado um objeto.",
        "json_invalid": "JSON invalido.",
        "value_error": "Valor invalido.",
    }
    schema_fields = {"body", "contract_version", "request_id", "instances", "report_text"}
    return [
        {
            "loc": [
                part if isinstance(part, int) or part in schema_fields else "extra"
                for part in error.get("loc", ("body",))
            ],
            "msg": messages.get(error["type"], "Valor invalido."),
            "type": error["type"] if error["type"] in messages else "invalid_input",
        }
        for error in exc.errors()
    ]


class _InstrumentedFastAPI(FastAPI):
    def build_middleware_stack(self):
        # Include ServerErrorMiddleware's response in the HTTP observation too.
        return HTTPLoggingMiddleware(
            HTTPMetricsMiddleware(
                super().build_middleware_stack(),
                metrics=self.state.metrics,
                route_templates={route.path for route in self.routes},
            ),
            route_templates={route.path for route in self.routes},
            model_version=lambda: self.state.log_model_version,
        )


def create_app() -> FastAPI:
    state: dict[str, object] = {"predictor": None, "load_error": None}
    metrics = TriageMetrics()
    metric_model_version = metrics.begin_model(None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal metric_model_version
        state.update(predictor=None, load_error=None)
        metric_model_version = metrics.begin_model(None)
        _app.state.log_model_version = metric_model_version
        try:
            predictor = SklearnPredictor(MODEL_PATH, PREPROCESSOR_PATH)
            state["predictor"] = predictor
        except ModelLoadError as exc:
            state["load_error"] = exc
        else:
            metric_model_version = metrics.begin_model(predictor.model_version)
            _app.state.log_model_version = metric_model_version
            metrics.set_model_ready(metric_model_version, True)
        try:
            yield
        finally:
            metrics.set_model_ready(metric_model_version, False)
            state["predictor"] = None

    app = _InstrumentedFastAPI(
        title="Triage de Laudos API",
        version="0.1.0",
        description="API tecnica de triagem, sem decisao clinica autonoma.",
        lifespan=lifespan,
    )
    app.state.metrics = metrics
    app.state.log_model_version = metric_model_version

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        request.state.error_type = "INVALID_INPUT_SCHEMA"
        metrics.observe_error("INVALID_INPUT_SCHEMA")
        return error_response(
            "INVALID_INPUT_SCHEMA", "Payload de triagem invalido.", 422, validation_details(exc)
        )

    @app.exception_handler(ModelLoadError)
    async def model_handler(request: Request, _exc: ModelLoadError):
        request.state.error_type = "MODEL_UNAVAILABLE"
        metrics.observe_error("MODEL_UNAVAILABLE")
        return error_response("MODEL_UNAVAILABLE", "Modelo de inferencia indisponivel.", 503)

    @app.exception_handler(InferenceError)
    async def inference_handler(request: Request, _exc: InferenceError):
        request.state.error_type = "INFERENCE_FAILED"
        metrics.observe_error("INFERENCE_FAILED")
        return error_response("INFERENCE_FAILED", "Falha durante a inferencia.", 500)

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, _exc: Exception):
        request.state.error_type = "INFERENCE_FAILED"
        metrics.observe_error("INFERENCE_FAILED")
        return error_response("INFERENCE_FAILED", "Falha durante a inferencia.", 500)

    @app.exception_handler(HTTPException)
    async def http_handler(_request: Request, exc: HTTPException):
        # Parser/HTTP exception details and custom headers can contain input too.
        messages = {404: "Not Found", 405: "Method Not Allowed"}
        methods = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
        allowed = ", ".join(
            sorted(
                {
                    method.strip()
                    for key, value in (exc.headers or {}).items()
                    if key.lower() == "allow"
                    for method in value.split(",")
                    if method.strip() in methods
                }
            )
        )
        headers = {"Allow": allowed} if exc.status_code == 405 and allowed else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": messages.get(exc.status_code, "Requisicao HTTP invalida.")},
            headers=headers,
        )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> HealthResponse:
        model_loaded = state["predictor"] is not None
        preprocessor_loaded = model_loaded
        return HealthResponse(
            status="ok" if model_loaded and preprocessor_loaded else "degraded",
            contract_version=CONTRACT_VERSION,
            model_loaded=model_loaded,
            preprocessor_loaded=preprocessor_loaded,
        )

    @app.get(
        "/metrics",
        response_class=Response,
        responses={
            200: {
                "description": "Metricas tecnicas no formato Prometheus, sem texto de laudos.",
                "content": {"text/plain": {"schema": {"type": "string"}}},
            }
        },
    )
    def prometheus_metrics() -> Response:
        content, content_type = metrics.render()
        return Response(content=content, headers={"Content-Type": content_type})

    @app.post(
        "/v1/triage",
        responses={
            422: {"model": ErrorResponse, "description": "Payload de triagem invalido."},
            500: {"model": ErrorResponse, "description": "Falha durante a inferencia."},
            503: {"model": ErrorResponse, "description": "Modelo de inferencia indisponivel."},
        },
    )
    def triage(payload: TriageBatchRequest, request: Request) -> TriageBatchResponse:
        predictor = state["predictor"]
        if predictor is None:
            raise ModelLoadError("Modelo de inferencia indisponivel.")
        service = TriageService(predictor)  # type: ignore[arg-type]
        request_id = payload.request_id or request.state.request_id
        texts = [item.report_text.strip() for item in payload.instances]
        metrics.observe_inputs(len(text) for text in texts)
        started = time.perf_counter()
        try:
            predictions = service.classify(texts)
        finally:
            metrics.observe_inference(metric_model_version, time.perf_counter() - started)
        response = TriageBatchResponse(
            contract_version=payload.contract_version,
            request_id=request_id,
            model_version=predictor.model_version,  # type: ignore[union-attr]
            results=[
                TriageResult(prediction_id=str(uuid4()), **prediction.model_dump())
                for prediction in predictions
            ],
        )
        metrics.observe_predictions(metric_model_version, predictions)
        return response

    return app


app = create_app()
