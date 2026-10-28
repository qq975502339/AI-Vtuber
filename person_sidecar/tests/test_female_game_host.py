from __future__ import annotations

from pathlib import Path

from person_sidecar.personas import PersonaRegistry


def test_female_game_host_persona_has_fixed_nonvisual_identity() -> None:
    registry = PersonaRegistry.load(Path(__file__).parents[1] / "personas")

    persona = registry.require("female_game_host")

    assert persona.session_mood == "focused"
    assert persona.autonomy_think_ms > 0
    assert persona.idle_speak_interval_ms > persona.reply_cooldown_ms
    assert "虚拟游戏主播" in persona.system_prompt
    assert "不长期记住观众" in persona.system_prompt
    assert "不使用视觉" in persona.system_prompt
