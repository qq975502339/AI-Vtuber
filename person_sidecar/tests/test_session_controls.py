from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.app import create_app
from person_sidecar.config import Settings
from person_sidecar.decisions import RuleFirstDecisionEngine
from person_sidecar.models import ChatMessage, Decision, Event, GameState, LiveState
from person_sidecar.personas import Persona, PersonaRegistry
from person_sidecar.runtime import PersonRuntime


@dataclass
class NoopSpeaker:
    spoken: list[str] = field(default_factory=list)

    async def speak(self, content: str):
        self.spoken.append(content)
        return type("Result", (), {"ok": True, "error": None})()


class NoopProvider:
    async def decide(self, *_args) -> Decision:
        return Decision(intent="wait", reason="test", confidence=1.0)


class ReplyProvider:
    async def decide(self, *_args) -> Decision:
        return Decision(intent="reply", tool_name="speak", speak_text="hello", reason="test", confidence=0.8)


class GameActionProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, *_args) -> Decision:
        self.calls += 1
        return Decision(intent="game_action", tool_name="request_hint", reason="test", confidence=0.8)


def make_client() -> tuple[TestClient, PersonRuntime]:
    registry = PersonaRegistry.load(Path(__file__).parents[1] / "personas")
    runtime = PersonRuntime(registry, "female_game_host", NoopSpeaker(), GameGateway(), RuleFirstDecisionEngine(NoopProvider()))
    settings = Settings(game_bridge_token="test-token", project_send_url="http://vtuber.local/send", admin_token="admin-token")
    return TestClient(create_app(settings, runtime)), runtime


def test_pause_mute_and_reset_affect_only_current_session() -> None:
    client, runtime = make_client()
    old_session_id = runtime.state.session_id
    headers = {"x-person-sidecar-admin-token": "admin-token"}
    with client:
        assert client.post("/controls/pause", headers=headers, json={"paused": True}).status_code == 200
        assert client.post("/controls/mute", headers=headers, json={"muted": True}).status_code == 200
        reset = client.post("/controls/reset-session", headers=headers)
    assert reset.status_code == 200
    assert runtime.state.session_id != old_session_id
    assert runtime.state.paused is False
    assert runtime.state.muted is False


def test_controls_require_admin_token() -> None:
    client, _runtime = make_client()
    with client:
        response = client.post("/controls/pause", json={"paused": True})
    assert response.status_code == 401


def test_controls_are_disabled_without_configured_token() -> None:
    registry = PersonaRegistry.load(Path(__file__).parents[1] / "personas")
    runtime = PersonRuntime(registry, "female_game_host", NoopSpeaker(), GameGateway(), RuleFirstDecisionEngine(NoopProvider()))
    settings = Settings(game_bridge_token="test-token", project_send_url="http://vtuber.local/send")
    with TestClient(create_app(settings, runtime)) as client:
        response = client.post("/controls/pause", json={"paused": True})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_muted_session_does_not_call_speaker() -> None:
    _client, runtime = make_client()
    runtime.decision_engine = RuleFirstDecisionEngine(ReplyProvider())
    runtime.set_muted(True)
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer", content="hello")))
    results = await runtime.drain_once()
    assert results[0].status == "rejected"
    assert runtime._speaker.spoken == []


@pytest.mark.asyncio
async def test_paused_session_does_not_request_game_action() -> None:
    _client, runtime = make_client()
    provider = GameActionProvider()
    runtime.decision_engine = RuleFirstDecisionEngine(provider)
    state = GameState.model_validate({"game_id": "g", "round_id": "r", "revision": 1, "phase": "answering", "question_type": "idiom_fill_blank", "question": "text", "remaining_ms": 10000, "allowed_actions": ["request_hint"]})
    await runtime.publish(Event.game_state(state))
    await runtime.drain_once()
    runtime.set_paused(True)
    await runtime.publish(Event.chat(ChatMessage(platform="test", user_id="u1", username="viewer", content="hello")))
    results = await runtime.drain_once()
    assert results == []
    assert provider.calls == 0