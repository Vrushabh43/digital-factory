"""ASGI entry point: ``uvicorn dmf.main:app`` (run with ONE worker — the tracker lives in-process)."""

from dmf.api.app import create_app

app = create_app()
