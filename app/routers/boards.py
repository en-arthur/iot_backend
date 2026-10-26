"""Board endpoints: relay commands and claiming."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.db import get_session
from app.models import Board
from app.mqtt import MqttService, MqttUnavailable
from app.ownership import get_owned_board
from app.schemas import (
    BoardResponse,
    ClaimBoardRequest,
    RelayCommandRequest,
    RelayCommandResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/boards", tags=["boards"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
BoardIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Board ID exactly as printed on the hardware.",
    ),
]
RelayNumberPath = Annotated[int, Path(ge=1, le=999)]


def get_mqtt_service(request: Request) -> MqttService:
    service: MqttService | None = getattr(request.app.state, "mqtt", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MQTT service is not running.",
        )
    return service


@router.post(
    "/{board_id}/relays/{relay_number}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RelayCommandResponse,
    summary="Publish a relay command",
)
async def set_relay(
    board_id: BoardIdPath,
    relay_number: RelayNumberPath,
    command: RelayCommandRequest,
    user_id: CurrentUser,
    session: SessionDep,
    mqtt: Annotated[MqttService, Depends(get_mqtt_service)],
) -> RelayCommandResponse:
    """Validate ownership and online state, then publish to ``.../relays/{n}/set``.

    Nothing is written to ``relay_states`` here. The board's state message is the
    source of truth, and the frontend waits for the Realtime update it produces.
    """
    board = await get_owned_board(session, board_id, user_id)

    if board.status != "online":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Board is offline",
        )

    try:
        await mqtt.publish_relay_command(board_id, relay_number, command.state)
    except MqttUnavailable as exc:
        logger.warning(
            "relay command could not be published",
            extra={"board_id": board_id, "relay": relay_number, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MQTT broker is not connected",
        ) from exc

    logger.info(
        "relay command accepted",
        extra={
            "board_id": board_id,
            "relay": relay_number,
            "state": command.state,
            "user_id": str(user_id),
        },
    )

    return RelayCommandResponse(
        board_id=board_id,
        relay=relay_number,
        state=command.state,
    )


@router.post(
    "/claim",
    response_model=BoardResponse,
    summary="Claim an unowned board",
)
async def claim_board(
    payload: ClaimBoardRequest,
    user_id: CurrentUser,
    session: SessionDep,
) -> Board:
    """Assign ``owner_id`` to the caller, but only while the board is unowned."""
    board = await session.get(Board, payload.board_id)

    if board is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )

    if board.owner_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This board has already been claimed.",
        )

    # Conditional update closes the race between two users claiming at once.
    result = await session.execute(
        update(Board)
        .where(Board.id == payload.board_id, Board.owner_id.is_(None))
        .values(owner_id=user_id)
    )

    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This board has already been claimed.",
        )

    await session.commit()
    await session.refresh(board)

    logger.info(
        "board claimed",
        extra={"board_id": payload.board_id, "user_id": str(user_id)},
    )

    return board
