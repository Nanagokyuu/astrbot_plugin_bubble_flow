"""Public extension API (`bubble-flow.v1`) for astrbot_plugin_bubble_flow."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from astrbot.api import logger

from .coordinator import BubblePolicy, BurstBatch, SessionFlightCoordinator

PLUGIN_ID = "astrbot_plugin_bubble_flow"
API_VERSION = "bubble-flow.v1"


@dataclass(frozen=True)
class PolicyResolveContext:
    """Context passed to registered policy resolvers."""

    umo: str
    event: Any = None
    base_policy: BubblePolicy | None = None
    channel_type: str = ""
    sender_id: str = ""


@dataclass(frozen=True)
class CoalesceFormatContext:
    """Context passed to registered coalesce prompt formatters."""

    umo: str
    event: Any = None
    policy: BubblePolicy | None = None
    batch: BurstBatch | None = None


@dataclass(frozen=True)
class BubbleFilterContext:
    """Context passed to registered outgoing bubble filters."""

    umo: str
    event: Any = None
    policy: BubblePolicy | None = None
    interrupted: bool = False
    raw_bubbles: tuple[str, ...] = ()


@dataclass
class _Registration:
    token: str
    kind: str
    name: str
    priority: int
    fn: Callable[..., Any]


def _extract_umo(umo_or_event: Any) -> str:
    if isinstance(umo_or_event, str):
        return umo_or_event
    umo = getattr(umo_or_event, "unified_msg_origin", None)
    if isinstance(umo, str):
        return umo
    return ""


def discover_bubble_flow_api(context: Any) -> "BubbleFlowAPI | None":
    """Locate the activated BubbleFlowAPI instance from an AstrBot Context."""
    get_stars = getattr(context, "get_all_stars", None)
    if not callable(get_stars):
        return None
    try:
        stars = get_stars() or []
    except Exception as exc:
        logger.warning("[BubbleFlow] discover_bubble_flow_api failed to list stars: %s", exc)
        return None

    for star in stars:
        name = getattr(star, "name", "") or ""
        if name not in (PLUGIN_ID, "bubble_flow"):
            continue
        if not getattr(star, "activated", False):
            continue
        star_cls = getattr(star, "star_cls", None)
        api = getattr(star_cls, "extension_api", None)
        if getattr(api, "api_version", None) == API_VERSION:
            return api
    return None


class BubbleFlowAPI:
    """Reserved extension API exposed on `BubbleFlowPlugin.extension_api`."""

    api_version = API_VERSION
    plugin_id = PLUGIN_ID

    def __init__(
        self,
        coordinator: SessionFlightCoordinator,
        base_policy_getter: Callable[[], BubblePolicy] | BubblePolicy,
    ) -> None:
        self._coordinator = coordinator
        self._base_policy_getter = (
            base_policy_getter
            if callable(base_policy_getter)
            else (lambda: base_policy_getter)
        )
        self._policy_resolvers: list[_Registration] = []
        self._coalesce_formatters: list[_Registration] = []
        self._bubble_filters: list[_Registration] = []
        self._event_listeners: list[_Registration] = []
        self._session_overrides: dict[str, dict[str, Any]] = {}
        self._suppressed_umos: set[str] = set()

    @property
    def base_policy(self) -> BubblePolicy:
        return self._base_policy_getter()

    def _add_reg(
        self,
        bucket: list[_Registration],
        kind: str,
        name: str,
        priority: int,
        fn: Callable[..., Any],
    ) -> str:
        if not callable(fn):
            raise TypeError(f"{kind} callback must be callable")
        token = f"{kind}:{name}:{uuid.uuid4().hex[:10]}"
        bucket.append(
            _Registration(
                token=token,
                kind=kind,
                name=name,
                priority=int(priority),
                fn=fn,
            )
        )
        bucket.sort(key=lambda item: item.priority)
        return token

    def register_policy_resolver(
        self,
        resolver_id: str,
        fn: Callable[[PolicyResolveContext], dict[str, Any] | BubblePolicy | None],
        *,
        priority: int = 100,
    ) -> str:
        """Register a dynamic per-turn policy resolver (lower priority runs first)."""
        return self._add_reg(
            self._policy_resolvers, "policy_resolver", resolver_id, priority, fn
        )

    def register_coalesce_formatter(
        self,
        formatter_id: str,
        fn: Callable[[BurstBatch, CoalesceFormatContext], str | None],
        *,
        priority: int = 100,
    ) -> str:
        """Register a custom formatter for merged-burst LLM hints."""
        return self._add_reg(
            self._coalesce_formatters,
            "coalesce_formatter",
            formatter_id,
            priority,
            fn,
        )

    def register_bubble_filter(
        self,
        filter_id: str,
        fn: Callable[[list[str], BubbleFilterContext], list[str] | tuple[str, ...] | None],
        *,
        priority: int = 100,
    ) -> str:
        """Register a post-planning filter/transformer over outgoing text bubbles."""
        return self._add_reg(
            self._bubble_filters, "bubble_filter", filter_id, priority, fn
        )

    def register_event_listener(
        self,
        listener_id: str,
        fn: Callable[[str, dict[str, Any]], Any],
    ) -> str:
        """Register a passive lifecycle observer (`turn.started`, `burst.flushed`, etc.)."""
        return self._add_reg(
            self._event_listeners, "event_listener", listener_id, 100, fn
        )

    def unregister(self, token: str) -> bool:
        """Remove a previously registered callback by token."""
        for bucket in (
            self._policy_resolvers,
            self._coalesce_formatters,
            self._bubble_filters,
            self._event_listeners,
        ):
            for idx, item in enumerate(bucket):
                if item.token == token:
                    bucket.pop(idx)
                    return True
        return False

    def clear_registrations(self) -> None:
        """Remove all registered callbacks, overrides, and suppressions."""
        self._policy_resolvers.clear()
        self._coalesce_formatters.clear()
        self._bubble_filters.clear()
        self._event_listeners.clear()
        self._session_overrides.clear()
        self._suppressed_umos.clear()

    def set_session_policy(self, umo: str, **overrides: Any) -> None:
        """Store sticky policy field overrides for a specific `umo`."""
        if not umo:
            return
        current = self._session_overrides.setdefault(umo, {})
        current.update(overrides)

    def clear_session_policy(self, umo: str) -> None:
        """Clear sticky policy overrides for a specific `umo`."""
        self._session_overrides.pop(umo, None)

    def suppress_next_coalesce(self, umo_or_event: Any) -> None:
        """Bypass intake coalescing for the current event or next turn of `umo`."""
        if not isinstance(umo_or_event, str) and umo_or_event is not None:
            try:
                setattr(umo_or_event, "_bubble_flow_bypass", True)
            except Exception:
                pass
        umo = _extract_umo(umo_or_event)
        if umo:
            self._suppressed_umos.add(umo)

    def consume_suppressed(self, umo_or_event: Any) -> bool:
        """Consume a pending bypass flag on an event or its `umo`."""
        if (
            not isinstance(umo_or_event, str)
            and umo_or_event is not None
            and getattr(umo_or_event, "_bubble_flow_bypass", False)
        ):
            try:
                setattr(umo_or_event, "_bubble_flow_bypass", False)
            except Exception:
                pass
            umo = _extract_umo(umo_or_event)
            if umo:
                self._suppressed_umos.discard(umo)
            return True
        umo = _extract_umo(umo_or_event)
        if umo and umo in self._suppressed_umos:
            self._suppressed_umos.discard(umo)
            return True
        return False

    def resolve_policy(self, ctx: PolicyResolveContext) -> BubblePolicy:
        """Compute effective policy: base -> session overrides -> policy resolvers."""
        policy = ctx.base_policy or self.base_policy
        session_ov = self._session_overrides.get(ctx.umo)
        if session_ov:
            policy = policy.with_overrides(session_ov)

        for reg in list(self._policy_resolvers):
            try:
                res = reg.fn(
                    PolicyResolveContext(
                        umo=ctx.umo,
                        event=ctx.event,
                        base_policy=policy,
                        channel_type=ctx.channel_type,
                        sender_id=ctx.sender_id,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "[BubbleFlow] policy_resolver %r failed: %s", reg.name, exc
                )
                continue
            if isinstance(res, BubblePolicy):
                policy = res
            elif isinstance(res, dict) and res:
                policy = policy.with_overrides(res)
        return policy

    def get_session_policy(
        self,
        umo: str,
        event: Any = None,
        *,
        channel_type: str = "",
        sender_id: str = "",
    ) -> BubblePolicy:
        """Convenience wrapper returning the effective BubblePolicy for `umo`."""
        return self.resolve_policy(
            PolicyResolveContext(
                umo=umo,
                event=event,
                base_policy=self.base_policy,
                channel_type=channel_type,
                sender_id=sender_id,
            )
        )

    def format_coalesce_hint(
        self, batch: BurstBatch, ctx: CoalesceFormatContext
    ) -> str | None:
        """Run registered coalesce formatters; returns the first non-empty hint."""
        for reg in list(self._coalesce_formatters):
            try:
                rendered = reg.fn(batch, ctx)
            except Exception as exc:
                logger.warning(
                    "[BubbleFlow] coalesce_formatter %r failed: %s", reg.name, exc
                )
                continue
            if isinstance(rendered, str) and rendered.strip():
                return rendered.strip()
        return None

    def apply_bubble_filters(
        self, bubbles: list[str], ctx: BubbleFilterContext
    ) -> list[str]:
        """Run registered bubble filters sequentially with exception isolation."""
        current = list(bubbles)
        for reg in list(self._bubble_filters):
            try:
                updated = reg.fn(list(current), ctx)
            except Exception as exc:
                logger.warning(
                    "[BubbleFlow] bubble_filter %r failed: %s", reg.name, exc
                )
                continue
            if updated is None:
                continue
            cleaned = [str(item) for item in updated if str(item).strip()]
            current = cleaned
        return current

    async def emit_event(self, event_name: str, payload: dict[str, Any]) -> None:
        """Notify registered event listeners safely (sync or async)."""
        for reg in list(self._event_listeners):
            try:
                res = reg.fn(event_name, dict(payload))
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning(
                    "[BubbleFlow] event_listener %r failed on %s: %s",
                    reg.name,
                    event_name,
                    exc,
                )

    def get_session_state(self, umo: str) -> dict[str, Any]:
        """Return an inspection snapshot of flight and interrupt state for `umo`."""
        return self._coordinator.snapshot(umo)

    def has_pending_interrupt(self, umo: str) -> bool:
        """Whether `umo` received new user messages while its current turn was running."""
        return self._coordinator.has_pending_interrupt(umo)

    def will_preempt(self, event: Any) -> bool:
        """Return True if BubbleFlow is poised to silently preempt this event's draft."""
        from . import compat

        umo = compat.umo(event)
        token = compat.read_meta(event, compat.META_TOKEN, "")
        if not umo or not token:
            return False
        result = compat.result_of(event)
        if compat.already_sent_plain(event, result):
            return False
        policy = compat.read_meta(event, compat.META_POLICY) or self.get_session_policy(umo, event)
        if not policy.enabled:
            return False
        return self._coordinator.will_preempt(
            umo,
            token,
            policy=policy,
            channel_type=compat.detect_channel_type(event),
        )

    def get_stats(self) -> dict[str, int]:
        """Return aggregate coordinator counters."""
        return self._coordinator.stats()

    async def force_release(self, umo: str) -> bool:
        """Manually release an active flight slot if an upstream failure occurred."""
        return await self._coordinator.force_release(umo)
