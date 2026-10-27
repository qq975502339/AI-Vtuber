from __future__ import annotations

import json

import httpx

from person_sidecar.models import Decision, Event, LiveState
from person_sidecar.personas import Persona


class OpenAICompatibleDecisionProvider:
    """Minimal OpenAI-compatible JSON decision client with fail-closed behavior."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        client: httpx.AsyncClient,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._api_key = api_key
        self._client = client

    async def decide(self, persona: Persona, state: LiveState, event: Event) -> Decision:
        payload = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": persona.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "event": event.model_dump(mode="json"),
                            "game_state": state.game_state.model_dump(mode="json") if state.game_state else None,
                            "required_schema": {
                                "intent": "reply|game_action|wait",
                                "speak_text": "string|null",
                                "tool_name": "string|null",
                                "tool_args": "object",
                                "reason": "string",
                                "confidence": "number from 0 to 1",
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            response = await self._client.post(self._endpoint, json=payload, headers=headers, timeout=12.0)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return Decision.model_validate_json(content)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return Decision(intent="wait", reason="decision provider unavailable", confidence=1.0)
