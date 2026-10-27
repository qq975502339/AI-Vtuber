from __future__ import annotations

import pytest
from pydantic import ValidationError

from person_sidecar.models import GameAction, GameState


def test_game_action_keeps_round_identity() -> None:
    state = GameState.model_validate(
        {
            "game_id": "idiom-demo",
            "round_id": "42",
            "revision": 3,
            "phase": "answering",
            "question_type": "idiom_fill_blank",
            "question": "一__一__",
            "options": ["心", "意"],
            "remaining_ms": 12000,
            "allowed_actions": ["submit_answer"],
        }
    )

    action = GameAction.for_state(state, "submit_answer", {"answer": "一心一意"})

    assert (action.game_id, action.round_id, action.revision) == ("idiom-demo", "42", 3)


def test_game_state_rejects_negative_remaining_time() -> None:
    with pytest.raises(ValidationError):
        GameState.model_validate(
            {
                "game_id": "g",
                "round_id": "1",
                "revision": 1,
                "phase": "answering",
                "question_type": "idiom_fill_blank",
                "question": "",
                "options": [],
                "remaining_ms": -1,
                "allowed_actions": [],
            }
        )
