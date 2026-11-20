# astrbot_plugin_bubble_flow

[English](README.md) | [简体中文](README_zh.md)

A standalone, zero-dependency AstrBot Star plugin (`v0.3.0`) for **adaptive burst coalescing**, **pre-send LLM preemption**, **typing indicator keepalive**, and **natural multi-bubble pacing**.

---

### Why v0.3? (Solving Real-World Chat Pain Points)

In real IM conversations (QQ, Telegram, WeChat, Discord, etc.), humans rarely wait politely for a single turn:
- **Rapid or slow-paced bursts (`1.5s ~ 7s`)**: People split one thought across 2–4 short messages (*"Guess what happened"* → *"This morning at 7:30"* → *"They cancelled it"*). A `0s` debounce interrupts immediately on message #1; a fixed `6s` debounce makes single greetings (*"Morning"*) feel sluggish; and different platforms/users type at different speeds.
- **Mid-thought follow-ups**: While the LLM is still thinking (`8s ~ 19s`), the user adds context or corrects themselves. Without preemption, the bot still sends an awkward half-baked reply first and then a redundant follow-up.
- **Radio silence while thinking**: Long LLM inference makes users think the bot missed their message, triggering extra "hello?" follow-ups.
- **Wall-of-text merging**: Joining multi-line outputs with spaces turns natural short chat bubbles into stiff paragraphs.

`BubbleFlow v0.3` solves all four with an adaptive **turn pacer**:

1. **Semantic + Adaptive Idle Debounce (`adaptive_cadence`)**:
   - **Quick-pass (`0.4s`)** for clearly complete greetings (*"Morning"*, *"?"*) or punctuated long messages.
   - **Normal (`1.2s`)** for everyday short lines.
   - **Continuation (`2.8s`, capped at `4.0s`)** when detecting pure images, reply quotes, lead-ins (*"Wait"*, *"Guess what"*), trailing conjunctions (*"then"*, *"so"*, *"because"*), unclosed brackets/quotes, or unpunctuated short fragments.
   - **EMA Burst Cadence**: Learns each session's real burst interval (`ema_burst_gap`) and elastically widens `continuation` for slower multi-part typers without slowing down fast single-turn users.
2. **Pre-Send Preemption + Grace Buffer (`preempt_before_send`, `preempt_grace_seconds`)**:
   - If new user messages arrive **while the LLM is still generating** (before any bubble is sent), `BubbleFlow` silently clears `result.chain` at `on_decorating_result`, waits a brief `preempt_grace_seconds` (default `0.8s`) to catch a trailing 3rd message, merges the original turn + follow-ups into one `BurstBatch`, and re-runs a single unified reply (`max_preempt_rounds = 2` cap prevents infinite loops).
   - If the first bubble was already sent (or upstream streamed/sent directly), it gracefully falls back to **follow-up continuation mode** with `last_reply_preview` context.
3. **Typing Indicator Keepalive (`send_typing_action`)**:
   - Periodically refreshes the platform's native `"typing"` chat action (e.g. Telegram) every `4s` during LLM thinking, with guaranteed `try/finally` cancellation on finish, preemption, or error (safe no-op on platforms without typing support).
4. **Safe Multi-Bubble Pacing**:
   - Splits overly long bubbles (`soft_max_bubble_chars`, default `44`) at top-level punctuation/clause boundaries while protecting URLs, code blocks, quotes (`「...」`, `"..."`), and parentheses (`(...)`, `（...）`).
   - Merges tiny micro-fragments (`< min_bubble_chars`), deduplicates identical bubbles, and collapses remaining bubbles on mid-send interrupts.

---

### Architecture

