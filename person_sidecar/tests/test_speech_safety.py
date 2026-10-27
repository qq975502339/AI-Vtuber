from __future__ import annotations

import httpx
import pytest

from person_sidecar.adapters.project_speaker import ProjectSpeaker


@pytest.mark.asyncio
async def test_project_speaker_does_not_retry_ambiguous_transport_failure() -> None:
    attempts = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("connection closed after request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        speaker = ProjectSpeaker("http://vtuber.local/send", "Person", client)
        result = await speaker.speak("只说一次")

    assert result.ok is False
    assert attempts == 1
