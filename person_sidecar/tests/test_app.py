from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.app import create_app
from person_sidecar.config import Settings
from person_sidecar.decisions import RuleFirstDecisionEngine
from person_sidecar.models import Decision, Event, LiveState
from person_sidecar.personas import PersonaRegistry
from person_sidecar.runtime import PersonRuntime


@dataclass
class NoopSpeaker:
    async def speak(self, _content: str):
        return type("Result", (), {"ok": True, "error": None})()


class NoopProvider:
    async def decide(self, *_args) -> Decision:
        return Decision(intent="wait", reason="test", confidence=1.0)


def make_client() -> TestClient:
    registry = PersonaRegistry.load(__import__("pathlib").Path(__file__).parents[1] / "personas")
    runtime = PersonRuntime(
        registry=registry,
        persona_id="host_interactive",
        speaker=NoopSpeaker(),
        game_gateway=GameGateway(),
        decision_engine=RuleFirstDecisionEngine(NoopProvider()),
    )
    settings = Settings(
        game_bridge_token="test-token",
        project_send_url="http://vtuber.local/send",
    )
    return TestClient(create_app(settings, runtime))


def test_chat_event_is_accepted() -> None:
    with make_client() as client:
        response = client.post(
            "/events/chat",
            json={
                "event_id": "d292e6bb-4b7a-4d4d-9aa0-443273d47916",
                "platform": "test",
                "user_id": "u1",
                "username": "观众",
                "content": "一心一意",
            },
        )

    assert response.status_code == 202
    assert response.json()["accepted"] is True


def test_game_websocket_rejects_bad_token() -> None:
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/game/idiom-demo?token=wrong") as websocket:
                websocket.receive_json()
    assert exc_info.value.code == 1008


def test_game_websocket_accepts_full_state() -> None:
    with make_client() as client:
        with client.websocket_connect("/ws/game/idiom-demo?token=test-token") as websocket:
            websocket.send_json(
                {
                    "type": "game.state",
                    "game_id": "idiom-demo",
                    "round_id": "42",
                    "revision": 1,
                    "phase": "answering",
                    "question_type": "idiom_fill_blank",
                    "question": "一__一__",
                    "remaining_ms": 10000,
                    "allowed_actions": ["submit_answer"],
                }
            )
