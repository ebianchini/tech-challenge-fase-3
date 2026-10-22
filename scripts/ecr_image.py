"""Resolve immutable ECR commit tags without creating or changing registry resources."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z")
COMMIT_TAG = re.compile(r"git-[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}\Z")
REPOSITORY_URI = re.compile(
    r"(?P<account>[0-9]{12})\.dkr\.ecr\.[a-z]{2}(?:-[a-z]+)+-[0-9]+"
    r"\.amazonaws\.com(?:\.cn)?/(?P<repository>[a-z0-9._/-]+)\Z"
)


class ECRImageError(RuntimeError):
    """Only predefined codes, never AWS output or credentials, leave this helper."""


def _aws(operation: str, *args: str, run) -> dict:
    command = ["aws", "ecr", operation, *args, "--output", "json", "--no-cli-pager"]
    try:
        result = run(command, capture_output=True, text=True, check=True, timeout=30)
        document = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ECRImageError("aws_operation_failed") from None
    if not isinstance(document, dict):
        raise ECRImageError("invalid_aws_response")
    return document


def resolve_image(
    repository: str, tag: str, *, run=None, require_image: bool = False
) -> dict[str, str]:
    """Return a validated repository URI and digest (empty only on ImageNotFound).

    Repository tag mutability must be exactly IMMUTABLE: exclusions could allow
    overwriting the commit tag. Both AWS operations are read-only. ``run`` accepts
    the same arguments as subprocess.run and supports credential-free tests.
    """
    if not 2 <= len(repository) <= 256 or not REPOSITORY.fullmatch(repository):
        raise ECRImageError("invalid_repository")
    if not COMMIT_TAG.fullmatch(tag):
        raise ECRImageError("invalid_commit_tag")
    run = run or subprocess.run
    document = _aws("describe-repositories", "--repository-names", repository, run=run)
    repositories = document.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise ECRImageError("invalid_repository_response")
    current = repositories[0]
    if not isinstance(current, dict) or current.get("repositoryName") != repository:
        raise ECRImageError("invalid_repository_response")
    if current.get("imageTagMutability") != "IMMUTABLE":
        raise ECRImageError("immutable_repository_required")
    uri = current.get("repositoryUri")
    match = REPOSITORY_URI.fullmatch(uri) if isinstance(uri, str) else None
    if (
        match is None
        or match["repository"] != repository
        or current.get("registryId", match["account"]) != match["account"]
    ):
        raise ECRImageError("invalid_repository_uri")

    document = _aws(
        "batch-get-image",
        "--repository-name",
        repository,
        "--image-ids",
        f"imageTag={tag}",
        run=run,
    )
    images, failures = document.get("images"), document.get("failures")
    if not isinstance(images, list) or not isinstance(failures, list):
        raise ECRImageError("invalid_image_response")
    digest = ""
    if failures:
        if images or len(failures) != 1:
            raise ECRImageError("image_lookup_failed")
        failure = failures[0]
        if (
            not isinstance(failure, dict)
            or failure.get("failureCode") != "ImageNotFound"
            or not isinstance(failure.get("imageId"), dict)
            or failure["imageId"].get("imageTag") != tag
        ):
            raise ECRImageError("image_lookup_failed")
    elif len(images) == 1:
        image = images[0]
        if (
            not isinstance(image, dict)
            or image.get("repositoryName") != repository
            or not isinstance(image.get("imageId"), dict)
            or image["imageId"].get("imageTag") != tag
        ):
            raise ECRImageError("invalid_image_response")
        digest = image["imageId"].get("imageDigest")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ECRImageError("invalid_image_digest")
        digest = digest.lower()
    else:
        raise ECRImageError("invalid_image_response")
    if require_image and not digest:
        raise ECRImageError("image_required")
    return {"repository_uri": uri, "digest": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", required=True, type=Path, help="GitHub Actions output file.")
    parser.add_argument("--require-image", action="store_true")
    args = parser.parse_args()
    try:
        outputs = resolve_image(args.repository, args.tag, require_image=args.require_image)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a", encoding="utf-8") as stream:
            for name, value in outputs.items():
                stream.write(f"{name}={value}\n")
    except ECRImageError as error:
        print(f"ECR image lookup failed: {error}")
        return 1
    except Exception:
        print("ECR image lookup failed: output_unavailable")
        return 1
    print("ECR image lookup passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
