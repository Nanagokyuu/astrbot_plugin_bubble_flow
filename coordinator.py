"""Pure asyncio session turn coordinator & multi-bubble rhythm planner.

Zero coupling to AstrBot internals or project-specific memory plugins so it can
be tested standalone and reused across any AstrBot plugin setup.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

_RE_URL = re.compile(r"https?://[^\s\n。，！？；：、）】》」』\"'<>]+")
_RE_MD_BOLD_ASTERISK = re.compile(r"\*\*(.+?)\*\*")
_RE_MD_BOLD_UNDER = re.compile(r"(?<!\w)__(?![A-Za-z0-9_]+__(?!\w))(.+?)__(?!\w)")
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
    idle_quick_seconds: float = 0.0
    idle_debounce_seconds: float = 0.0
    idle_continuation_seconds: float = 0.0
    idle_max_wait_seconds: float = 4.0
    trailing_quiet_seconds: float = 0.6
    trailing_max_wait_seconds: float = 1.8
    max_flight_seconds: float = 45.0
    coalesce_images: bool = True
    preempt_before_send: bool = False
    max_preempt_rounds: int = 2
    enable_preempt_in_group: bool = False
    preempt_grace_seconds: float = 0.0
    adaptive_cadence: bool = True
    send_typing_action: bool = False
    max_bubbles: int = 2
    soft_max_bubble_chars: int = 0
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
            idle_quick_seconds=max(0.0, float(coalesce.get("idle_quick_seconds", raw.get("idle_quick_seconds", 0.0)))),
            idle_debounce_seconds=max(0.0, float(coalesce.get("idle_debounce_seconds", raw.get("idle_debounce_seconds", 0.0)))),
            idle_continuation_seconds=max(0.0, float(coalesce.get("idle_continuation_seconds", raw.get("idle_continuation_seconds", 0.0)))),
            idle_max_wait_seconds=max(0.2, float(coalesce.get("idle_max_wait_seconds", raw.get("idle_max_wait_seconds", 4.0)))),
            trailing_quiet_seconds=max(0.0, float(coalesce.get("trailing_quiet_seconds", raw.get("trailing_quiet_seconds", 0.6)))),
            trailing_max_wait_seconds=max(0.0, float(coalesce.get("trailing_max_wait_seconds", raw.get("trailing_max_wait_seconds", 1.8)))),
            max_flight_seconds=max(5.0, float(coalesce.get("max_flight_seconds", raw.get("max_flight_seconds", 45.0)))),
            coalesce_images=bool(coalesce.get("coalesce_images", raw.get("coalesce_images", True))),
            preempt_before_send=bool(coalesce.get("preempt_before_send", raw.get("preempt_before_send", False))),
            max_preempt_rounds=max(0, int(coalesce.get("max_preempt_rounds", raw.get("max_preempt_rounds", 2)))),
            enable_preempt_in_group=bool(coalesce.get("enable_preempt_in_group", raw.get("enable_preempt_in_group", False))),
            preempt_grace_seconds=max(0.0, float(coalesce.get("preempt_grace_seconds", raw.get("preempt_grace_seconds", 0.0)))),
            adaptive_cadence=bool(coalesce.get("adaptive_cadence", raw.get("adaptive_cadence", True))),
            send_typing_action=bool(coalesce.get("send_typing_action", raw.get("send_typing_action", False))),
            max_bubbles=max(1, int(bubbles.get("max_bubbles", raw.get("max_bubbles", 2)))),
            soft_max_bubble_chars=max(0, int(bubbles.get("soft_max_bubble_chars", raw.get("soft_max_bubble_chars", 0)))),
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
    is_preempted_merge: bool = False

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
    debounce_tier: str = "none"  # "none" | "quick" | "normal" | "continuation"
    debounce_seconds: float = 0.0


@dataclass(frozen=True)
class PreemptDecision:
    """Decision returned before sending outgoing bubbles when mid-flight input arrived."""

    preempt: bool
    merged_count: int = 0
    preempt_round: int = 0


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
    last_burst_at: float = 0.0
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    active_items: list[CapturedMessage] = field(default_factory=list)
    preempt_carry_items: list[CapturedMessage] = field(default_factory=list)
    preempt_count: int = 0

    waiter_token: str = ""
    burst_items: list[CapturedMessage] = field(default_factory=list)
    burst_updated: asyncio.Event = field(default_factory=asyncio.Event)

    idle_leader_token: str = ""
    idle_items: list[CapturedMessage] = field(default_factory=list)
    idle_updated: asyncio.Event = field(default_factory=asyncio.Event)
    idle_active: bool = False
    idle_debounce_seconds: float = 0.0
    ema_burst_gap: float = 0.0
    watchdog_task: asyncio.Task[Any] | None = None

    total_runs: int = 0
    total_coalesced: int = 0
    total_collapsed: int = 0
    total_preempted: int = 0
    total_fast_passed: int = 0
    total_continuation_waits: int = 0
    total_watchdog_resets: int = 0

    def __post_init__(self) -> None:
        self.done_event.set()


_RE_QUICK_COMPLETE = re.compile(
    r"^(?:早|早安|早上好|午安|晚安|睡了|再见|拜拜|在吗|在不在|嗯+|哦+|好+|行|ok|okay|好的|谢谢|多谢|笑死|哈哈+|[？?]+)$",
    re.IGNORECASE,
)
_RE_CONTINUATION_LEAD = re.compile(
    r"^(?:我跟你说|跟你说|话说|顺便|对了|还有|就是|其实|但是|不过|然后|结果|关键是|重点是|那个|等下|等等|刚|刚刚|刚才|今天早上|今天上午|今天下午|今天晚上|昨天晚上|em+|呃+|唉|卧槽)"
)
_RE_CONTINUATION_TAIL = re.compile(r"(?:然后|结果|就是|因为|所以|但是|不过|或者|而且|[,，、:：…\.]{1,3}|是这样|怎么说|点半|分钟|的时候)$")
_RE_TERMINAL_PUNCT = re.compile(r"[。！？!?~～…）)\"”'’」』]$")
_QUOTE_OPEN_TO_CLOSE = {
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "《": "》",
    "【": "】",
    '"': '"',
    "'": "'",
}


def _has_unclosed_enclosure(text: str) -> bool:
    """Return True when Chinese/ASCII brackets or quotes are opened without matching close."""
    stack: list[str] = []
    for ch in str(text or ""):
        if stack and ch == stack[-1]:
            stack.pop()
        elif ch in _QUOTE_OPEN_TO_CLOSE:
            if ch in ('"', "'") and stack and stack[-1] == ch:
                stack.pop()
            else:
                stack.append(_QUOTE_OPEN_TO_CLOSE[ch])
        elif ch in _QUOTE_OPEN_TO_CLOSE.values() and stack and ch == stack[-1]:
            stack.pop()
    return bool(stack)


def classify_debounce_seconds(
    message: CapturedMessage,
    policy: BubblePolicy,
    *,
    recent_burst: bool = False,
    ema_burst_gap: float = 0.0,
) -> tuple[str, float]:
    """Classify an idle inbound message into quick / normal / continuation debounce window."""
    normal = max(0.0, float(policy.idle_debounce_seconds))
    quick = max(0.0, float(policy.idle_quick_seconds))
    cont = max(0.0, float(policy.idle_continuation_seconds))

    if normal <= 0.0 and quick <= 0.0 and cont <= 0.0:
        return ("none", 0.0)

    # Legacy simple mode: only idle_debounce_seconds was configured
    if quick <= 0.0 and cont <= 0.0:
        return ("normal", normal)

    text = (message.text or "").strip()
    has_image = bool(message.image_components)
    has_reply = bool(message.reply_preview)

    is_continuation = (
        recent_burst
        or (has_image and len(text) <= 8)
        or has_reply
        or _has_unclosed_enclosure(text)
        or bool(_RE_CONTINUATION_LEAD.match(text))
        or bool(_RE_CONTINUATION_TAIL.search(text))
        or (0 < len(text) <= 10 and not _RE_QUICK_COMPLETE.match(text) and not _RE_TERMINAL_PUNCT.search(text))
    )
    if is_continuation and cont > 0.0:
        eff_cont = cont
        if policy.adaptive_cadence and ema_burst_gap > 0.0:
            eff_cont = max(cont, min(policy.idle_max_wait_seconds, ema_burst_gap * 0.72))
        return ("continuation", min(eff_cont, policy.idle_max_wait_seconds))

    is_quick = (
        not has_image
        and (
            bool(_RE_QUICK_COMPLETE.match(text))
            or (len(text) >= 20 and bool(_RE_TERMINAL_PUNCT.search(text)))
        )
    )
    if is_quick:
        return ("quick", quick)

    return ("normal", normal)


def default_coalesce_prompt(batch: BurstBatch) -> str:
    """Build a concise, neutral LLM hint explaining that rapid messages were coalesced."""
    if batch.is_preempted_merge:
        lines = [
            f"[bubble_flow_merged count={batch.count}]",
            f"对方在短短几秒内连续发来了 {batch.count} 条内容（已按时间顺序合在当前用户消息中）。",
            "请将它们当作同一段完整发言统一理解，一次性自然回应最新核心意思，不要逐条编号回复。",
        ]
        return "\n".join(lines)
    lines = [
        f"[bubble_flow_coalesced count={batch.count}]",
        f"你在处理或刚发完上一句期间，对方紧接着按时间顺序补充发来了 {batch.count} 条内容（已在当前用户消息中按 1..{batch.count} 合并）。",
        "请顺着对方的最新意图一次性自然接住重点，不要对每条机械逐条复述。",
    ]
    if batch.last_reply_preview:
        lines.append(f"刚才你已经发出去的上一条内容是「{batch.last_reply_preview[:60]}」，不要再重复回答这句。")
    return "\n".join(lines)


def split_long_bubble_safely(
    text: str,
    *,
    soft_max_chars: int,
    max_parts: int,
    min_chars: int = 4,
) -> list[str]:
    """Split an overly long bubble into 2..max_parts chunks at safe top-level punctuation."""
    raw = str(text or "").strip()
    if not raw or soft_max_chars <= 0 or len(raw) <= soft_max_chars or max_parts <= 1:
        return [raw] if raw else []
    if "```" in raw:
        return [raw]

    urls: list[str] = []

    def _mask_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\x00U{len(urls) - 1}\x00"

    masked = _RE_URL.sub(_mask_url, raw)

    # Walk masked string tracking bracket/quote nesting depth.
    stack: list[str] = []
    strong_breaks: list[int] = []
    comma_breaks: list[int] = []
    for idx, ch in enumerate(masked):
        if stack and ch == stack[-1]:
            stack.pop()
            continue
        if ch in _QUOTE_OPEN_TO_CLOSE:
            # Avoid toggling quotes on English contractions like don't
            if ch == "'" and idx > 0 and idx + 1 < len(masked) and masked[idx - 1].isalpha() and masked[idx + 1].isalpha():
                continue
            stack.append(_QUOTE_OPEN_TO_CLOSE[ch])
            continue
        if stack:
            continue
        if ch in ("。", "！", "？", "!", "?", "；", ";", "\n"):
            strong_breaks.append(idx + 1)
        elif ch in ("，", ","):
            comma_breaks.append(idx + 1)

    def _unmask(s: str) -> str:
        out = s.strip()
        for uidx, url in enumerate(urls):
            out = out.replace(f"\x00U{uidx}\x00", url)
        return out

    def _pick_splits(breaks: list[int], min_seg: int) -> list[str] | None:
        if not breaks:
            return None
        parts: list[str] = []
        start = 0
        while len(parts) < max_parts - 1:
            rem = masked[start:].strip()
            if len(rem) <= soft_max_chars:
                break
            target = start + soft_max_chars
            candidates = [
                b for b in breaks
                if b > start + min_seg and len(masked[b:].strip()) >= min_seg
            ]
            if not candidates:
                break
            # Prefer break at or before soft_max_chars, closest to soft_max_chars
            before = [b for b in candidates if b <= target]
            chosen = before[-1] if before else candidates[0]
            seg = _unmask(masked[start:chosen])
            if not seg:
                break
            parts.append(seg)
            start = chosen
        tail = _unmask(masked[start:])
        if tail:
            parts.append(tail)
        return parts if len(parts) > 1 else None

    min_strong = max(4, min_chars)
    min_comma = max(8, min_chars)
    result = _pick_splits(strong_breaks, min_strong) or _pick_splits(comma_breaks, min_comma)
    return result if result else [raw]


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

    if policy.soft_max_bubble_chars > 0 and policy.max_bubbles > 1:
        expanded: list[str] = []
        for item in cleaned:
            expanded.extend(
                split_long_bubble_safely(
                    item,
                    soft_max_chars=policy.soft_max_bubble_chars,
                    max_parts=policy.max_bubbles,
                    min_chars=policy.min_bubble_chars,
                )
            )
        cleaned = expanded

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

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_flight_expired: Callable[[str, str, float], Awaitable[None]] | None = None,
    ) -> None:
        self._clock = clock
        self._on_flight_expired = on_flight_expired
        self._lock = asyncio.Lock()
        self._flights: dict[str, _SessionFlight] = {}
        self._closed = False

    def clock(self) -> float:
        """Current time on this coordinator's (possibly injected) clock."""
        return self._clock()

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

    def _release_locked(self, flight: _SessionFlight) -> None:
        """Drop the flight lock, stop its watchdog timer, and wake waiters."""
        task = flight.watchdog_task
        flight.watchdog_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        flight.in_flight = False
        flight.flight_token = ""
        flight.llm_started = False
        if flight.waiter_token:
            flight.settling = True
        flight.done_event.set()

    def _arm_watchdog_locked(
        self, flight: _SessionFlight, timeout_seconds: float
    ) -> None:
        """Start the single timer that owns this turn's watchdog deadline."""
        if flight.watchdog_task is not None and not flight.watchdog_task.done():
            flight.watchdog_task.cancel()
        flight.watchdog_task = None
        if not flight.in_flight or timeout_seconds <= 0:
            return
        flight.watchdog_task = asyncio.create_task(
            self._run_watchdog(flight.umo, flight.flight_token, timeout_seconds)
        )

    async def _run_watchdog(
        self, umo: str, token: str, timeout_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            return
        expired = False
        async with self._lock:
            flight = self._flights.get(umo)
            if flight is not None and flight.in_flight and flight.flight_token == token:
                self._release_locked(flight)
                flight.total_watchdog_resets += 1
                expired = True
        if expired and self._on_flight_expired is not None:
            try:
                await self._on_flight_expired(umo, token, timeout_seconds)
            except Exception:
                pass

    @staticmethod
    def _drop_waiter_locked(flight: _SessionFlight, waiter_token: str) -> None:
        if flight.waiter_token == waiter_token:
            flight.waiter_token = ""
            flight.settling = False
            flight.burst_items = []
            flight.preempt_carry_items = []
            flight.preempt_count = 0

    @staticmethod
    def _drop_idle_locked(flight: _SessionFlight, leader_token: str) -> None:
        if flight.idle_leader_token == leader_token:
            flight.idle_active = False
            flight.idle_leader_token = ""
            flight.idle_items = []

    async def _drop_waiter(self, umo: str, waiter_token: str) -> None:
        async with self._lock:
            flight = self._flights.get(umo)
            if flight is not None:
                self._drop_waiter_locked(flight, waiter_token)

    async def _drop_idle_leader(self, umo: str, leader_token: str) -> None:
        async with self._lock:
            flight = self._flights.get(umo)
            if flight is not None:
                self._drop_idle_locked(flight, leader_token)

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
            now = self._clock()
            flight.last_activity_at = now

            # Record EMA burst gap when consecutive messages arrive within 0.5s .. 12.0s
            if flight.last_burst_at > 0:
                gap = now - flight.last_burst_at
                if 0.5 <= gap <= 12.0:
                    flight.ema_burst_gap = gap if flight.ema_burst_gap <= 0.0 else (0.65 * flight.ema_burst_gap + 0.35 * gap)

            # Case 1: Session currently has an in-flight LLM/send turn (or is settling trailing bursts)
            if flight.in_flight or flight.settling:
                flight.burst_items.append(message)
                flight.total_coalesced += 1
                flight.last_burst_at = now
                flight.burst_updated.set()
                if not flight.waiter_token:
                    token = uuid4().hex
                    flight.waiter_token = token
                    return AdmissionResult(mode="wait_flush", token=token)
                return AdmissionResult(mode="swallowed")

            # Case 2: Existing idle debounce window absorbs follow-ups regardless of per-event debounce
            if flight.idle_active:
                flight.idle_items.append(message)
                flight.total_coalesced += 1
                flight.last_burst_at = now
                _, new_delay = classify_debounce_seconds(message, policy, recent_burst=True)
                if new_delay > flight.idle_debounce_seconds:
                    flight.idle_debounce_seconds = new_delay
                flight.idle_updated.set()
                return AdmissionResult(mode="swallowed")

            # Case 3: Idle, semantic adaptive debounce
            recent_burst = flight.last_burst_at > 0 and (now - flight.last_burst_at) <= 15.0
            tier, delay = classify_debounce_seconds(
                message,
                policy,
                recent_burst=recent_burst,
                ema_burst_gap=flight.ema_burst_gap,
            )
            if tier == "quick":
                flight.total_fast_passed += 1
            elif tier == "continuation":
                flight.total_continuation_waits += 1

            if delay > 0:
                token = uuid4().hex
                flight.idle_active = True
                flight.idle_leader_token = token
                flight.idle_items = [message]
                flight.idle_debounce_seconds = delay
                flight.idle_updated.clear()
                return AdmissionResult(
                    mode="wait_idle",
                    token=token,
                    debounce_tier=tier,
                    debounce_seconds=delay,
                )

            # Case 4: Idle, immediate execution
            token = uuid4().hex
            flight.in_flight = True
            flight.flight_token = token
            flight.started_at = now
            flight.llm_started = False
            flight.last_reply_preview = ""
            flight.last_reply_at = 0.0
            flight.active_items = [message]
            flight.preempt_carry_items = []
            flight.preempt_count = 0
            flight.done_event.clear()
            flight.total_runs += 1
            self._arm_watchdog_locked(flight, policy.max_flight_seconds)
            return AdmissionResult(
                mode="run_now",
                token=token,
                debounce_tier=tier,
                debounce_seconds=0.0,
            )

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
                init_delay = flight.idle_debounce_seconds or policy.idle_debounce_seconds

            max_cap = max(init_delay, policy.idle_max_wait_seconds)
            max_end = first_at + min(max(init_delay * 2.5, init_delay + 1.5), max_cap)
            while not self._closed:
                async with self._lock:
                    flight = self._get(umo)
                    if not flight.idle_active or flight.idle_leader_token != leader_token:
                        return ("", None)
                    last_at = flight.idle_items[-1].received_at if flight.idle_items else first_at
                    eff_delay = flight.idle_debounce_seconds or policy.idle_debounce_seconds
                    quiet_target = last_at + eff_delay
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
                    flight.idle_active = False
                    flight.idle_leader_token = ""
                    flight.idle_items = []
                    return ("", None)
                items = tuple(flight.idle_items)
                flight.idle_active = False
                flight.idle_leader_token = ""
                flight.idle_items = []
                flight.idle_debounce_seconds = 0.0

                token = uuid4().hex
                now = self._clock()
                flight.in_flight = True
                flight.flight_token = token
                flight.started_at = now
                flight.llm_started = False
                flight.last_reply_preview = ""
                flight.last_reply_at = 0.0
                flight.active_items = list(items)
                flight.preempt_carry_items = []
                flight.preempt_count = 0
                flight.done_event.clear()
                flight.total_runs += 1
                self._arm_watchdog_locked(flight, policy.max_flight_seconds)
                batch = BurstBatch(items=items, last_reply_preview="", flushed_at=now) if len(items) > 1 else None
                return (token, batch)
        except BaseException:
            # Roll back under the coordinator lock so a cancelled leader never
            # leaves a half-reset idle window behind.
            await self._drop_idle_leader(umo, leader_token)
            raise

    def will_preempt(
        self,
        umo: str,
        token: str,
        *,
        policy: BubblePolicy,
        channel_type: str = "",
    ) -> bool:
        """Non-mutating check of whether try_preempt_or_finish would preempt this turn."""
        flight = self._flights.get(umo)
        if flight is None or not flight.in_flight or flight.flight_token != token:
            return False
        if not policy.preempt_before_send:
            return False
        if channel_type == "group" and not policy.enable_preempt_in_group:
            return False
        return bool(flight.burst_items) and flight.preempt_count < policy.max_preempt_rounds

    async def try_preempt_or_finish(
        self,
        umo: str,
        token: str,
        *,
        policy: BubblePolicy,
        channel_type: str = "",
    ) -> PreemptDecision:
        """Silently discard an unpublished turn when mid-flight follow-ups arrived."""
        async with self._lock:
            flight = self._flights.get(umo)
            if flight is None or not flight.in_flight or flight.flight_token != token:
                return PreemptDecision(preempt=False)
            if not policy.preempt_before_send:
                return PreemptDecision(preempt=False)
            if channel_type == "group" and not policy.enable_preempt_in_group:
                return PreemptDecision(preempt=False)
            if not flight.burst_items or flight.preempt_count >= policy.max_preempt_rounds:
                return PreemptDecision(preempt=False)

            flight.preempt_carry_items = [*flight.preempt_carry_items, *flight.active_items]
            flight.active_items = []
            flight.preempt_count += 1
            flight.total_preempted += 1
            flight.last_reply_preview = ""
            flight.last_reply_at = 0.0
            merged_count = len(flight.preempt_carry_items) + len(flight.burst_items)
            preempt_round = flight.preempt_count
            self._release_locked(flight)
            return PreemptDecision(
                preempt=True,
                merged_count=merged_count,
                preempt_round=preempt_round,
            )

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
                    if not flight.in_flight:
                        break
                    # Boundary wait only: this turn's own watchdog is the single
                    # authority on when a stalled flight gets released.
                    done_event = flight.done_event
                await done_event.wait()

            if self._closed:
                await self._drop_waiter(umo, waiter_token)
                return ("", None)

            # Brief tail settling window after the prior reply finished (or grace window after silent preemption)
            async with self._lock:
                _f0 = self._flights.get(umo)
                has_carry = bool(_f0 and _f0.preempt_carry_items)
            quiet_win = (
                max(policy.trailing_quiet_seconds, policy.preempt_grace_seconds)
                if has_carry
                else policy.trailing_quiet_seconds
            )
            max_tail_cap = max(policy.trailing_max_wait_seconds, quiet_win)
            max_tail_end = self._clock() + max(0.0, max_tail_cap)
            while quiet_win > 0 and not self._closed:
                async with self._lock:
                    flight = self._get(umo)
                    if flight.waiter_token != waiter_token:
                        return ("", None)
                    if not flight.burst_items:
                        break
                    now = self._clock()
                    last_at = max(item.received_at for item in flight.burst_items)
                    remaining = min(last_at + quiet_win, max_tail_end) - now
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
                    self._drop_waiter_locked(flight, waiter_token)
                    return ("", None)
                items = tuple(flight.burst_items)
                carry = list(flight.preempt_carry_items)
                flight.burst_items = []
                flight.preempt_carry_items = []
                flight.waiter_token = ""
                flight.settling = False
                if not items or flight.in_flight:
                    return ("", None)

                now = self._clock()
                if carry:
                    combined = (*carry, *items)
                    is_preempted = True
                    preview = ""
                else:
                    combined = items
                    is_preempted = False
                    flight.preempt_count = 0
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
                flight.active_items = list(combined)
                flight.done_event.clear()
                flight.total_runs += 1
                self._arm_watchdog_locked(flight, policy.max_flight_seconds)
                batch = BurstBatch(
                    items=combined,
                    last_reply_preview=preview,
                    flushed_at=now,
                    is_preempted_merge=is_preempted,
                )
                return (new_token, batch)
        except BaseException:
            await self._drop_waiter(umo, waiter_token)
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
            self._release_locked(flight)
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
                "settling": False,
                "idle_active": False,
                "has_waiter": False,
                "watchdog_armed": False,
                "pending_count": 0,
                "flight_age_seconds": 0.0,
                "last_reply_preview": "",
                "total_runs": 0,
                "total_coalesced": 0,
                "total_collapsed": 0,
                "total_preempted": 0,
                "total_fast_passed": 0,
                "total_continuation_waits": 0,
                "preempt_count": 0,
                "total_watchdog_resets": 0,
            }
        return {
            "umo": umo,
            "in_flight": flight.in_flight,
            "settling": flight.settling,
            "idle_active": flight.idle_active,
            "has_waiter": bool(flight.waiter_token),
            "watchdog_armed": bool(
                flight.watchdog_task and not flight.watchdog_task.done()
            ),
            "pending_count": len(flight.burst_items),
            "flight_age_seconds": round(max(0.0, self._clock() - flight.started_at), 3) if flight.in_flight else 0.0,
            "last_reply_preview": flight.last_reply_preview,
            "total_runs": flight.total_runs,
            "total_coalesced": flight.total_coalesced,
            "total_collapsed": flight.total_collapsed,
            "total_preempted": flight.total_preempted,
            "total_fast_passed": flight.total_fast_passed,
            "total_continuation_waits": flight.total_continuation_waits,
            "preempt_count": flight.preempt_count,
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
            "total_preempted": sum(f.total_preempted for f in self._flights.values()),
            "total_fast_passed": sum(f.total_fast_passed for f in self._flights.values()),
            "total_continuation_waits": sum(f.total_continuation_waits for f in self._flights.values()),
            "total_watchdog_resets": sum(f.total_watchdog_resets for f in self._flights.values()),
        }

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            for flight in self._flights.values():
                self._release_locked(flight)
                flight.settling = False
                flight.idle_active = False
                flight.burst_items.clear()
                flight.idle_items.clear()
                flight.active_items.clear()
                flight.preempt_carry_items.clear()
                flight.preempt_count = 0
                flight.waiter_token = ""
                flight.idle_leader_token = ""
                flight.done_event.set()
                flight.burst_updated.set()
                flight.idle_updated.set()
