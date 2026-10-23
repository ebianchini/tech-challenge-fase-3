from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

import triage.api.app as api_module
from triage.domain.exceptions import InferenceError
from triage.domain.models import TriageClass
from triage.infrastructure.model_loader import ModelLoadError

MODEL_VERSION = "triage-privacy-test-v1"
PRIVATE_MARKER = "synthetic-private-value-never-log"


@pytest.fixture(autouse=True)
def silence_test_client_logging():
    # Airflow tests may enable the root logger. HTTPX is the test client, not the API.
    logger = logging.getLogger("httpx")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


class SyntheticPredictor:
    model_version = MODEL_VERSION

    def __init__(self) -> None:
        self.failure: Exception | None = None

    def predict(self, texts: Sequence[str]):
        if self.failure is not None:
            raise self.failure
        return [
            (TriageClass.NORMAL, {"normal": 0.8, "atencao": 0.1, "urgente": 0.1}) for _text in texts
        ]


@pytest.fixture
def logging_app(monkeypatch, tmp_path):
    predictor = SyntheticPredictor()
    monkeypatch.setattr(api_module, "MODEL_PATH", tmp_path / f"{MODEL_VERSION}.joblib")
    monkeypatch.setattr(api_module, "SklearnPredictor", lambda *_args: predictor)
    return api_module.create_app(), predictor


def batch(text: str = "synthetic example") -> dict[str, object]:
    return {"instances": [{"report_text": text}]}


def request_logs(capsys) -> tuple[list[dict[str, object]], str]:
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    return [record for record in records if record.get("event") == "http_request"], (
        captured.out + captured.err
    )


def assert_request_log(record, *, route: str, status: int, method: str = "POST") -> None:
    assert record["route"] == route
    assert record["status_code"] == status
    assert record["method"] == method
    assert str(UUID(record["request_id"])) == record["request_id"]
    assert isinstance(record["latency_ms"], (int, float))
    assert math.isfinite(record["latency_ms"])
    assert record["latency_ms"] >= 0
    assert "model_version" in record
    assert set(record) <= {
        "event",
        "timestamp",
        "level",
        "request_id",
        "method",
        "route",
        "status_code",
        "latency_ms",
        "model_version",
        "error_type",
    }


def assert_private_error(response, marker: str = PRIVATE_MARKER) -> None:
    assert marker not in response.text
    for forbidden in ("Traceback", "stack", "File ", "input", "ctx"):
        assert forbidden not in response.text


def test_success_logs_json_stdout_and_correlates_generated_request_id(logging_app, capsys):
    app, _predictor = logging_app
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=batch(PRIVATE_MARKER))
    assert response.status_code == 200
    records, output = request_logs(capsys)
    assert len(records) == 1
    record = records[0]
    assert_request_log(record, route="/v1/triage", status=200)
    assert record["model_version"] == MODEL_VERSION
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert PRIVATE_MARKER not in output


def test_valid_header_uuid_is_used_in_body_header_and_log(logging_app, capsys):
    app, _predictor = logging_app
    request_id = str(uuid4())
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=batch(), headers={"X-Request-ID": request_id})
    records, _output = request_logs(capsys)
    assert response.status_code == 200
    assert len(records) == 1
    assert response.json()["request_id"] == request_id
    assert response.headers["X-Request-ID"] == request_id
    assert records[0]["request_id"] == request_id


@pytest.mark.parametrize("request_id", [PRIVATE_MARKER, "12345678123456781234567812345678"])
def test_untrusted_header_ids_are_replaced_before_logging(logging_app, capsys, request_id):
    app, _predictor = logging_app
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=batch(), headers={"X-Request-ID": request_id})
    records, output = request_logs(capsys)
    assert response.status_code == 200
    assert len(records) == 1
    assert_request_log(records[0], route="/v1/triage", status=200)
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert records[0]["request_id"] == response.headers["X-Request-ID"]
    assert request_id not in output


def test_body_id_contract_is_preserved_without_disclosing_id_in_logs(logging_app, capsys):
    app, _predictor = logging_app
    header_id = str(uuid4())
    payload = batch()
    payload["request_id"] = PRIVATE_MARKER
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=payload, headers={"X-Request-ID": header_id})
    records, output = request_logs(capsys)
    assert response.status_code == 200
    assert response.json()["request_id"] == PRIVATE_MARKER
    assert response.headers["X-Request-ID"] == header_id
    assert records[0]["request_id"] == header_id
    assert PRIVATE_MARKER not in output


