# astrbot_plugin_bubble_flow

A standalone, zero-dependency AstrBot Star plugin for **turn-flight burst coalescing** and **multi-bubble pacing**.

When a user sends several consecutive messages (text, images, or reply quotes) while the previous turn is still generating or sending segmented bubbles, `BubbleFlow` holds the first interrupter until the active turn finishes sending, swallows duplicate burst arrivals into a single ordered batch, injects a lightweight multimodal + context hint into the next LLM turn, and shapes/collapses outgoing bubbles to prevent chaotic interleaving.

## Features

- **Zero External Dependencies**: Works standalone in any AstrBot instance; requires no third-party companion or persona plugin.
- **Turn-Flight Interleave Prevention**:
  - Hooks `@filter.event_message_type` (`priority=-250000`) to ensure at most one active generation + delivery pipeline per `umo`.
  - Optional standalone idle debounce (`coalesce.idle_debounce_seconds`) or co-existence with upstream debouncers (`0.0` default).
  - Swallows mid-flight duplicates cleanly and flushes them as one ordered `BurstBatch` (`text + Image + Reply`) after `after_message_sent` (or `STREAMING_FINISH` in streaming mode).
  - Watchdog timeout (`max_flight_seconds`) + manual `force_release(umo)` prevent deadlocks if a turn aborts early.
- **Host Prerequisites & Delivery Modes**:
  - **Multi-bubble delivery**: AstrBot sends multiple `Plain` segments as separate chat bubbles only when `platform_settings.segmented_reply.enable = true` (default is `false` in AstrBot). When enabled, AstrBot's segmented reply regex also splits individual segments by punctuation, so `max_bubbles` controls the planned segment count rather than a hard platform packet cap.
  - **Streaming responses (`streaming_response = true`)**: Streaming chunks bypass `after_message_sent` in AstrBot; `bubble_flow` preserves the flight lock during `STREAMING_RESULT` and releases it on `STREAMING_FINISH` so follow-up turns do not stall until the watchdog timeout.
- **Multi-Bubble Shaping**:
  - Caps maximum bubbles per turn (`bubbles.max_bubbles`).
  - Merges micro-fragments shorter than `bubbles.min_bubble_chars`.
  - Deduplicates consecutive identical bubbles (`bubbles.dedup_identical_bubbles`).
  - Collapses remaining output into a single bubble (keeping only the first reaction bubble by default via `bubbles.interrupt_keep_first_only`) when a mid-flight interrupt is already waiting (`bubbles.collapse_on_interrupt`).
- **Reserved Extension API (`bubble-flow.v1`)**:
  - Allows downstream plugins to dynamically tune per-session policies, format coalesce prompts, post-process outgoing bubbles, bypass intake for special triggers, and observe lifecycle events with full callback exception isolation.

## Architecture

| Module | Responsibility |
| --- | --- |
| `main.py` | Wires the four AstrBot hooks; contains no host reflection. |
| `compat.py` | The only place touching AstrBot event/result/component APIs; tolerant of version and platform differences. |
| `coordinator.py` | Pure `asyncio` per-session state machine, bubble planner, and the flight watchdog. |
| `api.py` | Versioned `bubble-flow.v1` extension API. |

**Watchdog semantics**: `max_flight_seconds` is captured when a turn is admitted and stays fixed for that turn. A later policy change (for example a `policy_resolver` returning a shorter timeout for the next message) can never shorten or release a turn that is already running. The turn's own timer is the single authority on watchdog expiry, it is cancelled as soon as the turn releases its flight lock, and expiry is reported once per turn through the `turn.watchdog_released` event.

## Configuration (`_conf_schema.json`)

| Group | Key | Type | Default | Description |
| --- | --- | --- | --- | --- |
| root | `enabled` | `bool` | `true` | Master switch for intake coalescing and bubble shaping. |
| `channels` | `enable_private` | `bool` | `true` | Enable for private/friend chats. |
| `channels` | `enable_group` | `bool` | `false` | Enable for group chats. |
| `coalesce` | `idle_debounce_seconds` | `float` | `0.0` | Idle first-message debounce (`0.0` delegates initial debounce upstream; `1.0`–`1.5` recommended standalone). |
| `coalesce` | `trailing_quiet_seconds` | `float` | `0.6` | Quiet window after the previous turn finishes before flushing coalesced interrupters. |
| `coalesce` | `trailing_max_wait_seconds` | `float` | `1.8` | Hard cap waiting during continuous bursts after the previous turn finishes. |
| `coalesce` | `max_flight_seconds` | `float` | `45.0` | Watchdog timeout to auto-release stalled flight slots. |
| `coalesce` | `coalesce_images` | `bool` | `true` | Preserve and merge mid-flight `Image` components into the next multimodal request. |
| `bubbles` | `max_bubbles` | `int` | `2` | Maximum outgoing text bubbles retained per turn. |
| `bubbles` | `collapse_on_interrupt` | `bool` | `true` | Collapse outgoing bubbles into 1 bubble if new user messages arrived mid-flight. |
| `bubbles` | `interrupt_keep_first_only` | `bool` | `true` | When collapsed on interrupt, keep only the first bubble and drop trailing follow-up sentences. |
| `bubbles` | `min_bubble_chars` | `int` | `4` | Merge tiny fragments shorter than this length into an adjacent bubble. |
| `bubbles` | `dedup_identical_bubbles` | `bool` | `true` | Remove consecutive duplicate text bubbles. |
| `bubbles` | `bubble_sep` | `string` | `"\n"` | Separator used when merging bubbles together. |
| `bubbles` | `strip_markdown` | `bool` | `false` | Strip common Markdown formatting (`**bold**`, inline code, headings, bullets) for plain-text platforms. |
| `bubbles` | `strip_trailing_period` | `bool` | `false` | Strip stiff trailing Chinese periods (`。`) at bubble ends while preserving URLs. |
| `bubbles` | `split_on_cn_period` | `bool` | `false` | Split internal Chinese periods (`。`) into separate bubbles while masking URLs. |
| `prompts` | `inject_coalesce_hint` | `bool` | `true` | Append a lightweight hint via `req.extra_user_content_parts` when a batch merges multiple items. |

