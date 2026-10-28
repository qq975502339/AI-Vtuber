# Person Sidecar Reliability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent malformed game input and stale decisions from breaking or misdirecting the Person Sidecar while preserving the live game during a Person-only reset.

**Architecture:** Validate game events at the WebSocket boundary and keep a worker-level exception guard as a final safety net. Bind model-produced decisions to the exact round snapshot used for reasoning; reset only Person-owned session context and restore the current game state from the gateway. Use short-lived, per-round viewer identities solely to calculate distinct-user consensus.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pytest, pytest-asyncio.

## Global Constraints

- Only modify `person_sidecar/` and its documentation; do not change existing AI-Vtuber logic.
- No vision inputs, screenshots, `image_url`, long-term viewer memory, or persisted viewer identities.
- Person reset preserves the active game connection and active structured game state.
- New behavior must be test-first and all Sidecar tests must pass.

---

### Task 1: Reject malformed game events without killing the worker

**Files:**
- Modify: `person_sidecar/person_sidecar/app.py`, `person_sidecar/person_sidecar/runtime.py`
- Test: `person_sidecar/tests/test_app.py`, `person_sidecar/tests/test_runtime_safety.py`

- [ ] Write a failing WebSocket test that sends `game.action_result` without `action_id` after a valid state and expects close code 1003.
- [ ] Write a failing runtime test that publishes an invalid action result and verifies `drain_once()` returns a rejected audit result instead of raising.
- [ ] Validate `GameActionResult` in the WebSocket handler before publishing; close the socket with 1003 on a validation error.
- [ ] Make runtime reduction catch malformed internal events and append a rejected `ExecutionResult`.
- [ ] Wrap one worker iteration in `try/except Exception`, record a worker failure in runtime health, and continue after a short sleep.
- [ ] Run the focused tests and commit the task.

### Task 2: Bind decisions and actions to one game round

**Files:**
- Modify: `person_sidecar/person_sidecar/models.py`, `person_sidecar/person_sidecar/runtime.py`, `person_sidecar/person_sidecar/decisions.py`
- Test: `person_sidecar/tests/test_runtime_safety.py`

- [ ] Write a failing test where a provider awaits, the game state changes, and the returned game action is rejected as stale.
- [ ] Add optional `game_id`, `round_id`, and `revision` to `Decision` as the decision snapshot.
- [ ] Have the decision engine attach the active round to any model-proposed game action.
- [ ] Before creating `GameAction`, reject a decision whose snapshot differs from `LiveState.active_round_key()`.
- [ ] Run the focused test and commit the task.

### Task 3: Make deadline consensus distinct-viewer based

**Files:**
- Modify: `person_sidecar/person_sidecar/models.py`, `person_sidecar/person_sidecar/runtime.py`, `person_sidecar/person_sidecar/decisions.py`
- Test: `person_sidecar/tests/test_candidate_filter.py`, `person_sidecar/tests/test_runtime.py`

- [ ] Write a failing test proving one viewer cannot produce a deadline consensus.
- [ ] Write a failing test proving two distinct current-round viewers with the same answer can produce one.
- [ ] Store only `(user_id, answer)` candidate votes for the active round; replace a viewer’s prior vote instead of counting repeated chat messages.
- [ ] Require at least two distinct viewers for `best_consensus_answer()`.
- [ ] Clear votes when the game round changes or the Person session resets.
- [ ] Run focused tests and commit the task.

### Task 4: Reset only Person state, keep the live game

**Files:**
- Modify: `person_sidecar/person_sidecar/adapters/game_gateway.py`, `person_sidecar/person_sidecar/runtime.py`
- Test: `person_sidecar/tests/test_game_gateway.py`, `person_sidecar/tests/test_session_controls.py`

- [ ] Write a failing reset test that creates an active gateway state, resets Person, and expects the same state/connection but cleared submit lock and cleared chats/votes.
- [ ] Add `GameGateway.reset_session()` to retain sender and state while clearing `_submitted_round_id`.
- [ ] Rehydrate the new `LiveState.game_state` from the gateway during `PersonRuntime.reset_session()` and timestamp it as a new snapshot.
- [ ] Run focused tests and commit the task.

### Task 5: Clarify guard scope, configuration, and behavior tests

**Files:**
- Modify: `person_sidecar/person_sidecar/content_policy.py`, `person_sidecar/config.example.yaml`, `person_sidecar/README.md`, `person_sidecar/personas/female_game_host.yaml`
- Test: `person_sidecar/tests/test_session_controls.py`, `person_sidecar/tests/test_content_policy.py`

- [ ] Write failing behavior tests: muted Person does not call its speaker; paused Person does not send a game action.
- [ ] Rename/document `SpeechPolicy` as an identity-disclosure guard; do not claim it is moderation.
- [ ] Add `default_persona_id` and `admin_token` to the config example.
- [ ] Mark mood, lives, score, streak, and idle intervals as schema-only until a later state-machine phase.
- [ ] Run all Sidecar tests and `git diff --check`.