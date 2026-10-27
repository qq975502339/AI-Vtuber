from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from person_sidecar.adapters.game_gateway import GameGateway, StaleOrDuplicateAction
from person_sidecar.decisions import RuleFirstDecisionEngine
from person_sidecar.models import ChatMessage, Event, GameAction, GameState, LiveState
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
    def __init__(
        self,
        registry: PersonaRegistry,
        persona_id: str,
        speaker: Speaker,
        game_gateway: GameGateway,
        decision_engine: RuleFirstDecisionEngine,
    ) -> None:
        registry.require(persona_id)
        self._registry = registry
        self._speaker = speaker
        self._game_gateway = game_gateway
        self.decision_engine = decision_engine
        self.state = LiveState(persona_id=persona_id, session_id=str(uuid4()))
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._audit: deque[ExecutionResult] = deque(maxlen=100)

    async def publish(self, event: Event) -> None:
        await self._events.put(event)

    def switch_persona(self, persona_id: str) -> None:
        self._registry.require(persona_id)
        self.state.persona_id = persona_id

    @property
    def game_connected(self) -> bool:
        return self._game_gateway.connected

    @property
    def audit(self) -> tuple[ExecutionResult, ...]:
        return tuple(self._audit)

    async def drain_once(self) -> list[ExecutionResult]:
        events: list[Event] = []
        while not self._events.empty():
            event = self._events.get_nowait()
            if event.event_id in self.state.consumed_event_ids:
                continue
            self.state.consumed_event_ids.add(event.event_id)
            events.append(event)
        if not events:
            return []

        events.sort(key=lambda item: 0 if item.type == "game.state" else 1)
        for event in events:
            self._reduce(event)

        persona = self._registry.require(self.state.persona_id)
        primary_event = next((event for event in reversed(events) if event.type == "chat.message"), events[-1])
        decision = await self.decision_engine.decide(persona, self.state, primary_event)
        result = await self._execute(decision, persona)
        return [result] if result is not None else []

    def _reduce(self, event: Event) -> None:
        if event.type == "game.state":
            state = GameState.model_validate(event.payload)
            self.state.game_state = state
            self._game_gateway.accept_state(state)
            return
        if event.type == "chat.message":
            message = ChatMessage.model_validate(event.payload)
            self.state.add_chat(message)
            if self.state.game_state and self.state.game_state.phase == "answering":
                candidate = message.content.strip()
                if len(candidate) == 4 and all("\u4e00" <= char <= "\u9fff" for char in candidate):
                    self.state.candidate_answers.append(candidate)
                    self.state.candidate_answers[:] = self.state.candidate_answers[-20:]

    async def _execute(self, decision, persona) -> ExecutionResult | None:
        if decision.intent == "wait":
            return None
        if decision.tool_name not in persona.allowed_tools:
            result = ExecutionResult(decision.tool_name, "rejected", "tool is not allowed for active persona")
            self._audit.append(result)
            return result
        if decision.tool_name == "speak":
            text = (decision.speak_text or "").strip()[: persona.max_speak_chars]
            if not text:
                result = ExecutionResult("speak", "rejected", "empty speech")
            else:
                response = await self._speaker.speak(text)
                if response.ok:
                    self.state.last_speak_at = datetime.now(timezone.utc)
                    self.state.last_speak_text = text
                    result = ExecutionResult("speak", "executed")
                else:
                    result = ExecutionResult("speak", "failed", response.error)
            self._audit.append(result)
            return result
        if decision.tool_name in {"submit_answer", "select_option", "request_hint", "next_round"}:
            game_state = self.state.game_state
            if game_state is None:
                result = ExecutionResult(decision.tool_name, "rejected", "game state unavailable")
            else:
                try:
                    action = GameAction.for_state(game_state, decision.tool_name, decision.tool_args)
                    await self._game_gateway.send(action)
                    result = ExecutionResult(decision.tool_name, "executed")
                except StaleOrDuplicateAction as exc:
                    result = ExecutionResult(decision.tool_name, "rejected", str(exc))
            self._audit.append(result)
            return result
        result = ExecutionResult(decision.tool_name, "rejected", "unknown tool")
        self._audit.append(result)
        return result
