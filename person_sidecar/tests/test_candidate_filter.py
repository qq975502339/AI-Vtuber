from __future__ import annotations

import pytest

from person_sidecar.models import ChatMessage, Event, GameState
from person_sidecar.tests.test_runtime import answer_state, runtime as runtime_fixture


@pytest.mark.asyncio
async def test_deadline_does_not_submit_non_idiom_chat(runtime_fixture, answer_state: GameState) -> None:
    await runtime_fixture.publish(Event.game_state(answer_state))
    await runtime_fixture.publish(
        Event.chat(ChatMessage(platform="test", user_id="u1", username="观众", content="加油"))
    )

    results = await runtime_fixture.drain_once()

    assert [result.tool_name for result in results] == ["speak"]


@pytest.mark.asyncio
async def test_deadline_does_not_submit_one_viewer_answer(runtime_fixture, answer_state: GameState) -> None:
    await runtime_fixture.publish(Event.game_state(answer_state))
    await runtime_fixture.publish(
        Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer", content="一心一意"))
    )

    results = await runtime_fixture.drain_once()

    assert [result.tool_name for result in results] == ["speak"]


@pytest.mark.asyncio
async def test_deadline_submits_same_answer_from_two_distinct_viewers(runtime_fixture, answer_state: GameState) -> None:
    await runtime_fixture.publish(Event.game_state(answer_state))
    await runtime_fixture.publish(
        Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer1", content="一心一意"))
    )
    await runtime_fixture.publish(
        Event.chat(ChatMessage(platform="test", user_id="u2", username="viewer2", content="一心一意"))
    )

    results = await runtime_fixture.drain_once()

    assert [result.tool_name for result in results] == ["submit_answer"]


@pytest.mark.asyncio
async def test_repeated_answer_from_one_viewer_is_not_consensus(runtime_fixture, answer_state: GameState) -> None:
    await runtime_fixture.publish(Event.game_state(answer_state))
    for _ in range(2):
        await runtime_fixture.publish(
            Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer", content="一心一意"))
        )

    results = await runtime_fixture.drain_once()

    assert [result.tool_name for result in results] == ["speak"]