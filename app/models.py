"""SQLModel table definitions.

The canonical schema lives in ``supabase/migrations``; these models mirror it
for queries and type hints. Foreign keys to ``auth.users`` are declared only in
the migrations so the models can be created against a plain Postgres/SQLite
database in tests.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Column, DateTime, Double, Integer, func
from sqlmodel import Field, SQLModel


class Board(SQLModel, table=True):
    __tablename__ = "boards"

    id: str = Field(primary_key=True, max_length=128)
    name: str | None = Field(default=None)
    owner_id: UUID | None = Field(default=None, index=True)
    status: str = Field(default="offline", max_length=16)
    last_seen: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime | None = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )


class Reading(SQLModel, table=True):
    __tablename__ = "readings"

    id: int | None = Field(
        sa_column=Column(
            BigInteger().with_variant(Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
    )
    board_id: str = Field(foreign_key="boards.id", max_length=128)
    sensor: str = Field(max_length=128)
    value: float = Field(sa_column=Column(Double, nullable=False))
    ts: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class LatestReading(SQLModel, table=True):
    __tablename__ = "latest_readings"

    board_id: str = Field(foreign_key="boards.id", primary_key=True, max_length=128)
    sensor: str = Field(primary_key=True, max_length=128)
    value: float = Field(sa_column=Column(Double, nullable=False))
    ts: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class RelayState(SQLModel, table=True):
    __tablename__ = "relay_states"

    board_id: str = Field(foreign_key="boards.id", primary_key=True, max_length=128)
    relay_number: int = Field(primary_key=True)
    state: str = Field(max_length=8)
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )
