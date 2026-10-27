# Person Sidecar Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated Python Person Sidecar that can switch among personas, receive structured idiom-game state and chat events, make validated reply/game decisions, and request speech through the existing AI-Vtuber `/send` endpoint.

**Architecture:** Add a new self-contained `person_sidecar/` package with its own dependencies and test suite; do not import or modify the existing project runtime. A FastAPI control service accepts chat/test events and one authenticated game WebSocket. A single async runtime owns short-lived `LiveState`, and it can invoke only registered `speak`, `submit_answer`, `request_hint`, and `next_round` tools.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, HTTPX, PyYAML, pytest, pytest-asyncio.

## Global Constraints

- Create and modify files only below `person_sidecar/` plus this plan and its accompanying documentation; do not modify `main.py`, `utils/`, `game/`, `webui.py`, root `requirements.txt`, root `pyproject.toml`, or `config.json`.
- Run the Sidecar in its own virtual environment. It must not change or depend on the original project鈥檚 dependency set.
- All test cases use fake HTTP transports, fake decision providers, FastAPI `TestClient`, or in-memory adapters; they must not connect to a live platform, a real LLM, a real webpage, or real TTS.
- Runtime state is per live session and in memory only. Persona definition files are reusable and persistent; audience conversations are not retained after shutdown.
- Every game action includes `game_id`, `round_id`, and `revision`; stale revisions, unsupported actions, and a second successful `submit_answer` in one round are rejected before dispatch.
- The only integration with the existing project is an HTTP `POST` to its existing `/send` endpoint using `{"type":"reread","data":{"username":...,"content":...}}`.
- Do not use raw LLM text as an action. Decision-provider output is validated against a Pydantic schema and a persona-specific tool allowlist.

---

## Planned File Structure

```text
person_sidecar/
  pyproject.toml
  README.md
  config.example.yaml
  person_sidecar/
    __init__.py
    app.py
    config.py
    models.py
    personas.py
    runtime.py
    decisions.py
    adapters/
      __init__.py
      project_speaker.py
      game_gateway.py
  personas/
    host_interactive.yaml
    idiom_teacher.yaml
    playful_gamer.yaml
  tests/
    conftest.py
    test_models.py
    test_personas.py
    test_project_speaker.py
    test_runtime.py
    test_app.py
```

`models.py` owns wire and state models. `personas.py` owns persona loading only. `game_gateway.py` owns live game connections and action dispatch. `project_speaker.py` owns the outbound `/send` call. `decisions.py` owns rule-first and LLM-provider decision selection. `runtime.py` is the only owner of mutable state. `app.py` exposes transport endpoints and contains no business policy.

### Task 1: Isolated package, configuration, and protocol models

**Files:**
- Create: `person_sidecar/pyproject.toml`
- Create: `person_sidecar/config.example.yaml`
- Create: `person_sidecar/person_sidecar/__init__.py`
- Create: `person_sidecar/person_sidecar/config.py`
- Create: `person_sidecar/person_sidecar/models.py`
- Create: `person_sidecar/tests/conftest.py`
- Create: `person_sidecar/tests/test_models.py`

**Interfaces:**
- Produces `Settings.from_yaml(path: Path) -> Settings`.
- Produces `ChatMessage`, `GameState`, `GameAction`, `Event`, `Decision`, and `LiveState` Pydantic models used by every later task.
- `GameState` has `game_id: str`, `round_id: str`, `revision: int`, `phase: Literal["answering", "resolved", "idle"]`, `question_type: Literal["idiom_fill_blank", "idiom_picture_guess"]`, `remaining_ms: int`, and `allowed_actions: set[GameActionName]`.

- [ ] **Step 1: Write failing protocol tests**

```python
# person_sidecar/tests/test_models.py
from pydantic import ValidationError
from person_sidecar.models import GameAction, GameState


def test_game_action_keeps_round_identity() -> None:
    state = GameState.model_validate({
        "game_id": "idiom-demo", "round_id": "42", "revision": 3,
        "phase": "answering", "question_type": "idiom_fill_blank",
        "question": "涓€__涓€__", "options": ["蹇?, "鎰?],
        "remaining_ms": 12000, "allowed_actions": ["submit_answer"],
    })
    action = GameAction.for_state(state, "submit_answer", {"answer": "涓€蹇冧竴鎰?})
    assert (action.game_id, action.round_id, action.revision) == ("idiom-demo", "42", 3)


def test_game_state_rejects_negative_remaining_time() -> None:
    with pytest.raises(ValidationError):
        GameState.model_validate({
            "game_id": "g", "round_id": "1", "revision": 1,
            "phase": "answering", "question_type": "idiom_fill_blank",
            "question": "", "options": [], "remaining_ms": -1,
            "allowed_actions": [],
        })
```

