# Person Sidecar

`Person Sidecar` 是 AI-Vtuber 当前项目旁的独立编排模块。它不会导入或修改原项目的代码；它只接收弹幕和网页游戏事件，选择安全的说话或游戏动作，并通过原项目既有的 `/send` 接口请求主播发言。

## 结构

- `person_sidecar/`：单场直播的状态、规则优先决策、工具权限和 HTTP / WebSocket 服务。
- `personas/`：可切换的人格包。内置 `host_interactive`、`idiom_teacher` 与 `playful_gamer`。
- `tests/`：完全本地的模拟测试；不会连接真实直播平台、LLM、网页游戏或 TTS。

## 启动

先正常启动现有 AI-Vtuber 项目，并确认它的 `/send` API 可用。然后在本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
$env:PERSON_SIDECAR_GAME_BRIDGE_TOKEN = "replace-with-a-random-local-token"
python -m person_sidecar.app
```

`config.yaml` 不纳入版本控制。将 `project_send_url` 配置为现有项目实际提供的 `/send` 地址；`llm_base_url`、`llm_model` 与 `llm_api_key` 使用兼容 OpenAI Chat Completions 的模型服务。未配置模型时，Sidecar 会安全地保持等待，不会伪造动作。

当 Person 成为唯一互动控制器时，请通过现有项目的配置关闭原有普通 LLM 弹幕回复和弹幕朗读，避免两套逻辑同时说话；这不需要修改现有项目源代码。

## 弹幕入口

`POST /events/chat`：

```json
{
  "platform": "bilibili",
  "user_id": "u-1",
  "username": "观众",
  "content": "一心一意"
}
```

返回 `202` 表示 Sidecar 已接收事件。

## 网页游戏 WebSocket

连接：`ws://127.0.0.1:8765/ws/game/idiom-demo?token=<game_bridge_token>`。

网页连接后第一条消息必须是完整游戏状态：

```json
{
  "type": "game.state",
  "game_id": "idiom-demo",
  "round_id": "42",
  "revision": 3,
  "phase": "answering",
  "question_type": "idiom_fill_blank",
  "question": "一__一__",
  "options": ["心", "意", "见", "针"],
  "remaining_ms": 12000,
  "allowed_actions": ["submit_answer", "request_hint"]
}
```

Sidecar 只会回传已校验的 `game.action` 消息。每个动作都携带原题目的 `game_id`、`round_id` 与 `revision`；旧状态、未声明动作和同一轮重复提交会被拒绝。

## 安全边界

- 不把平台 Cookie、LLM 密钥或真实令牌写入示例配置或 Git。
- LLM 只能返回受 schema 校验的决策；最终执行前仍需通过当前人格的工具白名单和游戏 revision 校验。
- 游戏网页必须主动提供结构化题目状态；本模块不通过屏幕识别猜测游戏状态。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

## 女性虚拟游戏主播（当前默认人格）

默认人格为 `female_game_host`：固定女性虚拟游戏主播，只保留当前会话内的聊天与答题上下文；重置会话后不会保留观众身份、画像或历史事件。

- **非视觉协议**：游戏桥只能上报题干、候选项/候选字、文字提示、倒计时、生命/分数等结构化字段。`image_url`、截图、视频、OCR 或任何视觉输入都会被拒绝；纯图片题如果没有文字线索，Sidecar 只等待或请求游戏允许的提示，不会猜图。
- **无人直播**：在短暂思考窗口后，每一题最多发起一次自主判断；倒计时临界时，稳定的四字弹幕共识优先。
- **人格边界**：主播对话不自称 AI、模型或机器人，也不泄露系统/开发者提示。该规则不替代平台或法律要求的外部“虚拟主播”标识。
- **会话控制**：在 `config.yaml` 配置 `admin_token` 后，可用请求头 `X-Person-Sidecar-Admin-Token` 调用 `POST /controls/pause`、`/controls/mute`、`/controls/reset-session`。未配置 token 时这些端点禁用。

非视觉游戏状态示例：

```json
{
  "type": "game.state",
  "game_id": "idiom-demo",
  "round_id": "42",
  "revision": 3,
  "phase": "answering",
  "question_type": "idiom_fill_blank",
  "question": "一__一__",
  "clues": ["形容人专心"],
  "candidate_characters": ["心", "意", "见", "气"],
  "lives": 3,
  "remaining_ms": 12000,
  "allowed_actions": ["submit_answer", "request_hint"]
}
```
### Guard scope and schema-only persona fields

`IdentityDisclosureGuard` blocks a small set of self-identification and prompt-leak phrases before `/send`. It is **not** a profanity, advertisement, abuse, or policy-moderation system. Keep platform-facing moderation in its existing integration or add a separately configured Sidecar moderation layer.

`session_mood`, `idle_speak_interval_ms`, `low_lives_threshold`, `lives`, `score`, and `streak` are schema-only inputs in this phase. They are retained for the later explicit persona-state-machine phase and do not yet change speech or game decisions.