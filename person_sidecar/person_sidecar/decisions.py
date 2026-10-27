from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from person_sidecar.models import Decision, Event, LiveState
from person_sidecar.personas import Persona


class DecisionProvider(Protocol):
    async def decide(self, persona: Persona, state: LiveState, event: Event) -> Decision:
        """Return one schema-valid proposal without executing it."""


class RuleFirstDecisionEngine:
    def __init__(self, provider: DecisionProvider, deadline_ms: int = 3000) -> None:
        self._provider = provider
        self._deadline_ms = deadline_ms

    async def decide(self, persona: Persona, state: LiveState, event: Event) -> Decision:
        game_state = state.game_state
        if event.type == "game.action_result" or (game_state and game_state.phase == "resolved"):
            return self._wait("game result takes precedence")
        if (
            game_state
            and game_state.phase == "answering"
            and game_state.remaining_ms <= self._deadline_ms
        ):
            answer = state.best_consensus_answer()
            if answer:
                return Decision(
                    intent="game_action",
                    tool_name="submit_answer",
                    tool_args={"answer": answer},
                    reason="deadline consensus",
                    confidence=0.95,
                )
        if event.type != "chat.message":
            return self._wait("no chat requires a response")
        if state.last_speak_at is not None:
            elapsed_ms = (datetime.now(timezone.utc) - state.last_speak_at).total_seconds() * 1000
            if elapsed_ms < persona.reply_cooldown_ms:
                return self._wait("speech cooldown")
        return await self._provider.decide(persona, state, event)

    @staticmethod
    def _wait(reason: str) -> Decision:
        return Decision(intent="wait", reason=reason, confidence=1.0)

from person_sidecar.openai_provider import OpenAICompatibleDecisionProvider
