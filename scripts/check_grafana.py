"""Check the local provisioned Grafana dashboard without recording credentials or data."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import time
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DATASOURCE_UID = "triage-prometheus"
DASHBOARD_UID = "triage-overview"
DEFAULT_PROMETHEUS_URL = "http://prometheus:9090"
MAX_RESPONSE_BYTES = 2_097_152
POLL_INTERVAL = 0.25
CHECKS = ("health", "datasource", "datasource_health", "dashboard", "panel_queries")


class CheckGrafanaError(RuntimeError):
    """A fixed failure code and a report containing no server or credential data."""

    def __init__(self, code: str, report: dict[str, Any]):
        super().__init__(code)
        self.report = report


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward admin credentials to a redirect destination.
        return None


def _valid_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


class _GrafanaCheck:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        prometheus_url: str | None,
        timeout: float,
        request_timeout: float,
    ):
        self.report: dict[str, Any] = {
            "status": "failed",
            "checks": dict.fromkeys(CHECKS, "pending"),
            "request_count": 0,
            "readiness_attempts": 0,
            "panel_count": 0,
            "query_count": 0,
        }
        self.current_check = "health"
        if (
            not _valid_url(base_url)
            or (prometheus_url is not None and not _valid_url(prometheus_url))
            or not username
            or ":" in username
            or not password
            or not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(request_timeout)
            or request_timeout <= 0
        ):
            self.fail("invalid_configuration")
        self.base_url = base_url.rstrip("/")
        self.prometheus_url = None if prometheus_url is None else prometheus_url.rstrip("/")
        self.deadline = time.monotonic() + timeout
        self.request_timeout = request_timeout
        self.opener = build_opener(_NoRedirects())
        credential = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self.headers = {"Accept": "application/json", "Authorization": f"Basic {credential}"}

    def fail(self, code: str) -> None:
        self.report["checks"][self.current_check] = "failed"
        self.report["reason"] = code
        raise CheckGrafanaError(code, self.report) from None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.fail("deadline_exceeded")
        return remaining

    def pause(self) -> None:
        time.sleep(min(POLL_INTERVAL, self.remaining()))

    def read_json(self, response: Any, deadline: float) -> Any:
        if response.headers.get_content_type() != "application/json":
            self.fail("invalid_content_type")
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.remaining()
                self.fail("transport_error")
            stream = response
            for _ in range(2):
                buffered = getattr(stream, "fp", None)
                transport = getattr(getattr(buffered, "raw", None), "_sock", None)
                if transport is not None:
                    transport.settimeout(remaining)
                    break
                stream = buffered
            # read1 performs at most one socket read, allowing an absolute
            # deadline even when a server keeps trickling small chunks.
            read1 = getattr(response, "read1", None)
            chunk = (
                read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if callable(read1)
                else response.read(1)
            )
            if time.monotonic() >= deadline:
                self.remaining()
                self.fail("transport_error")
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                self.fail("response_too_large")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            self.fail("invalid_json")

    def request(self, path: str, payload: dict | None = None) -> tuple[int, Any]:
        budget = min(self.request_timeout, self.remaining())
        request_deadline = min(self.deadline, time.monotonic() + budget)
        headers = dict(self.headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode(),
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
                if response.code != 200:
                    return response.code, None
                return response.code, self.read_json(response, request_deadline)
        except (OSError, URLError, ValueError, HTTPException):
            self.remaining()
            self.fail("transport_error")

    def require_ok(self, status: int) -> None:
        if status == 401:
            self.fail("authentication_failed")
        if status == 403:
            self.fail("permission_denied")
        if status != 200:
            self.fail("unexpected_http_status")

    def wait_health(self) -> None:
        while True:
            self.report["readiness_attempts"] += 1
            try:
                status, body = self.request("/api/health")
            except CheckGrafanaError as exc:
                if str(exc) != "transport_error":
                    raise
                self.report["checks"][self.current_check] = "pending"
                self.report.pop("reason", None)
            else:
                if status == 200:
                    if not isinstance(body, dict) or not isinstance(body.get("database"), str):
                        self.fail("malformed_response")
                    if body["database"] == "ok":
                        self.report["checks"][self.current_check] = "passed"
                        return
                elif status not in {502, 503, 504}:
                    self.require_ok(status)
            self.pause()

    def wait_resource(self, path: str) -> dict[str, Any]:
        # Provisioning can finish after HTTP health is already available.
        while True:
            status, body = self.request(path)
            if status in {404, 502, 503, 504}:
                self.pause()
                continue
            self.require_ok(status)
            if not isinstance(body, dict):
                self.fail("malformed_response")
            return body

    def datasource(self) -> None:
        self.current_check = "datasource"
        body = self.wait_resource(f"/api/datasources/uid/{DATASOURCE_UID}")
        if body.get("uid") != DATASOURCE_UID or body.get("type") != "prometheus":
            self.fail("datasource_mismatch")
        if body.get("readOnly") is not True:
            self.fail("datasource_not_provisioned")
        if not isinstance(body.get("url"), str) or (
            self.prometheus_url is not None
            and body["url"].rstrip("/") != self.prometheus_url
        ):
            self.fail("datasource_url_mismatch")
        self.report["checks"][self.current_check] = "passed"

        self.current_check = "datasource_health"
        status, body = self.request(f"/api/datasources/uid/{DATASOURCE_UID}/health")
        if status in {404, 405, 501}:
            # Plugin health support is optional. Actual dashboard queries below
            # remain mandatory, including when this optional endpoint is absent.
            self.report["checks"][self.current_check] = "unsupported"
            return
        self.require_ok(status)
        if not isinstance(body, dict) or body.get("status") != "OK":
            self.fail("datasource_unhealthy")
        self.report["checks"][self.current_check] = "passed"

    def query_expression(self, expression: str) -> str:
        # /api/ds/query receives frontend-interpolated PromQL. Exercise the
        # dashboard's All-instances selection over a fixed, bounded one-hour view.
        replacements = {
            "${instance:regex}": ".*",
            "${instance}": ".*",
            "$instance": ".*",
            "$__rate_interval": "1m",
            "$__range_s": "3600",
            "$__range_ms": "3600000",
            "$__range": "1h",
            "$__interval_ms": "15000",
            "$__interval": "15s",
        }
        for variable, value in replacements.items():
            expression = expression.replace(variable, value)
        if re.search(r"\$(?:[A-Za-z_]|\{)", expression):
            self.fail("unsupported_dashboard_variable")
        return expression

    def panel_queries(self, panels: Any) -> list[list[dict[str, Any]]]:
        if not isinstance(panels, list):
            self.fail("malformed_dashboard")
        batches = []
        for panel in panels:
            if not isinstance(panel, dict):
                self.fail("malformed_dashboard")
            if panel.get("type") == "row":
                batches.extend(self.panel_queries(panel.get("panels", [])))
                continue
            self.report["panel_count"] += 1
            targets = panel.get("targets")
            if not isinstance(targets, list) or not targets:
                self.fail("panel_queries_missing")
            queries, references = [], set()
            for target in targets:
                if not isinstance(target, dict):
                    self.fail("malformed_dashboard")
                source = target.get("datasource", panel.get("datasource"))
                if not isinstance(source, dict) or source != {
                    "type": "prometheus",
                    "uid": DATASOURCE_UID,
                }:
                    self.fail("panel_datasource_mismatch")
                if target.get("hide") is True:
                    continue
                expression, reference = target.get("expr"), target.get("refId")
                if (
                    not isinstance(expression, str)
                    or not expression.strip()
                    or not isinstance(reference, str)
                    or not reference
                    or reference in references
                ):
                    self.fail("malformed_panel_query")
                references.add(reference)
                queries.append(
                    {
                        **target,
                        "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                        "expr": self.query_expression(expression),
                        "intervalMs": 15000,
                        "maxDataPoints": 240,
                    }
                )
            if not queries:
                self.fail("panel_queries_missing")
            batches.append(queries)
        return batches

    def dashboard(self) -> list[list[dict[str, Any]]]:
        self.current_check = "dashboard"
        body = self.wait_resource(f"/api/dashboards/uid/{DASHBOARD_UID}")
        dashboard, meta = body.get("dashboard"), body.get("meta")
        if not isinstance(dashboard, dict) or not isinstance(meta, dict):
            self.fail("malformed_dashboard")
        if dashboard.get("uid") != DASHBOARD_UID:
            self.fail("dashboard_uid_mismatch")
        if meta.get("provisioned") is not True:
            self.fail("dashboard_not_provisioned")
        queries = self.panel_queries(dashboard.get("panels"))
        if not queries:
            self.fail("panel_queries_missing")
        self.report["checks"][self.current_check] = "passed"
        return queries

    def run(self) -> dict[str, Any]:
        self.wait_health()
        self.datasource()
        batches = self.dashboard()
        self.current_check = "panel_queries"
        for queries in batches:
            status, body = self.request(
                "/api/ds/query", {"from": "now-1h", "to": "now", "queries": queries}
            )
            self.require_ok(status)
            if not isinstance(body, dict) or not isinstance(body.get("results"), dict):
                self.fail("malformed_query_response")
            for query in queries:
                result = body["results"].get(query["refId"])
                if not isinstance(result, dict) or not isinstance(result.get("frames"), list):
                    self.fail("malformed_query_response")
                query_status = result.get("status", 200)
                if (
                    result.get("error")
                    or not isinstance(query_status, int)
                    or not 200 <= query_status < 300
                ):
                    self.fail("panel_query_failed")
                self.report["query_count"] += 1
        self.report["checks"][self.current_check] = "passed"
        self.report["status"] = "passed"
        return self.report


def verify_grafana(
    base_url: str = "http://127.0.0.1:3000",
    *,
    username: str | None = None,
    password: str | None = None,
    prometheus_url: str | None = DEFAULT_PROMETHEUS_URL,
    timeout: float = 90,
    request_timeout: float = 5,
) -> dict[str, Any]:
    """Check provisioning and execute every visible panel query; empty data is valid.

    Credentials default to GRAFANA_ADMIN_USER/GRAFANA_ADMIN_PASSWORD and the
    Compose demo values. Reports omit URLs, credentials, queries and data frames.
    ``timeout`` is one total budget, including startup and provisioning retries.
    """
    return _GrafanaCheck(
        base_url,
        os.environ.get("GRAFANA_ADMIN_USER", "admin") if username is None else username,
        (
            os.environ.get("GRAFANA_ADMIN_PASSWORD", "triage-local-demo")
            if password is None
            else password
        ),
        prometheus_url,
        timeout,
        request_timeout,
    ).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--request-timeout", type=float, default=5)
    parser.add_argument("--output", type=Path, default=Path("reports/ci/grafana-smoke.json"))
    args = parser.parse_args()
    try:
        report = verify_grafana(
            args.base_url,
            username=args.username,
            password=args.password,
            prometheus_url=args.prometheus_url,
            timeout=args.timeout,
            request_timeout=args.request_timeout,
        )
    except CheckGrafanaError as exc:
        report = exc.report
    except Exception:
        report = {"status": "failed", "checks": {}, "reason": "internal_error"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Grafana checks {report['status']}.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
