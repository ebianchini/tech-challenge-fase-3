"""Validate OpenAPI and protect the reviewed HTTP 1.0 contract without loading artifacts.

Existing operations and their schemas are checked conservatively: any semantic change
requires review of the versioned baseline. Documentation and new operations are allowed.
The baseline must not be regenerated automatically during CI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from openapi_spec_validator import validate

from triage.api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "openapi-contract.json"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
ANNOTATIONS = {"title", "description", "summary", "example", "examples", "externalDocs"}


def resolve_schema(document: dict[str, Any], value: Any, *, property_map: bool = False) -> Any:
    """Expand local references and remove annotations, retaining schema constraints."""
    if isinstance(value, list):
        return [resolve_schema(document, item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if not reference.startswith("#/"):
            raise ValueError("Only local OpenAPI references are allowed in the CI contract.")
        target = document
        for component in reference[2:].split("/"):
            target = target[component.replace("~1", "/").replace("~0", "~")]
        value = {**target, **{key: item for key, item in value.items() if key != "$ref"}}
    return {
        key: (
            sorted(item)
            if not property_map and key in {"required", "enum"} and isinstance(item, list)
            else resolve_schema(document, item, property_map=key in {"properties", "$defs"})
        )
        for key, item in value.items()
        if property_map or key not in ANNOTATIONS
    }


def operation_contract(document: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    """Keep only the request, response, parameter and security contract of an operation."""
    return resolve_schema(
        document,
        {
            key: operation[key]
            for key in ("parameters", "requestBody", "responses", "security")
            if key in operation
        },
    )


def validate_contract(document: dict[str, Any], baseline: dict[str, Any]) -> None:
    validate(document)
    validate(baseline)
    if document["openapi"] != baseline["openapi"]:
        raise ValueError("OpenAPI version changed; review the HTTP 1.0 contract baseline.")
    if document.get("security", []) != baseline.get("security", []):
        raise ValueError("Global security changed; review the HTTP 1.0 contract baseline.")
    for path, path_item in baseline["paths"].items():
        current_path = document["paths"].get(path, {})
        for method in HTTP_METHODS.intersection(path_item):
            operation = current_path.get(method)
            if operation is None:
                raise ValueError(f"Contract operation removed: {method.upper()} {path}")
            if resolve_schema(document, current_path.get("parameters", [])) != resolve_schema(
                baseline, path_item.get("parameters", [])
            ) or operation_contract(document, operation) != operation_contract(
                baseline, path_item[method]
            ):
                raise ValueError(
                    f"Contract changed: {method.upper()} {path}; "
                    "review the HTTP 1.0 contract and its baseline."
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the validated schema as a CI artifact.")
    args = parser.parse_args()
    # openapi() does not execute lifespan: no model, dataset, server or credentials are needed.
    document = create_app().openapi()
    validate_contract(document, json.loads(BASELINE.read_text(encoding="utf-8")))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print("OpenAPI valid; HTTP 1.0 contract matches the reviewed baseline.")


if __name__ == "__main__":
    main()
