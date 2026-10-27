from __future__ import annotations

import pytest

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.models import GameState


@pytest.mark.asyncio
async def test_old_connection_cannot_disconnect_new_game_sender() -> None:
    async def first_sender(_payload: dict) -> None:
        return None

    async def second_sender(_payload: dict) -> None:
        return None

    gateway = GameGateway(first_sender)
    first_connection = gateway.connection_id
    second_connection = gateway.bind_sender(second_sender)

    gateway.disconnect("idiom-demo", first_connection)

    assert gateway.connected is True
    assert gateway.connection_id == second_connection
