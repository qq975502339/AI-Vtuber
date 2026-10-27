from __future__ import annotations

import pytest

from person_sidecar.models import ChatMessage, Decision, Event, GameState
from person_sidecar.tests.test_runtime import FakeDecisionProvider, answer_state, runtime


@pytest.mark.asyncio
async def test_new_round_clears_previous_round_answer_candidates(runtime, answer_state: GameState) -> None:
    await runtime.publish(Event.game_state(answer_state))
    await runtime.publish(
        Event.chat(ChatMessage(platform="test", user_id="u1", username="观众", content="一心一意"))
    )
    await runtime.drain_once()

    next_round = answer_state.model_copy(
        update={"round_id": "43", "revision": 0, "remaining_ms": 12000}
    )
    await runtime.publish(Event.game_state(next_round))
    await runtime.drain_once()

    assert runtime.state.candidate_answers == []


@pytest.mark.asyncio
async def test_runtime_rejects_out_of_order_game_state(runtime, answer_state: GameState) -> None:
    newest = answer_state.model_copy(update={"revision": 4})
    stale = answer_state.model_copy(update={"revision": 3})
    await runtime.publish(Event.game_state(newest))
    await runtime.drain_once()
    await runtime.publish(Event.game_state(stale))
    await runtime.drain_once()

    assert runtime.state.game_state is not None
    assert runtime.state.game_state.revision == 4


@pytest.mark.asyncio
async def test_reply_intent_cannot_execute_game_tool(runtime) -> None:
    runtime.decision_engine = __import__("person_sidecar.decisions", fromlist=["RuleFirstDecisionEngine"]).RuleFirstDecisionEngine(
        FakeDecisionProvider("submit_answer")
    )
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="观众", content="你好")))

    results = await runtime.drain_once()

    assert results[0].status == "rejected"
    assert results[0].detail == "intent and tool do not match"