## Extension API (`bubble-flow.v1`)

Downstream AstrBot plugins can discover and bind to `BubbleFlowPlugin.extension_api` without tight coupling.

### Discovery

```python
from astrbot_plugin_bubble_flow.api import discover_bubble_flow_api

api = discover_bubble_flow_api(self.context)
```

Or without importing this package directly:

```python
for star in self.context.get_all_stars():
    if star.name in ("astrbot_plugin_bubble_flow", "bubble_flow") and star.activated:
        api = getattr(star.star_cls, "extension_api", None)
        if getattr(api, "api_version", None) == "bubble-flow.v1":
            break
```

### API Methods

- **Callbacks (all support `unregister(token)` and run with exception isolation)**:
  - `register_policy_resolver(resolver_id, fn, *, priority=100) -> str`
    - `fn(PolicyResolveContext)` returns a `dict` of `BubblePolicy` overrides, a `BubblePolicy` instance, or `None`.
  - `register_coalesce_formatter(formatter_id, fn, *, priority=100) -> str`
    - `fn(BurstBatch, CoalesceFormatContext)` returns a custom hint string (`str`) or `None`.
  - `register_bubble_filter(filter_id, fn, *, priority=100) -> str`
    - `fn(bubbles: list[str], BubbleFilterContext)` returns the transformed `list[str]`.
  - `register_event_listener(listener_id, fn) -> str`
    - `fn(event_name: str, payload: dict[str, Any])` (sync or async) observes lifecycle events.
- **Session Control & Introspection**:
  - `set_session_policy(umo, **overrides)` / `clear_session_policy(umo)`
  - `get_session_policy(umo, event=None, *, channel_type="", sender_id="") -> BubblePolicy`
  - `suppress_next_coalesce(umo_or_event)` — bypass coalescing once (e.g., for proactive/scheduled triggers).
  - `get_session_state(umo) -> dict[str, Any]`
  - `has_pending_interrupt(umo) -> bool`
  - `get_stats() -> dict[str, int]`
  - `await force_release(umo) -> bool`

### Lifecycle Events

| Event | Payload Fields |
| --- | --- |
| `burst.debouncing` | `umo`, `token` |
| `message.swallowed` | `umo`, `token`, `text` |
| `turn.interrupted` | `umo`, `token`, `text` |
| `burst.flushed` | `umo`, `token`, `count`, `merged_text`, `image_count` |
| `turn.started` | `umo`, `token`, `channel_type`, `coalesced_count` |
| `bubbles.collapsed` | `umo`, `token`, `before`, `after`, `preview` |
| `turn.finished` | `umo`, `token`, `empty_result` |
| `turn.watchdog_released` | `umo`, `token`, `timeout_seconds` |

### Integration Example

```python
class MyBridgePlugin(Star):
    async def initialize(self) -> None:
        self._bubble_tokens = []
        self._bubble_api = None
        for star in self.context.get_all_stars():
            if star.name == "astrbot_plugin_bubble_flow" and star.activated:
                api = getattr(star.star_cls, "extension_api", None)
                if getattr(api, "api_version", None) == "bubble-flow.v1":
                    self._bubble_api = api
                    break
        if self._bubble_api is not None:
            self._bubble_tokens.append(
                self._bubble_api.register_policy_resolver(
                    "my.policy",
                    lambda ctx: {"max_bubbles": 3} if "vip" in ctx.sender_id else None,
                )
            )
            self._bubble_tokens.append(
                self._bubble_api.register_bubble_filter(
                    "my.punct",
                    lambda bubbles, ctx: [b.rstrip("。") for b in bubbles],
                )
            )

    async def terminate(self) -> None:
        if self._bubble_api is not None:
            for tok in self._bubble_tokens:
                self._bubble_api.unregister(tok)
```

## Installation

Install directly from GitHub in AstrBot WebUI (**Plugins -> Install from URL**), or clone into your AstrBot `data/plugins/` directory:

```bash
cd <astrbot_root>/data/plugins
git clone https://github.com/Nanagokyuu/astrbot_plugin_bubble_flow.git
```

Then enable `astrbot_plugin_bubble_flow` and restart AstrBot (or reload plugins).

## Testing

Run the standalone unit tests with AstrBot's Python environment:

```bash
pytest tests/test_bubble_flow.py -v
```

## License

MIT License. See [LICENSE](LICENSE).
