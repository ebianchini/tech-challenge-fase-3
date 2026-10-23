from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from email.message import Message
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

from scripts import check_grafana

PRIVATE_MARKER = "synthetic-private-server-data"
SOURCE = {"type": "prometheus", "uid": check_grafana.DATASOURCE_UID}
SOURCE_PATH = f"/api/datasources/uid/{check_grafana.DATASOURCE_UID}"
DASHBOARD_PATH = f"/api/dashboards/uid/{check_grafana.DASHBOARD_UID}"


def dashboard_response():
    return {
        "meta": {"provisioned": True},
        "dashboard": {
            "uid": check_grafana.DASHBOARD_UID,
            "panels": [
                {
                    "id": 1,
                    "type": "stat",
                    "datasource": SOURCE,
                    "targets": [
                        {
                            "refId": "A",
                            "expr": 'sum(up{job="triage-api",instance=~"${instance:regex}"})',
                            "instant": True,
                            "range": False,
                        }
                    ],
                },
                {
                    "id": 2,
                    "type": "timeseries",
                    "datasource": SOURCE,
                    "targets": [
                        {
                            "refId": "A",
                            "expr": "rate(triage_http_requests_total[$__rate_interval])",
                            "instant": False,
                            "range": True,
                        },
                        {
                            "refId": "B",
                            "datasource": SOURCE,
                            "expr": "increase(triage_http_requests_total[$__range])",
                            "instant": False,
                            "range": True,
                        },
                    ],
                },
            ],
        },
    }


