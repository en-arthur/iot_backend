from __future__ import annotations

import os
from uuid import UUID

# Settings are read at import time by app.main/create_app, so populate the
# environment before importing anything from ``app``.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("LOG_JSON", "false")

import pytest
import pytest_asyncio
from app.auth import get_current_user_id
from app.db import get_session
from app.main import create_app
from app.models import Board, RelayState  # noqa: F401 - register metadata
from app.mqtt import MqttUnavailable
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeMqttService:
    """Stand-in for MqttService so tests never touch a broker."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.published: list[dict[str, object]] = []

    async def publish_relay_command(self, board_id: str, relay_number: int, state: str) -> None:
        if not self.connected:
            raise MqttUnavailable("MQTT broker is not connected")
        self.published.append({"board_id": board_id, "relay_number": relay_number, "state": state})

    def status(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "client_id": "test-client",
            "topic_prefix": "boards",
            "last_error": None,
            "last_message_at": None,
            "last_connected_at": None,
            "reconnect_count": 0,
            "messages_processed": 0,
            "pending_publishes": 0,
        }


@pytest_asyncio.fixture
async def engine():
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def fake_mqtt() -> FakeMqttService:
    return FakeMqttService()


@pytest.fixture
def app(session_factory, fake_mqtt):
    application = create_app()

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_user_id] = lambda: USER_ID
    application.state.mqtt = fake_mqtt
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def add_board(
    session_factory,
    board_id: str,
    *,
    owner_id: UUID | None = USER_ID,
    status: str = "online",
) -> Board:
    async with session_factory() as session:
        board = Board(id=board_id, name=board_id, owner_id=owner_id, status=status)
        session.add(board)
        await session.commit()
        return board
