from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from person_sidecar.adapters.game_gateway import GameGateway, StaleOrDuplicateAction
from person_sidecar.content_policy import IdentityDisclosureGuard
from person_sidecar.decisions import RuleFirstDecisionEngine
from person_sidecar.models import ChatMessage, Event, GameAction, GameActionResult, GameState, LiveState
from person_sidecar.personas import PersonaRegistry


class Speaker(Protocol):
    async def speak(self, content: str):
        """Send speech to the existing AI-Vtuber project."""


@dataclass(frozen=True)
class ExecutionResult:
    tool_name: str | None
    status: str
    detail: str | None = None


class PersonRuntime:
    def __init__(self, registry: PersonaRegistry, persona_id: str, speaker: Speaker, game_gateway: GameGateway, decision_engine: RuleFirstDecisionEngine) -> None:
        persona = registry.require(persona_id)
        self._registry = registry
        self._speaker = speaker
        self._game_gateway = game_gateway
        self.decision_engine = decision_engine
        self.state = LiveState(persona_id=persona_id, session_id=str(uuid4()), session_mood=persona.session_mood)
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._audit: deque[ExecutionResult] = deque(maxlen=100)
        self._identity_guard = IdentityDisclosureGuard()
        self._worker_failure_count = 0
        self._last_worker_error: str | None = None

    async def publish(self, event: Event) -> None:
        await self._events.put(event)

    def switch_persona(self, persona_id: str) -> None:
        persona = self._registry.require(persona_id)
        self.state.persona_id = persona_id
        self.state.session_mood = persona.session_mood

    def set_paused(self, paused: bool) -> None:
        self.state.paused = paused

    def set_muted(self, muted: bool) -> None:
        self.state.muted = muted

    def reset_session(self) -> None:
        persona = self._registry.require(self.state.persona_id)
        self._game_gateway.reset_session()
        game_state = self._game_gateway.state
        self.state = LiveState(
            persona_id=persona.id,
            session_id=str(uuid4()),
            game_state=game_state,
            game_state_received_at=datetime.now(timezone.utc) if game_state else None,
            session_mood=persona.session_mood,
        )
        self._audit.clear()

    def record_worker_failure(self, error: Exception) -> None:
        self._worker_failure_count += 1
        self._last_worker_error = type(error).__name__

    @property
    def worker_failure_count(self) -> int:
        return self._worker_failure_count

    @property
    def last_worker_error(self) -> str | None:
        return self._last_worker_error

    @property
    def game_connected(self) -> bool:
        return self._game_gateway.connected

    def bind_game_sender(self, send_json) -> str:
        return self._game_gateway.bind_sender(send_json)

    def disconnect_game(self, game_id: str, connection_id: str | None) -> None:
        if self._game_gateway.disconnect(game_id, connection_id):
            if self.state.game_state and self.state.game_state.game_id == game_id:
                self.state.game_state = None
                self.state.clear_candidate_votes()

    @property
    def audit(self) -> tuple[ExecutionResult, ...]:
        return tuple(self._audit)

    async def drain_once(self) -> list[ExecutionResult]:
        events: list[Event] = []
        while not self._events.empty():
            event = self._events.get_nowait()
            if self.state.consume_event(event.event_id):
                events.append(event)
        if not events:
            return []

        events.sort(key=lambda item: 0 if item.type == "game.state" else 1)
        reduction_results: list[ExecutionResult] = []
        reduced_events: list[Event] = []
        for event in events:
            try:
                self._reduce(event)
            except (ValidationError, ValueError) as exc:
                result = ExecutionResult(event.type, "rejected", f"invalid event: {exc}")
                self._audit.append(result)
                reduction_results.append(result)
            else:
                reduced_events.append(event)
        if not reduced_events:
            return reduction_results

        persona = self._registry.require(self.state.persona_id)
        primary_event = next((event for event in reversed(reduced_events) if event.type == "chat.message"), reduced_events[-1])
        decision = await self.decision_engine.decide(persona, self.state, primary_event)
        result = await self._execute(decision, persona)
        return reduction_results + ([result] if result is not None else [])

    def _reduce(self, event: Event) -> None:
        if event.type == "game.state":
            state = GameState.model_validate(event.payload)
            previous = self.state.game_state
            if not self._game_gateway.accept_state(state):
                return
            if previous is None or self._round_key(state) != self._round_key(previous) or state.phase != "answering":
                self.state.clear_candidate_votes()
                self.state.autonomy_requested_for = None
            self.state.game_state = state
            self.state.game_state_received_at = event.occurred_at
            return
        if event.type == "chat.message":
            message = ChatMessage.model_validate(event.payload)
            self.state.add_chat(message)
            if self.state.game_state and self.state.game_state.phase == "answering":
                candidate = message.content.strip()
                if len(candidate) == 4 and all("\u4e00" <= char <= "\u9fff" for char in candidate):
                    self.state.add_candidate_vote(message.user_id, candidate)
            return
        if event.type == "game.action_result":
            result = GameActionResult.model_validate(event.payload)
            if self.state.active_round_key() == (result.game_id, result.round_id, result.revision):
                self.state.last_action_status = result.status
                if result.status in {"rejected", "resolved"}:
                    self.state.autonomy_requested_for = self.state.active_round_key()

    async def _execute(self, decision: Decision, persona) -> ExecutionResult | None:
        if decision.intent == "wait":
            return None
        game_tools = {"submit_answer", "select_option", "request_hint", "next_round"}
        if (decision.intent == "reply" and decision.tool_name != "speak") or (decision.intent == "game_action" and decision.tool_name not in game_tools):
            return self._record(ExecutionResult(decision.tool_name, "rejected", "intent and tool do not match"))
        if decision.tool_name not in persona.allowed_tools:
            return self._record(ExecutionResult(decision.tool_name, "rejected", "tool is not allowed for active persona"))
        if decision.tool_name == "speak":
            text = (decision.speak_text or "").strip()[: persona.max_speak_chars]
            if not text:
                result = ExecutionResult("speak", "rejected", "empty speech")
            elif self.state.muted:
                result = ExecutionResult("speak", "rejected", "session is muted")
            elif rejection_reason := self._identity_guard.rejection_reason(text):
                result = ExecutionResult("speak", "rejected", rejection_reason)
            else:
                response = await self._speaker.speak(text)
                if response.ok:
                    self.state.last_speak_at = datetime.now(timezone.utc)
                    self.state.last_speak_text = text
                    result = ExecutionResult("speak", "executed")
                else:
                    result = ExecutionResult("speak", "failed", response.error)
            return self._record(result)
        if decision.tool_name in game_tools:
            game_state = self.state.game_state
            if self.state.paused:
                result = ExecutionResult(decision.tool_name, "rejected", "session is paused")
            elif game_state is None:
                result = ExecutionResult(decision.tool_name, "rejected", "game state unavailable")
            elif decision.target_round_key() != self.state.active_round_key():
                result = ExecutionResult(decision.tool_name, "rejected", "decision targets a stale game state")
            else:
                try:
                    action = GameAction.for_state(game_state, decision.tool_name, decision.tool_args)
                    await self._game_gateway.send(action)
                    result = ExecutionResult(decision.tool_name, "executed")
                except StaleOrDuplicateAction as exc:
                    result = ExecutionResult(decision.tool_name, "rejected", str(exc))
            return self._record(result)
        return self._record(ExecutionResult(decision.tool_name, "rejected", "unknown tool"))

    def _record(self, result: ExecutionResult) -> ExecutionResult:
        self._audit.append(result)
        return result

    @staticmethod
    def _round_key(state: GameState) -> tuple[str, str, int]:
        return (state.game_id, state.round_id, state.revision)