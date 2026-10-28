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
        if state.paused and event.type != "game.action_result":
            return self._wait("session is paused")
        if event.type == "game.action_result" or (game_state and game_state.phase == "resolved"):
            return self._wait("game result takes precedence")
        active_round = state.active_round_key()
        if game_state and game_state.phase == "answering" and self._effective_remaining_ms(state) <= self._deadline_ms:
            answer = state.best_consensus_answer()
            if answer:
                return self._for_round(
                    Decision(intent="game_action", tool_name="submit_answer", tool_args={"answer": answer}, reason="deadline consensus", confidence=0.95),
                    active_round,
                )
        if event.type == "timer.tick" and game_state and game_state.phase == "answering":
            if self._game_state_age_ms(state) < persona.autonomy_think_ms:
                return self._wait("autonomous thinking window")
            if not game_state.has_textual_clue:
                return self._wait("nonvisual game state has no textual clue")
            if active_round == state.autonomy_requested_for:
                return self._wait("autonomous decision already requested for this round")
            state.autonomy_requested_for = active_round
            return self._for_round(await self._provider.decide(persona, state, event), active_round)
        if event.type != "chat.message":
            return self._wait("event does not require a response")
        if state.last_speak_at is not None:
            elapsed_ms = (datetime.now(timezone.utc) - state.last_speak_at).total_seconds() * 1000
            if elapsed_ms < persona.reply_cooldown_ms:
                return self._wait("speech cooldown")
        return self._for_round(await self._provider.decide(persona, state, event), active_round)

    @staticmethod
    def _for_round(decision: Decision, round_key: tuple[str, str, int] | None) -> Decision:
        if decision.intent != "game_action" or round_key is None:
            return decision
        return decision.model_copy(update={"target_game_id": round_key[0], "target_round_id": round_key[1], "target_revision": round_key[2]})

    @staticmethod
    def _wait(reason: str) -> Decision:
        return Decision(intent="wait", reason=reason, confidence=1.0)

    @staticmethod
    def _effective_remaining_ms(state: LiveState) -> int:
        game_state = state.game_state
        if game_state is None or state.game_state_received_at is None:
            return game_state.remaining_ms if game_state else 0
        elapsed_ms = (datetime.now(timezone.utc) - state.game_state_received_at).total_seconds() * 1000
        return max(0, int(game_state.remaining_ms - elapsed_ms))

    @staticmethod
    def _game_state_age_ms(state: LiveState) -> int:
        if state.game_state_received_at is None:
            return 0
        return max(0, int((datetime.now(timezone.utc) - state.game_state_received_at).total_seconds() * 1000))


from person_sidecar.openai_provider import OpenAICompatibleDecisionProvider  # noqa: E402