def test_routes_methods_metrics_and_sensitive_headers_are_sanitized(logging_app, capsys):
    app, _predictor = logging_app
    route = "/synthetic-items/{item_id}"

    @app.get(route)
    async def item(item_id: str):
        return {"item_id": item_id}

    with TestClient(app) as client:
        assert (
            client.post(
                f"/v1/triage?token={PRIVATE_MARKER}-query",
                json=batch(f"{PRIVATE_MARKER}-report"),
                headers={
                    "Authorization": f"Bearer {PRIVATE_MARKER}-authorization",
                    "Cookie": f"session={PRIVATE_MARKER}-cookie",
                    "X-API-Key": f"{PRIVATE_MARKER}-api-key",
                    "X-Request-ID": f"{PRIVATE_MARKER}-request-id",
                },
            ).status_code
            == 200
        )
        assert client.get(f"/unknown-{PRIVATE_MARKER}?secret={PRIVATE_MARKER}").status_code == 404
        assert client.get(f"/synthetic-items/{PRIVATE_MARKER}").status_code == 200
        assert client.request(f"PRIVATE-{PRIVATE_MARKER}", "/health/live").status_code == 405
        assert client.get(f"/metrics?token={PRIVATE_MARKER}").status_code == 200
    records, output = request_logs(capsys)
    assert len(records) == 5
    for record, expected_route, status, method in zip(
        records,
        ["/v1/triage", "unmatched", route, "/health/live", "/metrics"],
        [200, 404, 200, 405, 200],
        ["POST", "GET", "GET", "OTHER", "GET"],
        strict=True,
    ):
        assert_request_log(record, route=expected_route, status=status, method=method)
    assert PRIVATE_MARKER not in output
    assert "Bearer" not in output


@pytest.mark.parametrize(
    "payload",
    [
        {"instances": [{"report_text": {PRIVATE_MARKER: PRIVATE_MARKER}}]},
        {"instances": [{"report_text": "synthetic", PRIVATE_MARKER: PRIVATE_MARKER}]},
        {"instances": [{"report_text": "synthetic"}], PRIVATE_MARKER: PRIVATE_MARKER},
    ],
)
def test_validation_details_do_not_echo_values_or_untrusted_field_names(
    logging_app, capsys, payload
):
    app, _predictor = logging_app
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=payload)
    assert response.status_code == 422
    assert_private_error(response)
    details = response.json()["error"]["details"]
    assert details
    assert all(set(detail) == {"loc", "msg", "type"} for detail in details)
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/v1/triage", status=422)
    assert PRIVATE_MARKER not in output


def test_malformed_json_response_and_log_do_not_contain_request_body(logging_app, capsys):
    app, _predictor = logging_app
    with TestClient(app) as client:
        response = client.post(
            "/v1/triage",
            content='{"instances": [{"report_text": "' + PRIVATE_MARKER,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert_private_error(response)
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/v1/triage", status=422)
    assert PRIVATE_MARKER not in output


def test_validation_value_error_messages_are_sanitized(logging_app, capsys):
    app, _predictor = logging_app

    @app.get("/synthetic-validation")
    async def invalid():
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "instances", 0, "report_text"),
                    "msg": f"Value error, {PRIVATE_MARKER}",
                    "input": PRIVATE_MARKER,
                    "ctx": {"error": ValueError(PRIVATE_MARKER)},
                }
            ]
        )

    with TestClient(app) as client:
        response = client.get("/synthetic-validation")
    assert response.status_code == 422
    assert_private_error(response)
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/synthetic-validation", status=422, method="GET")
    assert PRIVATE_MARKER not in output


@pytest.mark.parametrize("exception_type", [InferenceError, RuntimeError])
def test_inference_exceptions_return_and_log_only_safe_metadata(
    logging_app, capsys, exception_type
):
    app, predictor = logging_app
    predictor.failure = exception_type(
        f"C:\\private\\{PRIVATE_MARKER}.joblib payload={PRIVATE_MARKER}"
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/triage", json=batch(PRIVATE_MARKER))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INFERENCE_FAILED"
    assert_private_error(response)
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/v1/triage", status=500)
    assert records[0]["request_id"] == response.headers["X-Request-ID"]
    assert PRIVATE_MARKER not in output
    assert "Traceback" not in output


def test_model_load_exception_does_not_expose_path_or_payload(monkeypatch, tmp_path, capsys):
    def unavailable(*_args):
        raise ModelLoadError(f"C:\\private\\{PRIVATE_MARKER}.joblib payload={PRIVATE_MARKER}")

    monkeypatch.setattr(api_module, "SklearnPredictor", unavailable)
    monkeypatch.setattr(api_module, "MODEL_PATH", tmp_path / f"{PRIVATE_MARKER}.joblib")
    app = api_module.create_app()
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=batch(PRIVATE_MARKER))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_UNAVAILABLE"
    assert_private_error(response)
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/v1/triage", status=503)
    assert PRIVATE_MARKER not in output


def test_unexpected_endpoint_exception_has_safe_response_and_completion_log(logging_app, capsys):
    app, _predictor = logging_app

    @app.get("/synthetic-unexpected")
    async def unexpected():
        raise RuntimeError(f"C:\\private\\{PRIVATE_MARKER}.txt payload={PRIVATE_MARKER}")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/synthetic-unexpected")
    assert response.status_code == 500
    assert_private_error(response)
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/synthetic-unexpected", status=500, method="GET")
    assert records[0]["request_id"] == response.headers["X-Request-ID"]
    assert PRIVATE_MARKER not in output
    assert "Traceback" not in output


