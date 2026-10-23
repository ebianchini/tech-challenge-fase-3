from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import check_compose

PRIVATE_MARKER = "synthetic-secret-from-command-or-response"


@pytest.fixture
def compose_runtime(monkeypatch):
    """Model the container/HTTP boundary; no Docker daemon or host model is used."""
    clock = [0.0]
    monkeypatch.setattr(check_compose.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        check_compose.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def setup(**options):
        state = {
            "commands": [],
            "api_checks": [],
            "grafana_checks": [],
            "http": [],
            "predictions": 0,
            "invalid_requests": 0,
            "rounds": 0,
            "marker": "",
            "grafana_ports": 0,
            "clock": clock,
        }

        def run(command, **kwargs):
            state["commands"].append((command, kwargs))
            assert kwargs["capture_output"] is True
            assert kwargs["timeout"] > 0
            if command[1] == "info":
                if options.get("no_docker"):
                    raise FileNotFoundError(PRIVATE_MARKER)
                return SimpleNamespace(returncode=0, stdout=options.get("os", "linux"))
            env_file = Path(command[command.index("--env-file") + 1])
            assert env_file.read_text() == ""
            args = command[command.index("--env-file") + 2 :]
            stdout, returncode = "", 0
            if args[0] == "config" and options.get("config_failure"):
                returncode = 1
            elif args[0] == "up":
                if "--force-recreate" in args:
                    returncode = int(options.get("recreate_failure", False))
                else:
                    returncode = int(options.get("startup_failure", False))
            elif args[0] == "port":
                port = {"api": 38000, "prometheus": 39090, "grafana": 33000}[args[1]]
                if args[1] == "grafana":
                    port += state["grafana_ports"]
                    state["grafana_ports"] += 1
                stdout = options.get("port_output", f"127.0.0.1:{port}")
            elif args[:4] == ["exec", "-T", "grafana", "sh"]:
                state["marker"] = args[-2]
                assert args[-1] == "/var/lib/grafana/." + state["marker"]
            elif args[:4] == ["exec", "-T", "grafana", "cat"]:
                stdout = "missing-marker" if options.get("marker_lost") else state["marker"]
            elif args[0] == "down":
                if options.get("cleanup_failure"):
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"], PRIVATE_MARKER)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=PRIVATE_MARKER)

        def api_check(base_url, **kwargs):
            state["api_checks"].append((base_url, kwargs))
            assert "TRIAGE_API_TOKEN" not in check_compose.os.environ
            if options.get("api_failure"):
                raise check_compose.check_api.CheckAPIError(PRIVATE_MARKER, {"status": "failed"})
            return {"status": "passed", "request_count": 4}

        def grafana_check(base_url, **kwargs):
            state["grafana_checks"].append((base_url, kwargs))
            assert kwargs["prometheus_url"] == "http://prometheus:9090"
            if options.get("grafana_failure"):
                raise check_compose.check_grafana.CheckGrafanaError(PRIVATE_MARKER, {})
            return {"status": "passed", "panel_count": 9, "query_count": 11}

        def request(checker, url, payload=None):
            checker.remaining()
            state["http"].append((url, payload))
            parsed = urlsplit(url)
            if parsed.path == "/v1/triage":
                if payload == {"instances": []}:
                    state["invalid_requests"] += 1
                    return 422, None
                assert payload == {"instances": check_compose.check_api.SYNTHETIC_INSTANCES}
                state["rounds"] += 1
                state["predictions"] += len(payload["instances"])
                return 200, {
                    "model_version": (
                        "changed"
                        if options.get("model_changed") and state["rounds"] > 1
                        else "demo"
                    ),
                    "results": [{"triage_class": "normal"} for _ in payload["instances"]],
                }
            assert parsed.path == "/api/v1/query"
            clock[0] += 0.1
            expression = parse_qs(parsed.query)["query"][0]
            name = next(k for k, query in check_compose.PROM_QUERIES.items() if query == expression)
            value = 1 if name == "up" else state[name]
            if options.get("unchanged") and name != "up":
                value = {"predictions": 3, "invalid_requests": 1}[name]
            result = [] if options.get("no_series") else [{"value": [100, str(value)]}]
            return 200, {"status": "success", "data": {"resultType": "vector", "result": result}}

        monkeypatch.setattr(check_compose.subprocess, "run", run)
        monkeypatch.setattr(check_compose.check_api, "verify_api", api_check)
        monkeypatch.setattr(check_compose.check_grafana, "verify_grafana", grafana_check)
        monkeypatch.setattr(check_compose._ComposeCheck, "request", request)
        return state

    return setup


def arguments(command):
    return command[command.index("--env-file") + 2 :]


def test_integrated_smoke_isolates_environment_collects_increments_and_recreates_grafana(
    compose_runtime, monkeypatch, capsys
):
    monkeypatch.setenv("COMPOSE_FILE", "user-stack.yml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "user-project")
    monkeypatch.setenv("COMPOSE_PROFILES", "airflow")
    monkeypatch.setenv("COMPOSE_ENV_FILES", "user-secret.env")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote-linux:2376")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", PRIVATE_MARKER)
    monkeypatch.setenv("TRIAGE_API_TOKEN", PRIVATE_MARKER)
    state = compose_runtime()
    report = check_compose.verify_compose()
    assert report["status"] == "passed"
    assert report["checks"] == dict.fromkeys(check_compose.CHECKS, "passed")
    assert report["metrics"] == {
        "up": 1,
        "predictions": 6,
        "invalid_requests": 2,
        "prediction_increase": 3,
        "invalid_request_increase": 1,
    }
    compose_commands = [(c, k) for c, k in state["commands"] if c[1] == "compose"]
    projects = {command[command.index("-p") + 1] for command, _ in compose_commands}
    assert len(projects) == 1
    project = projects.pop()
    assert re.fullmatch(r"triage-smoke-[a-f0-9]{32}", project)
    for command, kwargs in compose_commands:
        assert command[command.index("-f") + 1] == str(check_compose.ROOT / "docker-compose.yml")
        env = kwargs["env"]
        assert env["COMPOSE_PROFILES"] == ""
        assert not any(
            key in env for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES")
        )
        assert env["DOCKER_HOST"] == "tcp://remote-linux:2376"
        assert all(env[key] == "0" for key in ("API_PORT", "PROMETHEUS_PORT", "GRAFANA_PORT"))
        assert env["TRIAGE_API_IMAGE"] == project + ":local"
        assert env["GRAFANA_ADMIN_PASSWORD"] != PRIVATE_MARKER
        assert "TRIAGE_API_TOKEN" not in env
    assert arguments(compose_commands[-1][0]) == ["down", "--volumes", "--remove-orphans"]
    assert state["api_checks"][0][0] == "http://127.0.0.1:38000"
    assert [url for url, _ in state["grafana_checks"]] == [
        "http://127.0.0.1:33000",
        "http://127.0.0.1:33001",
    ]
    assert check_compose.os.environ["TRIAGE_API_TOKEN"] == PRIVATE_MARKER
    assert PRIVATE_MARKER not in json.dumps(report)
    assert "http://" not in json.dumps(report)
    assert project not in json.dumps(report)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"no_docker": True}, "docker_unavailable"),
        ({"os": "windows"}, "linux_docker_required"),
        ({"config_failure": True}, "compose_config_failed"),
    ],
)
def test_preflight_failure_does_not_start_or_remove_any_stack(compose_runtime, options, reason):
    state = compose_runtime(**options)
    with pytest.raises(check_compose.CheckComposeError, match=reason) as exc:
        check_compose.verify_compose()
    assert exc.value.report["checks"]["cleanup"] == "not_needed"
    assert not any(
        c[1] == "compose" and arguments(c)[0] in {"up", "down"} for c, _ in state["commands"]
    )


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"startup_failure": True}, "compose_startup_failed"),
        ({"recreate_failure": True}, "grafana_recreate_failed"),
        ({"marker_lost": True}, "persistence_marker_mismatch"),
        ({"api_failure": True}, "api_check_failed"),
        ({"grafana_failure": True}, "grafana_check_failed"),
    ],
)
def test_partial_failures_always_remove_only_the_new_project(compose_runtime, options, reason):
    state = compose_runtime(**options)
    with pytest.raises(check_compose.CheckComposeError, match=reason) as exc:
        check_compose.verify_compose()
    assert exc.value.report["checks"]["cleanup"] == "passed"
    last, kwargs = state["commands"][-1]
    assert arguments(last) == ["down", "--volumes", "--remove-orphans"]
    assert last[last.index("-p") + 1].startswith("triage-smoke-")
    assert kwargs["timeout"] == check_compose.CLEANUP_TIMEOUT
    assert PRIVATE_MARKER not in json.dumps(exc.value.report)


