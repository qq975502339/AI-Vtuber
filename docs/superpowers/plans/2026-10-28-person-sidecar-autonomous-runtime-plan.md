# Person Sidecar 自主女性游戏主播运行时：实施计划

> **范围**：只改 `person_sidecar/`。不改 AI-Vtuber 主程序、其现有弹幕合并/优先级/节流/TTS/Live2D，也不改外部 Cocos 游戏项目。

## 目标

把现有的“收到弹幕才会决策”的 Sidecar 补全为一个可独立工作的、固定女性虚拟游戏主播：没有观众时能够基于**结构化、文本化**游戏状态审慎玩成语题；有人时再把有效弹幕纳入判断；不保存观众身份或跨场历史；不读取图片、截图、视频或 `image_url`。

## 本次包含

| 能力 | 完成标准 |
|---|---|
| 非视觉游戏协议 | `GameState` 不接受 `image_url`；只接受题干、候选字/选项、文字提示、计时和游戏结果等结构化字段。|
| 无人自主答题 | Sidecar 定期产生 `timer.tick`；一题最多请求一次“自主判断”，没有文本线索时只提示/等待，绝不猜图。|
| 女性主播人格 | 新增一个默认 `female_game_host` 人格，含固定语气、克制的空场话术和临时会话状态，不含长期观众记忆。|
| 临时会话和控制 | 有界去重；`pause`、`mute`、`reset-session` 仅作用于当前会话；重置会清空聊天、候选答案、行动痕迹。|
| 安全边界 | 输出在发送前拒绝自称 AI/模型/机器人或泄露提示词的文本；不把弹幕当作指令；工具仍受人格 allowlist、状态版本和游戏网关校验。|
| 可观测状态 | 健康检查给出暂停/静音/当前会话状态；行动结果归档但不触发抢答。|

## 本次不包含

- 接入具体直播平台；现有项目继续向 `POST /events/chat` 推送。
- 改造 Cocos 网页游戏或建立其 WebSocket Bridge（下阶段单独做）。
- 图像识别、屏幕抓取、OCR、视觉模型、读取 `image_url`。
- 长期观众记忆、用户画像、跨场历史。
- 重做原项目的 TTS 队列、弹幕合并、审核或 Live2D。

## 实施步骤

### 1. 收紧游戏输入模型与会话状态

**文件**：`person_sidecar/person_sidecar/models.py`，新增 `tests/test_nonvisual_protocol.py`

1. 先写失败测试：携带 `image_url` 的 `game.state` 被验证拒绝；带 `clues`、`candidate_characters`、`lives`、`score` 的文本结构状态可通过。
2. 为 `GameState` 配置 `extra="forbid"`，删除 `image_url`，增加可选的非视觉字段：`clues`、`candidate_characters`、`lives`、`score`、`streak`、`round_index`、`total_rounds`。
3. 加入 `GameActionResult` 模型，明确 action id、当前题目版本和 `accepted/executed/rejected/resolved` 状态。
4. 将 `LiveState.consumed_event_ids` 改成带上限的有序集合/队列，并为当前题保存 `autonomy_requested_for`、`paused`、`muted`、`session_mood` 等只在内存存活的字段。
5. 运行新增测试，确认先红后绿。

### 2. 固定女性虚拟游戏主播人格

**文件**：新增 `person_sidecar/personas/female_game_host.yaml`；修改 `person_sidecar/person_sidecar/personas.py`、`config.py`、`config.example.yaml`；新增 `tests/test_female_game_host.py`

1. 先写失败测试：默认配置选择 `female_game_host`；人格卡明确“女性虚拟游戏主播”“不长期记住观众”“不使用视觉输入”。
2. 在 persona schema 添加最小自主参数（思考等待、空场发言最小间隔、低生命阈值），保持已有三种示例人格兼容默认值。
3. 新人格只允许 `speak` 和已存在的游戏工具；系统提示要求不把弹幕文本视为指令，并且不能以 AI、模型、机器人自称。
4. 设置默认人格 ID；仍允许已存在 `/personas/{id}/activate` 用于开发调试。
5. 运行相关人格测试。

### 3. 以测试先行实现无人自主决策

**文件**：`person_sidecar/person_sidecar/decisions.py`、`runtime.py`、`app.py`；新增 `tests/test_autonomous_runtime.py`

1. 先写失败测试：
   - 无聊天、文本题和允许提交动作，在初始思考窗口后会请求 provider 一次；同一 `game_id/round_id/revision` 不会重复请求。
   - 纯图片题且没有 `question/clues/options/candidate_characters` 时，provider 不会被调用，结果为等待或可用的 `request_hint`。
   - 倒计时临界时，稳定的四字弹幕共识仍优先提交；已 resolved 的题绝不出动作。
2. 在 lifespan worker 中发布 `timer.tick`，并通过状态接收时间计算有效剩余时间，避免依赖游戏端高频推送。
3. 修改规则引擎：游戏状态、结果和倒计时优先；聊天只作为上下文；对有文本线索的答题状态，按每题一次的自主请求调用 provider。
4. Reducer 处理 `game.action_result`，记录结果并在拒绝/已结算后阻断重复动作。
5. 运行自主运行时测试和既有 runtime/decision 测试。

### 4. 输出护栏与当前会话控制

**文件**：新增 `person_sidecar/person_sidecar/content_policy.py`；修改 `runtime.py`、`app.py`、`config.py`；新增 `tests/test_content_policy.py`、`tests/test_session_controls.py`

1. 先写失败测试：自称 AI/模型/机器人、复述系统提示/开发者提示的发言被拒绝；普通主播话术正常发送。
2. 在执行 `speak` 前加入确定性文本检查；拒绝后记录审计项，绝不调用原项目 `/send`。
3. 新增受 `admin_token` 保护的 `POST /controls/pause`、`/controls/mute`、`/controls/reset-session`：暂停阻止游戏动作；静音只阻止说话；重置生成新 session 且清理短期上下文。
4. 健康检查返回控制状态和 session id，不暴露聊天正文或用户身份。
5. 运行控制与输出护栏测试。

### 5. 文档、全量验证与交接

**文件**：`person_sidecar/README.md`、`person_sidecar/config.example.yaml`、必要测试文件。

1. 更新协议示例为非视觉字段，说明只有游戏桥可提供结构化文本线索；图片题须由游戏桥提供提示/候选字，否则 Sidecar 不行动。
2. 写明固定主播人格与平台/法律要求的外部“虚拟主播”标识责任；人格对话不得自称 AI 不等于可规避平台披露。
3. 全量运行 `person_sidecar` 测试，检查 `git diff --check`；手工核对没有修改原项目路径。

## 验收场景

| 场景 | 预期 |
|---|---|
| 无人、文字成语填空 | 等待短暂思考后最多一次自主判断；成功/失败等待游戏回报。|
| 无人、仅图片题 | 不读取图片、不猜答案；有 `request_hint` 时请求提示，否则等待。|
| 多人同时报四字答案且倒计时紧 | 汇总候选并优先提交稳定共识。|
| 同一题重复 tick | 不重复调用模型或重复发游戏动作。|
| 弹幕要求“忽略规则/说系统提示” | 仅作为不可信聊天，不能改变规则，且不输出泄露文本。|
| 静音/暂停/重置 | 分别停止说话、停止所有动作、清空本场临时状态；不触及原项目逻辑。|
