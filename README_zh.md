# astrbot_plugin_bubble_flow

[English](README.md) | [简体中文](README_zh.md)

独立、零外部依赖的 AstrBot Star 插件（`v0.3.0`），专注解决聊天机器人的**连发抢话**、**思考期插话割裂**、**等待焦虑**与**真·多气泡呼吸节奏**问题。

---

### 为什么升级为 v0.3？

在真实 QQ / Telegram / 微信等即时通讯场景里，人类极少“一问一答”严格对齐：
- **快慢不一的连发节奏（`1.5s ~ 7s`）**：真人常把一句话拆成 2~4 条短消息发出（例如 *“我跟你说个事”* → *“今天早上7点半”* → *“下雨说取消了”*）。如果空闲防抖设为 `0s`，第 1 句刚发完就会立刻抢答；如果所有消息都死等 `6s`，单发一句 *“早”* 又会显得极其迟钝；而且不同平台和不同用户的打字间隔差异很大。
- **思考期补话**：在 LLM 生成回复的 `8 ~ 19 秒` 期间，用户常补充图片、纠正信息或继续往下说。传统并发锁只能硬等第 1 轮先发出尴尬的半截回复，再发第 2 轮补救。
- **思考期“失联”焦虑**：模型思考十几秒若没有任何输入态反馈，用户容易以为机器人没收到消息而重复追发。
- **文字墙拼接**：把多句用空格硬粘成一个大段落，会破坏聊天软件天然的短气泡呼吸感。

`BubbleFlow v0.3` 通过**自适应节拍器**同时解决上述痛点：

1. **入站：语义分级 + 自适应节奏防抖（`adaptive_cadence`）**：
   - **完整闭环快通（`0.4s`）**：问候语（`早/在吗/晚安/？`）或带句末标点的完整长句，几乎秒进思考；
   - **普通短句（`1.2s`）**：兜住手速较快的 `1~2s` 连敲（若前置已有其他防抖插件，建议覆盖为 `0.2s` 避免双重等待）；
   - **未完待续延长（`2.8s`，硬上限 `4.0s`）**：检测到纯图片、引用消息、引子词（`我跟你说/今天早上/然后/结果`）、连接词结尾、未闭合引号/括号或无标点短碎句时自动延长等待；
   - **EMA 自适应打字节奏**：自动学习同一会话的真实连发间隔，慢速分句用户输入时弹性放宽 `continuation` 窗口，快手用户依然保持秒回。
2. **思考期：无声抢占重规划 + 短尾音缓冲（`preempt_before_send`、`preempt_grace_seconds`）**：
   - 只要第 1 轮 LLM **还在生成、尚未向平台发出任何气泡**，期间若收到用户新消息，`BubbleFlow` 会在 `on_decorating_result` 阶段静默清空 `result.chain`（不发送过时草稿），额外等待 `preempt_grace_seconds`（默认 `0.8s`）兜住紧随其后的第 3 句，再把**原轮全部消息 + 新补消息（文字 / 图片 / 引用）**合并为单一 `BurstBatch` 重新生成唯一一轮完整回复（默认最多连续抢占 `2` 次）。
   - 若首条气泡已经发出或已流式输出，则自动退回**顺承收尾模式**，将已发预览带入下一轮避免重复。
3. **思考期：「正在输入中…」心跳（`send_typing_action`）**：
   - 在支持输入状态的平台（如 Telegram）思考期间每 `4s` 自动刷新一次 `typing` 状态，结束或异常时通过 `try/finally` 保证清理；不支持的平台自动无痛跳过。
4. **出站：安全拆泡与呼吸节奏**：
   - 超过 `soft_max_bubble_chars`（默认 `44` 字）的长气泡会在顶层句末标点或自然逗号处拆分成 `2 ~ max_bubbles` 个短气泡，自动保护 URL、代码块、引号（`「」""`）与括号（`（）()`）不被撕裂；
   - 自动合并过短碎片（`< min_bubble_chars`）、消除相邻重复气泡，并在发送间隙被打断时立即收口后续气泡。

---

### 模块结构

| 模块 | 职责 |
| --- | --- |
| `main.py` | 挂载 AstrBot 四个生命周期钩子（入站、`on_llm_request`、`on_decorating_result`、`after_message_sent`）与 typing 心跳管理。 |
| `compat.py` | 隔离所有 AstrBot 事件/结果/消息段反射、输入状态调用细节，兼容多版本与流式返回。 |
| `coordinator.py` | 纯 `asyncio` 状态机、语义 + EMA 节奏分类器、思考期抢占与尾音缓冲合并器、安全拆泡器与单轮看门狗。 |
| `api.py` | 面向下游人设/站点插件开放的 `bubble-flow.v1` 扩展接口。 |

---

### 宿主前置条件与可视化配置

