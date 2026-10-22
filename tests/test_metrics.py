from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from starlette.responses import Response

import triage.api.app as api_module
import triage.infrastructure.metrics as metrics_module
from triage.domain.exceptions import InferenceError
from triage.domain.models import TriageClass
from triage.infrastructure.metrics import HTTPMetricsMiddleware
from triage.infrastructure.model_loader import ModelLoadError

MODEL_VERSION = "triage-test-v1"


class SyntheticPredictor:
    model_version = MODEL_VERSION

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.failure: Exception | None = None

    def predict(self, texts: Sequence[str]):
        self.texts.extend(texts)
        if self.failure is not None:
            raise self.failure
        outcomes = [
            (TriageClass.NORMAL, {"normal": 0.8, "atencao": 0.1, "urgente": 0.1}),
            (TriageClass.ATENCAO, {"normal": 0.3, "atencao": 0.5, "urgente": 0.2}),
            (TriageClass.URGENTE, {"normal": 0.05, "atencao": 0.05, "urgente": 0.9}),
        ]
        return [outcomes[index % len(outcomes)] for index, _text in enumerate(texts)]


@pytest.fixture
def metrics_app(monkeypatch, tmp_path):
    predictor = SyntheticPredictor()
    monkeypatch.setattr(api_module, "MODEL_PATH", tmp_path / f"{MODEL_VERSION}.joblib")
    monkeypatch.setattr(api_module, "SklearnPredictor", lambda *_args: predictor)
    return api_module.create_app(), predictor


def batch(*texts: str) -> dict[str, object]:
    return {"instances": [{"report_text": text} for text in texts]}


def metric_sum(app, name: str, labels: dict[str, str] | None = None) -> float:
    return sum(
        sample.value
        for family in app.state.metrics.registry.collect()
        for sample in family.samples
        if sample.name == name
        and all(sample.labels.get(key) == value for key, value in (labels or {}).items())
    )


def test_metrics_exposes_prometheus_text_directly_and_excludes_scrapes(metrics_app) -> None:
    app, _predictor = metrics_app
    with TestClient(app) as client:
        response = client.get("/metrics", follow_redirects=False)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert not response.history
        names = {family.name for family in text_string_to_metric_families(response.text)}
        assert {
            "triage_http_requests",
            "triage_http_request_duration_seconds",
            "triage_predictions",
            "triage_prediction_errors",
            "triage_abstentions",
            "triage_model_ready",
            "triage_input_length_characters",
            "triage_model_inference_duration_seconds",
        } <= names

        for _attempt in range(3):
            assert client.get("/metrics?synthetic-token=ignored").status_code == 200
        assert metric_sum(app, "triage_http_requests_total") == 0
        assert metric_sum(app, "triage_http_request_duration_seconds_count") == 0

        client.get("/health/live")
        client.get("/metrics")
        assert metric_sum(app, "triage_http_requests_total") == 1


