"""AstrBot Star plugin providing generic turn-flight coalescing and bubble shaping."""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

from .api import (
    PLUGIN_ID,
    BubbleFilterContext,
    BubbleFlowAPI,
    CoalesceFormatContext,
    PolicyResolveContext,
)
from .coordinator import (
    BubblePolicy,
    BurstBatch,
    CapturedMessage,
    SessionFlightCoordinator,
    default_coalesce_prompt,
    plan_outgoing_bubbles,
)

_META_TOKEN = "_bubble_flow_token"
_META_BATCH = "_bubble_flow_batch"
_META_POLICY = "_bubble_flow_policy"
_META_LLM_STARTED = "_bubble_flow_llm_started"


def _set_event_meta(event: Any, key: str, value: Any) -> None:
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


def _get_event_meta(event: Any, key: str, default: Any = None) -> Any:
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            val = getter(key)
            if val is not None:
                return val
        except Exception:
            pass
    return getattr(event, key, default)


def _detect_channel_type(event: Any) -> str:
    is_private = getattr(event, "is_private_chat", None)
    explicit_non_private = False
    if callable(is_private):
        try:
            res = is_private()
            if res is True:
                return "private"
            if res is False:
                explicit_non_private = True
        except Exception:
            pass
    msg_type_getter = getattr(event, "get_message_type", None)
    if callable(msg_type_getter):
        try:
            msg_type = msg_type_getter()
            val = getattr(msg_type, "value", str(msg_type)).lower()
            if "friend" in val or "private" in val:
                return "private"
            if "group" in val:
                return "group"
        except Exception:
            pass
    if getattr(event, "group_id", None):
        return "group"
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    if ":GroupMessage:" in umo or ":group:" in umo.lower():
        return "group"
    if explicit_non_private:
        return "group"
    if umo:
        return "private"
    return "unknown"


def _safe_sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            val = getter()
            if val is not None:
                return str(val)
        except Exception:
            pass
    return str(getattr(event, "sender_id", "") or "")


def _safe_message_str(event: Any) -> str:
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            val = getter()
            if val is not None:
                return str(val).strip()
        except Exception:
            pass
    return str(getattr(event, "message_str", "") or "").strip()


def _is_image_component(comp: Any) -> bool:
    return isinstance(comp, Image) or type(comp).__name__ == "Image"


def _is_reply_component(comp: Any) -> bool:
    return isinstance(comp, Reply) or type(comp).__name__ == "Reply"


def _is_plain_component(comp: Any) -> bool:
    return isinstance(comp, Plain) or type(comp).__name__ == "Plain"


def _reply_snippet(comp: Any, limit: int = 80) -> str:
    raw = getattr(comp, "message_str", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:limit]
    chain = getattr(comp, "chain", None) or []
    parts = [str(getattr(c, "text", "")) for c in chain if hasattr(c, "text")]
    joined = "".join(parts).strip()
    if joined:
        return joined[:limit]
    rid = getattr(comp, "id", None)
    return f"reply:{rid}" if rid else ""


def _image_dedup_key(comp: Any) -> tuple[Any, ...]:
    url = getattr(comp, "url", None) or ""
    file_val = getattr(comp, "file", None) or ""
    path_val = getattr(comp, "path", None) or ""
    if url or file_val or path_val:
        return (str(url), str(file_val), str(path_val))
    return ("id", id(comp))


def _extract_captured_message(
    event: Any,
    policy: BubblePolicy,
    received_at: float,
) -> CapturedMessage:
    text = _safe_message_str(event)
    sender_id = _safe_sender_id(event)
    images: list[Any] = []
    extras: list[Any] = []
    reply_preview = ""

    msg_obj = getattr(event, "message_obj", None)
    components = getattr(msg_obj, "message", None) or ()
    for comp in components:
        if _is_plain_component(comp):
            continue
        if _is_image_component(comp):
            if policy.coalesce_images:
                images.append(comp)
            else:
                extras.append(comp)
            continue
        if _is_reply_component(comp):
            if not reply_preview:
                reply_preview = _reply_snippet(comp)
            extras.append(comp)
            continue
        extras.append(comp)

    return CapturedMessage(
        text=text,
        received_at=received_at,
        sender_id=sender_id,
        reply_preview=reply_preview,
        image_components=tuple(images),
        extra_components=tuple(extras),
        raw_event=event,
    )


