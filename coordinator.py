"""Pure asyncio session turn coordinator & multi-bubble rhythm planner.

Zero coupling to AstrBot internals or project-specific memory plugins so it can
be tested standalone and reused across any AstrBot plugin setup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import Any, Callable
from uuid import uuid4

_RE_URL = re.compile(r"https?://[^\s\n]+")
_RE_MD_BOLD_ASTERISK = re.compile(r"\*\*(.+?)\*\*")
_RE_MD_BOLD_UNDER = re.compile(r"__(.+?)__")
_RE_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_MD_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+")
_RE_MD_BULLET = re.compile(r"(?m)^[ \t]*[-*+][ \t]+")
_RE_INTERNAL_CN_PERIOD = re.compile(r"[ \t]*。+[ \t]*(?=[^\s\n”」』’)）\"'])")
_RE_TRAILING_CN_PERIOD = re.compile(r"。+(?=\s*(?:\n|$))")


@dataclass(frozen=True)
class BubblePolicy:
    """Per-turn coalescing and outgoing bubble shaping policy."""

    enabled: bool = True
    enable_private: bool = True
    enable_group: bool = False
    idle_debounce_seconds: float = 0.0
    trailing_quiet_seconds: float = 0.6
    trailing_max_wait_seconds: float = 1.8
    max_flight_seconds: float = 45.0
    coalesce_images: bool = True
    max_bubbles: int = 2
    collapse_on_interrupt: bool = True
    interrupt_keep_first_only: bool = True
    min_bubble_chars: int = 4
    dedup_identical_bubbles: bool = True
    bubble_sep: str = "\n"
    strip_markdown: bool = False
    strip_trailing_period: bool = False
    split_on_cn_period: bool = False
    inject_coalesce_hint: bool = True

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "BubblePolicy":
        """Build a normalized policy from an AstrBot plugin config mapping."""
        if not isinstance(raw, dict):
            return cls()
        channels = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
        coalesce = raw.get("coalesce") if isinstance(raw.get("coalesce"), dict) else {}
        bubbles = raw.get("bubbles") if isinstance(raw.get("bubbles"), dict) else {}
        prompts = raw.get("prompts") if isinstance(raw.get("prompts"), dict) else {}

        return cls(
            enabled=bool(raw.get("enabled", True)),
            enable_private=bool(channels.get("enable_private", raw.get("enable_private", True))),
            enable_group=bool(channels.get("enable_group", raw.get("enable_group", False))),
            idle_debounce_seconds=max(0.0, float(coalesce.get("idle_debounce_seconds", raw.get("idle_debounce_seconds", 0.0)))),
            trailing_quiet_seconds=max(0.0, float(coalesce.get("trailing_quiet_seconds", raw.get("trailing_quiet_seconds", 0.6)))),
            trailing_max_wait_seconds=max(0.0, float(coalesce.get("trailing_max_wait_seconds", raw.get("trailing_max_wait_seconds", 1.8)))),
            max_flight_seconds=max(5.0, float(coalesce.get("max_flight_seconds", raw.get("max_flight_seconds", 45.0)))),
            coalesce_images=bool(coalesce.get("coalesce_images", raw.get("coalesce_images", True))),
            max_bubbles=max(1, int(bubbles.get("max_bubbles", raw.get("max_bubbles", 2)))),
            collapse_on_interrupt=bool(bubbles.get("collapse_on_interrupt", raw.get("collapse_on_interrupt", True))),
            interrupt_keep_first_only=bool(bubbles.get("interrupt_keep_first_only", raw.get("interrupt_keep_first_only", True))),
            min_bubble_chars=max(0, int(bubbles.get("min_bubble_chars", raw.get("min_bubble_chars", 4)))),
            dedup_identical_bubbles=bool(bubbles.get("dedup_identical_bubbles", raw.get("dedup_identical_bubbles", True))),
            bubble_sep=str(bubbles.get("bubble_sep", raw.get("bubble_sep", "\n")) or "\n"),
            strip_markdown=bool(bubbles.get("strip_markdown", raw.get("strip_markdown", False))),
            strip_trailing_period=bool(bubbles.get("strip_trailing_period", raw.get("strip_trailing_period", False))),
            split_on_cn_period=bool(bubbles.get("split_on_cn_period", raw.get("split_on_cn_period", False))),
            inject_coalesce_hint=bool(prompts.get("inject_coalesce_hint", raw.get("inject_coalesce_hint", True))),
        )

    def with_overrides(self, overrides: dict[str, Any] | None) -> "BubblePolicy":
        """Return a new policy with safe typed field overrides applied."""
        if not isinstance(overrides, dict) or not overrides:
            return self
        valid = {k: v for k, v in overrides.items() if k in self.__dataclass_fields__}
        if not valid:
            return self
        merged = {k: getattr(self, k) for k in self.__dataclass_fields__}
        merged.update(valid)
        return BubblePolicy.from_mapping(merged)


@dataclass(frozen=True)
class CapturedMessage:
    """Snapshot of one inbound human message captured during flight or idle debounce."""

    text: str
    received_at: float
    sender_id: str = ""
    reply_preview: str = ""
    image_components: tuple[Any, ...] = ()
    extra_components: tuple[Any, ...] = ()
    raw_event: Any = None


@dataclass(frozen=True)
class BurstBatch:
    """Chronologically merged batch of rapid messages flushed into one unified LLM turn."""

    items: tuple[CapturedMessage, ...]
    last_reply_preview: str = ""
    flushed_at: float = 0.0

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def merged_text(self) -> str:
        distinct_senders = {item.sender_id for item in self.items if item.sender_id}
        multi_sender = self.count > 1 and len(distinct_senders) > 1
        lines: list[str] = []
        for idx, item in enumerate(self.items, start=1):
            parts: list[str] = []
            if item.reply_preview:
                parts.append(f"[引用:{item.reply_preview[:48]}]")
            if item.text.strip():
                parts.append(item.text.strip())
            elif item.image_components:
                parts.append("[图片]")
            line = " ".join(parts).strip()
            if line:
                if self.count == 1:
                    lines.append(line)
                elif multi_sender and item.sender_id:
                    lines.append(f"{idx}. [{item.sender_id}] {line}")
                else:
                    lines.append(f"{idx}. {line}")
        return "\n".join(lines)

    @property
    def merged_images(self) -> tuple[Any, ...]:
        imgs: list[Any] = []
        for item in self.items:
            imgs.extend(item.image_components)
        return tuple(imgs)

    @property
    def merged_components(self) -> tuple[Any, ...]:
        comps: list[Any] = []
        for item in self.items:
            comps.extend(item.extra_components)
            comps.extend(item.image_components)
        return tuple(comps)


@dataclass(frozen=True)
class AdmissionResult:
    """Result of registering an inbound message with the flight coordinator."""

    mode: str  # "run_now" | "wait_idle" | "wait_flush" | "swallowed" | "bypass"
    token: str = ""


@dataclass
class _SessionFlight:
    umo: str
    in_flight: bool = False
    settling: bool = False
    flight_token: str = ""
    started_at: float = 0.0
    llm_started: bool = False
    last_reply_preview: str = ""
    last_reply_at: float = 0.0
    last_activity_at: float = 0.0
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    waiter_token: str = ""
    burst_items: list[CapturedMessage] = field(default_factory=list)
    burst_updated: asyncio.Event = field(default_factory=asyncio.Event)

    idle_leader_token: str = ""
    idle_items: list[CapturedMessage] = field(default_factory=list)
    idle_updated: asyncio.Event = field(default_factory=asyncio.Event)
    idle_active: bool = False

    total_runs: int = 0
    total_coalesced: int = 0
    total_collapsed: int = 0
    total_watchdog_resets: int = 0

    def __post_init__(self) -> None:
        self.done_event.set()


def default_coalesce_prompt(batch: BurstBatch) -> str:
    """Build a concise, neutral LLM hint explaining that rapid messages were coalesced."""
    lines = [
        f"[bubble_flow_coalesced count={batch.count}]",
        f"你在处理或刚发完上一句期间，对方紧接着按时间顺序补充发来了 {batch.count} 条内容（已在当前用户消息中按 1..{batch.count} 合并）。",
        "请顺着对方的最新意图一次性自然接住重点，不要对每条机械逐条复述。",
    ]
    if batch.last_reply_preview:
        lines.append(f"刚才你已经发出去的上一条内容是「{batch.last_reply_preview[:60]}」，不要再重复回答这句。")
    return "\n".join(lines)


def plan_outgoing_bubbles(
    texts: list[str] | tuple[str, ...],
    *,
    policy: BubblePolicy,
    interrupted: bool = False,
) -> list[str]:
    """Normalize, deduplicate, cap, or collapse outgoing multi-bubble plain segments."""
    cleaned = [str(t).strip() for t in texts if str(t or "").strip()]
    if not cleaned:
        return []

    if policy.dedup_identical_bubbles:
        deduped: list[str] = []
        for item in cleaned:
            if not deduped or item != deduped[-1]:
                deduped.append(item)
        cleaned = deduped

    if interrupted and policy.collapse_on_interrupt:
        if policy.interrupt_keep_first_only and len(cleaned) > 1:
            first = cleaned[0]
            if policy.min_bubble_chars > 0 and len(first) < policy.min_bubble_chars:
                return [f"{first}{policy.bubble_sep}{cleaned[1]}"]
            return [first]
        return [policy.bubble_sep.join(cleaned)]

    # Merge tiny micro-fragments (< min_bubble_chars) into an adjacent bubble
    if policy.min_bubble_chars > 0 and len(cleaned) > 1:
        merged: list[str] = []
        pending_prefix = ""
        for item in cleaned:
            candidate = f"{pending_prefix}{policy.bubble_sep}{item}".strip() if pending_prefix else item
            pending_prefix = ""
            if len(candidate) < policy.min_bubble_chars:
                if merged:
                    merged[-1] = f"{merged[-1]}{policy.bubble_sep}{candidate}"
                else:
                    pending_prefix = candidate
            else:
                merged.append(candidate)
        if pending_prefix:
            if merged:
                merged[-1] = f"{merged[-1]}{policy.bubble_sep}{pending_prefix}"
            else:
                merged.append(pending_prefix)
        cleaned = merged

    cap = max(1, int(policy.max_bubbles))
    if len(cleaned) > cap:
        head = cleaned[: cap - 1]
        tail = policy.bubble_sep.join(cleaned[cap - 1 :])
        cleaned = [*head, tail]
    return cleaned


def clean_plain_bubble_text(
    text: str,
    *,
    strip_markdown: bool = False,
    strip_trailing_period: bool = False,
    split_on_cn_period: bool = False,
) -> str:
    """Optionally normalize markdown, trailing Chinese periods, and internal period splits while masking URLs."""
    if not text or not (strip_markdown or strip_trailing_period or split_on_cn_period):
        return text
    urls: list[str] = []

    def _mask_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\x00URL{len(urls) - 1}\x00"

    cleaned = _RE_URL.sub(_mask_url, text)
    if strip_markdown:
        cleaned = _RE_MD_HEADING.sub("", cleaned)
        cleaned = _RE_MD_BULLET.sub("", cleaned)
        cleaned = _RE_MD_BOLD_ASTERISK.sub(r"\1", cleaned)
        cleaned = _RE_MD_BOLD_UNDER.sub(r"\1", cleaned)
        cleaned = _RE_MD_INLINE_CODE.sub(r"\1", cleaned)
    if split_on_cn_period:
        cleaned = _RE_INTERNAL_CN_PERIOD.sub("\n", cleaned)
    if strip_trailing_period:
        cleaned = _RE_TRAILING_CN_PERIOD.sub("", cleaned)
    for idx, url in enumerate(urls):
        cleaned = cleaned.replace(f"\x00URL{idx}\x00", url)
    return cleaned


class SessionFlightCoordinator:
    """Async per-session flight coordinator eliminating serial follow-up avalanches."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._flights: dict[str, _SessionFlight] = {}
        self._closed = False

    def _get(self, umo: str) -> _SessionFlight:
        flight = self._flights.get(umo)
        if flight is None:
            if len(self._flights) > 128:
                now = self._clock()
                stale = [
                    k
                    for k, v in self._flights.items()
                    if not v.in_flight
                    and not v.settling
                    and not v.idle_active
                    and not v.waiter_token
                    and not v.burst_items
                    and v.last_activity_at > 0
                    and (now - v.last_activity_at) > 86400.0
                ]
                for k in stale:
                    self._flights.pop(k, None)
            flight = _SessionFlight(umo=umo, last_activity_at=self._clock())
            self._flights[umo] = flight
        return flight

    def _check_watchdog_locked(self, flight: _SessionFlight, max_flight_seconds: float) -> bool:
        if not flight.in_flight or max_flight_seconds <= 0:
            return False
        if (self._clock() - flight.started_at) >= max_flight_seconds:
            flight.in_flight = False
            flight.flight_token = ""
            flight.llm_started = False
            if flight.waiter_token:
                flight.settling = True
            flight.total_watchdog_resets += 1
            flight.done_event.set()
            return True
        return False

    async def admit_or_coalesce(
        self,
        umo: str,
        message: CapturedMessage,
        *,
        policy: BubblePolicy,
    ) -> AdmissionResult:
        """Admit a new turn immediately or queue it into the active session's burst buffer."""
        if self._closed or not umo or not policy.enabled:
            return AdmissionResult(mode="bypass")

        async with self._lock:
            flight = self._get(umo)
            flight.last_activity_at = self._clock()
            self._check_watchdog_locked(flight, policy.max_flight_seconds)

            # Case 1: Session currently has an in-flight LLM/send turn (or is settling trailing bursts)
            if flight.in_flight or flight.settling:
                flight.burst_items.append(message)
                flight.total_coalesced += 1
                flight.burst_updated.set()
                if not flight.waiter_token:
                    token = uuid4().hex
                    flight.waiter_token = token
                    return AdmissionResult(mode="wait_flush", token=token)
                return AdmissionResult(mode="swallowed")

            # Case 2: Idle, with optional standalone idle debounce enabled
            if policy.idle_debounce_seconds > 0:
                if flight.idle_active:
                    flight.idle_items.append(message)
                    flight.total_coalesced += 1
                    flight.idle_updated.set()
                    return AdmissionResult(mode="swallowed")
                token = uuid4().hex
                flight.idle_active = True
                flight.idle_leader_token = token
                flight.idle_items = [message]
                flight.idle_updated.clear()
                return AdmissionResult(mode="wait_idle", token=token)

            # Case 3: Idle, immediate execution
            token = uuid4().hex
            flight.in_flight = True
            flight.flight_token = token
            flight.started_at = self._clock()
            flight.llm_started = False
            flight.last_reply_preview = ""
            flight.last_reply_at = 0.0
            flight.done_event.clear()
            flight.total_runs += 1
            return AdmissionResult(mode="run_now", token=token)

    async def wait_idle_and_promote(
        self,
        umo: str,
        leader_token: str,
        *,
        policy: BubblePolicy,
    ) -> tuple[str, BurstBatch | None]:
        """Wait for standalone idle debounce window and promote collected messages to in-flight."""
        try:
            async with self._lock:
                flight = self._get(umo)
                if not flight.idle_active or flight.idle_leader_token != leader_token:
                    return ("", None)
                first_at = flight.idle_items[0].received_at if flight.idle_items else self._clock()

            max_end = self._clock() + min(policy.idle_debounce_seconds * 2.5, max(policy.idle_debounce_seconds, 0.2) + 2.0)
            while not self._closed:
                async with self._lock:
                    flight = self._get(umo)
                    if not flight.idle_active or flight.idle_leader_token != leader_token:
                        return ("", None)
                    last_at = flight.idle_items[-1].received_at if flight.idle_items else first_at
                    quiet_target = last_at + policy.idle_debounce_seconds
                    now = self._clock()
                    remaining = min(quiet_target, max_end) - now
                    if remaining <= 0:
                        break
                    flight.idle_updated.clear()
                try:
                    await asyncio.wait_for(flight.idle_updated.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

            async with self._lock:
                flight = self._get(umo)
                if self._closed or not flight.idle_active or flight.idle_leader_token != leader_token:
                    return ("", None)
                if flight.in_flight or flight.settling:
                    return ("", None)
                items = tuple(flight.idle_items)
                flight.idle_active = False
                flight.idle_leader_token = ""
                flight.idle_items = []

                token = uuid4().hex
                flight.in_flight = True
                flight.flight_token = token
                flight.started_at = self._clock()
                flight.llm_started = False
                flight.last_reply_preview = ""
                flight.last_reply_at = 0.0
                flight.done_event.clear()
                flight.total_runs += 1
                batch = BurstBatch(items=items, last_reply_preview="", flushed_at=self._clock()) if len(items) > 1 else None
                return (token, batch)
        except BaseException:
            flight = self._flights.get(umo)
            if flight is not None and flight.idle_leader_token == leader_token:
                flight.idle_active = False
                flight.idle_leader_token = ""
                flight.idle_items = []
            raise

    async def wait_and_collect_flush(
        self,
        umo: str,
        waiter_token: str,
        *,
        policy: BubblePolicy,
    ) -> tuple[str, BurstBatch | None]:
        """Wait for the in-flight turn to finish, absorb tail messages, and promote batch to flight."""
        try:
            while not self._closed:
                async with self._lock:
                    flight = self._get(umo)
                    if flight.waiter_token != waiter_token:
                        return ("", None)
                    self._check_watchdog_locked(flight, policy.max_flight_seconds)
                    if not flight.in_flight:
                        break
                    wait_timeout = max(0.05, policy.max_flight_seconds - (self._clock() - flight.started_at))
                try:
                    await asyncio.wait_for(flight.done_event.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue

            if self._closed:
                flight = self._flights.get(umo)
                if flight is not None and flight.waiter_token == waiter_token:
                    flight.waiter_token = ""
                    flight.settling = False
                    flight.burst_items = []
                return ("", None)

            # Brief tail settling window after the prior reply finished
            max_tail_end = self._clock() + max(0.0, policy.trailing_max_wait_seconds)
            while policy.trailing_quiet_seconds > 0 and not self._closed:
                async with self._lock:
                    flight = self._get(umo)
                    if flight.waiter_token != waiter_token:
                        return ("", None)
                    if not flight.burst_items:
                        break
                    now = self._clock()
                    last_at = max(item.received_at for item in flight.burst_items)
                    remaining = min(last_at + policy.trailing_quiet_seconds, max_tail_end) - now
                    if remaining <= 0:
                        break
                    flight.burst_updated.clear()
                try:
                    await asyncio.wait_for(flight.burst_updated.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

            async with self._lock:
                flight = self._get(umo)
                if self._closed or flight.waiter_token != waiter_token:
                    if flight.waiter_token == waiter_token:
                        flight.waiter_token = ""
                        flight.settling = False
                        flight.burst_items = []
                    return ("", None)
                items = tuple(flight.burst_items)
                flight.burst_items = []
                flight.waiter_token = ""
                flight.settling = False
                if not items or flight.in_flight:
                    return ("", None)

                now = self._clock()
                preview = ""
                if (
                    flight.last_reply_preview
                    and flight.last_reply_at > 0
                    and (now - flight.last_reply_at) <= max(60.0, policy.max_flight_seconds)
                ):
                    preview = flight.last_reply_preview
                flight.last_reply_preview = ""
                flight.last_reply_at = 0.0

                new_token = uuid4().hex
                flight.in_flight = True
                flight.flight_token = new_token
                flight.started_at = now
                flight.llm_started = False
                flight.done_event.clear()
                flight.total_runs += 1
                batch = BurstBatch(
                    items=items,
                    last_reply_preview=preview,
                    flushed_at=now,
                )
                return (new_token, batch)
        except BaseException:
            flight = self._flights.get(umo)
            if flight is not None and flight.waiter_token == waiter_token:
                flight.waiter_token = ""
                flight.settling = False
                flight.burst_items = []
            raise

    async def mark_llm_started(self, umo: str, token: str) -> None:
        async with self._lock:
            flight = self._flights.get(umo)
            if flight and flight.in_flight and (not token or flight.flight_token == token):
                flight.llm_started = True

    async def record_reply_preview(self, umo: str, token: str, text: str, *, collapsed: bool = False) -> None:
        async with self._lock:
            flight = self._flights.get(umo)
            if flight and (not token or flight.flight_token == token or not flight.in_flight):
                clean = str(text or "").strip()
                if clean:
                    flight.last_reply_preview = clean[:120]
                    flight.last_reply_at = self._clock()
                if collapsed:
                    flight.total_collapsed += 1

    async def release_flight(self, umo: str, token: str = "") -> bool:
        """Release active flight lock and wake any waiting flush batch."""
        async with self._lock:
            flight = self._flights.get(umo)
            if flight is None or not flight.in_flight:
                return False
            if token and flight.flight_token != token:
                return False
            flight.in_flight = False
            flight.flight_token = ""
            flight.llm_started = False
            if flight.waiter_token:
                flight.settling = True
            flight.done_event.set()
            return True

    async def force_release(self, umo: str) -> bool:
        return await self.release_flight(umo, token="")

    def has_pending_interrupt(self, umo: str) -> bool:
        """Return True when new inbound messages arrived while the current turn is in flight."""
        flight = self._flights.get(umo)
        return bool(flight and flight.in_flight and flight.burst_items)

    def snapshot(self, umo: str) -> dict[str, Any]:
        flight = self._flights.get(umo)
        if flight is None:
            return {
                "umo": umo,
                "in_flight": False,
                "pending_count": 0,
                "flight_age_seconds": 0.0,
                "last_reply_preview": "",
                "total_runs": 0,
                "total_coalesced": 0,
                "total_collapsed": 0,
                "total_watchdog_resets": 0,
            }
        return {
            "umo": umo,
            "in_flight": flight.in_flight,
            "pending_count": len(flight.burst_items),
            "flight_age_seconds": round(max(0.0, self._clock() - flight.started_at), 3) if flight.in_flight else 0.0,
            "last_reply_preview": flight.last_reply_preview,
            "total_runs": flight.total_runs,
            "total_coalesced": flight.total_coalesced,
            "total_collapsed": flight.total_collapsed,
            "total_watchdog_resets": flight.total_watchdog_resets,
        }

    def stats(self) -> dict[str, int]:
        return {
            "sessions_tracked": len(self._flights),
            "active_flights": sum(1 for f in self._flights.values() if f.in_flight),
            "pending_burst_messages": sum(len(f.burst_items) for f in self._flights.values()),
            "total_runs": sum(f.total_runs for f in self._flights.values()),
            "total_coalesced": sum(f.total_coalesced for f in self._flights.values()),
            "total_collapsed": sum(f.total_collapsed for f in self._flights.values()),
            "total_watchdog_resets": sum(f.total_watchdog_resets for f in self._flights.values()),
        }

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            for flight in self._flights.values():
                flight.in_flight = False
                flight.idle_active = False
                flight.burst_items.clear()
                flight.idle_items.clear()
                flight.waiter_token = ""
                flight.idle_leader_token = ""
                flight.done_event.set()
                flight.burst_updated.set()
                flight.idle_updated.set()