- [ ] **Step 2: Run tests to verify the protocol does not yet exist**

Run: `cd person_sidecar; python -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'person_sidecar.models'`.

- [ ] **Step 3: Create the isolated package and models**

Create `pyproject.toml` with an independent build and test configuration:

```toml
[project]
name = "ai-vtuber-person-sidecar"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["fastapi>=0.115,<1", "httpx>=0.27,<1", "pydantic>=2.8,<3", "PyYAML>=6,<7", "uvicorn>=0.30,<1"]

[project.optional-dependencies]
dev = ["pytest>=8,<9", "pytest-asyncio>=0.23,<1"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Implement the identity-preserving constructor and model validation:

```python
class GameAction(BaseModel):
    action_id: UUID = Field(default_factory=uuid4)
    game_id: str
    round_id: str
    revision: int = Field(ge=0)
    action: GameActionName
    args: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_state(cls, state: GameState, action: GameActionName, args: dict[str, Any]) -> "GameAction":
        return cls(game_id=state.game_id, round_id=state.round_id, revision=state.revision, action=action, args=args)
```

Add `Settings` with `host`, `port`, `game_bridge_token`, `project_send_url`, `project_username`, `llm_base_url`, `llm_model`, and optional `llm_api_key`. `Settings.from_yaml` must read the YAML file and allow environment variables `PERSON_SIDECAR_GAME_BRIDGE_TOKEN` and `PERSON_SIDECAR_LLM_API_KEY` to override only their corresponding secrets.

- [ ] **Step 4: Run protocol tests**

Run: `cd person_sidecar; python -m pip install -e ".[dev]"; python -m pytest tests/test_models.py -v`
Expected: PASS with both tests passing.

- [ ] **Step 5: Commit the package foundation**

```bash
git add person_sidecar/pyproject.toml person_sidecar/config.example.yaml person_sidecar/person_sidecar person_sidecar/tests/conftest.py person_sidecar/tests/test_models.py
git commit -m "feat: scaffold isolated person sidecar"
```

### Task 2: Reusable persona registry and three persona packs

**Files:**
- Create: `person_sidecar/person_sidecar/personas.py`
- Create: `person_sidecar/personas/host_interactive.yaml`
- Create: `person_sidecar/personas/idiom_teacher.yaml`
- Create: `person_sidecar/personas/playful_gamer.yaml`
- Create: `person_sidecar/tests/test_personas.py`

**Interfaces:**
- Consumes `Settings.persona_dir`.
- Produces `Persona`, `PersonaRegistry.load(directory: Path) -> PersonaRegistry`, and `PersonaRegistry.require(persona_id: str) -> Persona`.
- Every `Persona` exposes `id`, `display_name`, `system_prompt`, `reply_cooldown_ms`, `max_speak_chars`, `prefer_audience_answers`, and `allowed_tools: frozenset[str]`.

- [ ] **Step 1: Write failing persona tests**

```python
def test_registry_loads_three_switchable_personas(persona_dir: Path) -> None:
    registry = PersonaRegistry.load(persona_dir)
    assert registry.require("host_interactive").allowed_tools == frozenset({"speak", "submit_answer", "request_hint", "next_round"})
    assert registry.require("idiom_teacher").prefer_audience_answers is True
    assert registry.require("playful_gamer").reply_cooldown_ms > 0