def _apply_batch_to_event(event: Any, batch: BurstBatch, policy: BubblePolicy) -> None:
    merged_text = batch.merged_text
    if merged_text:
        try:
            event.message_str = merged_text
        except Exception:
            pass

    msg_obj = getattr(event, "message_obj", None)
    chain = getattr(msg_obj, "message", None)
    if not isinstance(chain, list):
        return

    new_chain: list[Any] = []
    if merged_text:
        new_chain.append(Plain(merged_text))

    seen_images: set[tuple[Any, ...]] = set()
    kept_non_plain: list[Any] = []
    for comp in chain:
        if _is_plain_component(comp):
            continue
        if _is_image_component(comp):
            key = _image_dedup_key(comp)
            if key not in seen_images:
                seen_images.add(key)
                kept_non_plain.append(comp)
        else:
            kept_non_plain.append(comp)

    if policy.coalesce_images:
        for img in batch.merged_images:
            key = _image_dedup_key(img)
            if key not in seen_images:
                seen_images.add(key)
                kept_non_plain.append(img)

    new_chain.extend(kept_non_plain)
    msg_obj.message = new_chain


async def _resolve_image_ref(img: Any) -> str | None:
    path_val = getattr(img, "path", None)
    if isinstance(path_val, str) and path_val.strip():
        return path_val.strip()
    converter = getattr(img, "convert_to_file_path", None)
    if callable(converter):
        try:
            resolved = await converter()
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
        except Exception:
            pass
    for attr in ("url", "file"):
        val = getattr(img, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


@register(
    PLUGIN_ID,
    "Nanagokyuu",
    "Generic turn-flight burst coalescing & multi-bubble shaping plugin",
    "0.1.0",
)
class BubbleFlowPlugin(Star):
    """AstrBot plugin managing burst coalescing and multi-bubble pacing."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config: dict[str, Any] = dict(config or {})
        self.coordinator = SessionFlightCoordinator()
        self.extension_api = BubbleFlowAPI(
            self.coordinator,
            lambda: BubblePolicy.from_mapping(self.config),
        )
        self._watchdogs: dict[tuple[str, str], asyncio.Task[Any]] = {}

    def _resolve_event_policy(self, event: Any, umo: str) -> tuple[BubblePolicy, str]:
        channel_type = _detect_channel_type(event)
        sender_id = _safe_sender_id(event)
        policy = self.extension_api.resolve_policy(
            PolicyResolveContext(
                umo=umo,
                event=event,
                base_policy=BubblePolicy.from_mapping(self.config),
                channel_type=channel_type,
                sender_id=sender_id,
            )
        )
        return policy, channel_type

    @staticmethod
    def _is_channel_active(policy: BubblePolicy, channel_type: str) -> bool:
        if not policy.enabled:
            return False
        if channel_type == "private":
            return policy.enable_private
        if channel_type == "group":
            return policy.enable_group
        return policy.enable_private or policy.enable_group

    def _start_watchdog(self, umo: str, token: str, timeout_seconds: float) -> None:
        if not umo or not token or timeout_seconds <= 0:
            return
        self._cancel_watchdog(umo, token)
        key = (umo, token)

        async def _watch() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
                released = await self.coordinator.release_flight(umo, token)
                if released:
                    logger.warning(
                        "[BubbleFlow] Watchdog released stalled flight for umo=%s token=%s after %.1fs",
                        umo,
                        token,
                        timeout_seconds,
                    )
                    await self.extension_api.emit_event(
                        "turn.watchdog_released",
                        {"umo": umo, "token": token, "timeout_seconds": timeout_seconds},
                    )
            except asyncio.CancelledError:
                return
            finally:
                if self._watchdogs.get(key) is asyncio.current_task():
                    self._watchdogs.pop(key, None)

        self._watchdogs[key] = asyncio.create_task(_watch())

    def _cancel_watchdog(self, umo: str, token: str = "") -> None:
        if token:
            task = self._watchdogs.pop((umo, token), None)
            if task and not task.done():
                task.cancel()
            return
        for key in [k for k in self._watchdogs if k[0] == umo]:
            task = self._watchdogs.pop(key, None)
            if task and not task.done():
                task.cancel()

    async def _release_turn_slot(
        self, umo: str, token: str, *, empty_result: bool = False
    ) -> None:
        if not umo:
            return
        self._cancel_watchdog(umo, token)
        released = await self.coordinator.release_flight(umo, token)
        if released:
            await self.extension_api.emit_event(
                "turn.finished",
                {"umo": umo, "token": token, "empty_result": empty_result},
            )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE,
        priority=-250000,
    )
    async def handle_interleave_intake(self, event: AstrMessageEvent) -> None:
        """Admit an idle message immediately, debounce, or coalesce mid-flight bursts."""
        is_stopped = getattr(event, "is_stopped", None)
        if callable(is_stopped) and is_stopped():
            return
        if bool(getattr(event, "call_llm", False)) or bool(getattr(event, "_has_send_oper", False)):
            return

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        if self.extension_api.consume_suppressed(event):
            return

        policy, channel_type = self._resolve_event_policy(event, umo)
        if not self._is_channel_active(policy, channel_type):
            return
        if channel_type == "group" and getattr(event, "is_at_or_wake_command", None) is False:
            return

        msg = _extract_captured_message(
            event, policy, received_at=self.coordinator._clock()
        )
        if not msg.text and not msg.image_components and not any(_is_image_component(c) for c in msg.extra_components):
            return
        adm = await self.coordinator.admit_or_coalesce(umo, msg, policy=policy)
        effective_mode = adm.mode
        effective_token = adm.token
        batch: BurstBatch | None = None

        if effective_mode == "bypass":
            return

        if effective_mode == "swallowed":
            event.stop_event()
            await self.extension_api.emit_event(
                "message.swallowed",
                {"umo": umo, "token": effective_token, "text": msg.text},
            )
            return

        if effective_mode == "wait_idle":
            await self.extension_api.emit_event(
                "burst.debouncing", {"umo": umo, "token": effective_token}
            )
            promoted_token, batch = await self.coordinator.wait_idle_and_promote(
                umo, effective_token, policy=policy
            )
            if not promoted_token:
                event.stop_event()
                return
            effective_token = promoted_token

        elif effective_mode == "wait_flush":
            await self.extension_api.emit_event(
                "turn.interrupted",
                {"umo": umo, "token": effective_token, "text": msg.text},
            )
            promoted_token, batch = await self.coordinator.wait_and_collect_flush(
                umo, effective_token, policy=policy
            )
            if not promoted_token:
                event.stop_event()
                return
            effective_token = promoted_token

        _set_event_meta(event, _META_TOKEN, effective_token)
        _set_event_meta(event, _META_POLICY, policy)
        if batch is not None and batch.count > 0:
            _apply_batch_to_event(event, batch, policy)
            _set_event_meta(event, _META_BATCH, batch)
            if batch.count > 1:
                logger.info(
                    "[BubbleFlow] Coalesced %d message(s) for %s (images=%d)",
                    batch.count,
                    umo,
                    len(batch.merged_images),
                )
            await self.extension_api.emit_event(
                "burst.flushed",
                {
                    "umo": umo,
                    "token": effective_token,
                    "count": batch.count,
                    "merged_text": batch.merged_text,
                    "image_count": len(batch.merged_images),
                },
            )

        self._start_watchdog(umo, effective_token, policy.max_flight_seconds)
        await self.extension_api.emit_event(
            "turn.started",
            {
                "umo": umo,
                "token": effective_token,
                "channel_type": channel_type,
                "coalesced_count": batch.count if batch is not None else 1,
            },
        )

    @filter.on_llm_request(priority=100)
    async def inject_coalesce_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Mark LLM turn active and inject merged burst context + images if coalesced."""
        umo = getattr(event, "unified_msg_origin", "") or ""
        token = _get_event_meta(event, _META_TOKEN, "")
        if not umo or not token:
            return

        _set_event_meta(event, _META_LLM_STARTED, True)
        await self.coordinator.mark_llm_started(umo, token)

        policy: BubblePolicy = _get_event_meta(event, _META_POLICY) or (
            self.extension_api.get_session_policy(umo, event)
        )
        batch: BurstBatch | None = _get_event_meta(event, _META_BATCH)
        if batch is None:
            return

        if not (req.prompt or "").strip() and batch.merged_text:
            req.prompt = batch.merged_text

        if policy.coalesce_images and batch.merged_images:
            if req.image_urls is None:
                req.image_urls = []
            existing = set(req.image_urls)
            for img in batch.merged_images:
                ref = await _resolve_image_ref(img)
                if ref and ref not in existing:
                    req.image_urls.append(ref)
                    existing.add(ref)

        if policy.inject_coalesce_hint and (
            batch.count > 1 or batch.last_reply_preview or len(batch.merged_images) > 1
        ):
            hint = self.extension_api.format_coalesce_hint(
                batch,
                CoalesceFormatContext(
                    umo=umo,
                    event=event,
                    policy=policy,
                    batch=batch,
                ),
            )
            if not hint:
                hint = default_coalesce_prompt(batch)
            if hint:
                if req.extra_user_content_parts is None:
                    req.extra_user_content_parts = []
                req.extra_user_content_parts.append(TextPart(text=hint))

    @filter.on_decorating_result(priority=-2000000)
    async def shape_outgoing_bubbles(self, event: AstrMessageEvent) -> None:
        """Plan, filter, and optionally collapse outgoing text bubbles."""
        umo = getattr(event, "unified_msg_origin", "") or ""
        token = _get_event_meta(event, _META_TOKEN, "")
        if not umo or not token:
            return

        result = getattr(event, "get_result", lambda: None)()
        chain = getattr(result, "chain", None) if result is not None else None
        if result is None or not chain:
            await self._release_turn_slot(umo, token, empty_result=True)
            return

        is_model_fn = getattr(result, "is_model_result", None)
        llm_started = bool(_get_event_meta(event, _META_LLM_STARTED, False))
        if callable(is_model_fn) and not is_model_fn() and not llm_started:
            await self._release_turn_slot(umo, token, empty_result=False)
            return

        policy: BubblePolicy = _get_event_meta(event, _META_POLICY) or (
            self.extension_api.get_session_policy(umo, event)
        )
        if not policy.enabled:
            await self._release_turn_slot(umo, token, empty_result=False)
            return

        leading_comps: list[Any] = []
        trailing_comps: list[Any] = []
        raw_texts: list[str] = []
        saw_plain = False

        for comp in chain:
            if _is_plain_component(comp):
                saw_plain = True
                text_val = str(getattr(comp, "text", "") or "")
                lines = [ln.strip() for ln in text_val.splitlines() if ln.strip()]
                if lines:
                    raw_texts.extend(lines)
            elif not saw_plain and (
                isinstance(comp, (Reply, At))
                or type(comp).__name__ in ("Reply", "At")
            ):
                leading_comps.append(comp)
            else:
                trailing_comps.append(comp)

        if not raw_texts:
            await self._release_turn_slot(umo, token, empty_result=False)
            return

        interrupted = self.coordinator.has_pending_interrupt(umo)
        planned = plan_outgoing_bubbles(raw_texts, policy=policy, interrupted=interrupted)
        planned = self.extension_api.apply_bubble_filters(
            planned,
            BubbleFilterContext(
                umo=umo,
                event=event,
                policy=policy,
                interrupted=interrupted,
                raw_bubbles=tuple(raw_texts),
            ),
        )
        if not planned:
            planned = [raw_texts[0]]

        collapsed = interrupted and policy.collapse_on_interrupt and len(raw_texts) > len(planned)
        new_chain: list[Any] = [*leading_comps]
        new_chain.extend(Plain(text) for text in planned)
        new_chain.extend(trailing_comps)
        result.chain = new_chain

        preview = " / ".join(planned)
        await self.coordinator.record_reply_preview(
            umo, token, preview, collapsed=collapsed
        )
        if collapsed:
            logger.info(
                "[BubbleFlow] Collapsed %d->%d bubble(s) on mid-flight interrupt for %s",
                len(raw_texts),
                len(planned),
                umo,
            )
            await self.extension_api.emit_event(
                "bubbles.collapsed",
                {
                    "umo": umo,
                    "token": token,
                    "before": len(raw_texts),
                    "after": len(planned),
                    "preview": preview[:120],
                },
            )

        sent_plain = getattr(event, "get_extra", lambda *_: None)(
            "_send_message_to_user_current_session_plain_texts", ()
        )
        result_plain = (
            result.get_plain_text().strip()
            if callable(getattr(result, "get_plain_text", None))
            else ""
        )
        if result_plain and isinstance(sent_plain, list) and result_plain in sent_plain:
            await self._release_turn_slot(umo, token, empty_result=False)

    @filter.after_message_sent(priority=-100)
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Release the active turn slot after all segmented bubbles are sent."""
        umo = getattr(event, "unified_msg_origin", "") or ""
        token = _get_event_meta(event, _META_TOKEN, "")
        await self._release_turn_slot(umo, token, empty_result=False)

    async def terminate(self) -> None:
        """Cancel background watchdogs and release coordinator resources."""
        for task in list(self._watchdogs.values()):
            if not task.done():
                task.cancel()
        self._watchdogs.clear()
        await self.coordinator.close()
        self.extension_api.clear_registrations()