def test_success_counts_batch_predictions_abstentions_and_separate_durations(metrics_app) -> None:
    app, predictor = metrics_app
    texts = ("  alpha  ", "\tbeta\n", "gamma")
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=batch(*texts))
        assert response.status_code == 200
        abstentions = [result["abstained"] for result in response.json()["results"]]
        assert abstentions == [False, True, False]
        assert predictor.texts == ["alpha", "beta", "gamma"]

        for triage_class in ("normal", "atencao", "urgente"):
            assert metric_sum(
                app,
                "triage_predictions_total",
                {"model_version": MODEL_VERSION, "triage_class": triage_class},
            ) == 1
        assert metric_sum(app, "triage_abstentions_total", {"model_version": MODEL_VERSION}) == 1
        assert metric_sum(app, "triage_prediction_errors_total") == 0
        assert metric_sum(
            app,
            "triage_http_requests_total",
            {"method": "POST", "route": "/v1/triage", "status": "200"},
        ) == 1
        assert metric_sum(app, "triage_http_request_duration_seconds_count") == 1
        assert metric_sum(app, "triage_model_inference_duration_seconds_count") == 1
        assert metric_sum(app, "triage_http_request_duration_seconds_sum") > 0
        assert metric_sum(app, "triage_model_inference_duration_seconds_sum") > 0
        assert metric_sum(app, "triage_input_length_characters_count") == 3
        assert metric_sum(app, "triage_input_length_characters_sum") == 14

        client.post("/v1/triage", json=batch("another synthetic item"))
        assert metric_sum(app, "triage_http_requests_total") == 2
        assert metric_sum(app, "triage_predictions_total") == 4
        assert metric_sum(app, "triage_model_inference_duration_seconds_count") == 2
        assert metric_sum(app, "triage_input_length_characters_count") == 4


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"instances": []},
        {"instances": [{"report_text": "   "}]},
        {"instances": [{"report_text": "synthetic", "extra": "private-extra"}]},
        {"instances": [{"report_text": "x" * 10_001}]},
    ],
)
def test_invalid_inputs_count_one_error_without_inference(metrics_app, invalid_payload) -> None:
    app, predictor = metrics_app
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=invalid_payload)
        assert response.status_code == 422
        assert metric_sum(
            app, "triage_prediction_errors_total", {"error_type": "INVALID_INPUT_SCHEMA"}
        ) == 1
        assert metric_sum(app, "triage_prediction_errors_total") == 1
        assert metric_sum(app, "triage_http_requests_total", {"status": "422"}) == 1
        assert metric_sum(app, "triage_http_request_duration_seconds_count") == 1
        assert metric_sum(app, "triage_predictions_total") == 0
        assert metric_sum(app, "triage_model_inference_duration_seconds_count") == 0
        assert metric_sum(app, "triage_input_length_characters_count") == 0
        assert predictor.texts == []


def test_unavailable_model_counts_request_error_and_stays_unready(monkeypatch, tmp_path) -> None:
    def unavailable(*_args):
        raise ModelLoadError("synthetic-private-load-failure")

    monkeypatch.setattr(api_module, "SklearnPredictor", unavailable)
    monkeypatch.setattr(api_module, "MODEL_PATH", tmp_path / "missing-model.joblib")
    app = api_module.create_app()
    with TestClient(app) as client:
        assert metric_sum(app, "triage_model_ready") == 0
        assert metric_sum(app, "triage_prediction_errors_total") == 0
        response = client.post("/v1/triage", json=batch("synthetic unavailable input"))
        assert response.status_code == 503
        assert metric_sum(
            app, "triage_prediction_errors_total", {"error_type": "MODEL_UNAVAILABLE"}
        ) == 1
        assert metric_sum(app, "triage_prediction_errors_total") == 1
        assert metric_sum(app, "triage_http_requests_total", {"status": "503"}) == 1
        assert metric_sum(app, "triage_predictions_total") == 0
        assert metric_sum(app, "triage_model_inference_duration_seconds_count") == 0
        assert "synthetic-private-load-failure" not in client.get("/metrics").text
    assert metric_sum(app, "triage_model_ready") == 0


@pytest.mark.parametrize("exception_type", [InferenceError, RuntimeError])
def test_failed_inference_is_counted_once_without_predictions(metrics_app, exception_type) -> None:
    app, predictor = metrics_app
    predictor.failure = exception_type("synthetic-private-exception-marker")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/triage", json=batch("alpha", "beta"))
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INFERENCE_FAILED"
        assert metric_sum(
            app, "triage_prediction_errors_total", {"error_type": "INFERENCE_FAILED"}
        ) == 1
        assert metric_sum(app, "triage_prediction_errors_total") == 1
        assert metric_sum(app, "triage_http_requests_total", {"status": "500"}) == 1
        assert metric_sum(app, "triage_http_request_duration_seconds_count") == 1
        assert metric_sum(app, "triage_model_inference_duration_seconds_count") == 1
        assert metric_sum(app, "triage_predictions_total") == 0
        assert metric_sum(app, "triage_abstentions_total") == 0
        assert "synthetic-private-exception-marker" not in client.get("/metrics").text


