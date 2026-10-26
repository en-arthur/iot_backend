from __future__ import annotations

import pytest
from app.ownership import get_owned_board
from fastapi import HTTPException

from tests.conftest import OTHER_USER_ID, USER_ID, add_board


class TestGetOwnedBoard:
    async def test_owner_can_access(self, session_factory):
        await add_board(session_factory, "board-1")

        async with session_factory() as session:
            board = await get_owned_board(session, "board-1", USER_ID)

        assert board.id == "board-1"
        assert board.owner_id == USER_ID

    async def test_other_user_is_forbidden(self, session_factory):
        await add_board(session_factory, "board-1")

        async with session_factory() as session:
            with pytest.raises(HTTPException) as excinfo:
                await get_owned_board(session, "board-1", OTHER_USER_ID)

        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == "Not your board"

    async def test_unowned_board_is_forbidden(self, session_factory):
        await add_board(session_factory, "board-1", owner_id=None)

        async with session_factory() as session:
            with pytest.raises(HTTPException) as excinfo:
                await get_owned_board(session, "board-1", USER_ID)

        assert excinfo.value.status_code == 403

    async def test_unknown_board_is_not_found(self, session_factory):
        async with session_factory() as session:
            with pytest.raises(HTTPException) as excinfo:
                await get_owned_board(session, "missing", USER_ID)

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Board not found"