@pytest.mark.parametrize("unsafe_version", ["synthetic/private-version", "a" * 64])
def test_model_version_in_log_uses_safe_bounded_metadata(logging_app, capsys, unsafe_version):
    app, predictor = logging_app
    predictor.model_version = unsafe_version
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=batch())
    assert response.status_code == 200
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert records[0]["model_version"] == "unknown"
    assert unsafe_version not in output


@pytest.mark.parametrize(
    "header_values",
    [
        ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"],
        [f"{PRIVATE_MARKER}\r\nX-Secret: {PRIVATE_MARKER}"],
        [f"{PRIVATE_MARKER}\x00"],
        [f"{PRIVATE_MARKER}\t"],
    ],
)
def test_duplicate_and_control_request_id_headers_are_replaced(logging_app, capsys, header_values):
    app, _predictor = logging_app
    with TestClient(app) as client:
        response = client.post(
            "/v1/triage",
            json=batch(),
            headers=[("X-Request-ID", value) for value in header_values],
        )
    assert response.status_code == 200
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/v1/triage", status=200)
    request_id = response.headers["X-Request-ID"]
    assert request_id not in header_values
    assert records[0]["request_id"] == request_id
    assert response.json()["request_id"] == request_id
    assert PRIVATE_MARKER not in output


@pytest.mark.parametrize(
    "headers",
    [
        {"Allow": f"GET, {PRIVATE_MARKER}, POST", "X-Private": PRIVATE_MARKER},
        {"X-Private": PRIVATE_MARKER},
        None,
    ],
)
def test_405_exception_sanitizes_details_and_allow_header(logging_app, capsys, headers):
    app, _predictor = logging_app

    @app.get("/synthetic-method-error")
    async def method_error():
        raise HTTPException(status_code=405, detail=PRIVATE_MARKER, headers=headers)

    with TestClient(app) as client:
        response = client.get("/synthetic-method-error")
    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}
    assert PRIVATE_MARKER not in str(response.headers)
    assert "X-Private" not in response.headers
    if headers and "Allow" in headers:
        assert set(response.headers.get("Allow", "").split(", ")) == {"GET", "POST"}
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/synthetic-method-error", status=405, method="GET")
    assert PRIVATE_MARKER not in output


def test_400_exception_does_not_echo_detail_or_custom_header(logging_app, capsys):
    app, _predictor = logging_app

    @app.get("/synthetic-bad-request")
    async def bad_request():
        raise HTTPException(
            status_code=400,
            detail={"report_text": PRIVATE_MARKER, "path": f"C:\\private\\{PRIVATE_MARKER}"},
            headers={"X-Private": PRIVATE_MARKER},
        )

    with TestClient(app) as client:
        response = client.get("/synthetic-bad-request")
    assert response.status_code == 400
    assert_private_error(response)
    assert PRIVATE_MARKER not in str(response.headers)
    assert "X-Private" not in response.headers
    records, output = request_logs(capsys)
    assert len(records) == 1
    assert_request_log(records[0], route="/synthetic-bad-request", status=400, method="GET")
    assert PRIVATE_MARKER not in output


def test_server_logging_config_discards_messages_traces_and_access_logs():
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import json
        import logging
        import logging.config
        import sys

        from fastapi.testclient import TestClient
        from triage.api.app import create_app

        with open(sys.argv[1], encoding="utf-8") as config:
            logging.config.dictConfig(json.load(config))
        marker = sys.argv[2]
        for name in ("uvicorn", "uvicorn.error"):
            try:
                raise RuntimeError(f"C:/private/{marker}.joblib payload={marker}")
            except RuntimeError:
                logging.getLogger(name).error(
                    "server message %s", marker,
                    exc_info=True,
                    stack_info=True,
                    extra={"report_text": marker, "token": marker},
                )
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            marker, "GET", f"/{marker}?token={marker}", "1.1", 200,
        )
        response = TestClient(create_app()).get("/health/live")
        assert response.status_code == 200
        """
    )
    process = subprocess.run(
        [
            sys.executable,
            "-W",
            "ignore",
            "-c",
            script,
            str(root / "logging" / "uvicorn.json"),
            PRIVATE_MARKER,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    server_records = [json.loads(line) for line in process.stderr.splitlines()]
    assert len(server_records) == 2
    for record in server_records:
        assert set(record) == {"event", "level", "timestamp"}
        assert record["event"] == "server_log"
        assert record["level"] == "ERROR"
    audit_records = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(audit_records) == 1
    assert audit_records[0]["event"] == "http_request"
    assert_request_log(audit_records[0], route="/health/live", status=200, method="GET")
    assert PRIVATE_MARKER not in process.stdout + process.stderr
    assert "Traceback" not in process.stdout + process.stderr
