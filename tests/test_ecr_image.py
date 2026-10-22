from __future__ import annotations

import json
import subprocess
from copy import deepcopy

import pytest

from scripts import ecr_image

REPOSITORY = "triage/api"
TAG = "git-" + "a" * 40
URI = "123456789012.dkr.ecr.us-east-1.amazonaws.com/triage/api"
DIGEST = "sha256:" + "b" * 64
PRIVATE_MARKER = "synthetic-aws-private-marker"


def repository_document():
    return {
        "repositories": [
            {
                "repositoryName": REPOSITORY,
                "repositoryUri": URI,
                "registryId": "123456789012",
                "imageTagMutability": "IMMUTABLE",
            }
        ]
    }


def image_document():
    return {
        "images": [
            {
                "repositoryName": REPOSITORY,
                "imageId": {"imageTag": TAG, "imageDigest": DIGEST},
                "imageManifest": PRIVATE_MARKER,
            }
        ],
        "failures": [],
    }


def missing_document():
    return {
        "images": [],
        "failures": [
            {
                "imageId": {"imageTag": TAG},
                "failureCode": "ImageNotFound",
                "failureReason": PRIVATE_MARKER,
            }
        ],
    }


class Runner:
    def __init__(self, *documents):
        self.documents = list(documents)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        document = self.documents.pop(0)
        if isinstance(document, Exception):
            raise document
        return subprocess.CompletedProcess(command, 0, json.dumps(document), PRIVATE_MARKER)


def test_existing_commit_tag_returns_digest_without_writing_registry(capsys):
    runner = Runner(repository_document(), image_document())
    assert ecr_image.resolve_image(REPOSITORY, TAG, run=runner, require_image=True) == {
        "repository_uri": URI,
        "digest": DIGEST,
    }
    assert [command[2] for command, _ in runner.calls] == [
        "describe-repositories",
        "batch-get-image",
    ]
    for command, kwargs in runner.calls:
        assert command[:2] == ["aws", "ecr"]
        assert command[-3:] == ["--output", "json", "--no-cli-pager"]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 30}
    assert runner.calls[1][0][3:7] == [
        "--repository-name",
        REPOSITORY,
        "--image-ids",
        f"imageTag={TAG}",
    ]
    assert capsys.readouterr().out == ""


def test_image_not_found_is_the_only_accepted_absence():
    runner = Runner(repository_document(), missing_document())
    assert ecr_image.resolve_image(REPOSITORY, TAG, run=runner) == {
        "repository_uri": URI,
        "digest": "",
    }


def test_require_image_rejects_absent_commit_without_modifying_registry():
    runner = Runner(repository_document(), missing_document())
    with pytest.raises(ecr_image.ECRImageError, match="image_required"):
        ecr_image.resolve_image(REPOSITORY, TAG, run=runner, require_image=True)
    assert len(runner.calls) == 2


@pytest.mark.parametrize("mutability", ["MUTABLE", "IMMUTABLE_WITH_EXCLUSION", None])
def test_requires_strict_immutability_even_when_exclusions_exist(mutability):
    repository = repository_document()
    repository["repositories"][0]["imageTagMutability"] = mutability
    runner = Runner(repository)
    with pytest.raises(ecr_image.ECRImageError, match="immutable_repository_required"):
        ecr_image.resolve_image(REPOSITORY, TAG, run=runner)
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "tag", ["latest", "git-main", "git-" + "a" * 39, "git-" + "g" * 40, TAG + "\ndigest=evil"]
)
def test_rejects_non_commit_tags_before_aws(tag):
    runner = Runner()
    with pytest.raises(ecr_image.ECRImageError, match="invalid_commit_tag"):
        ecr_image.resolve_image(REPOSITORY, tag, run=runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    "repository", ["a", "BadName", "../triage", "triage//api", "triage/api\ndigest=evil", "x" * 257]
)
def test_rejects_invalid_repository_before_aws(repository):
    runner = Runner()
    with pytest.raises(ecr_image.ECRImageError, match="invalid_repository"):
        ecr_image.resolve_image(repository, TAG, run=runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    "uri",
    [
        "https://" + URI,
        "1234.dkr.ecr.us-east-1.amazonaws.com/triage/api",
        "123456789012.dkr.ecr.us-east-1.evil.example/triage/api",
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/other/api",
        URI + ":latest",
        URI + "\ndigest=evil",
        None,
    ],
)
def test_rejects_malformed_or_different_repository_uri(uri):
    repository = repository_document()
    repository["repositories"][0]["repositoryUri"] = uri
    with pytest.raises(ecr_image.ECRImageError, match="invalid_repository_uri"):
        ecr_image.resolve_image(REPOSITORY, TAG, run=Runner(repository))


