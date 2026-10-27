from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class GameActionName(str, Enum):
    SUBMIT_ANSWER = "submit_answer"
    SELECT_OPTION = "select_option"
    REQUEST_HINT = "request_hint"
    NEXT_ROUND = "next_round"


class ChatMessage(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    platform: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    username: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=500)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_admin: bool = False


class GameState(BaseModel):
    game_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    phase: Literal["answering", "resolved", "idle"]
    question_type: Literal["idiom_fill_blank", "idiom_picture_guess"]
    question: str = ""
    image_url: str | None = None
    options: list[str] = Field(default_factory=list)
    remaining_ms: int = Field(ge=0)
    allowed_actions: set[GameActionName] = Field(default_factory=set)


class GameAction(BaseModel):
    action_id: UUID = Field(default_factory=uuid4)
    game_id: str
    round_id: str
    revision: int = Field(ge=0)
    action: GameActionName
    args: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_state(
        cls, state: GameState, action: GameActionName | str, args: dict[str, Any]
    ) -> "GameAction":
        return cls(
            game_id=state.game_id,
            round_id=state.round_id,
            revision=state.revision,
            action=action,
            args=args,
        )


class Event(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    type: Literal["chat.message", "game.state", "game.action_result", "timer.tick"]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def chat(cls, message: ChatMessage) -> "Event":
        return cls(event_id=message.event_id, type="chat.message", occurred_at=message.occurred_at, payload=message.model_dump(mode="json"))

    @classmethod
    def game_state(cls, state: GameState) -> "Event":
        return cls(type="game.state", payload=state.model_dump(mode="json"))


class Decision(BaseModel):
    intent: Literal["reply", "game_action", "wait"]
    speak_text: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


@dataclass
class LiveState:
    persona_id: str
    session_id: str
    game_state: GameState | None = None
    recent_messages: list[ChatMessage] = field(default_factory=list)
    candidate_answers: list[str] = field(default_factory=list)
    consumed_event_ids: set[UUID] = field(default_factory=set)
    last_speak_at: datetime | None = None
    last_speak_text: str | None = None

    def add_chat(self, message: ChatMessage, limit: int = 20) -> None:
        self.recent_messages.append(message)
        self.recent_messages[:] = self.recent_messages[-limit:]

    def best_consensus_answer(self) -> str | None:
        if not self.candidate_answers:
            return None
        return max(self.candidate_answers, key=lambda answer: (self.candidate_answers.count(answer), answer))