@pytest.mark.parametrize("option", ["no_series", "unchanged"])
def test_missing_samples_or_unchanged_counters_never_pass_and_cleanup_has_its_own_budget(
    compose_runtime, option
):
    state = compose_runtime(**{option: True})
    with pytest.raises(check_compose.CheckComposeError, match="deadline_exceeded") as exc:
        check_compose.verify_compose(timeout=2)
    assert exc.value.report["checks"]["prometheus"] == "failed"
    assert exc.value.report["checks"]["cleanup"] == "passed"
    assert state["commands"][-1][1]["timeout"] == 30
    assert state["clock"][0] <= 2.1


def test_model_change_between_scrapes_is_reported_and_cleaned_up(compose_runtime):
    state = compose_runtime(model_changed=True)
    with pytest.raises(check_compose.CheckComposeError, match="model_changed") as exc:
        check_compose.verify_compose()
    assert state["rounds"] == 2
    assert exc.value.report["checks"]["prometheus"] == "failed"
    assert exc.value.report["checks"]["cleanup"] == "passed"


@pytest.mark.parametrize("binding", ["0.0.0.0:3000", "127.0.0.1:0", "127.0.0.1:3000\n[::1]:3000"])
def test_rejects_nonlocal_or_ambiguous_published_ports(compose_runtime, binding):
    compose_runtime(port_output=binding)
    with pytest.raises(check_compose.CheckComposeError, match="invalid_port_binding") as exc:
        check_compose.verify_compose()
    assert exc.value.report["checks"]["cleanup"] == "passed"


