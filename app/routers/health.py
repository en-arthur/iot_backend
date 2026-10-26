"""Health endpoint used by uptime checks and the README smoke test."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, ping_database
from app.schemas import DatabaseHealth, HealthResponse, MqttHealth

router = APIRouter(tags=["health"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Cheap root probe so hosting platforms see a 200 without touching the DB."""
    return {"service": "relayboard-backend", "status": "running"}


@router.get("/health", response_model=HealthResponse, summary="Liveness and dependencies")
async def health(request: Request, session: SessionDep) -> HealthResponse:
    """Report MQTT and database connectivity.

    Always returns HTTP 200 so a monitor can read the body; ``status`` is
    ``degraded`` when either dependency is down.
    """
    database_ok, database_error = await ping_database(session)

    mqtt_service = getattr(request.app.state, "mqtt", None)
    mqtt_status = mqtt_service.status() if mqtt_service is not None else None
    mqtt_health = MqttHealth(**mqtt_status) if mqtt_status else None
    mqtt_ok = bool(mqtt_status and mqtt_status.get("connected"))

    return HealthResponse(
        status="ok" if database_ok and mqtt_ok else "degraded",
        database=DatabaseHealth(connected=database_ok, error=database_error),
        mqtt=mqtt_health,
        timestamp=datetime.now(UTC),
    )
