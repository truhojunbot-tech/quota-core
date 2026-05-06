"""FastAPI dashboard app factory skeleton."""

from __future__ import annotations

from quota_core.session import build_empty_session_report


def create_app():
    """Create the dashboard app.

    FastAPI wiring will be implemented when dashboard extraction begins.
    """

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
        return build_empty_session_report(window=window, since=since, redaction=redaction)

    return app
