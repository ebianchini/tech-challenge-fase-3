"""Minimal JSON audit events: request content never enters a LogRecord.

The HTTP adapter supplies only bounded operational metadata. Server logging uses
a separate formatter which deliberately ignores messages, arguments and traces.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)
_ERRORS = frozenset({"INVALID_INPUT_SCHEMA", "MODEL_UNAVAILABLE", "INFERENCE_FAILED"})


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class SafeServerFormatter(logging.Formatter):
    """Keep server severity visible without serializing untrusted messages."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {"timestamp": _timestamp(), "level": record.levelname, "event": "server_log"}
        )


class _AuditFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = record.audit_fields
        event = {
            "timestamp": _timestamp(),
            "level": record.levelname,
            "event": "http_request",
            **{
                key: fields[key]
                for key in (
                    "request_id",
                    "method",
                    "route",
                    "status_code",
                    "latency_ms",
                    "model_version",
                )
            },
        }
        if fields.get("error_type") in _ERRORS:
            event["error_type"] = fields["error_type"]
        return json.dumps(event, ensure_ascii=True, allow_nan=False)


class _StdoutHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Resolve stdout at emission time, including under test capture or a supervisor.
        sys.stdout.write(self.format(record) + "\n")
        sys.stdout.flush()


_audit = logging.getLogger("triage.audit")
_audit.setLevel(logging.INFO)
_audit.propagate = False
if not _audit.handlers:
    _handler = _StdoutHandler()
    _handler.setFormatter(_AuditFormatter())
    _audit.addHandler(_handler)


def _request_id(scope: Scope) -> str:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == b"x-request-id"]
    if len(values) == 1:
        try:
            value = values[0].decode("ascii")
            if str(UUID(value)) == value:
                return value
        except (UnicodeError, ValueError):
            pass
    return str(uuid4())


class HTTPLoggingMiddleware:
    """Log once per response, outside ServerErrorMiddleware, without reading bodies.

    Only canonical UUIDs may enter the operational correlation header/log. The
    optional opaque ID in the inference body remains part of the HTTP contract.
    """

    def __init__(
        self,
        app: ASGIApp,
        route_templates: Iterable[str],
        model_version: Callable[[], str],
    ) -> None:
        self.app = app
        self.route_templates = frozenset(route_templates)
        self.model_version = model_version

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        state = scope.setdefault("state", {})
        request_id = _request_id(scope)
        state["request_id"] = request_id
        status = 500
        completed_at: float | None = None

        async def logged_send(message: Message) -> None:
            nonlocal status, completed_at
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", ())
                    if key.lower() not in {b"x-request-id", b"x-response-time-ms"}
                ]
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (
                            b"x-response-time-ms",
                            f"{(time.perf_counter() - started) * 1000:.3f}".encode("ascii"),
                        ),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed_at = time.perf_counter()

        try:
            await self.app(scope, receive, logged_send)
        finally:
            template = getattr(scope.get("route"), "path", None)
            method = scope.get("method", "").upper()
            fields = {
                "request_id": request_id,
                "method": method if method in _METHODS else "OTHER",
                "route": template if template in self.route_templates else "unmatched",
                "status_code": status,
                "latency_ms": round(((completed_at or time.perf_counter()) - started) * 1000, 3),
                "model_version": self.model_version(),
                "error_type": state.get("error_type"),
            }
            _audit.log(
                logging.ERROR if status >= 500 else logging.INFO, "", extra={"audit_fields": fields}
            )
