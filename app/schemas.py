"""Request and response models for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RelayCommandRequest(BaseModel):
    state: Literal["on", "off"] = Field(
        description="Desired relay state. The board's report is the source of truth."
    )


class ClaimBoardRequest(BaseModel):
    board_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Board ID exactly as printed on the hardware.",
    )


class BoardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str | None = None
    owner_id: UUID | None = None
    status: str
    last_seen: datetime | None = None
    created_at: datetime | None = None


class RelayCommandResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    board_id: str
    relay: int
    state: Literal["on", "off"]
    message: str = "Command published; waiting for the board to report the new state."


class DatabaseHealth(BaseModel):
    connected: bool
    error: str | None = None


class MqttHealth(BaseModel):
    connected: bool
    client_id: str
    topic_prefix: str
    last_error: str | None = None
    last_message_at: str | None = None
    last_connected_at: str | None = None
    reconnect_count: int = 0
    messages_processed: int = 0
    pending_publishes: int = 0


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: DatabaseHealth
    mqtt: MqttHealth | None = None
    timestamp: datetime
