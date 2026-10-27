from __future__ import annotations

import json

import httpx
import pytest

from person_sidecar.decisions import OpenAICompatibleDecisionProvider
from person_sidecar.models import ChatMessage, Event, LiveState
from person_sidecar.personas import Persona


@pytest.mark.asyncio
async def test_openai_compatible_provider_parses_structured_decision() -> None:
    received: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"intent":"reply","speak_text":"你好","tool_name":"speak","tool_args":{},"reason":"greeting","confidence":0.8}'
                        }
                    }
                ]
            },
        )

    persona = Persona(id="p", display_name="P", system_prompt="保持人设")
    state = LiveState(persona_id="p", session_id="s")
    event = Event.chat(ChatMessage(platform="test", user_id="u", username="观众", content="你好"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleDecisionProvider("http://llm.local/v1", "test-model", "secret", client)
        decision = await provider.decide(persona, state, event)

    assert decision.tool_name == "speak"
    assert received[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_openai_compatible_provider_fails_closed_on_invalid_response() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    persona = Persona(id="p", display_name="P", system_prompt="保持人设")
    state = LiveState(persona_id="p", session_id="s")
    event = Event.chat(ChatMessage(platform="test", user_id="u", username="观众", content="你好"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleDecisionProvider("http://llm.local/v1", "test-model", None, client)
        decision = await provider.decide(persona, state, event)

    assert decision.intent == "wait"
    assert decision.reason == "decision provider unavailable"
