from __future__ import annotations

from app.models import Board, RelayState
from sqlalchemy import func, select

from tests.conftest import OTHER_USER_ID, USER_ID, add_board


class TestSetRelay:
    async def test_owned_online_board_returns_202_and_publishes(
        self, client, fake_mqtt, session_factory
    ):
        await add_board(session_factory, "board-1")

        response = await client.post("/boards/board-1/relays/2", json={"state": "on"})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "accepted"
        assert body["board_id"] == "board-1"
        assert body["relay"] == 2
        assert body["state"] == "on"

        assert fake_mqtt.published == [{"board_id": "board-1", "relay_number": 2, "state": "on"}]

    async def test_does_not_write_relay_state(self, client, session_factory):
        await add_board(session_factory, "board-1")

        response = await client.post("/boards/board-1/relays/1", json={"state": "off"})

        assert response.status_code == 202
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(RelayState))
        assert count == 0

    async def test_offline_board_returns_409(self, client, fake_mqtt, session_factory):
        await add_board(session_factory, "board-1", status="offline")

        response = await client.post("/boards/board-1/relays/1", json={"state": "on"})

        assert response.status_code == 409
        assert response.json()["detail"] == "Board is offline"
        assert fake_mqtt.published == []

    async def test_other_owners_board_returns_403(self, client, fake_mqtt, session_factory):
        await add_board(session_factory, "board-1", owner_id=OTHER_USER_ID)

        response = await client.post("/boards/board-1/relays/1", json={"state": "on"})

        assert response.status_code == 403
        assert response.json()["detail"] == "Not your board"
        assert fake_mqtt.published == []

    async def test_missing_board_returns_404(self, client, fake_mqtt):
        response = await client.post("/boards/nope/relays/1", json={"state": "on"})

        assert response.status_code == 404
        assert fake_mqtt.published == []

    async def test_invalid_state_returns_422(self, client, fake_mqtt, session_factory):
        await add_board(session_factory, "board-1")

        response = await client.post("/boards/board-1/relays/1", json={"state": "maybe"})

        assert response.status_code == 422
        assert fake_mqtt.published == []

    async def test_invalid_relay_number_returns_422(self, client, fake_mqtt, session_factory):
        await add_board(session_factory, "board-1")

        response = await client.post("/boards/board-1/relays/0", json={"state": "on"})

        assert response.status_code == 422
        assert fake_mqtt.published == []

    async def test_mqtt_down_returns_503(self, client, fake_mqtt, session_factory):
        await add_board(session_factory, "board-1")
        fake_mqtt.connected = False

        response = await client.post("/boards/board-1/relays/1", json={"state": "on"})

        assert response.status_code == 503


class TestClaimBoard:
    async def test_claim_unowned_board(self, client, session_factory):
        await add_board(session_factory, "board-1", owner_id=None)

        response = await client.post("/boards/claim", json={"board_id": "board-1"})

        assert response.status_code == 200
        assert response.json()["owner_id"] == str(USER_ID)

        async with session_factory() as session:
            board = await session.get(Board, "board-1")
            assert board is not None
            assert board.owner_id == USER_ID

    async def test_claim_already_owned_board_returns_409(self, client, session_factory):
        await add_board(session_factory, "board-1", owner_id=OTHER_USER_ID)

        response = await client.post("/boards/claim", json={"board_id": "board-1"})

        assert response.status_code == 409
        assert "already been claimed" in response.json()["detail"]

        async with session_factory() as session:
            board = await session.get(Board, "board-1")
            assert board is not None
            assert board.owner_id == OTHER_USER_ID

    async def test_claim_missing_board_returns_404(self, client):
        response = await client.post("/boards/claim", json={"board_id": "missing"})

        assert response.status_code == 404

    async def test_claim_invalid_board_id_returns_422(self, client):
        response = await client.post("/boards/claim", json={"board_id": "bad id"})

        assert response.status_code == 422


class TestHealth:
    async def test_health_ok(self, client):
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"]["connected"] is True
        assert body["mqtt"]["connected"] is True

    async def test_health_degraded_without_mqtt(self, client, fake_mqtt):
        fake_mqtt.connected = False

        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["mqtt"]["connected"] is False
