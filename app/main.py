"""FastAPI application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import build_verifier
from app.batching import ReadingBatcher
from app.config import get_settings
from app.db import dispose_engine, get_session_factory, ping_database
from app.logging import configure_logging
from app.mqtt import MqttService
from app.routers import boards, health

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app.state.settings = settings
    app.state.jwt_verifier = build_verifier(settings)

    database_ok, database_error = await ping_database()
    if database_ok:
        logger.info("database connection established")
    else:
        # Start anyway: /health reports the failure and MQTT keeps retrying.
        logger.error("database is unreachable at startup", extra={"error": database_error})

    session_factory = get_session_factory()

    batcher = ReadingBatcher(
        session_factory,
        max_batch_size=settings.reading_batch_max_size,
        flush_interval=settings.reading_batch_flush_interval,
        queue_max_size=settings.reading_queue_max_size,
    )
    await batcher.start()
    app.state.batcher = batcher

    mqtt_service = MqttService(settings, session_factory, batcher)
    app.state.mqtt = mqtt_service
    await mqtt_service.start()

    logger.info(
        "backend started",
        extra={
            "environment": settings.app_env,
            "mqtt_host": settings.mqtt_host,
            "mqtt_port": settings.mqtt_port,
            "cors_origins": settings.cors_origins,
        },
    )

    try:
        yield
    finally:
        logger.info("backend shutting down")
        await mqtt_service.stop()
        await batcher.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Relayboard backend",
        version="0.1.0",
        summary="MQTT bridge between ESP32 boards and Supabase Postgres.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    app.include_router(health.router)
    app.include_router(boards.router)

    return app


app = create_app()
