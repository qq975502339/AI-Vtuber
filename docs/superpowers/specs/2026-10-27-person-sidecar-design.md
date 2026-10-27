# Person Sidecar 设计说明

## 目标

在不修改现有 AI-Vtuber 源码和既有直播处理逻辑的前提下，新增一个可独立启动的 Person Sidecar。它接收直播弹幕与网页小游戏状态，维护单场直播的共享短期状态，决定回复、等待或操作游戏，并复用现有项目的 `/send` 接口完成语音输出。

第一期以“成语填空 / 看图猜成语”网页小游戏为样例，同时保证后续小游戏只需实现游戏适配器，不需要改 Person 核心。

## 约束与非目标

- 不修改 `main.py`、`utils/`、`game/`、`webui.py` 或现有项目的任何业务逻辑。
- 不接管现有项目的 TTS、音频队列、Live2D、OBS 或平台监听实现；Person 只通过公开 HTTP 接口请求主播发言。
- Person 第一版只保存本次直播所需的短期状态，不实现跨直播长期记忆、关系记忆或人格自我演化。
- 不允许 LLM 直接执行浏览器脚本、系统命令或任意 HTTP 请求；它只能从显式注册的工具中选择。
- 不通过视觉识别猜测网页游戏状态。游戏网页必须主动上报结构化状态，并只接受受控动作。

## 推荐实现

采用“Cortico 风格的事件 / 世界（适配器）边界 + Python 轻量 Sidecar”。

不直接引入 Cortico 作为运行时依赖：当前项目是 Python，且现有直播能力已经完整；引入一个 TypeScript VTuber 运行时会造成平台、语音和形象能力重叠。Cortico 的事件和工具边界作为架构参考。Person Sidecar 使用 Python 实现，独立于原项目目录逻辑运行。

## 运行结构

```text
直播平台弹幕 ──> ChatAdapter ───┐
                                │
网页小游戏 <──WebSocket──> GameAdapter ──> Event Queue ──> PersonRuntime
                                                                     │
                                                               Decision Engine
                                                         ┌───────────┴───────────┐
                                                         │                       │
                                               ProjectSpeakerAdapter       GameAdapter
                                                   POST /send              game.action
                                                         │                       │
                                                  现有 TTS / Live2D          网页游戏
```

### 与原项目的边界

Person 不导入原项目的 Python 模块，也不订阅其内部 `process_data` 调用。它以独立进程运行：

- `ProjectSpeakerAdapter` 调用现有 API 的 `POST /send`，使用 `type: reread` 和 Person 生成的文本；现有项目继续负责合成、排队、播放和形象驱动。
- Person 通过独立的 `ChatAdapter` 连接直播平台或上游弹幕转发服务，获得与原项目无关的弹幕事件。
- 正式启用 Person 统一互动时，通过配置关闭原项目的普通 LLM 弹幕回复与弹幕朗读，避免两套大脑对同一弹幕分别说话。这是部署配置，不是源码改动。
- 若某平台不允许两套监听器同时连接，使用平台允许的只读弹幕连接或独立弹幕转发服务；不得通过修改现有项目内部事件链路绕过这个限制。

## 组件职责

### PersonRuntime

单个直播间对应一个 Runtime，负责串行消费事件、保存短期 `LiveState`、调用决策器和分派工具。Runtime 是唯一能发起“说话”与“游戏动作”的组件，避免弹幕和倒计时同时触发重复操作。

`LiveState` 仅包括：

- 当前 persona ID 与会话 ID；
- 当前游戏快照、题目 revision、倒计时和最近动作结果；
- 最近有效弹幕、候选答案和已消费事件 ID；
- 当前是否正在等待游戏结果、最近播报时间与最近播报文本；
- 工具调用冷却时间和单轮动作锁。

退出直播时销毁该状态。Person 配置和审计日志可落盘，但不把观众对话自动升级为长期人格记忆。

### PersonaRegistry

从 `personas/*.yaml` 加载人格包。每个包独立定义：

- 身份、背景、语言习惯和禁止事项；
- 主动程度、回复冷却时间、最大播报长度；
- 游戏偏好，例如“优先采纳观众答案”或“倒计时不足时自行作答”；
- 可使用工具白名单；
- LLM 系统提示词和结构化输出约束。

首批提供三个示例人格：`host_interactive`、`idiom_teacher`、`playful_gamer`。它们共享同一 Runtime、游戏协议和输出适配器，只更换行为策略和语言风格。

### ChatAdapter

把不同平台或弹幕转发服务的消息转成统一 `chat.message` 事件：`event_id`、`occurred_at`、`platform`、`user_id`、`username`、`content` 和可选礼物 / 管理员标记。适配器不做人格判断，不直接调用 LLM。

