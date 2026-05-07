"""FastAPI dashboard app factory skeleton."""

from __future__ import annotations


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

    return app