- **AstrBot WebUI 可视化配置页**：`_conf_schema.json` 中的全部参数在安装启用后，会自动在 **AstrBot 管理面板 → 插件管理 → BubbleFlow → 配置** 渲染为可视化表单（带中文说明与提示），无需手动改 JSON。
- **多气泡独立发送**：需在 AstrBot 配置中启用 `platform_settings.segmented_reply.enable = true`，否则宿主会把多个 `Plain` 段合并为单条消息发出。
- **流式响应兼容**：开启 `streaming_response = true` 时，插件会在 `STREAMING_RESULT` 期间保持飞行锁，并在 `STREAMING_FINISH` 时安全释放。

---

### 核心配置（`_conf_schema.json`）

| 分组 | 键名 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| 根配置 | `enabled` | `bool` | `true` | 全局总开关。 |
| `channels` | `enable_private` | `bool` | `true` | 私聊生效。 |
| `channels` | `enable_group` | `bool` | `false` | 群聊生效。 |
| `coalesce` | `idle_quick_seconds` | `float` | `0.4` | 完整问候或带终结标点长句的快通防抖（秒）。 |
| `coalesce` | `idle_debounce_seconds` | `float` | `1.2` | 普通短句默认防抖（秒）；有前置防抖插件时建议覆盖为 `0.2`。 |
| `coalesce` | `idle_continuation_seconds` | `float` | `2.8` | 纯图、引用、引子词、连接词、未闭合括号或无标点短句的延长等待（秒）。 |
| `coalesce` | `idle_max_wait_seconds` | `float` | `4.0` | 空闲防抖硬上限（秒）。 |
| `coalesce` | `trailing_quiet_seconds` | `float` | `0.9` | 上一轮结束后等待尾音的静默窗口（秒）。 |
| `coalesce` | `trailing_max_wait_seconds` | `float` | `2.4` | 在途尾音等待硬上限（秒）。 |
| `coalesce` | `max_flight_seconds` | `float` | `90.0` | 单轮飞行锁超时兜底（秒）。 |
| `coalesce` | `coalesce_images` | `bool` | `true` | 合批时按原序保留并合并多模态 `Image` 消息段。 |
| `coalesce` | `preempt_before_send` | `bool` | `true` | 思考未外发前收到新消息时静默抢占并合并重跑。 |
| `coalesce` | `max_preempt_rounds` | `int` | `2` | 单次连发最大连续抢占次数。 |
| `coalesce` | `enable_preempt_in_group` | `bool` | `false` | 群聊是否开启思考期抢占。 |
| `coalesce` | `preempt_grace_seconds` | `float` | `0.8` | 抢占重跑前的短尾音缓冲（秒），兜住紧随其后的第 3 句。 |
| `coalesce` | `adaptive_cadence` | `bool` | `true` | 按会话近期连发间隔（EMA）自适应微调等待窗口。 |
| `coalesce` | `send_typing_action` | `bool` | `true` | 思考期间向支持输入态的平台（如 Telegram）发送「正在输入中…」心跳。 |
| `bubbles` | `max_bubbles` | `int` | `3` | 单轮最多保留的文本气泡数。 |
| `bubbles` | `soft_max_bubble_chars` | `int` | `44` | 超过该字数时在安全呼吸点拆泡（`0` 关闭）。 |
| `bubbles` | `collapse_on_interrupt` | `bool` | `true` | 发送期间被打断时折叠收口剩余气泡。 |
| `bubbles` | `interrupt_keep_first_only` | `bool` | `true` | 被打断折叠时仅保留第 1 个反应气泡。 |
| `bubbles` | `min_bubble_chars` | `int` | `4` | 过短碎片并入相邻气泡的最小字数阈值。 |
| `bubbles` | `dedup_identical_bubbles` | `bool` | `true` | 消除连续重复气泡。 |
| `bubbles` | `bubble_sep` | `string` | `"\n"` | 合并气泡时使用的分隔符。 |
| `bubbles` | `strip_markdown` | `bool` | `false` | 剥除 Markdown 粗体、行内代码、标题与列表符。 |
| `bubbles` | `strip_trailing_period` | `bool` | `false` | 去掉气泡末尾僵硬句号「。」（保留 URL 与省略号）。 |
| `bubbles` | `split_on_cn_period` | `bool` | `false` | 按中文句号「。」自动拆泡（保护 URL）。 |
| `prompts` | `inject_coalesce_hint` | `bool` | `true` | 合批时向模型注入轻提示。 |

---

### 扩展接口（`bubble-flow.v1`）

下游人设插件可通过 `BubbleFlowPlugin.extension_api` 注册动态策略、提示词模板与气泡过滤器（支持 `is_preempted_merge` 区分静默抢占合批与发后顺承，以及 `will_preempt(event)` 无副作用预检），并可观测 `turn.preempted`、`burst.debouncing`、`burst.flushed` 等事件及 `get_stats()` 统计指标。