@pytest.fixture
def grafana_transport(monkeypatch):
    monkeypatch.delenv("GRAFANA_ADMIN_USER", raising=False)
    monkeypatch.delenv("GRAFANA_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(check_grafana, "POLL_INTERVAL", 0.001)

    def setup(*, transform=None, ready_after=0):
        state = {"requests": [], "health_calls": 0}

        class Response(io.BytesIO):
            def __init__(self, status, body, content_type="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                super().__init__(raw)
                self.code = status
                self.headers = Message()
                self.headers["Content-Type"] = content_type

        def open_response(request, *, timeout):
            path = urlsplit(request.full_url).path
            payload = json.loads(request.data) if request.data is not None else None
            state["requests"].append((path, dict(request.header_items()), payload, timeout))
            if path == "/api/health":
                state["health_calls"] += 1
                body = {
                    "database": "ok" if state["health_calls"] > ready_after else "starting",
                    "version": "13.2.4",
                }
            elif path == SOURCE_PATH:
                body = {
                    **SOURCE,
                    "url": check_grafana.DEFAULT_PROMETHEUS_URL,
                    "readOnly": True,
                }
            elif path == SOURCE_PATH + "/health":
                body = {"status": "OK", "message": PRIVATE_MARKER}
            elif path == DASHBOARD_PATH:
                body = dashboard_response()
            elif path == "/api/ds/query":
                body = {
                    "results": {
                        query["refId"]: {"frames": [], "status": 200}
                        for query in payload["queries"]
                    }
                }
            else:
                raise AssertionError("unexpected request path")
            status, content_type = 200, "application/json"
            if transform:
                status, body, content_type = transform(path, status, deepcopy(body), payload)
            return Response(status, body, content_type)

        monkeypatch.setattr(
            check_grafana, "build_opener", lambda *_args: SimpleNamespace(open=open_response)
        )
        return state

    return setup


def test_checks_provisioning_and_executes_every_panel_query_without_requiring_data(
    grafana_transport, capsys
):
    state = grafana_transport()
    report = check_grafana.verify_grafana(timeout=2)
    assert report == {
        "status": "passed",
        "checks": dict.fromkeys(check_grafana.CHECKS, "passed"),
        "request_count": 6,
        "readiness_attempts": 1,
        "panel_count": 2,
        "query_count": 3,
    }
    posts = [payload for _, _, payload, _ in state["requests"] if payload is not None]
    assert len(posts) == 2
    assert posts[0]["queries"][0]["expr"] == 'sum(up{job="triage-api",instance=~".*"})'
    assert posts[0]["queries"][0]["instant"] is True
    assert posts[1]["queries"][0]["expr"] == "rate(triage_http_requests_total[1m])"
    assert posts[1]["queries"][1]["expr"] == "increase(triage_http_requests_total[1h])"
    assert posts[1]["queries"][0]["range"] is True
    assert all(query["datasource"] == SOURCE for post in posts for query in post["queries"])
    assert all(post["from"] == "now-1h" and post["to"] == "now" for post in posts)
    assert PRIVATE_MARKER not in json.dumps(report)
    assert capsys.readouterr().out == ""


def test_uses_environment_credentials_and_allows_explicit_overrides(grafana_transport, monkeypatch):
    monkeypatch.setenv("GRAFANA_ADMIN_USER", "demo-operator")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", "synthetic-secret-password")
    for credentials, expected in [
        ({}, "demo-operator:synthetic-secret-password"),
        ({"username": "override", "password": "override-secret"}, "override:override-secret"),
    ]:
        state = grafana_transport()
        # The fixture resets envs only when it is created, before this test sets them.
        report = check_grafana.verify_grafana(timeout=2, **credentials)
        expected_header = "Basic " + base64.b64encode(expected.encode()).decode()
        assert all(h["Authorization"] == expected_header for _, h, _, _ in state["requests"])
        assert expected not in json.dumps(report)


def test_waits_for_database_and_for_file_provisioning(grafana_transport):
    calls = {SOURCE_PATH: 0, DASHBOARD_PATH: 0}

    def transform(path, status, body, _payload):
        if path in calls:
            calls[path] += 1
            if calls[path] == 1:
                return 404, {"message": PRIVATE_MARKER}, "text/plain"
        return status, body, "application/json"

    state = grafana_transport(transform=transform, ready_after=2)
    report = check_grafana.verify_grafana(timeout=2)
    assert report["readiness_attempts"] == 3
    assert report["request_count"] == 10
    assert state["health_calls"] == 3
    assert calls == {SOURCE_PATH: 2, DASHBOARD_PATH: 2}


@pytest.mark.parametrize("path", [SOURCE_PATH, DASHBOARD_PATH])
def test_missing_provisioning_cannot_pass(grafana_transport, path):
    def transform(actual, status, body, _payload):
        if actual == path:
            return 404, {"message": PRIVATE_MARKER}, "application/json"
        return status, body, "application/json"

    grafana_transport(transform=transform)
    with pytest.raises(check_grafana.CheckGrafanaError, match="deadline_exceeded") as exc:
        check_grafana.verify_grafana(timeout=0.015)
    assert exc.value.report["checks"]["datasource" if path == SOURCE_PATH else "dashboard"] == (
        "failed"
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda body: body.update(uid="other"), "datasource_mismatch"),
        (lambda body: body.update(type="loki"), "datasource_mismatch"),
        (lambda body: body.update(url="http://wrong:9090"), "datasource_url_mismatch"),
        (lambda body: body.update(readOnly=False), "datasource_not_provisioned"),
    ],
    ids=["uid", "plugin", "url", "editable"],
)
def test_rejects_wrong_or_manually_created_datasource(grafana_transport, mutation, reason):
    def transform(path, status, body, _payload):
        if path == SOURCE_PATH:
            mutation(body)
        return status, body, "application/json"

    grafana_transport(transform=transform)
    with pytest.raises(check_grafana.CheckGrafanaError, match=reason):
        check_grafana.verify_grafana(timeout=2)


def test_accepts_overridden_prometheus_url_for_native_operational_check(grafana_transport):
    def transform(path, status, body, _payload):
        if path == SOURCE_PATH:
            body["url"] = "http://127.0.0.1:19090/"
        return status, body, "application/json"

    grafana_transport(transform=transform)
    assert check_grafana.verify_grafana(prometheus_url="http://127.0.0.1:19090")["status"] == (
        "passed"
    )


@pytest.mark.parametrize("status", [404, 405, 501])
def test_optional_plugin_health_support_does_not_skip_real_queries(grafana_transport, status):
    def transform(path, current_status, body, _payload):
        if path == SOURCE_PATH + "/health":
            return status, {}, "text/plain"
        return current_status, body, "application/json"

    grafana_transport(transform=transform)
    report = check_grafana.verify_grafana(timeout=2)
    assert report["status"] == "passed"
    assert report["checks"]["datasource_health"] == "unsupported"
    assert report["query_count"] == 3


