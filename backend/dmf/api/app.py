"""FastAPI application: REST API, WebSocket and lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from dmf import __version__
from dmf.api import routes, ws
from dmf.logging_setup import setup_logging
from dmf.plant.model import load_plant
from dmf.services.tracker_service import TrackerService
from dmf.settings import Settings, get_settings, redact_url

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, service: TrackerService | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level, settings.log_format)
        svc = service
        if svc is None:
            plant = load_plant(settings.plant_config_path, settings.nodes_config_path)
            svc = TrackerService(plant, settings)
        app.state.service = svc
        app.state.settings = settings
        log.info(
            "DMF backend %s starting (source=%s, endpoint=%s, node map=%s)",
            __version__,
            settings.source_label,
            redact_url(settings.opcua_endpoint),
            svc.plant.node_verification,
        )
        await svc.start()
        broadcaster = ws.Broadcaster(svc, settings.ws_max_rate_hz)
        app.state.broadcaster = broadcaster
        await broadcaster.start()
        try:
            yield
        finally:
            await broadcaster.stop()
            await svc.stop()
            log.info("DMF backend stopped")

    app = FastAPI(
        title="DMF Workpiece Tracking API",
        version=__version__,
        description="Read-only tracking of workpieces in the Digital Model Factory. " "The backend never writes to the factory.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    if settings.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET"], allow_headers=["*"])
    app.include_router(routes.router)
    app.include_router(ws.router)

    @app.get("/docs", include_in_schema=False)
    async def docs_redirect() -> RedirectResponse:
        """The project description names /docs for the automatic API documentation."""
        return RedirectResponse("/api/docs")

    async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        # the database went away after start-up: answer 503 (try again), not 500
        log.warning("database unavailable for %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return JSONResponse({"detail": "The database is not reachable at the moment. Please try again shortly."}, 503)

    for exc_type in (DBAPIError, PoolTimeout, OSError, TimeoutError):
        app.add_exception_handler(exc_type, database_unavailable)
    return app