### GameAdapter

维护一条与可嵌入网页游戏的 WebSocket 连接，验证游戏上报状态，向网页发送受控动作。第一期动作固定为：`submit_answer`、`select_option`、`request_hint`、`next_round`。

网页必须上报：`game_id`、`round_id`、`revision`、`phase`、`question_type`、题面或图片引用、候选项、剩余时间和允许动作。每个动作携带当时的 `round_id` 与 `revision`；revision 不匹配、轮次结束或同轮已有成功提交时，Sidecar 必须拒绝动作。

### DecisionEngine

采用“确定性规则优先，LLM 只做受约束选择”。

1. 先做纯规则判断：无有效游戏状态时不操作；旧 revision 直接丢弃；距离截止时间很短时禁止闲聊；冷却中禁止重复播报；同轮只允许一个提交动作。
2. 将同一短窗口内的相似答案归并为候选集合；不把所有弹幕逐条交给模型。
3. 当规则不能确定答案或合适回复时，调用 LLM，并要求返回 JSON：`intent`、`speak_text`、`tool_name`、`tool_args`、`reason`、`confidence`。
4. 在工具执行前再次以 schema、persona 工具白名单和当前 `LiveState` 校验。校验失败时只允许安全的简短播报或保持等待。

LLM 不拥有最终执行权，也不能产生未注册工具名。

## 事件与工具协议

统一内部事件：

```json
{
  "event_id": "uuid",
  "type": "chat.message | game.state | game.action_result | timer.tick",
  "occurred_at": "2026-10-27T14:30:00+08:00",
  "payload": {}
}
```

网页向 Sidecar 发送状态示例：

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

Sidecar 向网页发送动作示例：

```json
{
  "type": "game.action",
  "action_id": "uuid",
  "game_id": "idiom-demo",
  "round_id": "42",
  "revision": 3,
  "action": "submit_answer",
  "args": {"answer": "一心一意"}
}
```

`ProjectSpeakerAdapter` 输出使用现有项目公开接口：

```json
{
  "type": "reread",
  "data": {
    "username": "Person",
    "content": "大家的答案很接近，我们试试一心一意！"
  }
}
```

## 成语游戏决策顺序

同一轮内，优先级固定为：

1. 游戏结束、答题结果和错误状态；
2. 即将到期的答题动作；
3. 观众高共识且合法的候选答案；
4. Person 主动推理、提示或请求提示；
5. 普通聊天互动；
6. 无事件时的低频主动串场。

现有项目已有的音频优先级和队列仍由原项目负责；上述优先级仅决定 Person 是否发起下一次动作或发言。

## 错误处理与安全边界

- 网页 WebSocket 断开：停止发送游戏动作，保留最近状态为过期并告知 Runtime 降级为聊天模式；重连后必须收到完整新状态才恢复。
- 原项目 `/send` 调用失败：指数退避重试一次；仍失败时记录失败事件，不重复执行关联的游戏动作。
- LLM 超时或返回非 JSON：不重试同一决策；使用确定性保底文本或等待下一事件。
- 接收到敏感、攻击性或明显无关的弹幕：由 Person 输入过滤器丢弃，不进入候选答案集合。
- 所有外部事件和动作都记录 `event_id`、`action_id`、persona ID 和结果；日志不得写入密钥、Cookie 或完整平台鉴权信息。

## 第一阶段验收标准

1. 原项目代码文件零修改，且启动方式不变。
2. Sidecar 启动后能加载三个人格，并在运行时切换人格而不丢失当前游戏轮次。
3. 模拟弹幕和网页状态可驱动 Person 选择“回复”“提交成语”“请求提示”或“等待”。
4. 旧 revision、重复事件、同轮重复提交和未声明动作都会被拒绝。
5. Person 发言可通过现有 `/send` 接口进入原项目既有语音链路。
6. Sidecar 的单元测试不依赖真实直播平台、真实 LLM、真实网页游戏或真实 TTS 服务。

## 分期范围

- Phase 1：Sidecar 核心、PersonaRegistry、事件队列、受控决策器、成语网页 WebSocket 适配器、`/send` 输出适配器和模拟测试。
- Phase 2：一个实际直播平台的独立 ChatAdapter、运行监控页、人工切换人格和更完整的审核规则。
- Phase 3：更多小游戏适配器、可选的跨直播记忆和可配置的运营后台。

Phase 1 完成后，新游戏接入的唯一要求是实现 `GameAdapter` 协议；不得为每款游戏改动 PersonRuntime 或现有 AI-Vtuber 逻辑。