def test_rejects_unhealthy_datasource_even_if_grafana_itself_is_healthy(grafana_transport):
    def transform(path, status, body, _payload):
        if path == SOURCE_PATH + "/health":
            body.update(status="ERROR", message=PRIVATE_MARKER)
        return status, body, "application/json"

    grafana_transport(transform=transform)
    with pytest.raises(check_grafana.CheckGrafanaError, match="datasource_unhealthy") as exc:
        check_grafana.verify_grafana(timeout=2)
    assert exc.value.report["checks"]["datasource_health"] == "failed"
    assert PRIVATE_MARKER not in str(exc.value)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda body: body["meta"].update(provisioned=False), "dashboard_not_provisioned"),
        (lambda body: body["dashboard"].update(uid="different"), "dashboard_uid_mismatch"),
        (lambda body: body["dashboard"].update(panels=[]), "panel_queries_missing"),
        (
            lambda body: body["dashboard"]["panels"][0].pop("datasource"),
            "panel_datasource_mismatch",
        ),
        (
            lambda body: body["dashboard"]["panels"][1]["targets"][1].update(
                datasource={"type": "prometheus", "uid": "missing-source"}
            ),
            "panel_datasource_mismatch",
        ),
        (
            lambda body: body["dashboard"]["panels"][0]["targets"][0].update(expr=""),
            "malformed_panel_query",
        ),
        (
            lambda body: body["dashboard"]["panels"][1]["targets"][1].update(refId="A"),
            "malformed_panel_query",
        ),
        (
            lambda body: body["dashboard"]["panels"][0]["targets"][0].update(
                expr='up{instance=~"$unsupported"}'
            ),
            "unsupported_dashboard_variable",
        ),
    ],
    ids=["manual", "uid", "empty", "missing-source", "target-source", "expr", "ref", "variable"],
)
def test_rejects_broken_dashboard_provisioning_or_panels(grafana_transport, mutation, reason):
    def transform(path, status, body, _payload):
        if path == DASHBOARD_PATH:
            mutation(body)
        return status, body, "application/json"

    grafana_transport(transform=transform)
    with pytest.raises(check_grafana.CheckGrafanaError, match=reason) as exc:
        check_grafana.verify_grafana(timeout=2)
    assert exc.value.report["checks"]["dashboard"] == "failed"


def test_visits_nested_rows_and_ignores_hidden_queries(grafana_transport):
    def transform(path, status, body, _payload):
        if path == DASHBOARD_PATH:
            panels = body["dashboard"]["panels"]
            panels[0]["targets"].append(
                {"refId": "hidden", "expr": "invalid-unused-promql", "hide": True}
            )
            body["dashboard"]["panels"] = [{"type": "row", "panels": panels}]
        return status, body, "application/json"

    grafana_transport(transform=transform)
    report = check_grafana.verify_grafana(timeout=2)
    assert report["panel_count"] == 2
    assert report["query_count"] == 3


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "malformed_query_response"),
        ({"results": {}}, "malformed_query_response"),
        ({"results": {"A": {"error": PRIVATE_MARKER, "frames": []}}}, "panel_query_failed"),
        ({"results": {"A": {"status": 500, "frames": []}}}, "panel_query_failed"),
        ({"results": {"A": {"frames": None}}}, "malformed_query_response"),
    ],
    ids=["no-results", "missing-ref", "plugin-error", "failed-status", "malformed-frames"],
)
def test_rejects_query_errors_inside_http_200(grafana_transport, body, reason):
    def transform(path, status, response_body, _payload):
        return status, body if path == "/api/ds/query" else response_body, "application/json"

    grafana_transport(transform=transform)
    with pytest.raises(check_grafana.CheckGrafanaError, match=reason) as exc:
        check_grafana.verify_grafana(timeout=2)
    assert exc.value.report["checks"]["panel_queries"] == "failed"
    assert PRIVATE_MARKER not in json.dumps(exc.value.report)


