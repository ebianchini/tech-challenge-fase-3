from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from unittest.mock import Mock

import pytest

from scripts import deploy_ecs as cd

OLD_DIGEST = "sha256:" + "a" * 64
NEW_IMAGE = "example.dkr.ecr.us-east-1.amazonaws.com/triage@sha256:" + "b" * 64
OLD_IMAGE = "example.dkr.ecr.us-east-1.amazonaws.com/triage:previous"
PREVIOUS = "arn:aws:ecs:us-east-1:123:task-definition/triage:1"


class FakeAWS:
    def __init__(self, *, pinned=False, circuit_breaker=False, fail_rollback=False):
        self.current = PREVIOUS
        self.circuit_breaker = circuit_breaker
        self.fail_rollback = fail_rollback
        self.calls = []
        self.registered = []
        self.updates = []
        self.digest = OLD_DIGEST
        self.task = {
            "family": "triage",
            "taskDefinitionArn": PREVIOUS,
            "revision": 1,
            "status": "ACTIVE",
            "registeredBy": "DO_NOT_PUBLISH",
            "requiresCompatibilities": ["FARGATE"],
            "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            "networkMode": "awsvpc",
            "cpu": "256",
            "memory": "512",
            "taskRoleArn": "arn:aws:iam::123:role/app",
            "executionRoleArn": "arn:aws:iam::123:role/ecs",
            "containerDefinitions": [
                {
                    "name": "api",
                    "image": f"repo@{OLD_DIGEST}" if pinned else OLD_IMAGE,
                    "environment": [{"name": "PRIVATE", "value": "DO_NOT_PUBLISH"}],
                    "secrets": [{"name": "TOKEN", "valueFrom": "arn:aws:ssm:secret"}],
                },
                {"name": "sidecar", "image": "sidecar:stable"},
            ],
        }

    def __call__(self, operation, *args, payload=None):
        self.calls.append((operation, args))
        if operation == "describe-services":
            return {
                "services": [
                    {
                        "status": "ACTIVE",
                        "desiredCount": 1,
                        "runningCount": 1,
                        "pendingCount": 0,
                        "taskDefinition": self.current,
                        "deployments": [
                            {
                                "status": "PRIMARY",
                                "taskDefinition": self.current,
                                "rolloutState": "COMPLETED",
                            }
                        ],
                    }
                ]
            }
        if operation == "describe-task-definition":
            return {
                "taskDefinition": copy.deepcopy(self.task),
                "tags": [{"key": "app", "value": "triage"}],
            }
        if operation == "list-tasks":
            return {"taskArns": ["running-task"]}
        if operation == "describe-tasks":
            return {
                "tasks": [
                    {
                        "taskDefinitionArn": PREVIOUS,
                        "containers": [{"name": "api", "imageDigest": self.digest}],
                    }
                ]
            }
        if operation == "register-task-definition":
            self.registered.append(copy.deepcopy(payload))
            return {"taskDefinition": {"taskDefinitionArn": f"revision-{len(self.registered)}"}}
        if operation == "update-service":
            target = args[args.index("--task-definition") + 1]
            self.updates.append(target)
            if self.fail_rollback and len(self.updates) > 1:
                raise cd.DeploymentError("aws_operation_failed")
            self.current = target
            return {}
        if operation == "wait":
            if self.circuit_breaker and len(self.updates) == 1:
                self.current = PREVIOUS
            return {}
        raise AssertionError(operation)


@pytest.fixture
def config(tmp_path):
    return cd.Config(
        "cluster",
        "service",
        "api",
        NEW_IMAGE,
        "https://api.example.test",
        timeout_seconds=1,
        poll_seconds=1,
        report=tmp_path / "deployment.json",
    )


@pytest.mark.parametrize("pinned", [False, True])
def test_success_preserves_task_definition_and_pins_previous_digest(config, pinned):
    aws = FakeAWS(pinned=pinned)
    ready, check = Mock(), Mock(return_value={"status": "passed"})
    report = cd.deploy(config, aws=aws, ready=ready, check=check)

    assert report["status"] == "passed"
    assert report["rollback"] == "not_needed"
    assert report["previous_image"].endswith(f"@{OLD_DIGEST}")
    assert len(aws.registered) == (1 if pinned else 2)
    new_task = aws.registered[-1]
    assert new_task["containerDefinitions"][0]["image"] == NEW_IMAGE
    assert (
        new_task["containerDefinitions"][0]["environment"]
        == aws.task["containerDefinitions"][0]["environment"]
    )
    assert (
        new_task["containerDefinitions"][0]["secrets"]
        == aws.task["containerDefinitions"][0]["secrets"]
    )
    assert new_task["containerDefinitions"][1] == aws.task["containerDefinitions"][1]
    assert new_task["runtimePlatform"] == aws.task["runtimePlatform"]
    assert new_task["executionRoleArn"] == aws.task["executionRoleArn"]
    assert new_task["tags"] == [{"key": "app", "value": "triage"}]
    assert not {"taskDefinitionArn", "registeredBy", "revision", "status"} & new_task.keys()
    assert "DO_NOT_PUBLISH" not in config.report.read_text()
    assert json.loads(config.report.read_text()) == report
    ready.assert_called_once()
    assert 0 < ready.call_args.args[0].timeout_seconds <= config.timeout_seconds
    check.assert_called_once()


