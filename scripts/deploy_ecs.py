"""Deploy one immutable image to ECS and restore the previous digest on failure."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.check_api import ResponseReadError, read_json_response

DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}\Z")
IMMUTABLE_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-fA-F]{64}\Z")
TASK_FIELDS = (
    "family taskRoleArn executionRoleArn networkMode containerDefinitions volumes "
    "placementConstraints requiresCompatibilities cpu memory tags pidMode ipcMode "
    "proxyConfiguration inferenceAccelerators ephemeralStorage runtimePlatform "
    "enableFaultInjection"
).split()


class DeploymentError(RuntimeError):
    """Only predefined, non-sensitive error codes leave this module."""


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise DeploymentError("deployment_timeout")
    return seconds


@dataclass(frozen=True)
class Config:
    cluster: str
    service: str
    container: str
    image: str
    base_url: str
    timeout_seconds: float = 600
    poll_seconds: float = 5
    report: Path = Path("reports/cd/deployment.json")


class AWS:
    def __init__(self, timeout: float):
        self.deadline = time.monotonic() + timeout

    def __call__(self, operation: str, *args: str, payload: dict | None = None) -> dict:
        # Keep task definitions (including environment values) out of argv and logs.
        with tempfile.TemporaryDirectory(prefix="triage-ecs-") as directory:
            command = ["aws", "ecs", operation, *args, "--output", "json", "--no-cli-pager"]
            if payload is not None:
                path = Path(directory) / "task.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                command += ["--cli-input-json", f"file://{path.as_posix()}"]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=remaining(self.deadline),
                )
                document = json.loads(result.stdout) if result.stdout.strip() else {}
                remaining(self.deadline)
                return document
            except subprocess.TimeoutExpired:
                raise DeploymentError("deployment_timeout") from None
            except (OSError, subprocess.SubprocessError, ValueError):
                raise DeploymentError("aws_operation_failed") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_ready(config: Config) -> None:
    deadline = time.monotonic() + config.timeout_seconds
    headers = {"Accept": "application/json"}
    if token := os.getenv("TRIAGE_API_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(config.base_url.rstrip("/") + "/health/ready", headers=headers)
    opener = build_opener(NoRedirect())
    while time.monotonic() < deadline:
        request_deadline = min(deadline, time.monotonic() + 5)
        try:
            with opener.open(request, timeout=remaining(request_deadline)) as response:
                body = read_json_response(response, request_deadline)
                if (
                    time.monotonic() < deadline
                    and response.status == 200
                    and isinstance(body, dict)
                    and body.get("status") == "ok"
                    and body.get("model_loaded") is True
                    and body.get("preprocessor_loaded") is True
                ):
                    return
        except (HTTPError, URLError, OSError, ValueError, ResponseReadError):
            pass
        time.sleep(min(config.poll_seconds, max(0, deadline - time.monotonic())))
    raise DeploymentError("readiness_failed")


def describe_service(aws, config: Config) -> dict:
    result = aws("describe-services", "--cluster", config.cluster, "--services", config.service)
    services = result.get("services", [])
    if result.get("failures") or len(services) != 1:
        raise DeploymentError("service_unavailable")
    service = services[0]
    if (
        service.get("status") != "ACTIVE"
        or service.get("deploymentController", {}).get("type", "ECS") != "ECS"
        or service.get("desiredCount", 0) < 1
    ):
        raise DeploymentError("unsupported_service")
    return service


def ensure_revision(service: dict, revision: str) -> None:
    deployments = service.get("deployments", [])
    if (
        service.get("taskDefinition") != revision
        or len(deployments) != 1
        or deployments[0].get("taskDefinition") != revision
        or deployments[0].get("status") != "PRIMARY"
        or deployments[0].get("rolloutState") != "COMPLETED"
        or service.get("runningCount") != service.get("desiredCount")
        or service.get("pendingCount", 0) != 0
    ):
        # services-stable can succeed after ECS silently restores the old revision.
        raise DeploymentError("desired_revision_not_running")


def previous_image(aws, config: Config, task: dict, revision: str) -> str:
    containers = [c for c in task["containerDefinitions"] if c["name"] == config.container]
    if len(containers) != 1:
        raise DeploymentError("container_not_found")
    image = containers[0]["image"]
    if "@" in image and DIGEST.fullmatch(image.rsplit("@", 1)[1]):
        return image
    task_arns = aws(
        "list-tasks",
        "--cluster",
        config.cluster,
        "--service-name",
        config.service,
        "--desired-status",
        "RUNNING",
    ).get("taskArns", [])
    digests = set()
    for offset in range(0, len(task_arns), 100):
        tasks = aws(
            "describe-tasks",
            "--cluster",
            config.cluster,
            "--tasks",
            *task_arns[offset : offset + 100],
        )
        if tasks.get("failures"):
            raise DeploymentError("previous_digest_unavailable")
        for running in tasks.get("tasks", []):
            if running.get("taskDefinitionArn") == revision:
                digests.update(
                    c["imageDigest"]
                    for c in running.get("containers", [])
                    if c.get("name") == config.container and c.get("imageDigest")
                )
    if len(digests) != 1 or not DIGEST.fullmatch(next(iter(digests))):
        raise DeploymentError("previous_digest_unavailable")
    repository = image.split("@", 1)[0]
    if repository.rfind(":") > repository.rfind("/"):
        repository = repository.rsplit(":", 1)[0]
    return f"{repository}@{next(iter(digests))}"


def register(aws, task: dict, container: str, image: str) -> str:
    payload = {key: copy.deepcopy(task[key]) for key in TASK_FIELDS if key in task}
    for definition in payload["containerDefinitions"]:
        if definition["name"] == container:
            definition["image"] = image
    return aws("register-task-definition", payload=payload)["taskDefinition"]["taskDefinitionArn"]


def deploy(config: Config, *, aws=None, ready=None, check=None) -> dict:
    report = {"status": "failed", "rollback": "not_needed"}
    updated = False
    client = aws or AWS(config.timeout_seconds)
    deadline = time.monotonic() + config.timeout_seconds
    ready = ready or wait_ready

    def call_aws(operation: str, *args: str, payload: dict | None = None) -> dict:
        nonlocal updated
        remaining(deadline)
        if isinstance(client, AWS):
            client.deadline = deadline
        if operation == "update-service":
            # A timeout after invoking update may still mean AWS accepted it.
            updated = True
        result = client(operation, *args, payload=payload)
        remaining(deadline)
        return result

    try:
        parsed = urlsplit(config.base_url)
        if (
            not IMMUTABLE_IMAGE.fullmatch(config.image)
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not math.isfinite(config.timeout_seconds)
            or config.timeout_seconds <= 0
            or not math.isfinite(config.poll_seconds)
            or config.poll_seconds <= 0
        ):
            raise DeploymentError("invalid_configuration")
        service = describe_service(call_aws, config)
        previous = service["taskDefinition"]
        ensure_revision(service, previous)
        result = call_aws(
            "describe-task-definition", "--task-definition", previous, "--include", "TAGS"
        )
        task = result["taskDefinition"]
        if "FARGATE" not in task.get("requiresCompatibilities", []):
            raise DeploymentError("fargate_task_required")
        if "tags" in result:
            task["tags"] = [tag for tag in result["tags"] if not tag["key"].startswith("aws:")]
        old_image = previous_image(call_aws, config, task, previous)
        original = next(
            c["image"] for c in task["containerDefinitions"] if c["name"] == config.container
        )
        rollback = (
            previous
            if original == old_image
            else register(call_aws, task, config.container, old_image)
        )
        report.update(
            previous_task_definition=previous,
            previous_image=old_image,
            rollback_task_definition=rollback,
            image=config.image,
        )
        revision = register(call_aws, task, config.container, config.image)
        report["task_definition"] = revision

        def activate(target: str) -> None:
            call_aws(
                "update-service",
                "--cluster",
                config.cluster,
                "--service",
                config.service,
                "--task-definition",
                target,
            )
            call_aws(
                "wait", "services-stable", "--cluster", config.cluster, "--services", config.service
            )
            ensure_revision(describe_service(call_aws, config), target)
            ready(replace(config, timeout_seconds=remaining(deadline)))
            remaining(deadline)

        activate(revision)
        if check is None:
            from scripts.check_api import verify_api

            check = verify_api
        try:
            seconds = remaining(deadline)
            check(
                config.base_url,
                timeout=min(seconds, 120),
                request_timeout=min(seconds, 5),
            )
        except Exception:
            remaining(deadline)
            raise DeploymentError("post_deploy_check_failed") from None
        remaining(deadline)
        report.update(status="passed", readiness="passed", contract="passed")
        return report
    except Exception as error:
        report["failure"] = (
            str(error) if isinstance(error, DeploymentError) else "deployment_failed"
        )
        if updated:
            try:
                deadline = time.monotonic() + config.timeout_seconds
                activate(rollback)
                report["rollback"] = "passed"
            except Exception:
                report["rollback"] = "failed"
                raise DeploymentError("deployment_and_rollback_failed") from None
        raise DeploymentError(report["failure"]) from None
    finally:
        config.report.parent.mkdir(parents=True, exist_ok=True)
        config.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("cluster", "service", "container", "image", "base-url"):
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--report", type=Path, default=Path("reports/cd/deployment.json"))
    try:
        deploy(Config(**vars(parser.parse_args())))
    except DeploymentError as error:
        print(f"ECS deployment failed: {error}")
        return 1
    except Exception:
        print("ECS deployment failed: deployment_report_unavailable")
        return 1
    print("ECS deployment and API checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
