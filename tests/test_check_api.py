from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import check_api

PRIVATE_MARKER = "synthetic-private-response-marker"


def prediction_response():
    return {
        "contract_version": "1.0",
        "request_id": check_api.SYNTHETIC_REQUEST_ID,
        "model_version": PRIVATE_MARKER,
        "results": [
            {
                "prediction_id": str(uuid4()),
                "triage_class": "normal",
                "probabilities": {"normal": 0.8, "atencao": 0.1, "urgente": 0.1},
                "abstained": False,
            }
            for _ in check_api.SYNTHETIC_INSTANCES
        ],
    }


@pytest.fixture
def api_server(monkeypatch):
    monkeypatch.delenv("TRIAGE_API_TOKEN", raising=False)
    monkeypatch.setattr(check_api, "POLL_INTERVAL", 0.01)

    @contextmanager
    def serve(*, transform=None, degraded=0, delay=0, proxy_errors=0, trickle_interval=0):
        state = {"requests": [], "ready_calls": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length)) if length else None
                state["requests"].append((self.path, dict(self.headers), payload))
                status, content_type = 200, "application/json"
                if self.path.endswith("/health/ready"):
                    state["ready_calls"] += 1
                    time.sleep(delay)
                    loaded = state["ready_calls"] > degraded
                    body = {
                        "status": "ok" if loaded else "degraded",
                        "contract_version": "1.0",
                        "model_loaded": loaded,
                        "preprocessor_loaded": loaded,
                    }
                    if state["ready_calls"] <= proxy_errors:
                        status, content_type, body = 503, "text/html", PRIVATE_MARKER
                elif self.path.endswith("/health/live"):
                    body = {"status": "ok"}
                elif payload.get("instances"):
                    body = prediction_response()
                else:
                    status = 422
                    body = {
                        "error": {
                            "code": "INVALID_INPUT_SCHEMA",
                            "message": PRIVATE_MARKER,
                            "details": [{"loc": ["body"], "msg": PRIVATE_MARKER, "type": "x"}],
                        }
                    }
                headers = {}
                if transform:
                    status, body, headers = transform(self.path, status, body)
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(raw)))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    if trickle_interval:
                        for offset in range(len(raw)):
                            self.wfile.write(raw[offset : offset + 1])
                            self.wfile.flush()
                            time.sleep(trickle_interval)
                    else:
                        self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return serve


def test_verifies_contract_with_synthetic_batch_and_optional_gateway_token(
    api_server, monkeypatch, capsys
):
    token = "synthetic-gateway-secret"
    monkeypatch.setenv("TRIAGE_API_TOKEN", token)
    with api_server() as (base_url, state):
        report = check_api.verify_api(base_url + "/gateway/", timeout=2)
    assert report == {
        "status": "passed",
        "checks": dict.fromkeys(check_api.CHECKS, "passed"),
        "request_count": 4,
        "readiness_attempts": 1,
    }
    assert all(headers["Authorization"] == f"Bearer {token}" for _, headers, _ in state["requests"])
    posts = [payload for _, _, payload in state["requests"] if payload is not None]
    assert posts[0]["instances"] == check_api.SYNTHETIC_INSTANCES
    assert posts[1] == {"instances": []}
    assert PRIVATE_MARKER not in json.dumps(report)
    assert token not in json.dumps(report)
    assert capsys.readouterr().out == ""


def test_waits_for_loaded_artifacts_despite_http_200_degraded(api_server):
    with api_server(degraded=2) as (base_url, _state):
        report = check_api.verify_api(base_url, timeout=2)
    assert report["status"] == "passed"
    assert report["readiness_attempts"] == 3
    assert report["request_count"] == 6


def test_retries_transient_gateway_error_without_reading_html_body(api_server):
    with api_server(proxy_errors=1) as (base_url, _state):
        report = check_api.verify_api(base_url, timeout=2)
    assert report["status"] == "passed"
    assert report["readiness_attempts"] == 2


@pytest.mark.parametrize("flag", ["model_loaded", "preprocessor_loaded"])
def test_status_ok_without_loaded_artifacts_never_passes_readiness(api_server, flag):
    def transform(path, status, body):
        if path.endswith("/health/ready"):
            body[flag] = False
        return status, body, {}

    with api_server(transform=transform) as (base_url, state):
        with pytest.raises(check_api.CheckAPIError, match="deadline_exceeded") as exc:
            check_api.verify_api(base_url, timeout=0.08)
    assert exc.value.report["checks"]["health_ready"] == "failed"
    assert all(path.endswith("/health/ready") for path, _, _ in state["requests"])