def test_partial_invalid_prediction_never_counts_successful_predictions(metrics_app, monkeypatch):
    app, predictor = metrics_app
    monkeypatch.setattr(
        predictor,
        "predict",
        lambda _texts: [
            (TriageClass.NORMAL, {TriageClass.NORMAL: 0.9}),
            (TriageClass.ATENCAO, {TriageClass.NORMAL: 0.1}),
        ],
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/v1/triage", json=batch("alpha", "beta")).status_code == 500
        assert metric_sum(app, "triage_predictions_total") == 0
        assert metric_sum(app, "triage_abstentions_total") == 0
        assert metric_sum(app, "triage_prediction_errors_total") == 1


def test_ready_gauge_tracks_lifespan_and_apps_have_isolated_registries(metrics_app) -> None:
    first_app, _predictor = metrics_app
    second_app = api_module.create_app()
    assert first_app.state.metrics.registry is not second_app.state.metrics.registry
    assert metric_sum(first_app, "triage_model_ready") == 0
    assert metric_sum(second_app, "triage_model_ready") == 0

    with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
        assert metric_sum(first_app, "triage_model_ready", {"model_version": MODEL_VERSION}) == 1
        assert metric_sum(second_app, "triage_model_ready", {"model_version": MODEL_VERSION}) == 1
        first_client.post("/v1/triage", json=batch("synthetic first app"))
        assert metric_sum(first_app, "triage_predictions_total") == 1
        assert metric_sum(second_app, "triage_predictions_total") == 0
        assert second_client.get("/metrics").status_code == 200

    assert metric_sum(first_app, "triage_model_ready") == 0
    assert metric_sum(second_app, "triage_model_ready") == 0


def test_metrics_never_include_request_data_or_create_unbounded_path_method_labels(metrics_app):
    app, _predictor = metrics_app
    markers = (
        "synthetic-private-body-marker",
        "synthetic-private-query-marker",
        "synthetic-private-request-id",
        "synthetic-private-token-marker",
        "synthetic-private-patient-marker",
    )
    with TestClient(app) as client:
        payload = batch(markers[0])
        payload["request_id"] = markers[2]
        assert client.post(
            f"/v1/triage?patient={markers[4]}&query={markers[1]}",
            json=payload,
            headers={"X-Request-ID": markers[2], "Authorization": f"Bearer {markers[3]}"},
        ).status_code == 200

        for index in range(20):
            response = client.get(f"/synthetic-private-path-{index}?token={markers[3]}")
            assert response.status_code == 404
        for index in range(8):
            client.request(f"SYNTHETIC-PRIVATE-METHOD-{index}", "/health/live")

        text = client.get("/metrics").text
        for marker in (*markers, "synthetic-private-path", "SYNTHETIC-PRIVATE-METHOD"):
            assert marker not in text

        http_samples = [
            sample
            for family in text_string_to_metric_families(text)
            for sample in family.samples
            if sample.name == "triage_http_requests_total"
        ]
        assert {sample.labels["route"] for sample in http_samples} <= {
            "/v1/triage",
            "/health/live",
            "unmatched",
        }
        assert metric_sum(
            app, "triage_http_requests_total", {"route": "unmatched", "status": "404"}
        ) == 20
        assert metric_sum(app, "triage_http_requests_total", {"method": "OTHER"}) == 8
        assert len(http_samples) <= 3
        assert metric_sum(app, "triage_http_requests_total") == 29


@pytest.mark.parametrize(
    "unsafe_version", ["synthetic-private/version-marker", "a" * 64, "synthetic version marker"]
)
def test_unsafe_model_version_is_replaced_by_bounded_label(metrics_app, unsafe_version) -> None:
    app, predictor = metrics_app
    predictor.model_version = unsafe_version
    with TestClient(app) as client:
        assert client.post("/v1/triage", json=batch("synthetic input")).status_code == 200
        response = client.get("/metrics")
        assert unsafe_version not in response.text
        assert metric_sum(app, "triage_model_ready", {"model_version": "unknown"}) == 1
        assert metric_sum(app, "triage_predictions_total", {"model_version": "unknown"}) == 1


def test_request_cannot_introduce_a_model_version_label(metrics_app) -> None:
    app, _predictor = metrics_app
    with TestClient(app) as client:
        payload = batch("synthetic input")
        payload["model_version"] = "synthetic-untrusted-model-marker"
        assert client.post("/v1/triage", json=payload).status_code == 422
        assert "synthetic-untrusted-model-marker" not in client.get("/metrics").text
        assert metric_sum(app, "triage_predictions_total") == 0


def test_stream_duration_includes_last_chunk_send_and_excludes_background_work(
    metrics_app, monkeypatch
) -> None:
    app, _predictor = metrics_app
    clock = {"now": 10.0}
    monkeypatch.setattr(
        metrics_module, "time", SimpleNamespace(perf_counter=lambda: clock["now"])
    )
    messages = []
    route = "/synthetic-stream/{stream_id}"

    async def streaming_app(scope, _receive, send):
        scope["route"] = SimpleNamespace(path=route)
        clock["now"] = 11.0
        await send({"type": "http.response.start", "status": 206, "headers": []})
        clock["now"] = 12.0
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert metric_sum(app, "triage_http_requests_total") == 0
        assert metric_sum(app, "triage_http_request_duration_seconds_count") == 0
        clock["now"] = 15.0
        await send({"type": "http.response.body", "body": b"last", "more_body": False})
        clock["now"] = 99.0

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        await asyncio.sleep(0)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            clock["now"] += 0.5
        messages.append(message.copy())

    middleware = HTTPMetricsMiddleware(streaming_app, app.state.metrics, [route])
    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "path": "/synthetic-stream/private-stream-id"},
            receive,
            send,
        )
    )
    assert [message.get("body") for message in messages] == [None, b"first", b"last"]
    assert metric_sum(
        app, "triage_http_requests_total", {"method": "GET", "route": route, "status": "206"}
    ) == 1
    assert metric_sum(app, "triage_http_request_duration_seconds_count") == 1
    assert metric_sum(app, "triage_http_request_duration_seconds_sum") == pytest.approx(5.5)


