"""Resolve only a trusted main-branch commit whose CI gate passed."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class ReleaseError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repository: str) -> None:
        self.root = f"{os.getenv('GITHUB_API_URL', 'https://api.github.com')}/repos/{repository}"

    def get(self, path: str) -> dict:
        request = Request(
            f"{self.root}/{path}",
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except (HTTPError, URLError, ValueError) as exc:
            raise ReleaseError("Nao foi possivel verificar a release na API do GitHub.") from exc


def resolve_release(event_name: str, event: dict, repository: str, ref: str, api) -> dict:
    if event_name == "workflow_run":
        run = event["workflow_run"]
        if (
            run.get("conclusion") != "success"
            or run.get("event") != "push"
            or run.get("head_branch") != "main"
            or run.get("head_repository", {}).get("full_name") != repository
        ):
            raise ReleaseError("CD automatico exige CI verde de push na main deste repositorio.")
        sha, environment, tag = run["head_sha"], "dev", ""
    elif event_name == "workflow_dispatch":
        if ref != "refs/heads/main":
            raise ReleaseError("Execute o workflow de CD a partir da branch main.")
        inputs = event.get("inputs", {})
        sha = inputs.get("commit_sha", "")
        environment = inputs.get("environment", "")
        tag = inputs.get("release_tag", "")
    else:
        raise ReleaseError("Evento de CD nao permitido.")

    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("Informe o SHA completo de 40 caracteres do commit.")
    if environment not in {"dev", "staging", "production"}:
        raise ReleaseError("Ambiente de CD invalido.")
    comparison = api.get(f"compare/{sha}...main")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise ReleaseError("O commit de release precisa pertencer ao historico da main.")

    query = urlencode({"head_sha": sha, "event": "push", "per_page": 100})
    runs = api.get(f"actions/workflows/ci.yml/runs?{query}")["workflow_runs"]
    matching = [
        run for run in runs
        if run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("head_repository", {}).get("full_name") == repository
    ]
    if not matching:
        raise ReleaseError("Nao existe execucao de ci.yml para este commit na main.")
    latest = max(matching, key=lambda run: run["id"])
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ReleaseError("A execucao mais recente da CI deste commit precisa estar verde.")
    jobs = api.get(f"actions/runs/{latest['id']}/jobs?filter=latest&per_page=100")["jobs"]
    if not any(job["name"] == "CI required" and job["conclusion"] == "success" for job in jobs):
        raise ReleaseError("O gate CI required deste commit nao foi aprovado.")

    if environment == "production":
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", tag):
            raise ReleaseError("Production exige uma tag de release vMAJOR.MINOR.PATCH.")
        target = api.get(f"git/ref/tags/{quote(tag, safe='')}")["object"]
        for _ in range(5):
            if target["type"] != "tag":
                break
            target = api.get(f"git/tags/{target['sha']}")["object"]
        if target["type"] != "commit" or target["sha"] != sha:
            raise ReleaseError("A tag de production precisa apontar para o commit aprovado.")

    return {"sha": sha, "environment": environment, "ci_run_id": str(latest["id"])}


def main() -> None:
    try:
        result = resolve_release(
            os.environ["GITHUB_EVENT_NAME"],
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")),
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_REF"],
            GitHub(os.environ["GITHUB_REPOSITORY"]),
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
        print("Commit e gate de CI verificados para CD.")
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