@pytest.mark.parametrize("failure", ["readiness", "contract", "circuit_breaker"])
def test_failure_restores_previous_immutable_revision_and_still_fails(config, failure):
    aws = FakeAWS(circuit_breaker=failure == "circuit_breaker")
    ready = Mock(
        side_effect=[cd.DeploymentError("readiness_failed"), None]
        if failure == "readiness"
        else None
    )
    check = Mock(side_effect=RuntimeError("DO_NOT_PUBLISH") if failure == "contract" else None)
    with pytest.raises(cd.DeploymentError):
        cd.deploy(config, aws=aws, ready=ready, check=check)
    report = json.loads(config.report.read_text())
    assert report["status"] == "failed"
    assert report["rollback"] == "passed"
    assert aws.updates == ["revision-2", "revision-1"]
    assert (
        aws.registered[0]["containerDefinitions"][0]["image"]
        == OLD_IMAGE.rsplit(":", 1)[0] + "@" + OLD_DIGEST
    )
    assert aws.current == report["rollback_task_definition"]
    if failure == "circuit_breaker":
        assert report["failure"] == "desired_revision_not_running"
        check.assert_not_called()
    assert "DO_NOT_PUBLISH" not in config.report.read_text()


def test_rollback_failure_is_explicit_and_preserves_deployment_failure(config):
    aws = FakeAWS(fail_rollback=True)
    with pytest.raises(cd.DeploymentError, match="deployment_and_rollback_failed"):
        cd.deploy(config, aws=aws, ready=Mock(side_effect=cd.DeploymentError("readiness_failed")))
    report = json.loads(config.report.read_text())
    assert report["rollback"] == "failed"
    assert report["failure"] == "readiness_failed"


@pytest.mark.parametrize("image", ["repo:latest", "repo@sha256:abc"])
def test_mutable_or_invalid_image_rejected_before_aws(config, image):
    aws = Mock()
    with pytest.raises(cd.DeploymentError, match="invalid_configuration"):
        cd.deploy(replace(config, image=image), aws=aws)
    aws.assert_not_called()


def test_missing_previous_digest_prevents_any_mutation(config):
    aws = FakeAWS()
    aws.digest = None
    with pytest.raises(cd.DeploymentError, match="previous_digest_unavailable"):
        cd.deploy(config, aws=aws)
    assert not aws.registered
    assert not aws.updates


@pytest.mark.parametrize(
    "body,success",
    [
        ({"status": "ok", "model_loaded": True, "preprocessor_loaded": True}, True),
        ({"status": "degraded", "model_loaded": False, "preprocessor_loaded": False}, False),
        ({"status": "ok", "model_loaded": True, "preprocessor_loaded": False}, False),
        ({"status": "ok", "model_loaded": "true", "preprocessor_loaded": True}, False),
    ],
)
def test_readiness_inspects_json_instead_of_accepting_http_200(config, monkeypatch, body, success):
    clock = [0.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cd.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    response = Mock(status=200)
    response.headers.get_content_type.return_value = "application/json"
    response.read1.side_effect = [json.dumps(body).encode(), b""]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        cd, "build_opener", Mock(return_value=Mock(open=Mock(return_value=response)))
    )
    if success:
        cd.wait_ready(config)
    else:
        with pytest.raises(cd.DeploymentError, match="readiness_failed"):
            cd.wait_ready(config)


def test_aws_failures_never_expose_command_output(monkeypatch, capsys):
    monkeypatch.setattr(
        cd.subprocess,
        "run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1, "aws", output="PRIVATE_OUTPUT", stderr="PRIVATE_ERROR"
            )
        ),
    )
    with pytest.raises(cd.DeploymentError, match="^aws_operation_failed$"):
        cd.AWS(1)("describe-services")
    assert "PRIVATE" not in capsys.readouterr().out


def test_aws_task_payload_is_temporary_file_and_not_command_line(monkeypatch):
    def execute(command, **kwargs):
        path = cd.Path(command[command.index("--cli-input-json") + 1].removeprefix("file://"))
        assert json.loads(path.read_text()) == {"environment": "PRIVATE_VALUE"}
        assert "PRIVATE_VALUE" not in str(command)
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(cd.subprocess, "run", execute)
    assert cd.AWS(1)("register-task-definition", payload={"environment": "PRIVATE_VALUE"}) == {}


