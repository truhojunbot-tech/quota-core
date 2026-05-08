"""FastAPI dashboard app factory."""

from __future__ import annotations

from typing import Any, Callable

from quota_core.session import build_empty_session_report

SessionReportProvider = Callable[..., dict[str, Any]]


def create_app(*, claude_session_report_provider: SessionReportProvider | None = None):
    """Create the dashboard app."""

    try:
        from fastapi import FastAPI
    except Exception as exc:
        raise RuntimeError("FastAPI is required to serve the dashboard") from exc

    app = FastAPI()

    @app.get("/")
    def index():
        return {"status": "quota_core dashboard skeleton"}

    @app.get("/api/claude_session_report")
    def claude_session_report(window: str | None = None, since: str | None = None, redaction: str | None = None):
        if claude_session_report_provider is not None:
            try:
                return claude_session_report_provider(window=window, since=since, redaction=redaction)
            except Exception as exc:
                return build_empty_session_report(window=window, since=since, redaction=redaction, errors=[f"claude session report failed: {exc}"])
        return build_empty_session_report(window=window, since=since, redaction=redaction)

    return app