def test_request_timeout_cannot_exceed_total_deadline(api_server):
    with api_server(delay=0.3) as (base_url, _state):
        started = time.monotonic()
        with pytest.raises(check_api.CheckAPIError, match="deadline_exceeded"):
            check_api.verify_api(base_url, timeout=0.08, request_timeout=5)
        elapsed = time.monotonic() - started
    assert elapsed < 0.5


def test_continuous_small_chunks_cannot_extend_total_deadline(api_server):
    # The complete body would take over two seconds, while every chunk arrives
    # within the socket timeout. A bulk read incorrectly waits for the whole body.
    with api_server(trickle_interval=0.025) as (base_url, _state):
        started = time.monotonic()
        with pytest.raises(check_api.CheckAPIError, match="deadline_exceeded"):
            check_api.verify_api(base_url, timeout=0.15, request_timeout=5)
        elapsed = time.monotonic() - started
    assert elapsed < 0.8


def test_incremental_reader_checks_absolute_deadline_after_each_chunk(monkeypatch):
    clock = [0.0]
    reads, timeouts = [], []
    headers = Message()
    headers["Content-Type"] = "application/json"

    def read1(size):
        reads.append(size)
        clock[0] += 0.05
        return b" "

    response = SimpleNamespace(
        headers=headers,
        read1=read1,
        fp=SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append))),
    )
    monkeypatch.setattr(check_api.time, "monotonic", lambda: clock[0])
    with pytest.raises(check_api.ResponseReadError, match="deadline_exceeded"):
        check_api.read_json_response(response, deadline=0.2)
    assert len(reads) == 4
    assert timeouts == pytest.approx([0.2, 0.15, 0.1, 0.05])
    assert clock[0] == 0.2


def test_incremental_reader_falls_back_to_single_byte_reads(monkeypatch):
    headers = Message()
    headers["Content-Type"] = "application/json"
    chunks = iter([b"{", b"}", b""])
    reads = []

    def read(size):
        reads.append(size)
        return next(chunks)

    response = SimpleNamespace(headers=headers, read=read)
    monkeypatch.setattr(check_api.time, "monotonic", lambda: 0)
    assert check_api.read_json_response(response, deadline=1) == {}
    assert reads == [1, 1, 1]


def test_request_budget_includes_opening_and_body_despite_long_global_deadline(monkeypatch):
    clock = [0.0]
    timeouts = []
    headers = Message()
    headers["Content-Type"] = "application/json"

    class Response:
        code = 200

        def __init__(self):
            self.headers = headers
            self.fp = SimpleNamespace(
                raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append))
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read1(self, _size):
            clock[0] += 3
            return b'{"status":"ok"}'

    def open_response(_request, *, timeout):
        assert timeout == 5
        clock[0] += 2  # Headers consume part of the same five-second request budget.
        return Response()

    monkeypatch.setattr(check_api.time, "monotonic", lambda: clock[0])
    checker = check_api._APICheck("https://example.invalid", timeout=120, request_timeout=5)
    checker.opener = SimpleNamespace(open=open_response)
    with pytest.raises(check_api.CheckAPIError, match="transport_error"):
        checker.request("/health/ready")
    assert timeouts == [3]
    assert clock[0] == 5
    assert checker.remaining() == 115


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.pop("request_id"),
        lambda body: body.update(request_id="unexpected-request"),
        lambda body: body.update(contract_version="2.0"),
        lambda body: body.update(model_version=""),
        lambda body: body["results"].pop(),
        lambda body: body["results"][0].update(triage_class="unknown"),
        lambda body: body["results"][0].update(abstained="false"),
        lambda body: body["results"][0].update(prediction_id="invalid"),
        lambda body: body["results"][1].update(prediction_id=body["results"][0]["prediction_id"]),
        lambda body: body["results"][0]["probabilities"].pop("urgente"),
        lambda body: body["results"][0]["probabilities"].update(normal=0.7),
        lambda body: body["results"][0]["probabilities"].update(normal=-0.1, atencao=1.0),
        lambda body: body["results"][0]["probabilities"].update(normal=float("nan")),
    ],
    ids=[
        "missing-field",
        "request-id",
        "contract-version",
        "model-version",
        "batch-size",
        "class-enum",
        "boolean-schema",
        "prediction-id",
        "duplicate-id",
        "missing-class",
        "probability-sum",
        "negative-probability",
        "non-finite-probability",
    ],
)
def test_rejects_malformed_prediction_and_semantic_contract_errors(api_server, mutation):
    def transform(path, status, body):
        if path.endswith("/v1/triage") and status == 200:
            mutation(body)
        return status, body, {}

    with api_server(transform=transform) as (base_url, _state):
        with pytest.raises(check_api.CheckAPIError) as exc:
            check_api.verify_api(base_url, timeout=2)
    assert exc.value.report["checks"]["triage"] == "failed"
    assert PRIVATE_MARKER not in str(exc.value)
    assert PRIVATE_MARKER not in json.dumps(exc.value.report)


