"""Smoke the Compose stack in an isolated project using synthetic traffic only."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import uuid4

if __package__:
    from scripts import check_api, check_grafana
else:
    import check_api
    import check_grafana

ROOT = Path(__file__).resolve().parents[1]
POLL_INTERVAL = 1.0
CLEANUP_TIMEOUT = 30
CHECKS = (
    "docker",
    "compose_config",
    "startup",
    "api",
    "prometheus",
    "grafana",
    "persistence",
    "grafana_recreate",
    "cleanup",
)
PROM_QUERIES = {
    "up": 'up{job="triage-api",instance="api:8000"}',
    "predictions": 'sum(triage_predictions_total{job="triage-api"})',
    "invalid_requests": (
        'sum(triage_http_requests_total{job="triage-api",method="POST",'
        'route="/v1/triage",status="422"})'
    ),
}


class CheckComposeError(RuntimeError):
    """A fixed reason code and a report without commands, credentials or responses."""

    def __init__(self, code: str, report: dict[str, Any]):
        super().__init__(code)
        self.report = report


class _ComposeCheck:
    def __init__(self, *, timeout: float, build: bool, docker: str):
        self.report: dict[str, Any] = {
            "status": "failed",
            "checks": dict.fromkeys(CHECKS, "pending"),
        }
        self.current_check = "docker"
        if not math.isfinite(timeout) or timeout <= 0 or not docker:
            self.fail("invalid_configuration")
        self.deadline = time.monotonic() + timeout
        self.build = build
        self.docker = docker
        # A caller cannot select a project: cleanup only targets this new UUID.
        self.project = "triage-smoke-" + uuid4().hex
        self.password = "triage-smoke-" + uuid4().hex
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")
        }
        self.env.update(
            COMPOSE_PROFILES="",
            API_PORT="0",
            PROMETHEUS_PORT="0",
            GRAFANA_PORT="0",
            GRAFANA_ADMIN_USER="admin",
            GRAFANA_ADMIN_PASSWORD=self.password,
            TRIAGE_API_IMAGE=self.project + ":local" if build else "triage-laudos:local",
        )
        self.env.pop("TRIAGE_API_TOKEN", None)
        # Preserve Docker Desktop/context selection. The daemon must expose its
        # published loopback ports to this host and resolve this checkout's mounts.
        self.started = False
        self.opener = build_opener(check_api._NoRedirects())

    def fail(self, reason: str) -> None:
        self.report["checks"][self.current_check] = "failed"
        self.report["reason"] = reason
        raise CheckComposeError(reason, self.report) from None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.fail("deadline_exceeded")
        return remaining

    def command(self, arguments: list[str], reason: str, *, limit: float | None = None) -> str:
        budget = self.remaining()
        if limit is not None:
            budget = min(budget, limit)
        try:
            result = subprocess.run(
                arguments,
                cwd=ROOT,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=budget,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self.fail(reason)
        self.remaining()
        if result.returncode:
            self.fail(reason)
        return result.stdout.strip()

    def compose(self, *arguments: str, reason: str, limit: float | None = None) -> str:
        return self.command(self.prefix + list(arguments), reason, limit=limit)

    def port(self, service: str, container_port: str) -> str:
        output = self.compose(
            "port", service, container_port, reason="port_lookup_failed", limit=10
        )
        # Reject wildcard bindings and multiple mappings instead of accidentally
        # checking an unrelated address or treating LAN exposure as a success.
        match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", output)
        if match is None or not 1 <= int(match[1]) <= 65535:
            self.fail("invalid_port_binding")
        return "http://" + output

    def request(self, url: str, payload: dict | None = None) -> tuple[int, Any]:
        budget = min(5, self.remaining())
        deadline = min(self.deadline, time.monotonic() + budget)
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            url,
            data=None if payload is None else json.dumps(payload).encode(),
            headers=headers,
            method="GET" if payload is None else "POST",
        )
        try:
            try:
                response = self.opener.open(request, timeout=budget)
            except HTTPError as exc:
                response = exc
            with response:
                if response.code != 200:
                    return response.code, None
                return response.code, check_api.read_json_response(response, deadline)
        except (OSError, URLError, check_api.ResponseReadError):
            self.remaining()
            self.fail("http_response_failed")

    def traffic(self, api_url: str, expected_version: str | None = None) -> str:
        status, body = self.request(
            api_url + "/v1/triage", {"instances": check_api.SYNTHETIC_INSTANCES}
        )
        if (
            status != 200
            or not isinstance(body, dict)
            or not isinstance(body.get("model_version"), str)
            or not body["model_version"]
            or not isinstance(body.get("results"), list)
            or len(body["results"]) != len(check_api.SYNTHETIC_INSTANCES)
        ):
            self.fail("synthetic_inference_failed")
        version = body["model_version"]
        if expected_version is not None and version != expected_version:
            self.fail("model_changed")
        status, _body = self.request(api_url + "/v1/triage", {"instances": []})
        if status != 422:
            self.fail("synthetic_validation_failed")
        return version

    def scalar(self, prometheus_url: str, expression: str) -> float | None:
        status, body = self.request(
            prometheus_url + "/api/v1/query?" + urlencode({"query": expression})
        )
        if status != 200 or not isinstance(body, dict) or body.get("status") != "success":
            self.fail("prometheus_query_failed")
        data = body.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "vector":
            self.fail("invalid_prometheus_response")
        result = data.get("result")
        if result == []:
            return None
        try:
            if not isinstance(result, list) or len(result) != 1:
                self.fail("invalid_prometheus_response")
            value = float(result[0]["value"][1])
            if not math.isfinite(value) or value < 0:
                self.fail("invalid_prometheus_response")
        except (IndexError, KeyError, TypeError, ValueError):
            self.fail("invalid_prometheus_response")
        return value

    def wait_samples(
        self, prometheus_url: str, baseline: dict[str, float] | None = None
    ) -> dict[str, float]:
        while True:
            values = {
                name: self.scalar(prometheus_url, query) for name, query in PROM_QUERIES.items()
            }
            if values["up"] == 1 and all(
                values[name] is not None
                and values[name] > (0 if baseline is None else baseline[name])
                for name in ("predictions", "invalid_requests")
            ):
                return values
            time.sleep(min(POLL_INTERVAL, self.remaining()))

    def api_check(self, base_url: str) -> None:
        # The local technical demo has no gateway. Do not send a caller's optional
        # production gateway token when reusing the public API contract checker.
        token = os.environ.pop("TRIAGE_API_TOKEN", None)
        try:
            self.report["api"] = check_api.verify_api(base_url, timeout=self.remaining())
        except check_api.CheckAPIError:
            self.fail("api_check_failed")
        finally:
            if token is not None:
                os.environ["TRIAGE_API_TOKEN"] = token

    def grafana_check(self, base_url: str) -> dict[str, Any]:
        try:
            return check_grafana.verify_grafana(
                base_url,
                username="admin",
                password=self.password,
                prometheus_url="http://prometheus:9090",
                timeout=self.remaining(),
            )
        except check_grafana.CheckGrafanaError:
            self.fail("grafana_check_failed")

    def execute(self) -> None:
        if self.command(
            [self.docker, "info", "--format", "{{.OSType}}"], "docker_unavailable", limit=15
        ) != ("linux"):
            self.fail("linux_docker_required")
        self.report["checks"][self.current_check] = "passed"
        self.current_check = "compose_config"
        self.compose("config", "--quiet", reason="compose_config_failed", limit=30)
        self.report["checks"][self.current_check] = "passed"

        self.current_check = "startup"
        self.started = True  # Even a partially failed `up` must be cleaned up.
        arguments = ["up", "-d", "--build" if self.build else "--no-build"]
        self.compose(
            *arguments,
            "--wait",
            "--wait-timeout",
            "180",
            "api",
            "prometheus",
            "grafana",
            reason="compose_startup_failed",
        )
        api_url = self.port("api", "8000")
        prometheus_url = self.port("prometheus", "9090")
        grafana_url = self.port("grafana", "3000")
        self.report["checks"][self.current_check] = "passed"

        self.current_check = "api"
        self.api_check(api_url)
        self.report["checks"][self.current_check] = "passed"
        self.current_check = "prometheus"
        version = self.traffic(api_url)
        baseline = self.wait_samples(prometheus_url)
        self.traffic(api_url, expected_version=version)
        collected = self.wait_samples(prometheus_url, baseline)
        self.report["metrics"] = {
            **collected,
            "prediction_increase": collected["predictions"] - baseline["predictions"],
            "invalid_request_increase": collected["invalid_requests"]
            - baseline["invalid_requests"],
        }
        self.report["checks"][self.current_check] = "passed"

        self.current_check = "grafana"
        self.report["grafana"] = {"initial": self.grafana_check(grafana_url)}
        self.report["checks"][self.current_check] = "passed"
        self.current_check = "persistence"
        marker_path = "/var/lib/grafana/." + self.project
        self.compose(
            "exec",
            "-T",
            "grafana",
            "sh",
            "-c",
            'printf %s "$1" > "$2"',
            "sh",
            self.project,
            marker_path,
            reason="persistence_marker_failed",
            limit=10,
        )
        self.current_check = "grafana_recreate"
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "180",
            "grafana",
            reason="grafana_recreate_failed",
        )
        grafana_url = self.port("grafana", "3000")
        self.report["grafana"]["recreated"] = self.grafana_check(grafana_url)
        self.report["checks"][self.current_check] = "passed"
        self.current_check = "persistence"
        marker = self.compose(
            "exec",
            "-T",
            "grafana",
            "cat",
            marker_path,
            reason="persistence_marker_missing",
            limit=10,
        )
        if marker != self.project:
            self.fail("persistence_marker_mismatch")
        self.report["checks"][self.current_check] = "passed"

    def cleanup(self) -> bool:
        if not self.started:
            self.report["checks"]["cleanup"] = "not_needed"
            return True
        try:
            result = subprocess.run(
                self.prefix + ["down", "--volumes", "--remove-orphans"],
                cwd=ROOT,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=CLEANUP_TIMEOUT,
                check=False,
            )
            success = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            success = False
        self.report["checks"]["cleanup"] = "passed" if success else "failed"
        return success

    def run(self) -> dict[str, Any]:
        error = None
        with tempfile.TemporaryDirectory(prefix="triage-compose-smoke-") as directory:
            env_file = Path(directory) / "empty.env"
            env_file.write_text("", encoding="utf-8")
            self.prefix = [
                self.docker,
                "compose",
                "-f",
                str(ROOT / "docker-compose.yml"),
                "-p",
                self.project,
                "--env-file",
                str(env_file),
            ]
            try:
                self.execute()
            except CheckComposeError as exc:
                error = exc
            except Exception:
                self.report["checks"][self.current_check] = "failed"
                self.report["reason"] = "internal_error"
                error = CheckComposeError("internal_error", self.report)
            finally:
                clean = self.cleanup()
            if not clean and error is None:
                self.report["reason"] = "cleanup_failed"
                error = CheckComposeError("cleanup_failed", self.report)
        if error is not None:
            raise error
        self.report["status"] = "passed"
        return self.report


def verify_compose(*, timeout: float = 600, build: bool = True, docker: str = "docker") -> dict:
    """Create, verify, recreate Grafana and remove only a new UUID-owned project.

    Build mode gives the API image a unique tag. ``build=False`` uses the existing
    ``triage-laudos:local`` image and explicitly forbids automatic Compose builds.
    The engine must make published loopback ports and this checkout's bind mounts
    available on the host running the checker; arbitrary remote engines do not.
    """
    return _ComposeCheck(timeout=timeout, build=build, docker=docker).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/ci/compose-smoke.json"))
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument(
        "--no-build", action="store_true", help="Reuse the existing triage-laudos:local image."
    )
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    try:
        report = verify_compose(timeout=args.timeout, build=not args.no_build, docker=args.docker)
    except CheckComposeError as exc:
        report = exc.report
    except Exception:
        report = {"status": "failed", "checks": {}, "reason": "internal_error"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Compose checks {report['status']}.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