def test_registry_rejects_duplicate_persona_ids(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("id: same\ndisplay_name: A\nsystem_prompt: x\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("id: same\ndisplay_name: B\nsystem_prompt: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate persona id"):
        PersonaRegistry.load(tmp_path)
```

- [ ] **Step 2: Run persona tests to verify they fail**

Run: `cd person_sidecar; python -m pytest tests/test_personas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'person_sidecar.personas'`.

- [ ] **Step 3: Implement data-only persona packs and validation**

Use this common YAML shape in all three persona files:

```yaml
id: idiom_teacher
display_name: 鎴愯灏忚€佸笀
system_prompt: >-
  浣犳槸鑰愬績銆佺畝娲佺殑涓枃鎴愯鐩存挱鑰佸笀銆備紭鍏堥噰绾宠浼楀舰鎴愬叡璇嗙殑绛旀锛?  鍙湪娓告垙鐘舵€佸厑璁告椂鎻愬嚭鍙楁帶鍔ㄤ綔锛岀粷涓嶇紪閫犳父鎴忕粨鏋溿€?reply_cooldown_ms: 3500
max_speak_chars: 72
prefer_audience_answers: true
allowed_tools: [speak, submit_answer, request_hint, next_round]
```

Implement `PersonaRegistry.load` to sort `*.yaml` paths, validate every document through `Persona.model_validate`, reject duplicate IDs, and reject an `allowed_tools` set that is not a subset of `{"speak", "submit_answer", "request_hint", "next_round"}`.

- [ ] **Step 4: Run persona and protocol tests**

Run: `cd person_sidecar; python -m pytest tests/test_personas.py tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit personas**

```bash
git add person_sidecar/person_sidecar/personas.py person_sidecar/personas person_sidecar/tests/test_personas.py
git commit -m "feat: add reusable person personas"
```

### Task 3: Safe outbound adapters for existing speech and game actions

**Files:**
- Create: `person_sidecar/person_sidecar/adapters/__init__.py`
- Create: `person_sidecar/person_sidecar/adapters/project_speaker.py`
- Create: `person_sidecar/person_sidecar/adapters/game_gateway.py`
- Create: `person_sidecar/tests/test_project_speaker.py`
- Create: `person_sidecar/tests/test_game_gateway.py`

**Interfaces:**
- Produces `ProjectSpeaker.speak(content: str) -> SpeakResult`.
- Produces `GameGateway.accept_state(state: GameState) -> None`, `GameGateway.validate_action(action: GameAction) -> None`, and `GameGateway.send(action: GameAction) -> None`.
- `GameGateway` receives its `send_json: Callable[[dict[str, Any]], Awaitable[None]]` at connection time, allowing the runtime to remain unaware of WebSocket details.

- [ ] **Step 1: Write failing adapter tests**

```python
@pytest.mark.asyncio
async def test_project_speaker_posts_existing_reread_contract() -> None:
    received: list[dict] = []
    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200})
    speaker = ProjectSpeaker("http://vtuber.local/send", "Person", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await speaker.speak("澶у濂?)
    assert received == [{"type": "reread", "data": {"username": "Person", "content": "澶у濂?}}]


@pytest.mark.asyncio
async def test_gateway_rejects_second_submit_in_same_round(state: GameState) -> None:
    sent: list[dict] = []
    gateway = GameGateway(sent.append)
    gateway.accept_state(state)
    await gateway.send(GameAction.for_state(state, "submit_answer", {"answer": "涓€蹇冧竴鎰?}))
    with pytest.raises(StaleOrDuplicateAction):
        await gateway.send(GameAction.for_state(state, "submit_answer", {"answer": "涓€蹇冧竴鎰?}))
```

- [ ] **Step 2: Run adapter tests to verify they fail**

Run: `cd person_sidecar; python -m pytest tests/test_project_speaker.py tests/test_game_gateway.py -v`
Expected: FAIL because adapter modules are absent.

- [ ] **Step 3: Implement explicit validation and one retry**

`ProjectSpeaker.speak` must reject blank text, truncate only after the runtime has applied persona limits, make one request with a five-second timeout, retry exactly once only for `httpx.TransportError` or a 5xx response, and return a failure result instead of raising to the event loop.

`GameGateway.validate_action` must enforce this exact sequence:

```python
if self._state is None:
    raise StaleOrDuplicateAction("game state unavailable")
if (action.game_id, action.round_id, action.revision) != (state.game_id, state.round_id, state.revision):
    raise StaleOrDuplicateAction("action targets a stale game state")
if action.action.value not in state.allowed_actions:
    raise StaleOrDuplicateAction("action is not allowed by game state")
if action.action is GameActionName.SUBMIT_ANSWER and self._submitted_round_id == state.round_id:
    raise StaleOrDuplicateAction("answer already submitted for this round")
```

Only after validation may `send` serialize `action.model_dump(mode="json")` and call the registered sender.

- [ ] **Step 4: Run all adapter tests**

Run: `cd person_sidecar; python -m pytest tests/test_project_speaker.py tests/test_game_gateway.py -v`
Expected: PASS.

- [ ] **Step 5: Commit adapters**

```bash
git add person_sidecar/person_sidecar/adapters person_sidecar/tests/test_project_speaker.py person_sidecar/tests/test_game_gateway.py
git commit -m "feat: add person output adapters"
```

### Task 4: Rule-first decision engine and single-owner runtime

**Files:**
- Create: `person_sidecar/person_sidecar/decisions.py`
- Create: `person_sidecar/person_sidecar/runtime.py`
- Create: `person_sidecar/tests/test_runtime.py`

**Interfaces:**
- Produces `DecisionProvider.decide(persona: Persona, state: LiveState, event: Event) -> Decision` and `RuleFirstDecisionEngine.decide(...) -> Decision`.
- Produces `PersonRuntime.publish(event: Event) -> None`, `PersonRuntime.switch_persona(persona_id: str) -> None`, and `PersonRuntime.drain_once() -> list[ExecutionResult]`.
- `Decision` has exactly `intent: Literal["reply", "game_action", "wait"]`, `speak_text: str | None`, `tool_name: str | None`, `tool_args: dict[str, Any]`, `reason: str`, and `confidence: float`.

- [ ] **Step 1: Write failing runtime tests**

```python
@pytest.mark.asyncio
async def test_deadline_answer_beats_normal_chat(runtime: PersonRuntime, answer_state: GameState) -> None:
    await runtime.publish(Event.game_state(answer_state.model_dump()))
    await runtime.publish(Event.chat("u1", "瑙備紬", "涓€蹇冧竴鎰?))
    await runtime.publish(Event.timer_tick(now_ms=answer_state.remaining_ms - 1000))
    results = await runtime.drain_once()
    assert [result.tool_name for result in results] == ["submit_answer"]


@pytest.mark.asyncio
async def test_persona_switch_keeps_current_round(runtime: PersonRuntime, answer_state: GameState) -> None:
    await runtime.publish(Event.game_state(answer_state.model_dump()))
    runtime.switch_persona("playful_gamer")
    assert runtime.state.game_state.round_id == "42"
    assert runtime.state.persona_id == "playful_gamer"


@pytest.mark.asyncio
async def test_llm_cannot_call_tool_outside_persona_allowlist(runtime: PersonRuntime) -> None:
    runtime.decision_provider = FakeDecisionProvider(tool_name="delete_files")
    await runtime.publish(Event.chat("u1", "瑙備紬", "浣犲ソ"))
    results = await runtime.drain_once()
    assert results[0].status == "rejected"
```

- [ ] **Step 2: Run tests to verify the runtime is absent**

Run: `cd person_sidecar; python -m pytest tests/test_runtime.py -v`
Expected: FAIL with `ModuleNotFoundError` for `person_sidecar.runtime`.

- [ ] **Step 3: Implement the deterministic policy before provider fallback**

Implement these ordered rules in `RuleFirstDecisionEngine`:

```python
if event.type == "game.action_result" or state.game_state and state.game_state.phase == "resolved":
    return Decision.wait("round result takes precedence")
if state.game_state and state.game_state.phase == "answering" and state.game_state.remaining_ms <= 3000:
    answer = state.best_consensus_answer()
    if answer:
        return Decision.game_action("submit_answer", {"answer": answer}, "deadline consensus", 0.95)
if event.type == "chat.message" and state.can_speak_now():
    return await provider.decide(persona, state, event)
return Decision.wait("no action needed")
```

`PersonRuntime.drain_once` must drain the queue into an ordered local list, reduce all `game.state` events before chat events, ignore duplicate event IDs, then execute at most one game action and one speech action. Before execution it must check `tool_name in persona.allowed_tools`, validate the action through `GameGateway`, truncate speech to `persona.max_speak_chars`, and record each `ExecutionResult` in the in-memory audit ring buffer.

- [ ] **Step 4: Run runtime and prior regression tests**

Run: `cd person_sidecar; python -m pytest tests/test_runtime.py tests/test_models.py tests/test_personas.py tests/test_project_speaker.py tests/test_game_gateway.py -v`
Expected: PASS.

- [ ] **Step 5: Commit decision and runtime logic**

```bash
git add person_sidecar/person_sidecar/decisions.py person_sidecar/person_sidecar/runtime.py person_sidecar/tests/test_runtime.py
git commit -m "feat: add rule-first person runtime"
```

### Task 5: FastAPI ingress and authenticated webpage game bridge

**Files:**
- Create: `person_sidecar/person_sidecar/app.py`
- Create: `person_sidecar/tests/test_app.py`

**Interfaces:**
- Produces `create_app(settings: Settings, runtime: PersonRuntime) -> FastAPI`.
- `POST /events/chat` accepts `ChatMessage`, creates an `Event`, and returns `202 {"accepted": true, "event_id": "..."}`.
- `POST /personas/{persona_id}/activate` switches only the active persona and returns the selected persona ID.
- `GET /healthz` returns `{"status": "ok", "persona_id": "...", "game_connected": bool}`.
- `WS /ws/game/{game_id}?token=...` accepts `game.state` and `game.action_result`; it rejects a wrong token with WebSocket close code `1008`.

- [ ] **Step 1: Write failing API and WebSocket tests**

```python
def test_chat_event_is_accepted(app_client: TestClient) -> None:
    response = app_client.post("/events/chat", json={
        "event_id": "evt-1", "platform": "test", "user_id": "u1",
        "username": "瑙備紬", "content": "涓€蹇冧竴鎰?,
    })
    assert response.status_code == 202
    assert response.json()["accepted"] is True


def test_game_websocket_rejects_bad_token(app_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with app_client.websocket_connect("/ws/game/idiom-demo?token=wrong"):
            pass
    assert exc_info.value.code == 1008
```

- [ ] **Step 2: Run endpoint tests to verify they fail**

Run: `cd person_sidecar; python -m pytest tests/test_app.py -v`
Expected: FAIL because `create_app` does not exist.

- [ ] **Step 3: Implement thin transports only**

`app.py` must not make decisions. It should construct events, call `await runtime.publish(event)`, and return transport responses. On a valid game connection, bind the connection鈥檚 `send_json` callback to `GameGateway`, require the first message to be a full `game.state`, and close with code `1003` for malformed JSON or unsupported message types. On disconnect, call `gateway.disconnect(game_id)` so the runtime sees game state as unavailable.

Provide the executable entry point:

```python
def main() -> None:
    settings = Settings.from_yaml(Path(os.environ.get("PERSON_SIDECAR_CONFIG", "config.yaml")))
    uvicorn.run(create_app_from_settings(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run API and full test suite**

Run: `cd person_sidecar; python -m pytest -v`
Expected: PASS with no network access.

- [ ] **Step 5: Commit service transport**

```bash
git add person_sidecar/person_sidecar/app.py person_sidecar/tests/test_app.py
git commit -m "feat: expose person sidecar ingress"
```

### Task 6: Operator documentation, sample configuration, and final verification

**Files:**
- Create: `person_sidecar/README.md`
- Modify: `person_sidecar/config.example.yaml`
- Modify: `person_sidecar/tests/conftest.py`

**Interfaces:**
- Documents a standalone launch command, a safe local demo sequence, the chat ingress payload, the game WebSocket payload, and the original-project configuration boundary.
- Test fixtures provide `FakeDecisionProvider`, a fake game sender, and an HTTPX mock; no fixture accesses credentials.

- [ ] **Step 1: Write a documentation smoke test**

```python
def test_example_config_has_no_real_secrets() -> None:
    text = Path("config.example.yaml").read_text(encoding="utf-8")
    assert "CHANGE_ME" in text
    assert "sk-" not in text
    assert "SESSDATA" not in text
```

- [ ] **Step 2: Run the smoke test to verify it fails or exposes missing safe config**

Run: `cd person_sidecar; python -m pytest tests/test_config_example.py -v`
Expected: FAIL until the test file and safe example configuration are present.

- [ ] **Step 3: Add safe operator instructions**

Document this exact deployment order:

```powershell
cd person_sidecar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
$env:PERSON_SIDECAR_GAME_BRIDGE_TOKEN = "replace-with-a-random-local-token"
python -m person_sidecar.app
```

The README must explicitly state: start the original AI-Vtuber normally; configure its existing base API first; have Person use only `/send`; disable the original ordinary chat responder/read-comment by configuration when Person becomes the sole interaction controller; never put platform cookies or LLM keys in the committed sample configuration.

- [ ] **Step 4: Run all checks**

Run: `cd person_sidecar; python -m pytest -v`
Expected: PASS.
Run: `git diff --check`
Expected: no output.

- [ ] **Step 5: Commit documentation and verify clean status**

```bash
git add person_sidecar/README.md person_sidecar/config.example.yaml person_sidecar/tests/test_config_example.py person_sidecar/tests/conftest.py
git commit -m "docs: add person sidecar operator guide"
git status --short
```

Expected: no output from `git status --short`.

## Plan Self-Review

- Coverage: Tasks 1鈥? implement the isolated package and multiple switchable personas; Task 3 covers the existing `/send` integration and stale game-action protection; Task 4 supplies short-lived state, priority policy, and controlled LLM decisions; Task 5 adds chat/game transport; Task 6 covers safe operation and final verification.
- Scope: A real platform-specific `ChatAdapter`, persistence, moderation service, dashboard, and additional games are deliberately deferred to Phase 2/3. Phase 1 uses `POST /events/chat` as the platform-neutral ingress.
- Consistency: `GameState`, `GameAction`, `Persona`, and `PersonRuntime` names and responsibilities are defined before later tasks reference them. The only previous-project endpoint is `/send`.
- Ambiguity resolved: Phase 1鈥檚 game page authenticates with a shared local bridge token; it does not accept anonymous game WebSockets.
