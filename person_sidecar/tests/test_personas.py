from __future__ import annotations

from pathlib import Path

import pytest

from person_sidecar.personas import PersonaRegistry


def test_registry_loads_three_switchable_personas() -> None:
    persona_dir = Path(__file__).parents[1] / "personas"

    registry = PersonaRegistry.load(persona_dir)

    assert registry.require("host_interactive").allowed_tools == frozenset(
        {"speak", "submit_answer", "request_hint", "next_round"}
    )
    assert registry.require("idiom_teacher").prefer_audience_answers is True
    assert registry.require("playful_gamer").reply_cooldown_ms > 0


def test_registry_rejects_duplicate_persona_ids(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "id: same\ndisplay_name: A\nsystem_prompt: x\n", encoding="utf-8"
    )
    (tmp_path / "b.yaml").write_text(
        "id: same\ndisplay_name: B\nsystem_prompt: x\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="duplicate persona id"):
        PersonaRegistry.load(tmp_path)
