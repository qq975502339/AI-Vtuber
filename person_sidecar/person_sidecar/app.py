from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status

from person_sidecar.adapters.game_gateway import GameGateway
from person_sidecar.adapters.project_speaker import ProjectSpeaker
from person_sidecar.config import Settings
from person_sidecar.decisions import OpenAICompatibleDecisionProvider, RuleFirstDecisionEngine
from person_sidecar.models import ChatMessage, Decision, Event, GameState, LiveState
from person_sidecar.personas import Persona, PersonaRegistry
from person_sidecar.runtime import PersonRuntime


class WaitDecisionProvider:
    async def decide(self, _persona: Persona, _state: LiveState, _event: Event) -> Decision:
        return Decision(intent="wait", reason="LLM provider is not configured", confidence=1.0)


async def _runtime_worker(runtime: PersonRuntime) -> None:
    while True:
        await runtime.drain_once()
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
        }

    @app.post("/events/chat", status_code=status.HTTP_202_ACCEPTED)
    async def post_chat(message: ChatMessage) -> dict[str, object]:
        await runtime.publish(Event.chat(message))
        return {"accepted": True, "event_id": str(message.event_id)}

    @app.post("/personas/{persona_id}/activate")
    async def activate_persona(persona_id: str) -> dict[str, str]:
        runtime.switch_persona(persona_id)
        return {"persona_id": runtime.state.persona_id}

    @app.websocket("/ws/game/{game_id}")
    async def game_socket(websocket: WebSocket, game_id: str, token: str = "") -> None:
        await websocket.accept()
        if token != settings.game_bridge_token:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        gateway: GameGateway = runtime._game_gateway
        gateway.bind_sender(websocket.send_json)
        received_first_state = False
        try:
            while True:
                message = await websocket.receive_json()
                message_type = message.get("type") if isinstance(message, dict) else None
                if message_type == "game.state":
                    state_model = GameState.model_validate(message)
                    if state_model.game_id != game_id:
                        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                        return
                    received_first_state = True
                    await runtime.publish(Event.game_state(state_model))
                elif message_type == "game.action_result" and received_first_state:
                    await runtime.publish(Event(type="game.action_result", payload=message))
                else:
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    return
        except (WebSocketDisconnect, ValueError):
            gateway.disconnect(game_id)

    return app


def create_app_from_settings(settings: Settings) -> FastAPI:
    persona_dir = settings.persona_dir
    registry = PersonaRegistry.load(persona_dir)
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
        persona_id=registry.all()[0].id,
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
