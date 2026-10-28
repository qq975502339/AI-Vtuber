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


@pytest.mark.asyncio
async def test_invalid_action_result_is_audited_instead_of_crashing(runtime) -> None:
    await runtime.publish(
        Event(
            type="game.action_result",
            payload={"game_id": "idiom-demo", "round_id": "42", "revision": 3, "status": "rejected"},
        )
    )

    results = await runtime.drain_once()

    assert len(results) == 1
    assert results[0].tool_name == "game.action_result"
    assert results[0].status == "rejected"

class AdvancingActionProvider:
    def __init__(self, runtime, next_state: GameState) -> None:
        self.runtime = runtime
        self.next_state = next_state

    async def decide(self, *_args) -> Decision:
        self.runtime.state.game_state = self.next_state
        self.runtime._game_gateway.accept_state(self.next_state)
        return Decision(
            intent="game_action",
            tool_name="submit_answer",
            tool_args={"answer": "一心一意"},
            reason="old round answer",
            confidence=0.9,
        )


@pytest.mark.asyncio
async def test_action_from_old_decision_round_is_rejected(runtime, answer_state: GameState) -> None:
    next_state = answer_state.model_copy(update={"round_id": "43", "revision": 0, "remaining_ms": 12000})
    runtime.decision_engine = __import__("person_sidecar.decisions", fromlist=["RuleFirstDecisionEngine"]).RuleFirstDecisionEngine(
        AdvancingActionProvider(runtime, next_state)
    )
    await runtime.publish(Event.game_state(answer_state.model_copy(update={"remaining_ms": 12000})))
    await runtime.drain_once()
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer", content="go")))

    results = await runtime.drain_once()

    assert results[0].tool_name == "submit_answer"
    assert results[0].status == "rejected"
    assert results[0].detail == "decision targets a stale game state"

@pytest.mark.asyncio
async def test_person_reset_keeps_current_game_but_clears_person_context(runtime, answer_state: GameState) -> None:
    await runtime.publish(Event.game_state(answer_state))
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer", content="一心一意")))
    await runtime.drain_once()
    old_session_id = runtime.state.session_id

    runtime.reset_session()

    assert runtime.state.session_id != old_session_id
    assert runtime.state.game_state == answer_state
    assert runtime.game_connected is True
    assert runtime.state.recent_messages == []
    assert runtime.state.candidate_answers == []