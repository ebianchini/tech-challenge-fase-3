"""Smoke and HTTP contract checks using synthetic requests and sanitized reports only."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from jsonschema import Draft202012Validator

BASELINE = Path(__file__).resolve().parents[1] / "docs" / "openapi-contract.json"
MAX_RESPONSE_BYTES = 65_536
POLL_INTERVAL = 0.25
CHECKS = ("health_ready", "health_live", "triage", "invalid_input")
SYNTHETIC_REQUEST_ID = "cd-smoke-synthetic"
SYNTHETIC_INSTANCES = [
    {"report_text": f"synthetic signal {name}"} for name in ("alpha", "beta", "gamma")
]


class CheckAPIError(RuntimeError):
    """Failure with a fixed reason code and a report containing no server data."""

    def __init__(self, code: str, report: dict[str, Any]):
        super().__init__(code)
        self.report = report


class ResponseReadError(RuntimeError):
    """A fixed, sanitized response read failure code."""


def read_json_response(response: Any, deadline: float) -> Any:
    """Read bounded JSON incrementally so a trickling body cannot extend the deadline.

    urllib's ordinary read() can perform many socket reads, each restarting its
    timeout. read1() performs at most one buffered/raw read, letting us check the
    absolute deadline between chunks. The HTTPResponse socket is available on the
    usual urllib transports; updating it also bounds a final stalled read by the
    remaining budget. Other file-like responses safely fall back to one-byte reads.
    """
    try:
        if response.headers.get_content_type() != "application/json":
            raise ResponseReadError("invalid_content_type")
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseReadError("deadline_exceeded")
            stream = response
            for _ in range(2):
                # HTTPError wraps HTTPResponse in .fp; HTTPResponse itself has a
                # buffered .fp whose raw transport owns the HTTP/HTTPS socket.
                buffered = getattr(stream, "fp", None)
                transport = getattr(getattr(buffered, "raw", None), "_sock", None)
                if transport is not None:
                    transport.settimeout(remaining)
                    break
                stream = buffered
            read1 = getattr(response, "read1", None)
            chunk = (
                read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if callable(read1)
                else response.read(1)
            )
            if time.monotonic() >= deadline:
                raise ResponseReadError("deadline_exceeded")
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ResponseReadError("response_too_large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ResponseReadError("invalid_json") from None
        if time.monotonic() >= deadline:
            raise ResponseReadError("deadline_exceeded")
        return body
    except (OSError, URLError, ValueError, HTTPException):
        raise ResponseReadError("transport_error") from None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an optional gateway token to an HTTP redirect destination.
        return None


class _APICheck:
    def __init__(self, base_url: str, timeout: float, request_timeout: float):
        self.report: dict[str, Any] = {
            "status": "failed",
            "checks": dict.fromkeys(CHECKS, "pending"),
            "request_count": 0,
            "readiness_attempts": 0,
        }
        self.current_check = "health_ready"
        try:
            parsed = urlsplit(base_url)
        except ValueError:
            self.fail("invalid_configuration")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(request_timeout)
            or request_timeout <= 0
        ):
            self.fail("invalid_configuration")
        self.base_url = base_url.rstrip("/")
        self.deadline = time.monotonic() + timeout
        self.request_timeout = request_timeout
        self.opener = build_opener(_NoRedirects())
        self.headers = {"Accept": "application/json", "X-Request-ID": SYNTHETIC_REQUEST_ID}
        token = os.environ.get("TRIAGE_API_TOKEN")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        try:
            self.contract = json.loads(BASELINE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.fail("contract_unavailable")

    def fail(self, code: str) -> None:
        self.report["checks"][self.current_check] = "failed"
        raise CheckAPIError(code, self.report) from None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.fail("deadline_exceeded")
        return remaining

    def request(self, path: str, payload: dict | None = None) -> tuple[int, Any]:
        budget = min(self.request_timeout, self.remaining())
        request_deadline = min(self.deadline, time.monotonic() + budget)
        headers = dict(self.headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="GET" if payload is None else "POST",
        )
        self.report["request_count"] += 1
        try:
            try:
                response = self.opener.open(request, timeout=budget)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                # Proxy errors and redirects need no body. Only 422 has an error
                # response that is intentionally part of the public contract check.
                if status >= 300 and status != 422:
                    return status, None
                try:
                    body = read_json_response(response, request_deadline)
                except ResponseReadError as exc:
                    self.remaining()
                    self.fail("transport_error" if str(exc) == "deadline_exceeded" else str(exc))
                return status, body
        except (OSError, URLError, ValueError, HTTPException):
            self.fail("transport_error")

    def validate(self, path: str, method: str, status: int, body: Any) -> None:
        try:
            schema = self.contract["paths"][path][method]["responses"][str(status)]["content"][
                "application/json"
            ]["schema"]
            validator = Draft202012Validator({**schema, "components": self.contract["components"]})
            if not validator.is_valid(body):
                self.fail("response_contract_mismatch")
        except (KeyError, TypeError):
            self.fail("contract_unavailable")

    def wait_ready(self) -> None:
        while True:
            self.report["readiness_attempts"] += 1
            try:
                status, body = self.request("/health/ready")
            except CheckAPIError as exc:
                if str(exc) != "transport_error":
                    raise
                self.report["checks"][self.current_check] = "pending"
            else:
                if status == 200:
                    self.validate("/health/ready", "get", status, body)
                    if (
                        body["status"] == "ok"
                        and body["contract_version"] == "1.0"
                        and body["model_loaded"] is True
                        and body["preprocessor_loaded"] is True
                    ):
                        self.report["checks"][self.current_check] = "passed"
                        return
                elif status not in {502, 503, 504}:
                    self.fail("unexpected_http_status")
            time.sleep(min(POLL_INTERVAL, self.remaining()))

    def run(self) -> dict[str, Any]:
        self.wait_ready()
        self.current_check = "health_live"
        status, body = self.request("/health/live")
        if status != 200:
            self.fail("unexpected_http_status")
        self.validate("/health/live", "get", status, body)
        if body.get("status") != "ok":
            self.fail("liveness_failed")
        self.report["checks"][self.current_check] = "passed"

        self.current_check = "triage"
        status, body = self.request(
            "/v1/triage",
            {
                "contract_version": "1.0",
                "request_id": SYNTHETIC_REQUEST_ID,
                "instances": SYNTHETIC_INSTANCES,
            },
        )
        if status != 200:
            self.fail("unexpected_http_status")
        self.validate("/v1/triage", "post", status, body)
        if (
            body["contract_version"] != "1.0"
            or body["request_id"] != SYNTHETIC_REQUEST_ID
            or not body["model_version"].strip()
            or len(body["results"]) != len(SYNTHETIC_INSTANCES)
        ):
            self.fail("prediction_metadata_mismatch")
        prediction_ids = []
        classes = set(self.contract["components"]["schemas"]["TriageClass"]["enum"])
        for result in body["results"]:
            try:
                UUID(result["prediction_id"])
            except ValueError:
                self.fail("invalid_prediction_id")
            prediction_ids.append(result["prediction_id"])
            probabilities = result["probabilities"]
            if (
                set(probabilities) != classes
                or any(not 0 <= p <= 1 or not math.isfinite(p) for p in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-6)
            ):
                self.fail("invalid_probabilities")
        if len(set(prediction_ids)) != len(prediction_ids):
            self.fail("duplicate_prediction_ids")
        self.report["checks"][self.current_check] = "passed"

        self.current_check = "invalid_input"
        status, body = self.request("/v1/triage", {"instances": []})
        if status != 422:
            self.fail("unexpected_http_status")
        self.validate("/v1/triage", "post", status, body)
        if body["error"]["code"] != "INVALID_INPUT_SCHEMA":
            self.fail("invalid_input_error_mismatch")
        self.report["checks"][self.current_check] = "passed"
        self.report["status"] = "passed"
        return self.report


def verify_api(
    base_url: str, *, timeout: float = 120, request_timeout: float = 5
) -> dict[str, Any]:
    """Check readiness, liveness, synthetic inference and the reviewed HTTP contract.

    ``timeout`` is the total budget, including readiness retries. The optional
    TRIAGE_API_TOKEN environment variable is sent as a Bearer gateway credential.
    Exceptions and reports deliberately omit URLs, headers and response contents.
    """
    return _APICheck(base_url, timeout, request_timeout).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--request-timeout", type=float, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = verify_api(
            args.base_url, timeout=args.timeout, request_timeout=args.request_timeout
        )
    except CheckAPIError as exc:
        report = exc.report
    except Exception:
        # Even an unexpected parser/library error must not expose response data in CI logs.
        report = {"status": "failed", "checks": {}, "request_count": 0, "readiness_attempts": 0}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"API checks {report['status']}.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