def test_supports_china_ecr_domain():
    repository = repository_document()
    uri = "123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn/triage/api"
    repository["repositories"][0]["repositoryUri"] = uri
    assert (
        ecr_image.resolve_image(REPOSITORY, TAG, run=Runner(repository, image_document()))[
            "repository_uri"
        ]
        == uri
    )


@pytest.mark.parametrize("failure_code", ["AccessDenied", "KmsError", "InvalidImageTag", None])
def test_never_treats_access_or_lookup_error_as_absent_image(failure_code):
    document = missing_document()
    document["failures"][0]["failureCode"] = failure_code
    with pytest.raises(ecr_image.ECRImageError, match="image_lookup_failed") as exc:
        ecr_image.resolve_image(REPOSITORY, TAG, run=Runner(repository_document(), document))
    assert PRIVATE_MARKER not in str(exc.value)


@pytest.mark.parametrize(
    "digest", [None, "sha256:short", "sha512:" + "a" * 64, "sha256:" + "z" * 64, DIGEST + "\n"]
)
def test_rejects_invalid_image_digest(digest):
    document = image_document()
    document["images"][0]["imageId"]["imageDigest"] = digest
    with pytest.raises(ecr_image.ECRImageError, match="invalid_image_digest"):
        ecr_image.resolve_image(REPOSITORY, TAG, run=Runner(repository_document(), document))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(images=[]),
        lambda document: document.update(failures=missing_document()["failures"]),
        lambda document: document["images"].append(deepcopy(document["images"][0])),
        lambda document: document["images"][0]["imageId"].update(imageTag="git-" + "c" * 40),
        lambda document: document["images"][0].update(repositoryName="other/api"),
        lambda document: document.update(images=None),
        lambda document: document.pop("failures"),
    ],
)
def test_rejects_ambiguous_or_wrong_image_response(mutation):
    document = image_document()
    mutation(document)
    with pytest.raises(ecr_image.ECRImageError):
        ecr_image.resolve_image(REPOSITORY, TAG, run=Runner(repository_document(), document))


def test_wrong_missing_tag_is_not_accepted():
    document = missing_document()
    document["failures"][0]["imageId"]["imageTag"] = "git-" + "c" * 40
    with pytest.raises(ecr_image.ECRImageError, match="image_lookup_failed"):
        ecr_image.resolve_image(REPOSITORY, TAG, run=Runner(repository_document(), document))


def test_cli_aws_error_never_leaks_stdout_stderr_or_error_text(monkeypatch, tmp_path, capsys):
    output = tmp_path / "output.txt"
    failure = subprocess.CalledProcessError(1, "aws", output=PRIVATE_MARKER, stderr=PRIVATE_MARKER)
    monkeypatch.setattr(ecr_image.subprocess, "run", Runner(repository_document(), failure))
    monkeypatch.setattr(
        "sys.argv",
        [
            "ecr_image",
            "--repository",
            REPOSITORY,
            "--tag",
            TAG,
            "--output",
            str(output),
        ],
    )
    assert ecr_image.main() == 1
    assert not output.exists()
    assert capsys.readouterr().out == "ECR image lookup failed: aws_operation_failed\n"


@pytest.mark.parametrize("existing", [True, False])
def test_cli_appends_only_validated_github_outputs(monkeypatch, tmp_path, capsys, existing):
    output = tmp_path / "output.txt"
    output.write_text("previous=value\n", encoding="utf-8")
    runner = Runner(repository_document(), image_document() if existing else missing_document())
    monkeypatch.setattr(ecr_image.subprocess, "run", runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ecr_image",
            "--repository",
            REPOSITORY,
            "--tag",
            TAG,
            "--output",
            str(output),
        ],
    )
    assert ecr_image.main() == 0
    assert output.read_text() == (
        f"previous=value\nrepository_uri={URI}\ndigest={DIGEST if existing else ''}\n"
    )
    assert PRIVATE_MARKER not in output.read_text()
    assert capsys.readouterr().out == "ECR image lookup passed.\n"


def test_cli_require_image_does_not_write_absent_digest(monkeypatch, tmp_path, capsys):
    output = tmp_path / "output.txt"
    monkeypatch.setattr(
        ecr_image.subprocess, "run", Runner(repository_document(), missing_document())
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ecr_image",
            "--repository",
            REPOSITORY,
            "--tag",
            TAG,
            "--output",
            str(output),
            "--require-image",
        ],
    )
    assert ecr_image.main() == 1
    assert not output.exists()
    assert capsys.readouterr().out == "ECR image lookup failed: image_required\n"
