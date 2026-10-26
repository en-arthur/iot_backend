"""Board ownership rules shared by the REST endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Board


async def get_owned_board(session: AsyncSession, board_id: str, user_id: UUID) -> Board:
    """Return the board if the user owns it.

    404 when the board does not exist (so unowned IDs are not leaked as
    forbidden), 403 when it exists but belongs to someone else.
    """
    board = await session.get(Board, board_id)

    if board is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )

    if board.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not your board",
        )

    return board