def test_invalid_input_must_return_documented_error_schema(api_server):
    def transform(_path, status, body):
        if status == 422:
            body["error"]["code"] = "undocumented-error"
        return status, body, {}

    with api_server(transform=transform) as (base_url, _state):
        with pytest.raises(check_api.CheckAPIError, match="response_contract_mismatch") as exc:
            check_api.verify_api(base_url, timeout=2)
    assert exc.value.report["checks"]["invalid_input"] == "failed"


@pytest.mark.parametrize(
    "raw",
    [b"not-json-private-response", b"[", b"x" * 65_537],
    ids=["text", "malformed-json", "oversized"],
)
def test_invalid_and_oversized_response_bodies_are_never_logged(api_server, raw, capsys):
    def transform(_path, status, _body):
        return status, raw, {}

    with api_server(transform=transform) as (base_url, _state):
        with pytest.raises(check_api.CheckAPIError) as exc:
            check_api.verify_api(base_url, timeout=2)
    assert raw[:20].decode() not in str(exc.value)
    assert capsys.readouterr().out == ""


def test_refuses_redirect_instead_of_forwarding_gateway_credentials(api_server, monkeypatch):
    def transform(_path, _status, _body):
        return 302, {}, {"Location": destination_url + "/health/ready"}

    with api_server() as (destination_url, destination_state):
        with api_server(transform=transform) as (base_url, _state):
            monkeypatch.setenv("TRIAGE_API_TOKEN", "synthetic-secret-token")
            with pytest.raises(check_api.CheckAPIError, match="unexpected_http_status"):
                check_api.verify_api(base_url, timeout=2)
        assert destination_state["requests"] == []


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/test",
        "https://user:secret@example.com",
        "https://x/?token=secret",
        "https://x/#secret",
        "http://[bad",
        "http://",
    ],
)
def test_rejects_credential_or_invalid_urls_without_echoing_them(base_url):
    with pytest.raises(check_api.CheckAPIError, match="invalid_configuration") as exc:
        check_api.verify_api(base_url)
    assert base_url not in str(exc.value)
    assert base_url not in json.dumps(exc.value.report)


def test_cli_writes_only_sanitized_report_on_failure(api_server, monkeypatch, tmp_path, capsys):
    def transform(_path, _status, _body):
        return 401, {"error": PRIVATE_MARKER}, {}

    output = tmp_path / "reports" / "smoke.json"
    with api_server(transform=transform) as (base_url, _state):
        monkeypatch.setattr(
            "sys.argv", ["check_api", "--base-url", base_url, "--output", str(output)]
        )
        assert check_api.main() == 1
    serialized = output.read_text()
    assert json.loads(serialized)["status"] == "failed"
    assert PRIVATE_MARKER not in serialized
    assert base_url not in serialized
    assert capsys.readouterr().out == "API checks failed.\n"


def test_cli_success_exit_code_and_report(api_server, monkeypatch, tmp_path, capsys):
    output = tmp_path / "smoke.json"
    with api_server() as (base_url, _state):
        monkeypatch.setattr(
            "sys.argv", ["check_api", "--base-url", base_url, "--output", str(output)]
        )
        assert check_api.main() == 0
    assert json.loads(output.read_text())["status"] == "passed"
    assert capsys.readouterr().out == "API checks passed.\n"