@pytest.fixture
def clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cd.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return clock


def test_readiness_rejects_healthy_json_received_after_deadline(config, monkeypatch, clock):
    response = Mock(status=200)
    response.headers.get_content_type.return_value = "application/json"

    def delayed_body(*_args):
        clock[0] = 2.0
        return b'{"status":"ok","model_loaded":true,"preprocessor_loaded":true}'

    response.read1.side_effect = delayed_body
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        cd, "build_opener", Mock(return_value=Mock(open=Mock(return_value=response)))
    )
    with pytest.raises(cd.DeploymentError, match="readiness_failed"):
        cd.wait_ready(config)


def test_readiness_retries_sanitized_response_read_failure(config, monkeypatch, clock):
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock(open=Mock(return_value=response))
    monkeypatch.setattr(cd, "build_opener", Mock(return_value=opener))
    reader = Mock(
        side_effect=[
            cd.ResponseReadError("transport_error"),
            {
                "status": "ok",
                "model_loaded": True,
                "preprocessor_loaded": True,
            },
        ]
    )
    monkeypatch.setattr(cd, "read_json_response", reader)
    cd.wait_ready(replace(config, timeout_seconds=2))
    assert opener.open.call_count == 2
    assert clock[0] == 1
    assert reader.call_args.args[1] == 2


def test_metadata_shares_global_budget_and_expiry_prevents_mutation(config, clock):
    aws = FakeAWS(pinned=True)

    def delayed_aws(operation, *args, **kwargs):
        result = aws(operation, *args, **kwargs)
        clock[0] += 0.6
        return result

    with pytest.raises(cd.DeploymentError, match="deployment_timeout"):
        cd.deploy(config, aws=delayed_aws)
    assert [call[0] for call in aws.calls] == ["describe-services", "describe-task-definition"]
    assert not aws.registered
    assert not aws.updates
    assert json.loads(config.report.read_text())["rollback"] == "not_needed"


def test_update_exhaustion_restores_previous_revision_using_fresh_budget(config, clock):
    aws = FakeAWS(pinned=True)
    ready, check = Mock(), Mock()

    def delayed_update(operation, *args, **kwargs):
        result = aws(operation, *args, **kwargs)
        if operation == "update-service" and len(aws.updates) == 1:
            clock[0] += 2
        return result

    with pytest.raises(cd.DeploymentError, match="deployment_timeout"):
        cd.deploy(config, aws=delayed_update, ready=ready, check=check)
    assert aws.updates == ["revision-1", PREVIOUS]
    assert aws.current == PREVIOUS
    report = json.loads(config.report.read_text())
    assert report["failure"] == "deployment_timeout"
    assert report["rollback"] == "passed"
    ready.assert_called_once()
    assert ready.call_args.args[0].timeout_seconds == 1
    check.assert_not_called()


def test_readiness_and_contract_receive_only_remaining_global_budget(config, clock):
    aws = FakeAWS(pinned=True)

    def timed_aws(operation, *args, **kwargs):
        result = aws(operation, *args, **kwargs)
        clock[0] += 0.05
        return result

    def check_ready(ready_config):
        assert ready_config.timeout_seconds == pytest.approx(0.7)
        clock[0] += 0.2

    def check_contract(base_url, *, timeout, request_timeout):
        assert base_url == config.base_url
        assert timeout == pytest.approx(0.5)
        assert request_timeout == pytest.approx(0.5)
        clock[0] += 0.1

    assert (
        cd.deploy(config, aws=timed_aws, ready=check_ready, check=check_contract)["status"]
        == "passed"
    )


def test_rollback_also_has_finite_independent_budget(config, clock):
    aws = FakeAWS(pinned=True)

    def delayed_aws(operation, *args, **kwargs):
        result = aws(operation, *args, **kwargs)
        if operation == "update-service" and len(aws.updates) == 1:
            clock[0] += 2
        elif len(aws.updates) > 1:
            clock[0] += 0.6
        return result

    with pytest.raises(cd.DeploymentError, match="deployment_and_rollback_failed"):
        cd.deploy(config, aws=delayed_aws, ready=Mock())
    report = json.loads(config.report.read_text())
    assert report["failure"] == "deployment_timeout"
    assert report["rollback"] == "failed"


def test_aws_cli_timeout_uses_remaining_phase_budget(monkeypatch, clock):
    timeouts = []

    def execute(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 2
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(cd.subprocess, "run", execute)
    aws = cd.AWS(5)
    aws("describe-services")
    aws("describe-task-definition")
    assert timeouts == [5, 3]


def test_aws_cli_rejects_result_received_after_deadline(monkeypatch, clock):
    def execute(command, **_kwargs):
        clock[0] += 2
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(cd.subprocess, "run", execute)
    with pytest.raises(cd.DeploymentError, match="deployment_timeout"):
        cd.AWS(1)("describe-services")