| Module | Responsibility |
| --- | --- |
| `main.py` | Wires the four AstrBot hooks (`intake`, `on_llm_request`, `on_decorating_result`, `after_message_sent`) and typing keepalive tasks. |
| `compat.py` | Isolates all AstrBot event/result/component reflection, typing action calls, and streaming quirks. |
| `coordinator.py` | Pure `asyncio` session state machine, semantic + EMA cadence classifier, pre-send preemption with grace window, safe bubble splitter, and per-turn watchdog. |
| `api.py` | Versioned `bubble-flow.v1` extension API for downstream persona/bridge plugins. |

---

### Host Prerequisites & Visual Configuration

- **Visual WebUI Form**: All settings in `_conf_schema.json` are automatically rendered as a visual configuration page inside **AstrBot Dashboard → Plugins → BubbleFlow → Config** (no manual JSON editing required).
- **Multi-bubble delivery**: AstrBot sends multiple `Plain` segments as separate chat bubbles when `platform_settings.segmented_reply.enable = true`.
- **Streaming mode (`streaming_response = true`)**: Streaming chunks bypass `after_message_sent`; `BubbleFlow` holds the flight lock during `STREAMING_RESULT` and releases on `STREAMING_FINISH`.

---

### Configuration (`_conf_schema.json`)

| Group | Key | Type | Default | Description |
| --- | --- | --- | --- | --- |
| root | `enabled` | `bool` | `true` | Master switch for coalescing and bubble shaping. |
| `channels` | `enable_private` | `bool` | `true` | Enable in private/friend chats. |
| `channels` | `enable_group` | `bool` | `false` | Enable in group chats. |
| `coalesce` | `idle_quick_seconds` | `float` | `0.4` | Fast-path debounce for complete greetings / punctuated long messages. |
| `coalesce` | `idle_debounce_seconds` | `float` | `1.2` | Default idle debounce for normal short messages (`0.2` recommended if an upstream debouncer already runs). |
| `coalesce` | `idle_continuation_seconds` | `float` | `2.8` | Extended wait for pure images, reply quotes, lead-ins, unclosed quotes, or fragments. |
| `coalesce` | `idle_max_wait_seconds` | `float` | `4.0` | Hard cap on idle debounce waiting. |
| `coalesce` | `trailing_quiet_seconds` | `float` | `0.9` | Quiet window after a turn finishes before flushing queued follow-ups. |
| `coalesce` | `trailing_max_wait_seconds` | `float` | `2.4` | Hard cap on trailing wait during continuous bursts. |
| `coalesce` | `max_flight_seconds` | `float` | `90.0` | Per-turn watchdog timeout to auto-release stalled flights. |
| `coalesce` | `coalesce_images` | `bool` | `true` | Merge mid-flight `Image` components in chronological order. |
| `coalesce` | `preempt_before_send` | `bool` | `true` | Silently discard an unpublished LLM draft and re-run merged when new input arrives mid-thought. |
| `coalesce` | `max_preempt_rounds` | `int` | `2` | Maximum consecutive silent preemptions per burst chain. |
| `coalesce` | `enable_preempt_in_group` | `bool` | `false` | Allow pre-send preemption in group chats. |
| `coalesce` | `preempt_grace_seconds` | `float` | `0.8` | Brief grace buffer after silent preemption to catch a trailing 3rd message before re-running LLM. |
| `coalesce` | `adaptive_cadence` | `bool` | `true` | Adapt continuation debounce to each session's observed burst interval (EMA). |
| `coalesce` | `send_typing_action` | `bool` | `true` | Refresh native `"typing"` indicator every 4s during LLM thinking on supported platforms. |
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

---

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
  - `will_preempt(event)` (non-mutating check of pending preemption)
  - `get_session_state(umo)` / `get_stats()` (`total_runs`, `total_coalesced`, `total_preempted`, `total_fast_passed`, `total_continuation_waits`, `total_collapsed`, `total_watchdog_resets`)
  - `await force_release(umo)`
- **Lifecycle Events**: `burst.debouncing`, `message.swallowed`, `turn.interrupted`, `turn.preempted`, `burst.flushed`, `turn.started`, `bubbles.collapsed`, `turn.finished`, `turn.watchdog_released`.
