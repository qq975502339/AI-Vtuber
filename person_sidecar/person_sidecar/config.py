from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    game_bridge_token: str
    project_send_url: str
    project_username: str = "Person"
    persona_dir: Path = Path("personas")
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    @classmethod
    def from_yaml(cls, path: Path) -> "Settings":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for env_name, field_name in (
            ("PERSON_SIDECAR_GAME_BRIDGE_TOKEN", "game_bridge_token"),
            ("PERSON_SIDECAR_LLM_API_KEY", "llm_api_key"),
        ):
            value = os.getenv(env_name)
            if value:
                raw[field_name] = value
        return cls.model_validate(raw)
