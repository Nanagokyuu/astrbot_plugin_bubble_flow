# astrbot_plugin_bubble_flow

[English](#english) | [简体中文](#简体中文)

---

<a id="english"></a>

## English

A standalone, zero-dependency AstrBot Star plugin for **adaptive burst coalescing**, **pre-send LLM preemption**, and **natural multi-bubble pacing**.

### Why v0.2? (Solving Real-World Chat Pain Points)

In real IM conversations (QQ, WeChat, Telegram, etc.), humans rarely wait politely for a single turn:
- **Rapid bursts (`3s ~ 9s`)**: People send a thought across 2–4 short messages (*"Guess what happened"* → *"I ordered food"* → *"They sent the wrong thing"*). A `0s` debounce interrupts immediately on message #1; a naive `6s` fixed debounce makes single messages (*"Morning"*) feel sluggish.
- **Mid-thought follow-ups**: While the LLM is still thinking (typically `8s ~ 19s`), the user adds context or corrects themselves. Without preemption, the bot still sends an awkward half-baked reply first and then a redundant follow-up.
- **Wall-of-text merging**: Joining multi-line outputs with spaces turns natural short chat bubbles into stiff paragraphs.

`BubbleFlow v0.2` solves all three with a **3-stage turn pacer**:

1. **Semantic Adaptive Idle Debounce**:
   - **Quick-pass (`0.4s`)** for clearly complete greetings (*"Morning"*, *"?"*) or punctuated long messages.
   - **Normal (`1.2s`)** for everyday short lines.
   - **Continuation (`2.8s`, capped at `4.0s`)** when the user sends a pure image, reply quote, lead-in phrase (*"Wait"*, *"Guess what"*), unpunctuated short fragment (`<= 10` chars), or is already mid-burst.
2. **Pre-Send Preemption (`preempt_before_send`)**:
   - If new user messages arrive **while the LLM is still generating** (before any bubble is sent), `BubbleFlow` silently clears `result.chain` at `on_decorating_result`, merges the original turn + new follow-ups (text, `Image`, `Reply`) into one `BurstBatch`, and re-runs a single unified reply (`max_preempt_rounds = 2` cap prevents infinite loops).
   - If the first bubble was already sent (or upstream streamed/sent directly), it gracefully falls back to **follow-up continuation mode** with `last_reply_preview` context.
3. **Safe Multi-Bubble Pacing**:
   - Splits overly long bubbles (`soft_max_bubble_chars`, default `44`) at top-level punctuation/clause boundaries while protecting URLs, code blocks, quotes (`「...」`, `"..."`), and parentheses (`(...)`, `（...）`).
   - Merges tiny micro-fragments (`< min_bubble_chars`), deduplicates identical bubbles, and collapses remaining bubbles on mid-send interrupts.

### Architecture

| Module | Responsibility |
| --- | --- |
| `main.py` | Wires the four AstrBot hooks (`intake`, `on_llm_request`, `on_decorating_result`, `after_message_sent`). |
| `compat.py` | Isolates all AstrBot event/result/component reflection and version quirks. |
| `coordinator.py` | Pure `asyncio` session state machine, semantic debounce classifier, pre-send preemption, safe bubble splitter, and per-turn watchdog. |
| `api.py` | Versioned `bubble-flow.v1` extension API for downstream persona/bridge plugins. |

### Host Prerequisites

- **Multi-bubble delivery**: AstrBot sends multiple `Plain` segments as separate chat bubbles when `platform_settings.segmented_reply.enable = true`.
- **Streaming mode (`streaming_response = true`)**: Streaming chunks bypass `after_message_sent`; `BubbleFlow` holds the flight lock during `STREAMING_RESULT` and releases on `STREAMING_FINISH`.

### Configuration (`_conf_schema.json`)

| Group | Key | Type | Default | Description |
| --- | --- | --- | --- | --- |
| root | `enabled` | `bool` | `true` | Master switch for coalescing and bubble shaping. |
| `channels` | `enable_private` | `bool` | `true` | Enable in private/friend chats. |
| `channels` | `enable_group` | `bool` | `false` | Enable in group chats. |
| `coalesce` | `idle_quick_seconds` | `float` | `0.4` | Fast-path debounce for complete greetings / punctuated long messages. |
| `coalesce` | `idle_debounce_seconds` | `float` | `1.2` | Default idle debounce for normal short messages (`0.2` recommended if an upstream debouncer already runs). |
| `coalesce` | `idle_continuation_seconds` | `float` | `2.8` | Extended wait for pure images, reply quotes, lead-ins, or unpunctuated fragments. |
| `coalesce` | `idle_max_wait_seconds` | `float` | `4.0` | Hard cap on idle debounce waiting. |
| `coalesce` | `trailing_quiet_seconds` | `float` | `0.9` | Quiet window after a turn finishes/preempts before flushing queued follow-ups. |
| `coalesce` | `trailing_max_wait_seconds` | `float` | `2.4` | Hard cap on trailing wait during continuous bursts. |
| `coalesce` | `max_flight_seconds` | `float` | `90.0` | Per-turn watchdog timeout to auto-release stalled flights. |
| `coalesce` | `coalesce_images` | `bool` | `true` | Merge mid-flight `Image` components in chronological order. |
| `coalesce` | `preempt_before_send` | `bool` | `true` | Silently discard an unpublished LLM draft and re-run merged when new input arrives mid-thought. |
| `coalesce` | `max_preempt_rounds` | `int` | `2` | Maximum consecutive silent preemptions per burst chain. |
| `coalesce` | `enable_preempt_in_group` | `bool` | `false` | Allow pre-send preemption in group chats. |
| `bubbles` | `max_bubbles` | `int` | `3` | Maximum outgoing text bubbles retained per turn. |
| `bubbles` | `soft_max_bubble_chars` | `int` | `44` | Soft length cap per bubble before safe semantic splitting (`0` disables). |
| `bubbles` | `collapse_on_interrupt` | `bool` | `true` | Collapse remaining bubbles if new messages arrive while sending. |
| `bubbles` | `interrupt_keep_first_only` | `bool` | `true` | Keep only the first reaction bubble when collapsed on interrupt. |
| `bubbles` | `min_bubble_chars` | `int` | `4` | Merge tiny fragments shorter than this threshold into an adjacent bubble. |
| `bubbles` | `dedup_identical_bubbles` | `bool` | `true` | Remove consecutive duplicate bubbles. |
| `bubbles` | `bubble_sep` | `string` | `"\n"` | Separator used when merging bubbles. |
| `bubbles` | `strip_markdown` | `bool` | `false` | Strip common Markdown formatting (`**bold**`, inline code, headings, bullets). |
| `bubbles` | `strip_trailing_period` | `bool` | `false` | Strip trailing Chinese periods (`。`) while preserving URLs and ellipsis. |
| `bubbles` | `split_on_cn_period` | `bool` | `false` | Split internal Chinese periods (`。`) into separate bubbles while masking URLs. |
| `prompts` | `inject_coalesce_hint` | `bool` | `true` | Inject a lightweight context hint when multiple items are merged. |

### Extension API (`bubble-flow.v1`)

Downstream plugins can discover `BubbleFlowPlugin.extension_api` without hard package coupling:

```python
for star in self.context.get_all_stars():
    if star.name in ("astrbot_plugin_bubble_flow", "bubble_flow") and star.activated:
        api = getattr(star.star_cls, "extension_api", None)
        if getattr(api, "api_version", None) == "bubble-flow.v1":
            break
```

- **Hooks (all run with exception isolation and return an `unregister(token)` handle)**:
  - `register_policy_resolver(resolver_id, fn, *, priority=100)`
  - `register_coalesce_formatter(formatter_id, fn, *, priority=100)` (`batch.is_preempted_merge` distinguishes silent preemption merges from post-send follow-ups)
  - `register_bubble_filter(filter_id, fn, *, priority=100)`
  - `register_event_listener(listener_id, fn)`
- **Session Control & Metrics**:
  - `set_session_policy(umo, **overrides)` / `clear_session_policy(umo)` / `get_session_policy(...)`
  - `suppress_next_coalesce(umo_or_event)`
  - `get_session_state(umo)` / `get_stats()` (`total_runs`, `total_coalesced`, `total_preempted`, `total_fast_passed`, `total_continuation_waits`, `total_collapsed`, `total_watchdog_resets`)
  - `await force_release(umo)`
- **Lifecycle Events**: `burst.debouncing`, `message.swallowed`, `turn.interrupted`, `turn.preempted`, `burst.flushed`, `turn.started`, `bubbles.collapsed`, `turn.finished`, `turn.watchdog_released`.

---

<a id="简体中文"></a>

## 简体中文

独立、零外部依赖的 AstrBot Star 插件，专注解决聊天机器人的**连发抢话**、**思考期插话割裂**与**真·多气泡呼吸节奏**问题。

### 为什么重做为 v0.2？

在真实 QQ / 微信等即时通讯场景里，人类极少“一问一答”严格对齐：
- **连发间隔规律**：真人常在 `3 ~ 9 秒` 内连续敲出 2~4 条短消息（例如 *“我跟你说个事”* → *“刚点外卖”* → *“送错了”*）。如果空闲防抖设为 `0s`，第 1 句刚发完就会立刻抢答；如果把所有消息都死等 `6s`，单发一句 *“早”* 又会显得极其迟钝。
- **思考期补话**：在 LLM 生成回复的 `8 ~ 19 秒` 期间，用户常补充图片、纠正信息或说“算了没事”。传统并发锁只能硬等第 1 轮先发出尴尬的半截回复，再发第 2 轮补救。
- **文字墙拼接**：把多句用空格硬粘成一个大段落，会破坏聊天软件天然的短气泡呼吸感。

`BubbleFlow v0.2` 通过**三段式节拍器**同时解决上述痛点：

1. **入站：语义分级短防抖（不拖慢单句，专接未完待续）**：
   - **完整闭环快通（`0.4s`）**：问候语（`早/在吗/晚安/？`）或带句末标点的完整长句，几乎秒进思考；
   - **普通短句（`1.2s`）**：兜住手速较快的 `1~2s` 连敲（若前置已有其他防抖插件，建议覆盖为 `0.2s` 避免双重等待）；
   - **未完待续延长（`2.8s`，硬上限 `4.0s`）**：检测到纯图片、引用消息、引子词（`我跟你说/刚刚/顺便/其实/但是`）、无标点短碎句（`<= 10` 字）或处于连发状态时自动延长等待。
2. **思考期：无声抢占重规划（`preempt_before_send`）**：
   - 只要第 1 轮 LLM **还在生成、尚未向平台发出任何气泡**，期间若收到用户新消息，`BubbleFlow` 会在 `on_decorating_result` 阶段静默清空 `result.chain`（不发送过时草稿），把**原轮全部消息 + 新补消息（文字 / 图片 / 引用）**合并为单一 `BurstBatch` 重新生成唯一一轮完整回复（默认最多连续抢占 `2` 次防刷屏卡死）。
   - 若首条气泡已经发出或已流式输出，则自动退回**顺承收尾模式**，将已发预览带入下一轮避免重复。
3. **出站：安全拆泡与呼吸节奏**：
   - 超过 `soft_max_bubble_chars`（默认 `44` 字）的长气泡会在顶层句末标点或自然逗号处拆分成 `2 ~ max_bubbles` 个短气泡，自动保护 URL、代码块、引号（`「」""`）与括号（`（）()`）不被撕裂；
   - 自动合并过短碎片（`< min_bubble_chars`）、消除相邻重复气泡，并在发送间隙被打断时立即收口后续气泡。

### 模块结构

| 模块 | 职责 |
| --- | --- |
| `main.py` | 挂载 AstrBot 四个生命周期钩子（入站、`on_llm_request`、`on_decorating_result`、`after_message_sent`）。 |
| `compat.py` | 隔离所有 AstrBot 事件/结果/消息段反射细节，兼容多版本与流式返回。 |
| `coordinator.py` | 纯 `asyncio` 状态机、语义防抖分类器、思考期抢占合并器、安全拆泡器与单轮看门狗。 |
| `api.py` | 面向下游人设/站点插件开放的 `bubble-flow.v1` 扩展接口。 |

### 宿主前置条件

- **多气泡独立发送**：需在 AstrBot 配置中启用 `platform_settings.segmented_reply.enable = true`，否则宿主会把多个 `Plain` 段合并为单条消息发出。
- **流式响应兼容**：开启 `streaming_response = true` 时，插件会在 `STREAMING_RESULT` 期间保持飞行锁，并在 `STREAMING_FINISH` 时安全释放。

### 核心配置（`_conf_schema.json`）

| 分组 | 键名 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| 根配置 | `enabled` | `bool` | `true` | 全局总开关。 |
| `channels` | `enable_private` | `bool` | `true` | 私聊生效。 |
| `channels` | `enable_group` | `bool` | `false` | 群聊生效。 |
| `coalesce` | `idle_quick_seconds` | `float` | `0.4` | 完整问候或带终结标点长句的快通防抖（秒）。 |
| `coalesce` | `idle_debounce_seconds` | `float` | `1.2` | 普通短句默认防抖（秒）；有前置防抖插件时建议覆盖为 `0.2`。 |
| `coalesce` | `idle_continuation_seconds` | `float` | `2.8` | 纯图、引用、引子词或无标点短句的延长等待（秒）。 |
| `coalesce` | `idle_max_wait_seconds` | `float` | `4.0` | 空闲防抖硬上限（秒）。 |
| `coalesce` | `trailing_quiet_seconds` | `float` | `0.9` | 上一轮结束或抢占后等待尾音的静默窗口（秒）。 |
| `coalesce` | `trailing_max_wait_seconds` | `float` | `2.4` | 在途尾音等待硬上限（秒）。 |
| `coalesce` | `max_flight_seconds` | `float` | `90.0` | 单轮飞行锁超时兜底（秒）。 |
| `coalesce` | `coalesce_images` | `bool` | `true` | 合批时按原序保留并合并多模态 `Image` 消息段。 |
| `coalesce` | `preempt_before_send` | `bool` | `true` | 思考未外发前收到新消息时静默抢占并合并重跑。 |
| `coalesce` | `max_preempt_rounds` | `int` | `2` | 单次连发最大连续抢占次数。 |
| `coalesce` | `enable_preempt_in_group` | `bool` | `false` | 群聊是否开启思考期抢占。 |
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

### 扩展接口（`bubble-flow.v1`）

下游人设插件可通过 `BubbleFlowPlugin.extension_api` 注册动态策略、提示词模板与气泡过滤器（支持 `is_preempted_merge` 区分静默抢占合批与发后顺承），并可观测 `turn.preempted`、`burst.debouncing`、`burst.flushed` 等事件及 `get_stats()` 统计指标。
