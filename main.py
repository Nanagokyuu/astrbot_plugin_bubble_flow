"""AstrBot Star plugin: turn-flight coalescing and multi-bubble pacing.

All state lives in coordinator.SessionFlightCoordinator; all host quirks live in
compat. This module only wires four AstrBot hooks to them:

    intake      -> admit / swallow / wait, then publish the merged batch
    llm_request -> mark the turn active, merge images, inject the batch hint
    decorating  -> shape outgoing bubbles, release the lock on terminal states
    after_sent  -> release the lock once every bubble has been delivered
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

from . import compat
from .api import (
    BubbleFilterContext,
    BubbleFlowAPI,
    CoalesceFormatContext,
    PolicyResolveContext,
)
from .coordinator import (
    BubblePolicy,
    BurstBatch,
    SessionFlightCoordinator,
    default_coalesce_prompt,
    plan_outgoing_bubbles,
)


class BubbleFlowPlugin(Star):
    """AstrBot plugin managing burst coalescing and multi-bubble pacing."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config: dict[str, Any] = dict(config or {})
        self.coordinator = SessionFlightCoordinator(
            on_flight_expired=self._on_flight_expired
        )
        self.extension_api = BubbleFlowAPI(
            self.coordinator,
            lambda: BubblePolicy.from_mapping(self.config),
        )

    # ------------------------------------------------------------------ #
    # Turn lifecycle plumbing
    # ------------------------------------------------------------------ #
    async def _emit(self, event_name: str, **payload: Any) -> None:
        await self.extension_api.emit_event(event_name, payload)

    def _resolve_event_policy(self, event: Any, umo: str) -> tuple[BubblePolicy, str]:
        channel_type = compat.detect_channel_type(event)
        policy = self.extension_api.resolve_policy(
            PolicyResolveContext(
                umo=umo,
                event=event,
                base_policy=BubblePolicy.from_mapping(self.config),
                channel_type=channel_type,
                sender_id=compat.sender_id(event),
            )
        )
        return policy, channel_type

    def _event_policy(self, event: Any, umo: str) -> BubblePolicy:
        return compat.read_meta(event, compat.META_POLICY) or (
            self.extension_api.get_session_policy(umo, event)
        )

    @staticmethod
    def _is_channel_active(policy: BubblePolicy, channel_type: str) -> bool:
        if not policy.enabled:
            return False
        if channel_type == "private":
            return policy.enable_private
        if channel_type == "group":
            return policy.enable_group
        return policy.enable_private or policy.enable_group

    async def _on_flight_expired(
        self, umo: str, token: str, timeout_seconds: float
    ) -> None:
        # The coordinator already released the flight; this is the observable side.
        logger.warning(
            "[BubbleFlow] Watchdog released stalled flight for umo=%s token=%s after %.1fs",
            umo,
            token,
            timeout_seconds,
        )
        await self._emit(
            "turn.watchdog_released",
            umo=umo,
            token=token,
            timeout_seconds=timeout_seconds,
        )

    async def _release_turn_slot(
        self, umo: str, token: str, *, empty_result: bool = False
    ) -> None:
        # Only the turn holding a flight token may release it. Tokenless events
        # (upstream call_llm/send_oper handlers, suppressed triggers, early-skipped
        # intake) must never cancel another turn's flight lock.
        if not umo or not token:
            return
        if await self.coordinator.release_flight(umo, token):
            await self._emit("turn.finished", umo=umo, token=token, empty_result=empty_result)

    # ------------------------------------------------------------------ #
    # Hook 1: inbound admission
    # ------------------------------------------------------------------ #
    def _admissible_umo(self, event: Any) -> str:
        """Return the session key when this event should enter the pipeline."""
        if compat.is_stopped(event):
            return ""
        if getattr(event, "call_llm", False) or getattr(event, "_has_send_oper", False):
            return ""
        umo = compat.umo(event)
        if not umo:
            return ""
        if self.extension_api.consume_suppressed(event):
            return ""
        return umo

    async def _admit(
        self, event: Any, umo: str, message: Any, policy: BubblePolicy
    ) -> tuple[str, BurstBatch | None] | None:
        """Register the message and, when queued, wait for it to be promoted."""
        admission = await self.coordinator.admit_or_coalesce(umo, message, policy=policy)
        if admission.mode == "bypass":
            return None
        if admission.mode == "swallowed":
            event.stop_event()
            await self._emit(
                "message.swallowed", umo=umo, token=admission.token, text=message.text
            )
            return None

        token = admission.token
        batch: BurstBatch | None = None
        if admission.mode == "wait_idle":
            await self._emit(
                "burst.debouncing",
                umo=umo,
                token=token,
                tier=admission.debounce_tier,
                delay_seconds=admission.debounce_seconds,
            )
            token, batch = await self.coordinator.wait_idle_and_promote(
                umo, token, policy=policy
            )
        elif admission.mode == "wait_flush":
            await self._emit(
                "turn.interrupted", umo=umo, token=token, text=message.text
            )
            token, batch = await self.coordinator.wait_and_collect_flush(
                umo, token, policy=policy
            )
        if not token:
            event.stop_event()
            return None
        return token, batch

    async def _publish_batch(
        self, event: Any, umo: str, token: str, batch: BurstBatch, policy: BubblePolicy
    ) -> None:
        """Expose the merged batch to the LLM turn and announce it."""
        compat.apply_batch(event, batch, policy)
        compat.write_meta(event, compat.META_BATCH, batch)
        if batch.count > 1:
            logger.info(
                "[BubbleFlow] Coalesced %d message(s) for %s (images=%d)",
                batch.count,
                umo,
                len(batch.merged_images),
            )
        await self._emit(
            "burst.flushed",
            umo=umo,
            token=token,
            count=batch.count,
            merged_text=batch.merged_text,
            image_count=len(batch.merged_images),
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE,
        priority=-250000,
    )
    async def handle_interleave_intake(self, event: AstrMessageEvent) -> None:
        """Admit an idle message immediately, debounce, or coalesce mid-flight bursts."""
        umo = self._admissible_umo(event)
        if not umo:
            return

        policy, channel_type = self._resolve_event_policy(event, umo)
        if not self._is_channel_active(policy, channel_type):
            return
        if (
            channel_type == "group"
            and getattr(event, "is_at_or_wake_command", None) is False
        ):
            return

        message = compat.capture_message(event, policy, self.coordinator.clock())
        if not compat.has_payload(message):
            return

        admitted = await self._admit(event, umo, message, policy)
        if admitted is None:
            return
        token, batch = admitted

        compat.write_meta(event, compat.META_TOKEN, token)
        compat.write_meta(event, compat.META_POLICY, policy)
        if batch is not None and batch.count > 0:
            await self._publish_batch(event, umo, token, batch, policy)

        await self._emit(
            "turn.started",
            umo=umo,
            token=token,
            channel_type=channel_type,
            coalesced_count=batch.count if batch is not None else 1,
        )

    # ------------------------------------------------------------------ #
    # Hook 2: LLM request enrichment
    # ------------------------------------------------------------------ #
    async def _merge_batch_images(self, req: ProviderRequest, batch: BurstBatch) -> None:
        if req.image_urls is None:
            req.image_urls = []
        known: set[str] = set()
        for ref in req.image_urls:
            known.update(compat.image_ref_keys(ref))
        for image in batch.merged_images:
            ref = await compat.resolve_image_ref(image)
            if not ref:
                continue
            keys = compat.image_ref_keys(ref)
            if any(key in known for key in keys):
                continue
            req.image_urls.append(ref)
            known.update(keys)

    @staticmethod
    def _needs_coalesce_hint(policy: BubblePolicy, batch: BurstBatch) -> bool:
        return bool(
            policy.inject_coalesce_hint
            and (
                batch.count > 1
                or batch.last_reply_preview
                or len(batch.merged_images) > 1
            )
        )

    @filter.on_llm_request(priority=100)
    async def inject_coalesce_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Mark LLM turn active and inject merged burst context + images."""
        umo = compat.umo(event)
        token = compat.read_meta(event, compat.META_TOKEN, "")
        if not umo or not token:
            return

        compat.write_meta(event, compat.META_LLM_STARTED, True)
        await self.coordinator.mark_llm_started(umo, token)

        policy = self._event_policy(event, umo)
        batch: BurstBatch | None = compat.read_meta(event, compat.META_BATCH)
        if batch is None:
            return

        if batch.last_reply_preview:
            compat.write_meta(event, compat.META_FOLLOWUP_AFTER_INTERRUPT, True)

        if not (req.prompt or "").strip() and batch.merged_text:
            req.prompt = batch.merged_text

        if policy.coalesce_images and batch.merged_images:
            await self._merge_batch_images(req, batch)

        if not self._needs_coalesce_hint(policy, batch):
            return
        hint = self.extension_api.format_coalesce_hint(
            batch,
            CoalesceFormatContext(umo=umo, event=event, policy=policy, batch=batch),
        )
        hint = hint or default_coalesce_prompt(batch)
        if hint:
            req.extra_user_content_parts = req.extra_user_content_parts or []
            req.extra_user_content_parts.append(TextPart(text=hint))

    # ------------------------------------------------------------------ #
    # Hook 3: outgoing bubble shaping
    # ------------------------------------------------------------------ #
    def _plan_bubbles(
        self,
        texts: tuple[str, ...],
        *,
        policy: BubblePolicy,
        umo: str,
        event: Any,
        interrupted: bool,
    ) -> list[str]:
        planned = plan_outgoing_bubbles(
            list(texts), policy=policy, interrupted=interrupted
        )
        planned = self.extension_api.apply_bubble_filters(
            planned,
            BubbleFilterContext(
                umo=umo,
                event=event,
                policy=policy,
                interrupted=interrupted,
                raw_bubbles=tuple(texts),
            ),
        )
        return planned or [texts[0]]

    async def _record_shaping(
        self,
        umo: str,
        token: str,
        *,
        raw_count: int,
        planned: list[str],
        policy: BubblePolicy,
        interrupted: bool,
    ) -> None:
        """Record what was actually sent; announce a collapse when bubbles were cut."""
        preview = " / ".join(planned)
        collapsed = (
            interrupted and policy.collapse_on_interrupt and raw_count > len(planned)
        )
        await self.coordinator.record_reply_preview(
            umo, token, preview, collapsed=collapsed
        )
        if not collapsed:
            return
        logger.info(
            "[BubbleFlow] Collapsed %d->%d bubble(s) on mid-flight interrupt for %s",
            raw_count,
            len(planned),
            umo,
        )
        await self._emit(
            "bubbles.collapsed",
            umo=umo,
            token=token,
            before=raw_count,
            after=len(planned),
            truncated=bool(policy.interrupt_keep_first_only and raw_count > 1),
            preview=preview[:120],
        )

    @filter.on_decorating_result(priority=-2000000)
    async def shape_outgoing_bubbles(self, event: AstrMessageEvent) -> None:
        """Plan, filter, and optionally collapse outgoing text bubbles."""
        umo = compat.umo(event)
        token = compat.read_meta(event, compat.META_TOKEN, "")
        if not umo or not token:
            return

        result = compat.result_of(event)
        content_type = compat.content_type_name(result)
        if "STREAMING_FINISH" in content_type:
            await self._release_turn_slot(umo, token)
            return
        if "STREAMING_RESULT" in content_type:
            return
        if result is None or not getattr(result, "chain", None):
            await self._release_turn_slot(umo, token, empty_result=True)
            return

        llm_started = bool(compat.read_meta(event, compat.META_LLM_STARTED, False))
        if compat.is_model_result(result) is False and not llm_started:
            await self._release_turn_slot(umo, token)
            return

        policy = self._event_policy(event, umo)
        if not policy.enabled:
            await self._release_turn_slot(umo, token)
            return

        if not compat.already_sent_plain(event, result):
            decision = await self.coordinator.try_preempt_or_finish(
                umo,
                token,
                policy=policy,
                channel_type=compat.detect_channel_type(event),
            )
            if decision.preempt:
                result.chain = []
                logger.info(
                    "[BubbleFlow] Preempted unpublished turn for %s (round=%d, merged=%d)",
                    umo,
                    decision.preempt_round,
                    decision.merged_count,
                )
                await self._emit(
                    "turn.preempted",
                    umo=umo,
                    token=token,
                    round=decision.preempt_round,
                    merged_count=decision.merged_count,
                )
                return

        plan = compat.partition_chain(result.chain, policy)
        if not plan.texts:
            await self._release_turn_slot(umo, token)
            return

        interrupted = self.coordinator.has_pending_interrupt(umo)
        # A follow-up answering an interrupted turn never needs more than one
        # bubble: the user is already waiting for the answer.
        effective_policy = (
            policy.with_overrides({"max_bubbles": 1})
            if compat.read_meta(event, compat.META_FOLLOWUP_AFTER_INTERRUPT, False)
            else policy
        )
        planned = self._plan_bubbles(
            plan.texts,
            policy=effective_policy,
            umo=umo,
            event=event,
            interrupted=interrupted,
        )

        result.chain = [
            *plan.leading,
            *(Plain(text) for text in planned),
            *plan.trailing,
        ]
        await self._record_shaping(
            umo,
            token,
            raw_count=len(plan.texts),
            planned=planned,
            policy=policy,
            interrupted=interrupted,
        )

        if compat.already_sent_plain(event, result):
            await self._release_turn_slot(umo, token)

    # ------------------------------------------------------------------ #
    # Hook 4 + teardown
    # ------------------------------------------------------------------ #
    @filter.after_message_sent(priority=-100)
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Release the active turn slot after all segmented bubbles are sent."""
        await self._release_turn_slot(
            compat.umo(event), compat.read_meta(event, compat.META_TOKEN, "")
        )

    async def terminate(self) -> None:
        """Cancel background watchdogs and release coordinator resources."""
        await self.coordinator.close()
        self.extension_api.clear_registrations()
