from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.adapters.project_speaker import ProjectSpeaker
from person_sidecar.config import Settings
from person_sidecar.decisions import RuleFirstDecisionEngine
from person_sidecar.models import ChatMessage, Decision, Event, GameActionResult, GameState, LiveState
from person_sidecar.openai_provider import OpenAICompatibleDecisionProvider
from person_sidecar.personas import Persona, PersonaRegistry
from person_sidecar.runtime import PersonRuntime


class WaitDecisionProvider:
    async def decide(self, _persona: Persona, _state: LiveState, _event: Event) -> Decision:
        return Decision(intent="wait", reason="LLM provider is not configured", confidence=1.0)


def parse_game_bridge_message(
    message: object, *, received_first_state: bool
) -> tuple[str, GameState | GameActionResult]:
    if not isinstance(message, dict):
        raise ValueError("game bridge message must be an object")
    message_type = message.get("type")
    if message_type == "game.state":
        return message_type, GameState.model_validate(message)
    if message_type == "game.action_result" and received_first_state:
        return message_type, GameActionResult.model_validate(message)
    raise ValueError("unexpected game bridge message")


async def _runtime_worker(runtime: PersonRuntime) -> None:
    while True:
        try:
            await runtime.drain_once()
            game_state = runtime.state.game_state
            if game_state is not None and game_state.phase == "answering":
                await runtime.publish(Event(type="timer.tick"))
        except Exception as exc:
            runtime.record_worker_failure(exc)
        await asyncio.sleep(0.05)


def create_app(settings: Settings, runtime: PersonRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = asyncio.create_task(_runtime_worker(runtime))
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(title="AI-Vtuber Person Sidecar", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "persona_id": runtime.state.persona_id,
            "game_connected": runtime.game_connected,
            "session_id": runtime.state.session_id,
            "paused": runtime.state.paused,
            "muted": runtime.state.muted,
            "worker_status": "degraded" if runtime.worker_failure_count else "ok",
            "worker_failure_count": runtime.worker_failure_count,
            "last_worker_error": runtime.last_worker_error,
        }

    def require_admin_token(token: str | None) -> None:
        if not settings.admin_token:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="controls are not configured")
        if token != settings.admin_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token")

    @app.post("/controls/pause")
    async def pause_control(
        payload: dict[str, bool], x_person_sidecar_admin_token: str | None = Header(default=None)
    ) -> dict[str, bool]:
        require_admin_token(x_person_sidecar_admin_token)
        runtime.set_paused(payload.get("paused", True))
        return {"paused": runtime.state.paused}

    @app.post("/controls/mute")
    async def mute_control(
        payload: dict[str, bool], x_person_sidecar_admin_token: str | None = Header(default=None)
    ) -> dict[str, bool]:
        require_admin_token(x_person_sidecar_admin_token)
        runtime.set_muted(payload.get("muted", True))
        return {"muted": runtime.state.muted}

    @app.post("/controls/reset-session")
    async def reset_session_control(
        x_person_sidecar_admin_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        require_admin_token(x_person_sidecar_admin_token)
        runtime.reset_session()
        return {"session_id": runtime.state.session_id}

    @app.post("/events/chat", status_code=status.HTTP_202_ACCEPTED)
    async def post_chat(message: ChatMessage) -> dict[str, object]:
        await runtime.publish(Event.chat(message))
        return {"accepted": True, "event_id": str(message.event_id)}

    @app.post("/personas/{persona_id}/activate")
    async def activate_persona(persona_id: str) -> dict[str, str]:
        try:
            runtime.switch_persona(persona_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown persona id: {persona_id}")
        return {"persona_id": runtime.state.persona_id}

    @app.websocket("/ws/game/{game_id}")
    async def game_socket(websocket: WebSocket, game_id: str, token: str = "") -> None:
        await websocket.accept()
        if token != settings.game_bridge_token:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        connection_id = runtime.bind_game_sender(websocket.send_json)
        received_first_state = False
        try:
            while True:
                message = await websocket.receive_json()
                try:
                    message_type, parsed = parse_game_bridge_message(
                        message, received_first_state=received_first_state
                    )
                except ValueError:
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    return
                if message_type == "game.state":
                    assert isinstance(parsed, GameState)
                    if parsed.game_id != game_id:
                        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                        return
                    received_first_state = True
                    await runtime.publish(Event.game_state(parsed))
                else:
                    assert isinstance(parsed, GameActionResult)
                    await runtime.publish(
                        Event(type="game.action_result", payload=parsed.model_dump(mode="json"))
                    )
        except WebSocketDisconnect:
            pass
        finally:
            runtime.disconnect_game(game_id, connection_id)

    return app


def create_app_from_settings(settings: Settings) -> FastAPI:
    registry = PersonaRegistry.load(settings.persona_dir)
    registry.require(settings.default_persona_id)
    client = httpx.AsyncClient()
    speaker = ProjectSpeaker(settings.project_send_url, settings.project_username, client)
    if settings.llm_base_url and settings.llm_model and settings.llm_model != "CHANGE_ME":
        provider = OpenAICompatibleDecisionProvider(
            settings.llm_base_url, settings.llm_model, settings.llm_api_key, client
        )
    else:
        provider = WaitDecisionProvider()
    runtime = PersonRuntime(
        registry=registry,
        persona_id=settings.default_persona_id,
        speaker=speaker,
        game_gateway=GameGateway(),
        decision_engine=RuleFirstDecisionEngine(provider),
    )
    app = create_app(settings, runtime)

    @app.on_event("shutdown")
    async def close_http_client() -> None:
        await client.aclose()

    return app


def main() -> None:
    config_path = Path(os.environ.get("PERSON_SIDECAR_CONFIG", "config.yaml"))
    settings = Settings.from_yaml(config_path)
    uvicorn.run(create_app_from_settings(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()