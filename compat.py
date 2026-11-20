"""AstrBot host adapter: the single place that touches fragile host APIs.

AstrBot versions and platforms differ in which attributes exist on an event,
result, or component. The plugin therefore has to stay defensive, but that
defensiveness should not be interleaved with turn/bubble logic. Everything that
does getattr/callable/try-except lives here; main.py then reads as plain
orchestration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api.message_components import At, Image, Plain, Reply

from .coordinator import (
    BubblePolicy,
    BurstBatch,
    CapturedMessage,
    clean_plain_bubble_text,
)

# Event metadata keys (AstrBot events carry no typed scratch space).
META_TOKEN = "_bubble_flow_token"
META_BATCH = "_bubble_flow_batch"
META_POLICY = "_bubble_flow_policy"
META_LLM_STARTED = "_bubble_flow_llm_started"
META_FOLLOWUP_AFTER_INTERRUPT = "_bubble_flow_followup_after_interrupt"

# AstrBot stores already-sent plain segments under this private key; we use it
# to detect "this reply was already delivered" early-release paths.
_SENT_PLAIN_META = "_send_message_to_user_current_session_plain_texts"


def _call(obj: Any, name: str, *args: Any) -> Any:
    """Call obj.name(*args) when it is callable, hiding host quirks."""
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Event metadata
# --------------------------------------------------------------------------- #
def read_meta(event: Any, key: str, default: Any = None) -> Any:
    """Read plugin metadata, preferring the host extra store over attributes."""
    value = _call(event, "get_extra", key)
    if value is not None:
        return value
    return getattr(event, key, default)


def write_meta(event: Any, key: str, value: Any) -> None:
    """Write plugin metadata via the host store and, if allowed, the attribute."""
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        try:
            setter(key, value)
        except Exception:
            pass
    try:
        setattr(event, key, value)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Event identity / channel
# --------------------------------------------------------------------------- #
def umo(event: Any) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def is_stopped(event: Any) -> bool:
    fn = getattr(event, "is_stopped", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return False


def sender_id(event: Any) -> str:
    value = _call(event, "get_sender_id")
    if value is not None:
        return str(value)
    return str(getattr(event, "sender_id", "") or "")


def message_text(event: Any) -> str:
    value = _call(event, "get_message_str")
    if value is None:
        value = getattr(event, "message_str", "") or ""
    return str(value).strip()


def detect_channel_type(event: Any) -> str:
    """Classify a session as private / group / unknown."""
    private_flag = _call(event, "is_private_chat")
    if private_flag is True:
        return "private"

    message_type = _call(event, "get_message_type")
    label = str(getattr(message_type, "value", message_type) or "").lower()
    if "friend" in label or "private" in label:
        return "private"
    if "group" in label:
        return "group"

    if getattr(event, "group_id", None):
        return "group"

    origin = umo(event)
    if ":GroupMessage:" in origin or ":group:" in origin.lower():
        return "group"
    if private_flag is False:
        return "group"
    return "private" if origin else "unknown"


# --------------------------------------------------------------------------- #
# Message components
# --------------------------------------------------------------------------- #
def _is_component(comp: Any, expected: type, type_name: str) -> bool:
    # Forks/subclasses of the SDK occasionally hand back same-named classes, so
    # fall back to the class name when the isinstance check misses.
    return isinstance(comp, expected) or type(comp).__name__ == type_name


def is_plain(comp: Any) -> bool:
    return _is_component(comp, Plain, "Plain")


def is_image(comp: Any) -> bool:
    return _is_component(comp, Image, "Image")


def is_reply(comp: Any) -> bool:
    return _is_component(comp, Reply, "Reply")


def is_at(comp: Any) -> bool:
    return _is_component(comp, At, "At")


def message_chain(event: Any) -> Sequence[Any]:
    message_obj = getattr(event, "message_obj", None)
    return getattr(message_obj, "message", None) or ()


def reply_snippet(comp: Any, limit: int = 80) -> str:
    raw_text = getattr(comp, "message_str", None)
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()[:limit]
    chain = getattr(comp, "chain", None) or []
    joined = "".join(
        str(getattr(part, "text", "")) for part in chain if hasattr(part, "text")
    ).strip()
    if joined:
        return joined[:limit]
    reply_id = getattr(comp, "id", None)
    return f"reply:{reply_id}" if reply_id else ""


def image_dedup_key(comp: Any) -> tuple[Any, ...]:
    url = getattr(comp, "url", None) or ""
    file_val = getattr(comp, "file", None) or ""
    path_val = getattr(comp, "path", None) or ""
    if url or file_val or path_val:
        return (str(url), str(file_val), str(path_val))
    return ("id", id(comp))


# --------------------------------------------------------------------------- #
# Inbound capture / merged-batch application
# --------------------------------------------------------------------------- #
def capture_message(
    event: Any, policy: BubblePolicy, received_at: float
) -> CapturedMessage:
    """Snapshot one inbound message, keeping ordered text/image/reply payloads."""
    images: list[Any] = []
    extras: list[Any] = []
    reply_preview = ""

    for comp in message_chain(event):
        if is_plain(comp):
            continue
        if is_image(comp):
            (images if policy.coalesce_images else extras).append(comp)
            continue
        if is_reply(comp) and not reply_preview:
            reply_preview = reply_snippet(comp)
        extras.append(comp)

    return CapturedMessage(
        text=message_text(event),
        received_at=received_at,
        sender_id=sender_id(event),
        reply_preview=reply_preview,
        image_components=tuple(images),
        extra_components=tuple(extras),
        raw_event=event,
    )


def has_payload(message: CapturedMessage) -> bool:
    """Whether a captured message carries anything worth starting a turn for."""
    return bool(
        message.text
        or message.image_components
        or any(is_image(comp) for comp in message.extra_components)
    )


def apply_batch(event: Any, batch: BurstBatch, policy: BubblePolicy) -> None:
    """Replace the event's inbound content with the merged burst batch."""
    merged_text = batch.merged_text
    if merged_text:
        try:
            event.message_str = merged_text
        except Exception:
            pass

    message_obj = getattr(event, "message_obj", None)
    chain = getattr(message_obj, "message", None)
    if not isinstance(chain, list):
        return

    seen: set[tuple[Any, ...]] = set()
    kept: list[Any] = []
    for comp in chain:
        if is_plain(comp):
            continue
        if is_image(comp):
            key = image_dedup_key(comp)
            if key in seen:
                continue
            seen.add(key)
        kept.append(comp)

    if policy.coalesce_images:
        for image in batch.merged_images:
            key = image_dedup_key(image)
            if key in seen:
                continue
            seen.add(key)
            kept.append(image)

    new_chain: list[Any] = [Plain(merged_text)] if merged_text else []
    new_chain.extend(kept)
    message_obj.message = new_chain


