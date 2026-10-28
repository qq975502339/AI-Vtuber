from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.decisions import DecisionProvider, RuleFirstDecisionEngine
from person_sidecar.models import Decision, Event, GameState
from person_sidecar.personas import PersonaRegistry
from person_sidecar.runtime import PersonRuntime


@dataclass
class SilentSpeaker:
    spoken: list[str] = field(default_factory=list)

    async def speak(self, content: str):
        self.spoken.append(content)
        return type("Result", (), {"ok": True, "error": None})()


class CountingProvider(DecisionProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, *_args) -> Decision:
        self.calls += 1
        return Decision(
            intent="game_action",
            tool_name="submit_answer",
            tool_args={"answer": "一心一意"},
            reason="structured textual clue",
            confidence=0.8,
        )


def make_runtime(provider: CountingProvider) -> PersonRuntime:
    registry = PersonaRegistry.load(__import__("pathlib").Path(__file__).parents[1] / "personas")
    return PersonRuntime(
        registry=registry,
        persona_id="female_game_host",
        speaker=SilentSpeaker(),
        game_gateway=GameGateway(),
        decision_engine=RuleFirstDecisionEngine(provider),
    )


def textual_state() -> GameState:
    return GameState.model_validate(
        {
            "game_id": "idiom-demo",
            "round_id": "42",
            "revision": 1,
            "phase": "answering",
            "question_type": "idiom_fill_blank",
            "question": "一__一__",
            "clues": ["形容人专心"],
            "remaining_ms": 10000,
            "allowed_actions": ["submit_answer"],
        }
    )


@pytest.mark.asyncio
async def test_textual_round_without_chat_requests_one_autonomous_decision() -> None:
    provider = CountingProvider()
    runtime = make_runtime(provider)
    await runtime.publish(Event.game_state(textual_state()))
    await runtime.drain_once()

    await runtime.publish(Event(type="timer.tick"))
    await runtime.drain_once()
    assert provider.calls == 0

    runtime.state.game_state_received_at = datetime.now(timezone.utc) - timedelta(milliseconds=1201)
    await runtime.publish(Event(type="timer.tick"))
    await runtime.drain_once()
    await runtime.publish(Event(type="timer.tick"))
    await runtime.drain_once()

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_picture_round_without_text_never_calls_provider() -> None:
    provider = CountingProvider()
    runtime = make_runtime(provider)
    picture_only_state = GameState.model_validate(
        {
            "game_id": "idiom-demo",
            "round_id": "43",
            "revision": 1,
            "phase": "answering",
            "question_type": "idiom_picture_guess",
            "remaining_ms": 10000,
            "allowed_actions": ["request_hint"],
        }
    )
    await runtime.publish(Event.game_state(picture_only_state))
    await runtime.drain_once()
    await runtime.publish(Event(type="timer.tick"))
    results = await runtime.drain_once()

    assert provider.calls == 0
    assert results == []
