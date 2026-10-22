"""Application-scoped, bounded Prometheus metrics without report content.

The registry belongs to one application instance. No default process, platform or
GC collectors are exported. HTTP labels use registered route templates only;
unmatched requests never expose paths, query strings, headers or request bodies.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from triage.domain.models import TriageClass, TriagePrediction

_ERROR_TYPES = frozenset({"INVALID_INPUT_SCHEMA", "MODEL_UNAVAILABLE", "INFERENCE_FAILED"})
_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)
_MODEL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FINGERPRINT = re.compile(r"[a-fA-F0-9]{24,}\Z")
_UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
_DURATION_BUCKETS = (0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_LENGTH_BUCKETS = (1, 50, 100, 250, 500, 1000, 2500, 5000, 7500, 10_000)


def _model_version(value: str | None) -> str:
    """Accept short deployment identifiers, never paths or artifact fingerprints."""
    if (
        not isinstance(value, str)
        or not _MODEL_VERSION.fullmatch(value)
        or _FINGERPRINT.fullmatch(value)
        or _UUID.fullmatch(value)
    ):
        return "unknown"
    return value


class TriageMetrics:
    """Prometheus instruments owned by a single FastAPI application.

    ``model_version`` must come from deployment/model metadata, never a request.
    Invalid identifiers are collapsed to ``unknown``. Call ``begin_model`` when
    loading an artifact so zero values also exist before the first prediction.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "triage_http_requests_total",
            "HTTP responses by method, registered route template and status.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "triage_http_request_duration_seconds",
            "HTTP duration through the final response body, excluding metrics scrapes.",
            ("method", "route"),
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.predictions = Counter(
            "triage_predictions_total",
            "Predictions produced by model version and triage class.",
            ("model_version", "triage_class"),
            registry=self.registry,
        )
        self.prediction_errors = Counter(
            "triage_prediction_errors_total",
            "Triage errors by bounded public error code.",
            ("error_type",),
            registry=self.registry,
        )
        self.abstentions = Counter(
            "triage_abstentions_total",
            "Predictions requiring human review because confidence was insufficient.",
            ("model_version",),
            registry=self.registry,
        )
        self.model_ready = Gauge(
            "triage_model_ready",
            "Whether the model and preprocessor are ready for inference (1 or 0).",
            ("model_version",),
            registry=self.registry,
        )
        self.input_length = Histogram(
            "triage_input_length_characters",
            "Length in characters of each validated report presented for triage.",
            buckets=_LENGTH_BUCKETS,
            registry=self.registry,
        )
        self.model_inference_duration = Histogram(
            "triage_model_inference_duration_seconds",
            "Duration of a complete model inference batch, including failed attempts.",
            ("model_version",),
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self._current_model_version = "unknown"
        self.model_ready.labels(model_version="unknown").set(0)
        for error_type in sorted(_ERROR_TYPES):
            self.prediction_errors.labels(error_type=error_type).inc(0)

    def begin_model(self, version: str | None) -> str:
        """Initialize the model's series as unavailable and return its safe label."""
        self.model_ready.labels(model_version=self._current_model_version).set(0)
        normalized = _model_version(version)
        self._current_model_version = normalized
        self.model_ready.labels(model_version=normalized).set(0)
        self.abstentions.labels(model_version=normalized).inc(0)
        self.model_inference_duration.labels(model_version=normalized)
        for triage_class in TriageClass:
            self.predictions.labels(
                model_version=normalized, triage_class=triage_class.value
            ).inc(0)
        return normalized

    def set_model_ready(self, version: str | None, ready: bool) -> None:
        self.model_ready.labels(model_version=_model_version(version)).set(1 if ready else 0)

    def observe_inputs(self, lengths: Iterable[int]) -> None:
        for length in lengths:
            self.input_length.observe(length)

    def observe_inference(self, version: str | None, seconds: float) -> None:
        self.model_inference_duration.labels(model_version=_model_version(version)).observe(seconds)

    def observe_predictions(
        self, version: str | None, predictions: Iterable[TriagePrediction]
    ) -> None:
        normalized = _model_version(version)
        for prediction in predictions:
            self.predictions.labels(
                model_version=normalized, triage_class=prediction.triage_class.value
            ).inc()
            if prediction.abstained:
                self.abstentions.labels(model_version=normalized).inc()

    def observe_error(self, error_type: str) -> None:
        bounded_type = error_type if error_type in _ERROR_TYPES else "INFERENCE_FAILED"
        self.prediction_errors.labels(error_type=bounded_type).inc()

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class HTTPMetricsMiddleware:
    """Measure ASGI responses without consuming bodies or recording raw URLs.

    Install outside application middleware so the duration includes its work.
    ``route_templates`` is a fixed whitelist of registered route patterns. Every
    unmatched path is grouped under ``unmatched`` and arbitrary methods under
    ``OTHER``. ``/metrics`` is excluded to avoid observing the scrape itself.
    """

    def __init__(
        self, app: ASGIApp, metrics: TriageMetrics, route_templates: Iterable[str]
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.route_templates = frozenset(route_templates)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500
        completed_at: float | None = None

        async def measured_send(message: Message) -> None:
            nonlocal status, completed_at
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed_at = time.perf_counter()

        try:
            await self.app(scope, receive, measured_send)
        finally:
            method = scope.get("method", "").upper()
            if method not in _HTTP_METHODS:
                method = "OTHER"
            template = getattr(scope.get("route"), "path", None)
            route = template if template in self.route_templates else "unmatched"
            self.metrics.http_requests.labels(method=method, route=route, status=str(status)).inc()
            self.metrics.http_request_duration.labels(method=method, route=route).observe(
                (completed_at if completed_at is not None else time.perf_counter()) - started
            )
