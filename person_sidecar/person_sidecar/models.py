from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bridge_message_type: Literal["game.state"] | None = Field(default=None, alias="type", exclude=True)
    game_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    phase: Literal["answering", "resolved", "idle"]
    question_type: Literal["idiom_fill_blank", "idiom_picture_guess"]
    question: str = ""
    options: list[str] = Field(default_factory=list)
    clues: list[str] = Field(default_factory=list)
    candidate_characters: list[str] = Field(default_factory=list)
    lives: int | None = Field(default=None, ge=0)
    score: int | None = Field(default=None, ge=0)
    streak: int | None = Field(default=None, ge=0)
    round_index: int | None = Field(default=None, ge=1)
    total_rounds: int | None = Field(default=None, ge=1)
    remaining_ms: int = Field(ge=0)
    allowed_actions: set[GameActionName] = Field(default_factory=set)

    @property
    def has_textual_clue(self) -> bool:
        return bool(self.question.strip() or self.options or self.clues or self.candidate_characters)


class GameAction(BaseModel):
    action_id: UUID = Field(default_factory=uuid4)
    game_id: str
    round_id: str
    revision: int = Field(ge=0)
    action: GameActionName
    args: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_state(cls, state: GameState, action: GameActionName | str, args: dict[str, Any]) -> "GameAction":
        return cls(game_id=state.game_id, round_id=state.round_id, revision=state.revision, action=action, args=args)


class GameActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bridge_message_type: Literal["game.action_result"] | None = Field(default=None, alias="type", exclude=True)
    action_id: UUID
    game_id: str
    round_id: str
    revision: int = Field(ge=0)
    status: Literal["accepted", "executed", "rejected", "resolved"]
    detail: str | None = Field(default=None, max_length=500)


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
    target_game_id: str | None = None
    target_round_id: str | None = None
    target_revision: int | None = Field(default=None, ge=0)

    def target_round_key(self) -> tuple[str, str, int] | None:
        if self.target_game_id is None or self.target_round_id is None or self.target_revision is None:
            return None
        return (self.target_game_id, self.target_round_id, self.target_revision)


@dataclass
class LiveState:
    persona_id: str
    session_id: str
    game_state: GameState | None = None
    recent_messages: list[ChatMessage] = field(default_factory=list)
    candidate_votes: dict[str, str] = field(default_factory=dict)
    consumed_event_ids: set[UUID] = field(default_factory=set)
    consumed_event_order: deque[UUID] = field(default_factory=deque)
    last_speak_at: datetime | None = None
    last_speak_text: str | None = None
    game_state_received_at: datetime | None = None
    autonomy_requested_for: tuple[str, str, int] | None = None
    paused: bool = False
    muted: bool = False
    session_mood: str = "focused"
    last_action_status: str | None = None

    @property
    def candidate_answers(self) -> list[str]:
        return list(self.candidate_votes.values())

    def add_chat(self, message: ChatMessage, limit: int = 20) -> None:
        self.recent_messages.append(message)
        self.recent_messages[:] = self.recent_messages[-limit:]

    def add_candidate_vote(self, user_id: str, answer: str) -> None:
        self.candidate_votes[user_id] = answer

    def clear_candidate_votes(self) -> None:
        self.candidate_votes.clear()

    def best_consensus_answer(self, min_distinct_viewers: int = 2) -> str | None:
        counts = Counter(self.candidate_votes.values())
        if not counts:
            return None
        top_count = max(counts.values())
        winners = [answer for answer, count in counts.items() if count == top_count]
        if top_count < min_distinct_viewers or len(winners) != 1:
            return None
        return winners[0]

    def consume_event(self, event_id: UUID, limit: int = 1024) -> bool:
        if event_id in self.consumed_event_ids:
            return False
        self.consumed_event_ids.add(event_id)
        self.consumed_event_order.append(event_id)
        while len(self.consumed_event_order) > limit:
            self.consumed_event_ids.discard(self.consumed_event_order.popleft())
        return True

    def active_round_key(self) -> tuple[str, str, int] | None:
        if self.game_state is None:
            return None
        return (self.game_state.game_id, self.game_state.round_id, self.game_state.revision)