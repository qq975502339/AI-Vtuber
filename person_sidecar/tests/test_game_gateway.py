from __future__ import annotations

import pytest

from person_sidecar.adapters.game_gateway import GameGateway, StaleOrDuplicateAction
from person_sidecar.models import GameAction, GameState


@pytest.fixture
def state() -> GameState:
    return GameState.model_validate(
        {
            "game_id": "idiom-demo",
            "round_id": "42",
            "revision": 3,
            "phase": "answering",
            "question_type": "idiom_fill_blank",
            "question": "一__一__",
            "options": ["心", "意"],
            "remaining_ms": 12000,
            "allowed_actions": ["submit_answer", "request_hint"],
        }
    )


@pytest.mark.asyncio
async def test_gateway_rejects_second_submit_in_same_round(state: GameState) -> None:
    sent: list[dict] = []

    async def send_json(payload: dict) -> None:
        sent.append(payload)

    gateway = GameGateway(send_json)
    gateway.accept_state(state)
    action = GameAction.for_state(state, "submit_answer", {"answer": "一心一意"})

    await gateway.send(action)

    with pytest.raises(StaleOrDuplicateAction, match="already submitted"):
        await gateway.send(action)

    assert sent[0]["action"] == "submit_answer"


@pytest.mark.asyncio
async def test_gateway_rejects_stale_revision(state: GameState) -> None:
    async def send_json(_: dict) -> None:
        raise AssertionError("stale action must not be sent")

    gateway = GameGateway(send_json)
    gateway.accept_state(state)
    stale_action = GameAction.for_state(state, "request_hint", {})
    stale_action.revision = 2

    with pytest.raises(StaleOrDuplicateAction, match="stale"):
        await gateway.send(stale_action)


@pytest.mark.asyncio
async def test_gateway_session_reset_keeps_connection_and_allows_resubmit(state: GameState) -> None:
    sent: list[dict] = []

    async def send_json(payload: dict) -> None:
        sent.append(payload)

    gateway = GameGateway(send_json)
    gateway.accept_state(state)
    action = GameAction.for_state(state, "submit_answer", {"answer": "一心一意"})
    await gateway.send(action)

    gateway.reset_session()
    await gateway.send(action)

    assert gateway.connected is True
    assert gateway.state == state
    assert len(sent) == 2