def test_registered_parameterized_route_groups_private_path_values(metrics_app) -> None:
    app, _predictor = metrics_app
    route = "/synthetic-reports/{report_id}"

    @app.get(route)
    async def synthetic_report(report_id: str):
        return {"synthetic_report": report_id}

    with TestClient(app) as client:
        for index in range(10):
            assert client.get(f"/synthetic-reports/private-report-value-{index}").status_code == 200

        text = client.get("/metrics").text
        assert "private-report-value" not in text
        assert metric_sum(
            app, "triage_http_requests_total", {"method": "GET", "route": route, "status": "200"}
        ) == 10
        assert metric_sum(app, "triage_http_requests_total", {"route": "unmatched"}) == 0
        samples = [
            sample
            for family in text_string_to_metric_families(text)
            for sample in family.samples
            if sample.name == "triage_http_requests_total"
        ]
        assert len(samples) == 1


def test_unexpected_error_duration_includes_generated_500_response(
    metrics_app, monkeypatch
) -> None:
    app, _predictor = metrics_app
    clock = {"now": 10.0}
    monkeypatch.setattr(
        metrics_module, "time", SimpleNamespace(perf_counter=lambda: clock["now"])
    )
    messages = []

    @app.get("/synthetic-failure")
    async def synthetic_failure():
        clock["now"] = 12.0
        raise RuntimeError("synthetic-private-unexpected-error")

    @app.exception_handler(Exception)
    async def controlled_handler(_request, _exception):
        clock["now"] = 14.0
        return Response(content="synthetic failure", status_code=500)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        await asyncio.sleep(0)
        if message["type"] == "http.response.start":
            clock["now"] = 15.0
        elif message["type"] == "http.response.body" and not message.get("more_body", False):
            clock["now"] = 17.0
        messages.append(message.copy())

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/synthetic-failure",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    with pytest.raises(RuntimeError, match="synthetic-private-unexpected-error"):
        asyncio.run(app(scope, receive, send))

    assert messages[0]["status"] == 500
    assert messages[-1]["body"] == b"synthetic failure"
    assert metric_sum(app, "triage_http_requests_total", {"status": "500"}) == 1
    assert metric_sum(app, "triage_http_request_duration_seconds_count") == 1
    assert metric_sum(app, "triage_http_request_duration_seconds_sum") == pytest.approx(7.0)