def test_cleanup_failure_keeps_otherwise_successful_run_failed(compose_runtime):
    compose_runtime(cleanup_failure=True)
    with pytest.raises(check_compose.CheckComposeError, match="cleanup_failed") as exc:
        check_compose.verify_compose()
    assert exc.value.report["status"] == "failed"
    assert exc.value.report["checks"]["cleanup"] == "failed"
    assert PRIVATE_MARKER not in str(exc.value)


def test_no_build_reuses_local_image_without_overwriting_its_tag(compose_runtime):
    state = compose_runtime()
    assert check_compose.verify_compose(build=False)["status"] == "passed"
    commands = [(c, k) for c, k in state["commands"] if c[1] == "compose"]
    startup = next(c for c, _ in commands if arguments(c)[0] == "up")
    assert "--no-build" in startup
    assert "--build" not in startup
    assert all(k["env"]["TRIAGE_API_IMAGE"] == "triage-laudos:local" for _, k in commands)


@pytest.mark.parametrize("timeout", [0, -1, float("nan")])
def test_invalid_budget_does_not_contact_docker(compose_runtime, timeout):
    state = compose_runtime()
    with pytest.raises(check_compose.CheckComposeError, match="invalid_configuration"):
        check_compose.verify_compose(timeout=timeout)
    assert state["commands"] == []


def test_cli_missing_docker_writes_honest_sanitized_failure(
    compose_runtime, monkeypatch, tmp_path, capsys
):
    compose_runtime(no_docker=True)
    output = tmp_path / "reports" / "compose.json"
    monkeypatch.setattr("sys.argv", ["check_compose", "--output", str(output)])
    assert check_compose.main() == 1
    report = json.loads(output.read_text())
    assert report["status"] == "failed"
    assert report["reason"] == "docker_unavailable"
    assert PRIVATE_MARKER not in output.read_text()
    assert capsys.readouterr().out == "Compose checks failed.\n"
