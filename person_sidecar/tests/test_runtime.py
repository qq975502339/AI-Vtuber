from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.decisions import DecisionProvider, RuleFirstDecisionEngine
from person_sidecar.models import ChatMessage, Decision, Event, GameState
from person_sidecar.personas import PersonaRegistry
from person_sidecar.runtime import PersonRuntime


@dataclass
class FakeSpeaker:
    spoken: list[str] = field(default_factory=list)

    async def speak(self, content: str):
        self.spoken.append(content)
        return type("Result", (), {"ok": True, "error": None})()


class FakeDecisionProvider(DecisionProvider):
    def __init__(self, tool_name: str = "speak") -> None:
        self.tool_name = tool_name

    async def decide(self, *_args) -> Decision:
        return Decision(
            intent="reply",
            speak_text="收到啦",
            tool_name=self.tool_name,
            reason="test provider",
            confidence=0.8,
        )


@pytest.fixture
def answer_state() -> GameState:
    return GameState.model_validate(
        {
            "game_id": "idiom-demo",
            "round_id": "42",
            "revision": 3,
            "phase": "answering",
            "question_type": "idiom_fill_blank",
            "question": "一__一__",
            "options": ["心", "意"],
            "remaining_ms": 2000,
            "allowed_actions": ["submit_answer", "request_hint"],
        }
    )


@pytest.fixture
def runtime() -> PersonRuntime:
    registry = PersonaRegistry.load(__import__("pathlib").Path(__file__).parents[1] / "personas")
    sent: list[dict] = []

    async def send_json(payload: dict) -> None:
        sent.append(payload)

    return PersonRuntime(
        registry=registry,
        persona_id="idiom_teacher",
        speaker=FakeSpeaker(),
        game_gateway=GameGateway(send_json),
        decision_engine=RuleFirstDecisionEngine(FakeDecisionProvider()),
    )


@pytest.mark.asyncio
async def test_deadline_answer_beats_normal_chat(runtime: PersonRuntime, answer_state: GameState) -> None:
    await runtime.publish(Event.game_state(answer_state))
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="观众", content="一心一意")))
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u2", username="观众2", content="一心一意")))

    results = await runtime.drain_once()

    assert [result.tool_name for result in results] == ["submit_answer"]


@pytest.mark.asyncio
async def test_persona_switch_keeps_current_round(runtime: PersonRuntime, answer_state: GameState) -> None:
    await runtime.publish(Event.game_state(answer_state))
    await runtime.drain_once()

    runtime.switch_persona("playful_gamer")

    assert runtime.state.game_state is not None
    assert runtime.state.game_state.round_id == "42"
    assert runtime.state.persona_id == "playful_gamer"


@pytest.mark.asyncio
async def test_llm_cannot_call_tool_outside_persona_allowlist(runtime: PersonRuntime) -> None:
    runtime.decision_engine = RuleFirstDecisionEngine(FakeDecisionProvider("delete_files"))
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="观众", content="你好")))

    results = await runtime.drain_once()

    assert results[0].status == "rejected"