# --------------------------------------------------------------------------- #
# Outbound chain partitioning
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChainPlan:
    """Outgoing chain split into leading components, text bubbles, and the rest."""

    leading: tuple[Any, ...] = ()
    texts: tuple[str, ...] = ()
    trailing: tuple[Any, ...] = ()


def partition_chain(chain: Sequence[Any], policy: BubblePolicy) -> ChainPlan:
    """Extract bubble texts while preserving leading quotes/At and trailing media."""
    leading: list[Any] = []
    trailing: list[Any] = []
    texts: list[str] = []
    saw_plain = False

    for comp in chain:
        if is_plain(comp):
            saw_plain = True
            cleaned = clean_plain_bubble_text(
                str(getattr(comp, "text", "") or ""),
                strip_markdown=policy.strip_markdown,
                strip_trailing_period=policy.strip_trailing_period,
                split_on_cn_period=policy.split_on_cn_period,
            )
            texts.extend(line.strip() for line in cleaned.splitlines() if line.strip())
        elif not saw_plain and (is_reply(comp) or is_at(comp)):
            leading.append(comp)
        else:
            trailing.append(comp)

    return ChainPlan(tuple(leading), tuple(texts), tuple(trailing))


# --------------------------------------------------------------------------- #
# Image references
# --------------------------------------------------------------------------- #
async def resolve_image_ref(image: Any) -> str | None:
    """Resolve an Image component to something ProviderRequest accepts."""
    path_val = getattr(image, "path", None)
    if isinstance(path_val, str) and path_val.strip():
        return path_val.strip()

    converter = getattr(image, "convert_to_file_path", None)
    if callable(converter):
        try:
            resolved = await converter()
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
        except Exception:
            pass

    for attr in ("url", "file"):
        value = getattr(image, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def image_ref_keys(ref: str) -> tuple[str, ...]:
    """Normalization keys so a relative path and its absolute form dedupe."""
    value = str(ref or "").strip()
    if not value:
        return ()
    keys = [value]
    if not value.startswith(("http://", "https://", "data:", "base64://")):
        try:
            keys.append(str(Path(value).resolve()))
        except Exception:
            pass
    return tuple(keys)


# --------------------------------------------------------------------------- #
# Result inspection
# --------------------------------------------------------------------------- #
def result_of(event: Any) -> Any:
    getter = getattr(event, "get_result", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def content_type_name(result: Any) -> str:
    if result is None:
        return ""
    content_type = getattr(result, "result_content_type", None)
    return str(getattr(content_type, "name", "") or content_type or "")


def is_model_result(result: Any) -> bool | None:
    """True/False when the host can tell, None when it cannot."""
    fn = getattr(result, "is_model_result", None)
    if not callable(fn):
        return None
    try:
        return bool(fn())
    except Exception:
        return None


def already_sent_plain(event: Any, result: Any) -> bool:
    """Detect replies the host already delivered (segmented/streamed flush paths)."""
    sent = _call(event, "get_extra", _SENT_PLAIN_META)
    if not isinstance(sent, list):
        return False
    getter = getattr(result, "get_plain_text", None)
    if not callable(getter):
        return False
    try:
        plain = getter().strip()
    except Exception:
        return False
    return bool(plain and plain in sent)


async def send_typing_indicator(event: Any) -> bool:
    """Best-effort, exception-isolated typing indicator for platforms that support it (e.g. Telegram)."""
    if event is None:
        return False
    try:
        # 1. Telegram adapter exposes event.bot.send_chat_action(chat_id, "typing")
        bot = getattr(event, "bot", None)
        send_action = getattr(bot, "send_chat_action", None)
        if callable(send_action):
            msg_obj = getattr(event, "message_obj", None)
            raw_msg = getattr(msg_obj, "raw_message", None)
            chat_id = (
                getattr(getattr(raw_msg, "chat", None), "id", None)
                or _call(event, "get_group_id")
                or _call(event, "get_sender_id")
            )
            if chat_id:
                await send_action(chat_id=chat_id, action="typing")
                return True
    except Exception:
        return False
    return False
