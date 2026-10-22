from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

import triage.api.app as api_module
from scripts.validate_openapi import BASELINE, resolve_schema, validate_contract
from triage.domain.exceptions import InferenceError
from triage.domain.models import TriageClass
from triage.infrastructure.model_loader import ModelLoadError


@pytest.fixture
def document(monkeypatch):
    def forbid_artifacts(*_args, **_kwargs):
        raise AssertionError("OpenAPI generation must not load model artifacts.")

    monkeypatch.setattr(api_module, "SklearnPredictor", forbid_artifacts)
    return api_module.create_app().openapi()


def test_openapi_is_valid_and_preserves_reviewed_contract(document):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    validate_contract(document, baseline)


def test_formal_validation_rejects_malformed_openapi(document):
    invalid = deepcopy(document)
    del invalid["info"]
    with pytest.raises(OpenAPIValidationError):
        validate_contract(invalid, document)


def test_contract_allows_documentation_changes_and_new_endpoints(document):
    compatible = deepcopy(document)
    compatible["info"]["description"] = "Updated technical documentation."
    compatible["components"]["schemas"]["TriageRequest"]["description"] = "Synthetic text."
    compatible["paths"]["/health/new"] = deepcopy(compatible["paths"]["/health/live"])
    compatible["paths"]["/health/new"]["get"]["operationId"] = "new_health_check"
    validate_contract(compatible, document)


@pytest.mark.parametrize(
    ("schema", "field", "value"),
    [
        ("TriageRequest", "properties.report_text.maxLength", 9_999),
        ("TriageBatchRequest", "properties.instances.maxItems", 31),
        ("TriageBatchRequest", "required", ["instances", "request_id"]),
        ("TriageClass", "enum", ["normal", "atencao"]),
        ("TriageBatchResponse", "required", ["contract_version", "request_id", "results"]),
        ("TriageBatchResponse", "properties.request_id.type", "integer"),
        ("ErrorBody", "properties.code.enum", ["INVALID_INPUT_SCHEMA", "INFERENCE_FAILED"]),
    ],
)
def test_contract_rejects_changed_limits_types_required_fields_and_enums(
    document, schema, field, value
):
    changed = deepcopy(document)
    target = changed["components"]["schemas"][schema]
    parts = field.split(".")
    for key in parts[:-1]:
        target = target[key]
    target[parts[-1]] = value
    with pytest.raises(ValueError, match="Contract changed"):
        validate_contract(changed, document)


def test_contract_rejects_removed_operation(document):
    changed = deepcopy(document)
    del changed["paths"]["/v1/triage"]["post"]
    with pytest.raises(ValueError, match="operation removed"):
        validate_contract(changed, document)


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        ({"instances": [{"report_text": "synthetic signal alpha"}]}, True),
        ({"instances": [{"report_text": "x" * 10_000}] * 32}, True),
        ({"instances": []}, False),
        ({"instances": [{"report_text": "x"}] * 33}, False),
        ({"instances": [{"report_text": ""}]}, False),
        ({"instances": [{"report_text": "x" * 10_001}]}, False),
        ({"instances": [{}]}, False),
        ({"instances": [{"report_text": "x", "extra": True}]}, False),
        ({"instances": [{"report_text": "x"}], "extra": True}, False),
        ({"instances": [{"report_text": "x"}], "contract_version": "2.0"}, False),
    ],
    ids=[
        "minimal",
        "maximum-size",
        "empty-batch",
        "oversized-batch",
        "empty-text",
        "oversized-text",
        "missing-text",
        "extra-instance-field",
        "extra-request-field",
        "unsupported-contract",
    ],
)
def test_openapi_request_schema_describes_published_boundaries(document, payload, valid):
    schema = resolve_schema(document, document["components"]["schemas"]["TriageBatchRequest"])
    assert Draft202012Validator(schema).is_valid(payload) is valid


@pytest.mark.parametrize("mode", ["success", "invalid", "unavailable", "failure"])
def test_actual_responses_match_openapi_without_real_artifacts(monkeypatch, mode):
    class SyntheticPredictor:
        model_version = "synthetic-contract-fixture"

        def __init__(self, *_args):
            if mode == "unavailable":
                raise ModelLoadError("Synthetic artifact unavailable.")

        def predict(self, texts):
            if mode == "failure":
                raise InferenceError("Synthetic inference failure.")
            return [
                (
                    TriageClass.NORMAL,
                    {TriageClass.NORMAL: 0.8, TriageClass.ATENCAO: 0.1, TriageClass.URGENTE: 0.1},
                )
                for _ in texts
            ]

    monkeypatch.setattr(api_module, "SklearnPredictor", SyntheticPredictor)
    app = api_module.create_app()
    document = app.openapi()
    # Whitespace is a runtime constraint that JSON Schema cannot express here.
    text = "   " if mode == "invalid" else "synthetic signal alpha"
    with TestClient(app) as client:
        response = client.post("/v1/triage", json={"instances": [{"report_text": text}]})
    assert (
        response.status_code
        == {
            "success": 200,
            "invalid": 422,
            "unavailable": 503,
            "failure": 500,
        }[mode]
    )
    schema = document["paths"]["/v1/triage"]["post"]["responses"][str(response.status_code)][
        "content"
    ]["application/json"]["schema"]
    Draft202012Validator(resolve_schema(document, schema)).validate(response.json())
