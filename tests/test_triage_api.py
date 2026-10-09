from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import triage.api.app as api_module
from triage.api.app import app


def payload() -> dict[str, object]:
    return {
        "contract_version": "1.0",
        "request_id": "client-request-1",
        "instances": [{"report_text": "synthetic signal alpha"}],
    }


def test_live_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_loaded_artifacts() -> None:
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["model_loaded"] is True
    assert response.json()["preprocessor_loaded"] is True


def test_triage_returns_contract_and_unique_prediction_id() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/triage", json=payload())

    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload["contract_version"] == "1.0"
    assert response_payload["request_id"] == "client-request-1"
    assert response_payload["model_version"] == "model"
    assert response_payload["results"][0]["triage_class"] in {"normal", "atencao", "urgente"}
    assert response_payload["results"][0]["prediction_id"]


def test_triage_rejects_extra_fields_without_loading_model() -> None:
    invalid = payload()
    invalid["instances"] = [{"report_text": "synthetic signal alpha", "raw_text": "no"}]

    with TestClient(app) as client:
        response = client.post("/v1/triage", json=invalid)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT_SCHEMA"


def test_triage_rejects_missing_report_text() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/triage", json={"instances": [{}]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT_SCHEMA"


def test_triage_rejects_batch_above_limit() -> None:
    invalid = payload()
    invalid["instances"] = [{"report_text": "synthetic signal alpha"}] * 33

    with TestClient(app) as client:
        response = client.post("/v1/triage", json=invalid)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT_SCHEMA"


def test_triage_rejects_empty_or_oversized_text() -> None:
    with TestClient(app) as client:
        empty_response = client.post(
            "/v1/triage",
            json={"instances": [{"report_text": "   "}]},
        )
        oversized_response = client.post(
            "/v1/triage",
            json={"instances": [{"report_text": "x" * 10_001}]},
        )

    assert empty_response.status_code == 422
    assert oversized_response.status_code == 422
    assert empty_response.json()["error"]["code"] == "INVALID_INPUT_SCHEMA"


def test_triage_generates_new_ids_for_each_request() -> None:
    first_payload = payload()
    second_payload = payload()
    first_payload.pop("request_id")
    second_payload.pop("request_id")

    with TestClient(app) as client:
        first = client.post("/v1/triage", json=first_payload).json()
        second = client.post("/v1/triage", json=second_payload).json()

    assert first["request_id"] != second["request_id"]
    assert first["results"][0]["prediction_id"] != second["results"][0]["prediction_id"]


def test_ready_is_degraded_when_artifacts_are_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_module, "MODEL_PATH", tmp_path / "missing-model.joblib")
    monkeypatch.setattr(api_module, "PREPROCESSOR_PATH", tmp_path / "missing-preprocessor.joblib")
    unavailable_app = api_module.create_app()

    with TestClient(unavailable_app) as client:
        ready_response = client.get("/health/ready")
        triage_response = client.post("/v1/triage", json=payload())

    assert ready_response.json()["status"] == "degraded"
    assert triage_response.status_code == 503
    assert triage_response.json()["error"]["code"] == "MODEL_UNAVAILABLE"


def test_triage_does_not_log_report_text(caplog) -> None:
    report_text = "synthetic private report marker"
    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            response = client.post("/v1/triage", json={"instances": [{"report_text": report_text}]})

    assert response.status_code == 200
    assert report_text not in caplog.text