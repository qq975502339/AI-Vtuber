from __future__ import annotations

import pytest
from pydantic import ValidationError

from person_sidecar.models import GameState


def test_game_state_rejects_visual_input() -> None:
    with pytest.raises(ValidationError):
        GameState.model_validate(
            {
                "type": "game.state",
                "game_id": "idiom-demo",
                "round_id": "42",
                "revision": 1,
                "phase": "answering",
                "question_type": "idiom_picture_guess",
                "image_url": "https://example.invalid/question.png",
                "remaining_ms": 10000,
                "allowed_actions": [],
            }
        )


def test_game_state_accepts_only_structured_textual_clues() -> None:
    state = GameState.model_validate(
        {
            "type": "game.state",
            "game_id": "idiom-demo",
            "round_id": "42",
            "revision": 1,
            "phase": "answering",
            "question_type": "idiom_fill_blank",
            "question": "一__一__",
            "clues": ["形容人专心"],
            "candidate_characters": ["心", "意"],
            "lives": 3,
            "score": 120,
            "remaining_ms": 10000,
            "allowed_actions": ["submit_answer"],
        }
    )

    assert state.clues == ["形容人专心"]
    assert state.candidate_characters == ["心", "意"]
    assert state.lives == 3
    assert state.has_textual_clue is True
