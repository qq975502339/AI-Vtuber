from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class SpeakResult:
    ok: bool
    status_code: int | None = None
    error: str | None = None


class ProjectSpeaker:
    def __init__(self, send_url: str, username: str, client: httpx.AsyncClient) -> None:
        self._send_url = send_url
        self._username = username
        self._client = client

    async def speak(self, content: str) -> SpeakResult:
        if not content.strip():
            return SpeakResult(ok=False, error="speech content is blank")
        payload = {
            "type": "reread",
            "data": {"username": self._username, "content": content},
        }
        last_error: str | None = None
        for _ in range(2):
            try:
                response = await self._client.post(self._send_url, json=payload, timeout=5.0)
                if response.status_code < 500:
                    return SpeakResult(ok=response.is_success, status_code=response.status_code)
                last_error = f"server returned {response.status_code}"
            except httpx.TransportError as exc:
                last_error = str(exc)
        return SpeakResult(ok=False, error=last_error or "speech request failed")
