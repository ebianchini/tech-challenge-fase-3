from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from triage.domain.exceptions import InferenceError
from triage.domain.models import TriageClass, TriageRequest
from triage.domain.service import TriageService
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


def error_response(code: str, message: str, http_status: int, details: list[object] | None = None):
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message, "details": details or []}},
    )


def validation_details(exc: RequestValidationError) -> list[dict[str, object]]:
    return [
        {key: error[key] for key in ("loc", "msg", "type") if key in error}
        for error in exc.errors()
    ]


def create_app() -> FastAPI:
    state: dict[str, object] = {"predictor": None, "load_error": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            state["predictor"] = SklearnPredictor(MODEL_PATH, PREPROCESSOR_PATH)
        except ModelLoadError as exc:
            state["load_error"] = exc
        yield

    app = FastAPI(
        title="Triage de Laudos API",
        version="0.1.0",
        description="API tecnica de triagem, sem decisao clinica autonoma.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_metadata(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("X-Request-ID", str(uuid4()))
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.3f}"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError):
        return error_response(
            "INVALID_INPUT_SCHEMA", "Payload de triagem invalido.", 422, validation_details(exc)
        )

    @app.exception_handler(ModelLoadError)
    async def model_handler(_request: Request, _exc: ModelLoadError):
        return error_response("MODEL_UNAVAILABLE", "Modelo de inferencia indisponivel.", 503)

    @app.exception_handler(InferenceError)
    async def inference_handler(_request: Request, _exc: InferenceError):
        return error_response("INFERENCE_FAILED", "Falha durante a inferencia.", 500)

    @app.exception_handler(Exception)
    async def unexpected_handler(_request: Request, _exc: Exception):
        return error_response("INFERENCE_FAILED", "Falha durante a inferencia.", 500)

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

    @app.post("/v1/triage")
    def triage(payload: TriageBatchRequest) -> TriageBatchResponse:
        predictor = state["predictor"]
        if predictor is None:
            raise ModelLoadError("Modelo de inferencia indisponivel.")
        service = TriageService(predictor)  # type: ignore[arg-type]
        request_id = payload.request_id or str(uuid4())
        predictions = service.classify([item.report_text.strip() for item in payload.instances])
        return TriageBatchResponse(
            contract_version=payload.contract_version,
            request_id=request_id,
            model_version=predictor.model_version,  # type: ignore[union-attr]
            results=[
                TriageResult(prediction_id=str(uuid4()), **prediction.model_dump())
                for prediction in predictions
            ],
        )

    return app


app = create_app()