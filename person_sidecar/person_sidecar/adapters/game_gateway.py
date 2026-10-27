from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from person_sidecar.models import GameAction, GameActionName, GameState


class StaleOrDuplicateAction(ValueError):
    """Raised when an action no longer matches the active game state."""


SendJson = Callable[[dict[str, Any]], Awaitable[None]]


class GameGateway:
    def __init__(self, send_json: SendJson | None = None) -> None:
        self._send_json = send_json
        self._state: GameState | None = None
        self._submitted_round_id: str | None = None

    @property
    def state(self) -> GameState | None:
        return self._state

    @property
    def connected(self) -> bool:
        return self._send_json is not None

    def bind_sender(self, send_json: SendJson) -> None:
        self._send_json = send_json

    def accept_state(self, state: GameState) -> None:
        if self._state is None or state.round_id != self._state.round_id:
            self._submitted_round_id = None
        self._state = state

    def disconnect(self, game_id: str) -> None:
        if self._state and self._state.game_id == game_id:
            self._state = None
        self._send_json = None

    def validate_action(self, action: GameAction) -> None:
        state = self._state
        if state is None:
            raise StaleOrDuplicateAction("game state unavailable")
        if (action.game_id, action.round_id, action.revision) != (
            state.game_id,
            state.round_id,
            state.revision,
        ):
            raise StaleOrDuplicateAction("action targets a stale game state")
        if action.action not in state.allowed_actions:
            raise StaleOrDuplicateAction("action is not allowed by game state")
        if (
            action.action is GameActionName.SUBMIT_ANSWER
            and self._submitted_round_id == state.round_id
        ):
            raise StaleOrDuplicateAction("answer already submitted for this round")
        if self._send_json is None:
            raise StaleOrDuplicateAction("game bridge is disconnected")

    async def send(self, action: GameAction) -> None:
        self.validate_action(action)
        assert self._send_json is not None
        await self._send_json({"type": "game.action", **action.model_dump(mode="json")})
        if action.action is GameActionName.SUBMIT_ANSWER:
            self._submitted_round_id = action.round_id
