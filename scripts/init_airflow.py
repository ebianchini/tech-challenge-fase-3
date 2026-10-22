"""Migrate Airflow metadata and create a demo administrator without hiding failures."""

from __future__ import annotations

import json
import os
import subprocess


def _run(arguments: list[str], *, capture_output: bool = False) -> str:
    result = subprocess.run(
        ["airflow", *arguments],
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if result.returncode:
        # Do not include arguments: the user creation command contains a password.
        raise SystemExit(f"Airflow initialization failed (exit {result.returncode}).")
    return result.stdout or ""


def main() -> None:
    _run(["db", "migrate"])
    username = os.environ["AIRFLOW_ADMIN_USER"]
    users = json.loads(_run(["users", "list", "--output", "json"], capture_output=True))
    if any(user["username"] == username for user in users):
        print("Airflow administrator already exists; credentials preserved.")
        return
    _run(
        [
            "users", "create",
            "--username", username,
            "--firstname", "Local",
            "--lastname", "Demo",
            "--role", "Admin",
            "--email", os.environ["AIRFLOW_ADMIN_EMAIL"],
            "--password", os.environ["AIRFLOW_ADMIN_PASSWORD"],
        ]
    )


if __name__ == "__main__":
    main()
