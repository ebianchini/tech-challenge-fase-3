from __future__ import annotations

import json
import subprocess

import pytest

from scripts import init_airflow


@pytest.fixture(autouse=True)
def administrator_environment(monkeypatch):
    monkeypatch.setenv("AIRFLOW_ADMIN_USER", "demo-admin")
    monkeypatch.setenv("AIRFLOW_ADMIN_PASSWORD", "private-demo-password")
    monkeypatch.setenv("AIRFLOW_ADMIN_EMAIL", "demo@example.com")


def _stub_commands(monkeypatch, responses):
    commands = []
    pending = iter(responses)

    def run(command, **kwargs):
        commands.append(command)
        returncode, stdout = next(pending)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout)

    monkeypatch.setattr(init_airflow.subprocess, "run", run)
    return commands


def test_failed_migration_prevents_user_initialization(monkeypatch):
    commands = _stub_commands(monkeypatch, [(8, "")])

    with pytest.raises(SystemExit, match="initialization failed.*8") as failure:
        init_airflow.main()

    assert len(commands) == 1
    assert commands[0][1:3] == ["db", "migrate"]
    assert "private-demo-password" not in str(failure.value)


def test_existing_administrator_preserves_credentials(monkeypatch, capsys):
    monkeypatch.delenv("AIRFLOW_ADMIN_PASSWORD")
    monkeypatch.delenv("AIRFLOW_ADMIN_EMAIL")
    commands = _stub_commands(
        monkeypatch,
        [(0, ""), (0, json.dumps([{"username": "demo-admin"}]))],
    )

    init_airflow.main()

    assert len(commands) == 2
    assert "credentials preserved" in capsys.readouterr().out


def test_missing_administrator_uses_configured_credentials(monkeypatch):
    commands = _stub_commands(
        monkeypatch,
        [(0, ""), (0, json.dumps([{"username": "another-user"}])), (0, "")],
    )

    init_airflow.main()

    assert len(commands) == 3
    creation = commands[-1]
    assert creation[1:3] == ["users", "create"]
    options = dict(zip(creation[3::2], creation[4::2], strict=True))
    assert options["--username"] == "demo-admin"
    assert options["--email"] == "demo@example.com"
    assert options["--password"] == "private-demo-password"
    assert options["--role"] == "Admin"


def test_failed_user_listing_prevents_creation(monkeypatch, capsys):
    commands = _stub_commands(monkeypatch, [(0, ""), (6, "sensitive CLI output")])

    with pytest.raises(SystemExit, match="initialization failed.*6") as failure:
        init_airflow.main()

    assert len(commands) == 2
    assert "sensitive CLI output" not in str(failure.value)
    assert "sensitive CLI output" not in capsys.readouterr().out


def test_invalid_user_listing_prevents_creation_without_echoing_body(monkeypatch, capsys):
    commands = _stub_commands(monkeypatch, [(0, ""), (0, "sensitive invalid body")])

    with pytest.raises(ValueError) as failure:
        init_airflow.main()

    assert len(commands) == 2
    assert "sensitive invalid body" not in str(failure.value)
    assert "sensitive invalid body" not in capsys.readouterr().out


def test_failed_creation_does_not_expose_password_in_exception(monkeypatch):
    commands = _stub_commands(monkeypatch, [(0, ""), (0, "[]"), (9, "")])

    with pytest.raises(SystemExit, match="initialization failed.*9") as failure:
        init_airflow.main()

    assert "private-demo-password" in commands[-1]
    assert "private-demo-password" not in str(failure.value)
    assert "--password" not in str(failure.value)
