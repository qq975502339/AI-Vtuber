from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


SUPPORTED_TOOLS = frozenset({"speak", "submit_answer", "request_hint", "next_round"})


class Persona(BaseModel):
    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    reply_cooldown_ms: int = Field(default=3000, ge=0)
    max_speak_chars: int = Field(default=72, ge=1, le=300)
    prefer_audience_answers: bool = True
    allowed_tools: frozenset[str] = frozenset({"speak"})


class PersonaRegistry:
    def __init__(self, personas: dict[str, Persona]) -> None:
        self._personas = personas

    @classmethod
    def load(cls, directory: Path) -> "PersonaRegistry":
        personas: dict[str, Persona] = {}
        for path in sorted(directory.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            persona = Persona.model_validate(raw)
            if persona.id in personas:
                raise ValueError(f"duplicate persona id: {persona.id}")
            unsupported = persona.allowed_tools - SUPPORTED_TOOLS
            if unsupported:
                raise ValueError(f"persona {persona.id} requests unsupported tools: {sorted(unsupported)}")
            personas[persona.id] = persona
        if not personas:
            raise ValueError(f"no persona files found in {directory}")
        return cls(personas)

    def require(self, persona_id: str) -> Persona:
        try:
            return self._personas[persona_id]
        except KeyError as exc:
            raise KeyError(f"unknown persona id: {persona_id}") from exc

    def all(self) -> tuple[Persona, ...]:
        return tuple(self._personas.values())
