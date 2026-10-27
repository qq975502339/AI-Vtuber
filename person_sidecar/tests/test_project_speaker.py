from __future__ import annotations

import json

import httpx
import pytest

from person_sidecar.adapters.project_speaker import ProjectSpeaker


@pytest.mark.asyncio
async def test_project_speaker_posts_existing_reread_contract() -> None:
    received: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        speaker = ProjectSpeaker("http://vtuber.local/send", "Person", client)
        result = await speaker.speak("大家好")

    assert result.ok is True
    assert received == [
        {"type": "reread", "data": {"username": "Person", "content": "大家好"}}
    ]


@pytest.mark.asyncio
async def test_project_speaker_retries_one_server_failure() -> None:
    attempts = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        speaker = ProjectSpeaker("http://vtuber.local/send", "Person", client)
        result = await speaker.speak("重试一次")

    assert result.ok is True
    assert attempts == 2