@pytest.mark.parametrize("status", [401, 403, 400, 500, 302])
def test_http_failures_never_include_server_content(grafana_transport, status, capsys):
    def transform(path, current_status, body, _payload):
        if path == SOURCE_PATH:
            return status, {"message": PRIVATE_MARKER}, "text/html"
        return current_status, body, "application/json"

    grafana_transport(transform=transform)
    with pytest.raises(check_grafana.CheckGrafanaError) as exc:
        check_grafana.verify_grafana(password="synthetic-password", timeout=2)
    assert PRIVATE_MARKER not in str(exc.value)
    assert "synthetic-password" not in json.dumps(exc.value.report)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("body", "content_type", "reason"),
    [
        (b"not-json", "application/json", "invalid_json"),
        (b"{}", "text/html", "invalid_content_type"),
        (b"x" * (check_grafana.MAX_RESPONSE_BYTES + 1), "application/json", "response_too_large"),
        ([], "application/json", "malformed_response"),
    ],
    ids=["json", "mime", "too-large", "shape"],
)
def test_malformed_responses_are_bounded_and_sanitized(
    grafana_transport, body, content_type, reason
):
    grafana_transport(transform=lambda *_args: (200, body, content_type))
    with pytest.raises(check_grafana.CheckGrafanaError, match=reason):
        check_grafana.verify_grafana(timeout=2)


def test_transport_error_retries_health_within_global_deadline(grafana_transport, monkeypatch):
    state = grafana_transport()
    ordinary_opener = check_grafana.build_opener()
    calls = [0]

    def open_response(request, *, timeout):
        calls[0] += 1
        if calls[0] == 1:
            raise URLError(PRIVATE_MARKER)
        return ordinary_opener.open(request, timeout=timeout)

    monkeypatch.setattr(
        check_grafana, "build_opener", lambda *_args: SimpleNamespace(open=open_response)
    )
    report = check_grafana.verify_grafana(timeout=2)
    assert report["status"] == "passed"
    assert report["readiness_attempts"] == 2
    assert state["health_calls"] == 1
    assert "reason" not in report


def test_request_and_chunk_reads_cannot_extend_total_budget(monkeypatch):
    clock = [0.0]
    timeouts = []
    checker = check_grafana._GrafanaCheck(
        "http://127.0.0.1:3000", "admin", "secret", None, 0.2, 5
    )
    checker.deadline = 0.2
    monkeypatch.setattr(check_grafana.time, "monotonic", lambda: clock[0])
    headers = Message()
    headers["Content-Type"] = "application/json"

    def read1(_size):
        clock[0] += 0.05
        return b" "

    response = SimpleNamespace(
        headers=headers,
        read1=read1,
        fp=SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append))),
    )
    with pytest.raises(check_grafana.CheckGrafanaError, match="deadline_exceeded"):
        checker.read_json(response, deadline=0.2)
    assert timeouts == pytest.approx([0.2, 0.15, 0.1, 0.05])
    assert clock[0] == 0.2


@pytest.mark.parametrize(
    "configuration",
    [
        {"base_url": "file:///tmp/test"},
        {"base_url": "http://user:secret@example.com"},
        {"base_url": "http://example.com/?token=secret"},
        {"base_url": "http://[bad"},
        {"prometheus_url": "http://prometheus:bad"},
        {"timeout": float("nan")},
        {"request_timeout": 0},
        {"username": "invalid:user"},
        {"password": ""},
    ],
)
def test_refuses_invalid_configuration_without_echoing_it(configuration):
    with pytest.raises(check_grafana.CheckGrafanaError, match="invalid_configuration") as exc:
        check_grafana.verify_grafana(**configuration)
    assert "secret" not in json.dumps(exc.value.report)


def test_redirect_handler_does_not_forward_authorization():
    redirect = check_grafana._NoRedirects().redirect_request(None, None, 302, "", {}, "http://x")
    assert redirect is None


@pytest.mark.parametrize("successful", [True, False])
def test_cli_writes_sanitized_report_and_exit_code(
    grafana_transport, monkeypatch, tmp_path, capsys, successful
):
    def transform(path, status, body, _payload):
        if not successful and path == SOURCE_PATH:
            return 401, {"message": PRIVATE_MARKER}, "text/html"
        return status, body, "application/json"

    grafana_transport(transform=transform)
    password = "synthetic-cli-secret"
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", password)
    output = tmp_path / "reports" / "grafana.json"
    monkeypatch.setattr("sys.argv", ["check_grafana", "--output", str(output)])
    assert check_grafana.main() == (0 if successful else 1)
    serialized = output.read_text()
    expected_status = "passed" if successful else "failed"
    assert json.loads(serialized)["status"] == expected_status
    assert PRIVATE_MARKER not in serialized
    assert password not in serialized
    assert capsys.readouterr().out == f"Grafana checks {expected_status}.\n"
