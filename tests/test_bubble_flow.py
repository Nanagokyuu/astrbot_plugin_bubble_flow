from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_PARENT = REPO_ROOT.parent
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

if REPO_ROOT.name != "astrbot_plugin_bubble_flow":
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_bubble_flow",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["astrbot_plugin_bubble_flow"] = _mod
    _spec.loader.exec_module(_mod)

from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.message_event_result import (
    EventResultType,
    MessageEventResult,
    ResultContentType,
)

from astrbot_plugin_bubble_flow import (
    API_VERSION,
    PLUGIN_ID,
    BubbleFlowAPI,
    BubblePolicy,
    BurstBatch,
    CapturedMessage,
    SessionFlightCoordinator,
    default_coalesce_prompt,
    discover_bubble_flow_api,
    plan_outgoing_bubbles,
)
from astrbot_plugin_bubble_flow.main import BubbleFlowPlugin


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> float:
        self.now += delta
        return self.now


class _FakeEvent:
    def __init__(
        self,
        umo: str,
        text: str = "",
        *,
        sender_id: str = "10001",
        channel: str = "private",
        components: list[Any] | None = None,
        stopped: bool = False,
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self.sender_id = sender_id
        self._channel = channel
        self._stopped = stopped
        self._extras: dict[str, Any] = {}
        self._result: MessageEventResult | None = None
        if components is None:
            comps = [Plain(text)] if text else []
        else:
            comps = list(components)
        self.message_obj = SimpleNamespace(message=comps)

    def get_message_str(self) -> str:
        return self.message_str

    def get_sender_id(self) -> str:
        return self.sender_id

    def is_private_chat(self) -> bool:
        return self._channel == "private"

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extras.get(key, default)

    def set_result(self, result: MessageEventResult) -> None:
        self._result = result

    def get_result(self) -> MessageEventResult | None:
        return self._result


class _FakeContext:
    def __init__(self, stars: list[Any]) -> None:
        self._stars = stars

    def get_all_stars(self) -> list[Any]:
        return list(self._stars)


def test_policy_from_nested_schema_mapping() -> None:
    policy = BubblePolicy.from_mapping(
        {
            "enabled": True,
            "channels": {"enable_private": True, "enable_group": True},
            "coalesce": {
                "idle_debounce_seconds": 1.2,
                "trailing_quiet_seconds": 0.4,
                "trailing_max_wait_seconds": 1.5,
                "max_flight_seconds": 30.0,
                "coalesce_images": False,
            },
            "bubbles": {
                "max_bubbles": 3,
                "collapse_on_interrupt": False,
                "min_bubble_chars": 6,
                "dedup_identical_bubbles": False,
                "bubble_sep": " ",
            },
            "prompts": {"inject_coalesce_hint": False},
        }
    )
    assert policy.enabled is True
    assert policy.enable_group is True
    assert policy.idle_debounce_seconds == pytest.approx(1.2)
    assert policy.trailing_quiet_seconds == pytest.approx(0.4)
    assert policy.trailing_max_wait_seconds == pytest.approx(1.5)
    assert policy.max_flight_seconds == pytest.approx(30.0)
    assert policy.coalesce_images is False
    assert policy.max_bubbles == 3
    assert policy.collapse_on_interrupt is False
    assert policy.min_bubble_chars == 6
    assert policy.dedup_identical_bubbles is False
    assert policy.bubble_sep == " "
    assert policy.inject_coalesce_hint is False


def test_coordinator_midflight_burst_coalescing_order_and_images() -> None:
    async def _run() -> None:
        coord = SessionFlightCoordinator()
        policy = BubblePolicy(
            trailing_quiet_seconds=0.04,
            trailing_max_wait_seconds=0.25,
        )
        umo = "aiocqhttp:FriendMessage:1001"

        try:
            adm1 = await coord.admit_or_coalesce(
                umo, CapturedMessage(text="在吗", received_at=1.0), policy=policy
            )
            assert adm1.mode == "run_now"
            await coord.mark_llm_started(umo, adm1.token)

            img = Image.fromURL("https://example.com/a.jpg")
            adm2 = await coord.admit_or_coalesce(
                umo,
                CapturedMessage(text="你看这个", received_at=1.1),
                policy=policy,
            )
            adm3 = await coord.admit_or_coalesce(
                umo,
                CapturedMessage(text="", received_at=1.2, image_components=(img,)),
                policy=policy,
            )
            adm4 = await coord.admit_or_coalesce(
                umo,
                CapturedMessage(
                    text="对就是这张",
                    received_at=1.3,
                    reply_preview="上一句引用",
                ),
                policy=policy,
            )

            assert adm2.mode == "wait_flush"
            assert adm3.mode == "swallowed"
            assert adm4.mode == "swallowed"
            assert coord.has_pending_interrupt(umo) is True

            waiter_task = asyncio.create_task(
                coord.wait_and_collect_flush(umo, adm2.token, policy=policy)
            )
            await asyncio.sleep(0.01)
            assert not waiter_task.done()

            await coord.record_reply_preview(umo, adm1.token, "在的", collapsed=True)
            assert await coord.release_flight(umo, adm1.token) is True

            token2, batch2 = await waiter_task
            assert token2
            assert batch2 is not None
            assert batch2.count == 3
            assert batch2.merged_text == "1. 你看这个\n2. [图片]\n3. [引用:上一句引用] 对就是这张"
            assert batch2.merged_images == (img,)
            assert batch2.last_reply_preview == "在的"

            hint = default_coalesce_prompt(batch2)
            assert "3" in hint
            assert "在的" in hint
            stats = coord.stats()
            assert stats["total_coalesced"] >= 2
            assert stats["total_collapsed"] == 1
            assert await coord.release_flight(umo, token2) is True
            assert coord.snapshot(umo)["in_flight"] is False
        finally:
            await coord.close()

    asyncio.run(_run())


def test_coordinator_standalone_idle_debounce_merges_burst() -> None:
    async def _run() -> None:
        coord = SessionFlightCoordinator()
        policy = BubblePolicy(idle_debounce_seconds=0.05)
        umo = "aiocqhttp:FriendMessage:2002"

        try:
            adm1 = await coord.admit_or_coalesce(
                umo, CapturedMessage(text="第一句", received_at=1.0), policy=policy
            )
            assert adm1.mode == "wait_idle"
            adm2 = await coord.admit_or_coalesce(
                umo, CapturedMessage(text="第二句", received_at=1.01), policy=policy
            )
            assert adm2.mode == "swallowed"

            promoted_token, batch = await coord.wait_idle_and_promote(
                umo, adm1.token, policy=policy
            )
            assert promoted_token
            assert batch is not None
            assert batch.count == 2
            assert batch.merged_text == "1. 第一句\n2. 第二句"
            assert await coord.release_flight(umo, promoted_token) is True
        finally:
            await coord.close()

    asyncio.run(_run())


def test_plan_outgoing_bubbles_shapes_dedups_and_collapses() -> None:
    policy = BubblePolicy(
        max_bubbles=2,
        min_bubble_chars=4,
        dedup_identical_bubbles=True,
        collapse_on_interrupt=True,
        bubble_sep=" / ",
    )

    shaped = plan_outgoing_bubbles(
        ["嗯", "我看看这个问题", "我看看这个问题", "等会告诉你结果", "还有补充"],
        policy=policy,
        interrupted=False,
    )
    assert len(shaped) == 2
    assert "嗯" in shaped[0]
    assert "还有补充" in shaped[1]
    assert shaped[0].count("我看看这个问题") == 1

    collapsed = plan_outgoing_bubbles(
        ["第一段内容比较长", "第二段内容比较长", "第三段内容比较长"],
        policy=policy,
        interrupted=True,
    )
    assert collapsed == ["第一段内容比较长"]

    collapsed_short = plan_outgoing_bubbles(
        ["嗯", "第二段内容比较长", "第三段内容比较长"],
        policy=policy,
        interrupted=True,
    )
    assert collapsed_short == ["嗯 / 第二段内容比较长"]

    policy_join = policy.with_overrides({"interrupt_keep_first_only": False})
    collapsed_join = plan_outgoing_bubbles(
        ["第一段内容比较长", "第二段内容比较长", "第三段内容比较长"],
        policy=policy_join,
        interrupted=True,
    )
    assert collapsed_join == ["第一段内容比较长 / 第二段内容比较长 / 第三段内容比较长"]


def test_extension_api_discovery_resolvers_filters_and_isolation() -> None:
    async def _run() -> None:
        plugin = BubbleFlowPlugin(
            _FakeContext([]),
            {
                "enabled": True,
                "coalesce": {"idle_debounce_seconds": 0.0},
                "bubbles": {"max_bubbles": 2, "min_bubble_chars": 2},
            },
        )
        fake_ctx = _FakeContext(
            [
                SimpleNamespace(name="other_plugin", activated=True, star_cls=object()),
                SimpleNamespace(name=PLUGIN_ID, activated=False, star_cls=plugin),
                SimpleNamespace(name=PLUGIN_ID, activated=True, star_cls=plugin),
            ]
        )
        api = discover_bubble_flow_api(fake_ctx)
        assert isinstance(api, BubbleFlowAPI)
        assert api.api_version == API_VERSION

        # Priority ordering + exception isolation in policy resolver.
        tok_bad = api.register_policy_resolver(
            "bad.resolver",
            lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")),
            priority=10,
        )
        tok_good = api.register_policy_resolver(
            "vip.resolver",
            lambda ctx: {"max_bubbles": 4} if ctx.sender_id == "vip" else None,
            priority=50,
        )
        api.set_session_policy("umo:1", bubble_sep=" | ")

        resolved = api.get_session_policy("umo:1", sender_id="vip", channel_type="private")
        assert resolved.max_bubbles == 4
        assert resolved.bubble_sep == " | "

        # Bubble filter + exception isolation + unregister.
        api.register_bubble_filter(
            "bad.filter",
            lambda bubbles, ctx: (_ for _ in ()).throw(ValueError("bad filter")),
            priority=10,
        )
        tok_trim = api.register_bubble_filter(
            "trim.filter",
            lambda bubbles, ctx: [b.rstrip("。") for b in bubbles],
            priority=20,
        )
        filtered = api.apply_bubble_filters(
            ["好啊。", "没问题。"],
            ctx=SimpleNamespace(),  # type: ignore[arg-type]
        )
        assert filtered == ["好啊", "没问题"]

        assert api.unregister(tok_bad) is True
        assert api.unregister(tok_good) is True
        assert api.unregister(tok_trim) is True
        assert api.unregister("nonexistent") is False

        # Suppress one coalesce check.
        evt = _FakeEvent("umo:suppress", "ping")
        api.suppress_next_coalesce(evt)
        assert api.consume_suppressed(evt) is True
        assert api.consume_suppressed(evt) is False

        # Event listener receives async + sync events cleanly even if one fails.
        seen: list[tuple[str, dict[str, Any]]] = []
        api.register_event_listener(
            "bad.listener", lambda name, payload: (_ for _ in ()).throw(RuntimeError("x"))
        )

        async def _observer(name: str, payload: dict[str, Any]) -> None:
            seen.append((name, payload))

        api.register_event_listener("good.observer", _observer)
        await api.emit_event("custom.ping", {"ok": True})
        assert seen == [("custom.ping", {"ok": True})]

        await plugin.terminate()

    asyncio.run(_run())


def test_plugin_hooks_end_to_end_coalesce_collapse_and_empty_release() -> None:
    async def _run() -> None:
        plugin = BubbleFlowPlugin(
            _FakeContext([]),
            {
                "enabled": True,
                "coalesce": {
                    "idle_debounce_seconds": 0.0,
                    "trailing_quiet_seconds": 0.03,
                    "trailing_max_wait_seconds": 0.2,
                    "max_flight_seconds": 5.0,
                    "coalesce_images": True,
                },
                "bubbles": {
                    "max_bubbles": 2,
                    "collapse_on_interrupt": True,
                    "min_bubble_chars": 2,
                    "bubble_sep": " ",
                },
                "prompts": {"inject_coalesce_hint": True},
            },
        )
        observed_events: list[str] = []
        plugin.extension_api.register_event_listener(
            "trace", lambda name, payload: observed_events.append(name)
        )
        plugin.extension_api.register_coalesce_formatter(
            "custom.fmt",
            lambda batch, ctx: f"[custom-merged:{batch.count}]",
        )

        umo = "aiocqhttp:FriendMessage:9988"

        try:
            # Upstream stopped event is ignored cleanly.
            stopped_evt = _FakeEvent(umo, "ignored", stopped=True)
            await plugin.handle_interleave_intake(stopped_evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False

            # Turn 1 starts normally.
            e1 = _FakeEvent(umo, "在忙吗")
            await plugin.handle_interleave_intake(e1)
            assert not e1.is_stopped()
            req1 = ProviderRequest(prompt="在忙吗", contexts=[], extra_user_content_parts=[])
            await plugin.inject_coalesce_context(e1, req1)

            # Mid-flight burst arrives: text + image + follow-up text.
            img = Image.fromURL("https://example.com/pic.png")
            e2 = _FakeEvent(umo, "你看这张图", components=[Plain("你看这张图"), img])
            e3 = _FakeEvent(
                umo,
                "是不是很像",
                components=[Plain("是不是很像"), Reply(id="m1", message_str="原图说明")],
            )

            e2_task = asyncio.create_task(plugin.handle_interleave_intake(e2))
            await asyncio.sleep(0.01)
            await plugin.handle_interleave_intake(e3)

            assert e3.is_stopped() is True
            assert plugin.extension_api.has_pending_interrupt(umo) is True

            # Turn 1 produces 3 bubbles -> collapsed into 1 because e2/e3 interrupted.
            res1 = MessageEventResult(
                chain=[Reply(id="0"), Plain("我在看啦\n刚才卡了一下\n现在看看你发的图")],
                result_type=EventResultType.CONTINUE,
                result_content_type=ResultContentType.LLM_RESULT,
            )
            e1.set_result(res1)
            await plugin.shape_outgoing_bubbles(e1)
            plain_parts = [c for c in res1.chain if isinstance(c, Plain)]
            assert len(plain_parts) == 1
            assert "我在看啦" == plain_parts[0].text
            assert isinstance(res1.chain[0], Reply)

            # Finish sending Turn 1 -> unblocks e2 with merged e2+e3.
            await plugin.on_after_message_sent(e1)
            await e2_task

            assert not e2.is_stopped()
            assert e2.message_str == "1. 你看这张图\n2. [引用:原图说明] 是不是很像"
            assert any(isinstance(c, Image) for c in e2.message_obj.message)

            req2 = ProviderRequest(
                prompt=e2.message_str,
                image_urls=[],
                contexts=[],
                extra_user_content_parts=[],
            )
            await plugin.inject_coalesce_context(e2, req2)
            assert "https://example.com/pic.png" in req2.image_urls
            assert any(
                getattr(part, "text", "") == "[custom-merged:2]"
                for part in req2.extra_user_content_parts
            )

            # Empty result on Turn 2 releases lock immediately in shape_outgoing_bubbles.
            e2.set_result(MessageEventResult(chain=[]))
            await plugin.shape_outgoing_bubbles(e2)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
            assert "bubbles.collapsed" in observed_events
            assert "burst.flushed" in observed_events
            assert "turn.finished" in observed_events

            # Force release recovers stuck slots cleanly.
            e4 = _FakeEvent(umo, "卡住测试")
            await plugin.handle_interleave_intake(e4)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is True
            assert await plugin.extension_api.force_release(umo) is True
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
            # Empty-text non-media event never acquires flight lock
            blank_evt = _FakeEvent(umo, "")
            await plugin.handle_interleave_intake(blank_evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
        finally:
            await plugin.terminate()

    asyncio.run(_run())


def test_plugin_watchdog_auto_releases_stalled_turn() -> None:
    async def _run() -> None:
        plugin = BubbleFlowPlugin(
            _FakeContext([]),
            {"enabled": True},
        )
        plugin.extension_api.register_policy_resolver(
            "fast.watchdog",
            lambda ctx: BubblePolicy(max_flight_seconds=0.05),
        )
        events: list[str] = []
        plugin.extension_api.register_event_listener(
            "watch", lambda name, payload: events.append(name)
        )
        umo = "aiocqhttp:FriendMessage:7777"
        try:
            evt = _FakeEvent(umo, "hello")
            await plugin.handle_interleave_intake(evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is True
            await asyncio.sleep(0.12)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
            assert "turn.watchdog_released" in events
            # Empty-text non-media event never acquires flight lock
            blank_evt = _FakeEvent(umo, "")
            await plugin.handle_interleave_intake(blank_evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
        finally:
            await plugin.terminate()

    asyncio.run(_run())


def test_intake_priority_and_upstream_suppressed_events_bypassed() -> None:
    from astrbot.core.star.star_handler import star_handlers_registry

    matched = [
        h
        for h in star_handlers_registry
        if getattr(h, "handler_name", "") == "handle_interleave_intake"
        and (
            "main" in getattr(h, "handler_module_path", "")
            or "bubble_flow" in getattr(h, "handler_module_path", "")
        )
    ]
    assert matched, "Expected handle_interleave_intake handler to be registered"
    assert matched[-1].extras_configs.get("priority", 0) <= -240000

    async def _run() -> None:
        plugin = BubbleFlowPlugin(_FakeContext([]), {"enabled": True})
        umo = "aiocqhttp:FriendMessage:8888"
        try:
            # Event suppressed by an upstream debouncer (call_llm=True in AstrBot means skip default LLM)
            suppressed_evt = _FakeEvent(umo, "merged follow-up")
            suppressed_evt.call_llm = True  # type: ignore[attr-defined]
            await plugin.handle_interleave_intake(suppressed_evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False

            # Event already answered by upstream plugin (_has_send_oper=True)
            handled_evt = _FakeEvent(umo, "already handled")
            handled_evt._has_send_oper = True  # type: ignore[attr-defined]
            await plugin.handle_interleave_intake(handled_evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
            # Empty-text non-media event never acquires flight lock
            blank_evt = _FakeEvent(umo, "")
            await plugin.handle_interleave_intake(blank_evt)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
        finally:
            await plugin.terminate()

    asyncio.run(_run())


def test_non_wake_group_chat_and_early_result_release_never_stall() -> None:
    async def _run() -> None:
        plugin = BubbleFlowPlugin(
            _FakeContext([]),
            {"enabled": True, "channels": {"enable_private": True, "enable_group": True}},
        )
        group_umo = "elle:GroupMessage:1091795017"
        priv_umo = "elle:FriendMessage:577866846"
        try:
            # 1. Non-wake casual group chatter (is_at_or_wake_command=False) must NOT lock group flight
            casual = _FakeEvent(group_umo, "有人打音游吗", channel="group")
            casual.is_at_or_wake_command = False  # type: ignore[attr-defined]
            await plugin.handle_interleave_intake(casual)
            assert plugin.extension_api.get_session_state(group_umo)["in_flight"] is False

            # 2. Woken group message (is_at_or_wake_command=True) DOES acquire flight
            woken = _FakeEvent(group_umo, "在干嘛", channel="group")
            woken.is_at_or_wake_command = True  # type: ignore[attr-defined]
            await plugin.handle_interleave_intake(woken)
            assert plugin.extension_api.get_session_state(group_umo)["in_flight"] is True
            # Non-model command result in shape_outgoing_bubbles releases flight immediately
            cmd_res = MessageEventResult().message("status ok")
            cmd_res.result_content_type = ResultContentType.GENERAL_RESULT
            woken.set_result(cmd_res)
            await plugin.shape_outgoing_bubbles(woken)
            assert plugin.extension_api.get_session_state(group_umo)["in_flight"] is False

            # 3. Non-text image-only LLM result releases flight instead of waiting for watchdog
            evt_img = _FakeEvent(priv_umo, "发图")
            await plugin.handle_interleave_intake(evt_img)
            assert plugin.extension_api.get_session_state(priv_umo)["in_flight"] is True
            await plugin.inject_coalesce_context(evt_img, ProviderRequest(prompt="发图"))
            img_res = MessageEventResult()
            img_res.chain = [Image.fromFileSystem(__file__)]
            img_res.result_content_type = ResultContentType.LLM_RESULT
            evt_img.set_result(img_res)
            await plugin.shape_outgoing_bubbles(evt_img)
            assert plugin.extension_api.get_session_state(priv_umo)["in_flight"] is False
        finally:
            await plugin.terminate()

    asyncio.run(_run())


def test_followup_after_interrupt_narrows_to_single_bubble() -> None:
    async def _run() -> None:
        plugin = BubbleFlowPlugin(
            _FakeContext([]),
            {
                "enabled": True,
                "channels": {"enable_private": True, "enable_group": False},
                "coalesce": {"trailing_quiet_seconds": 0.0, "trailing_max_wait_seconds": 0.0},
                "bubbles": {"max_bubbles": 2, "collapse_on_interrupt": True, "bubble_sep": " "},
            },
        )
        umo = "elle:FriendMessage:577866846"
        try:
            e1 = _FakeEvent(umo, "我确实有玩")
            await plugin.handle_interleave_intake(e1)
            await plugin.inject_coalesce_context(e1, ProviderRequest(prompt="我确实有玩"))

            e2 = _FakeEvent(umo, "但玩的不多")
            task2 = asyncio.create_task(plugin.handle_interleave_intake(e2))
            await asyncio.sleep(0.02)

            res1 = MessageEventResult().message("难怪一眼就认出来了\n那你平时基建搞得咋样？")
            res1.result_content_type = ResultContentType.LLM_RESULT
            e1.set_result(res1)
            await plugin.shape_outgoing_bubbles(e1)
            assert [c.text for c in res1.chain if isinstance(c, Plain)] == ["难怪一眼就认出来了"]
            await plugin.on_after_message_sent(e1)

            await task2
            req2 = ProviderRequest(prompt="但玩的不多", contexts=[], extra_user_content_parts=[])
            await plugin.inject_coalesce_context(e2, req2)
            res2 = MessageEventResult().message("慢慢玩也挺好的\n平时摸鱼推剧情就够了")
            res2.result_content_type = ResultContentType.LLM_RESULT
            e2.set_result(res2)
            await plugin.shape_outgoing_bubbles(e2)
            assert [c.text for c in res2.chain if isinstance(c, Plain)] == [
                "慢慢玩也挺好的 平时摸鱼推剧情就够了"
            ]
            await plugin.on_after_message_sent(e2)
        finally:
            await plugin.terminate()

    asyncio.run(_run())


def test_clean_plain_bubble_text_multi_sender_preview_expiry_and_eviction(tmp_path: Path) -> None:
    from astrbot_plugin_bubble_flow.coordinator import (
        BurstBatch,
        CapturedMessage,
        clean_plain_bubble_text,
    )

    # 1. Markdown + period + URL preservation
    raw = "**重点**：先看 https://example.com/a.html 。然后继续。"
    cleaned = clean_plain_bubble_text(
        raw,
        strip_markdown=True,
        strip_trailing_period=True,
        split_on_cn_period=True,
    )
    assert "**" not in cleaned
    assert "https://example.com/a.html" in cleaned
    assert cleaned == "重点：先看 https://example.com/a.html\n然后继续"

    # 2. Multi-sender attribution in merged_text
    batch_multi = BurstBatch(
        items=(
            CapturedMessage(text="第一句", received_at=1.0, sender_id="1001"),
            CapturedMessage(text="第二句", received_at=1.5, sender_id="1002"),
        )
    )
    assert batch_multi.merged_text == "1. [1001] 第一句\n2. [1002] 第二句"

    batch_single = BurstBatch(
        items=(
            CapturedMessage(text="第一句", received_at=1.0, sender_id="1001"),
            CapturedMessage(text="第二句", received_at=1.5, sender_id="1001"),
        )
    )
    assert batch_single.merged_text == "1. 第一句\n2. 第二句"

    # 3. Stale last_reply_preview must not leak across non-text/idle turns + lazy eviction
    async def _run() -> None:
        clk = _FakeClock(1000.0)
        coord = SessionFlightCoordinator(clock=clk)
        pol = BubblePolicy(
            enabled=True,
            trailing_quiet_seconds=0.0,
            trailing_max_wait_seconds=0.0,
            max_flight_seconds=30.0,
        )
        umo = "elle:FriendMessage:577866846"
        try:
            # Turn 1 records a reply preview
            a1 = await coord.admit_or_coalesce(umo, CapturedMessage("hi", clk()), policy=pol)
            await coord.record_reply_preview(umo, a1.token, "旧回复内容")
            await coord.release_flight(umo, a1.token)

            # Later Turn 2 (image-only, never calls record_reply_preview) interrupted by Turn 3
            clk.advance(500.0)
            a2 = await coord.admit_or_coalesce(umo, CapturedMessage("发图", clk()), policy=pol)
            t3 = asyncio.create_task(
                coord.wait_and_collect_flush(
                    umo,
                    (await coord.admit_or_coalesce(umo, CapturedMessage("补充", clk()), policy=pol)).token,
                    policy=pol,
                )
            )
            await asyncio.sleep(0.01)
            await coord.release_flight(umo, a2.token)
            _, batch3 = await t3
            assert batch3 is not None
            assert batch3.last_reply_preview == ""

            # Lazy eviction of >128 stale idle flights after 24h
            for i in range(135):
                u = f"elle:FriendMessage:stale_{i}"
                adm = await coord.admit_or_coalesce(u, CapturedMessage("ping", clk()), policy=pol)
                await coord.release_flight(u, adm.token)
            assert len(coord._flights) >= 135
            clk.advance(90000.0)
            adm_new = await coord.admit_or_coalesce("elle:FriendMessage:fresh", CapturedMessage("new", clk()), policy=pol)
            await coord.release_flight("elle:FriendMessage:fresh", adm_new.token)
            assert len(coord._flights) < 10

            # Normalized local path dedup in inject_coalesce_context
            pic = tmp_path / "pic.png"
            pic.write_bytes(b"img")
            plugin = BubbleFlowPlugin(_FakeContext([]), {"enabled": True})
            try:
                ev = _FakeEvent(umo, "看图", components=[Plain("看图"), Image.fromFileSystem(str(pic))])
                await plugin.handle_interleave_intake(ev)
                ev2 = _FakeEvent(umo, "还有", components=[Plain("还有"), Image.fromFileSystem(str(tmp_path / "." / "pic.png"))])
                t_wait = asyncio.create_task(plugin.handle_interleave_intake(ev2))
                await asyncio.sleep(0.01)
                await plugin._release_turn_slot(umo, ev.get_extra("_bubble_flow_token", ""))
                await t_wait
                req = ProviderRequest(prompt="还有", image_urls=[str(pic.resolve())], contexts=[], extra_user_content_parts=[])
                await plugin.inject_coalesce_context(ev2, req)
                assert len(req.image_urls) == 1
            finally:
                await plugin.terminate()
        finally:
            await coord.close()

    asyncio.run(_run())


def test_settling_window_race_cancellation_cleanup_and_streaming_release() -> None:
    async def _run() -> None:
        # 1. Trailing settling window must absorb m3 into m2 instead of concurrent run_now
        clk = _FakeClock(2000.0)
        coord = SessionFlightCoordinator(clock=clk)
        pol = BubblePolicy(
            enabled=True,
            trailing_quiet_seconds=0.05,
            trailing_max_wait_seconds=0.2,
            max_flight_seconds=30.0,
        )
        umo = "elle:FriendMessage:577866846"
        try:
            a1 = await coord.admit_or_coalesce(umo, CapturedMessage("m1", clk()), policy=pol)
            assert a1.mode == "run_now"
            a2 = await coord.admit_or_coalesce(umo, CapturedMessage("m2", clk()), policy=pol)
            assert a2.mode == "wait_flush"
            waiter_task = asyncio.create_task(coord.wait_and_collect_flush(umo, a2.token, policy=pol))
            await asyncio.sleep(0.01)

            # Release m1 -> enters settling=True while waiter sleeps in trailing window
            assert await coord.release_flight(umo, a1.token) is True
            await asyncio.sleep(0.01)
            a3 = await coord.admit_or_coalesce(umo, CapturedMessage("m3", clk()), policy=pol)
            assert a3.mode == "swallowed"

            tok2, batch2 = await waiter_task
            assert tok2 != ""
            assert batch2 is not None and batch2.count == 2
            assert batch2.merged_text == "1. m2\n2. m3"
            assert coord.snapshot(umo)["total_runs"] == 2
            assert await coord.release_flight(umo, tok2) is True
            assert coord.snapshot(umo)["in_flight"] is False

            # 2. Cancelled waiter cleans up waiter_token & settling so subsequent messages never swallow forever
            b1 = await coord.admit_or_coalesce(umo, CapturedMessage("x1", clk()), policy=pol)
            b2 = await coord.admit_or_coalesce(umo, CapturedMessage("x2", clk()), policy=pol)
            cancel_task = asyncio.create_task(coord.wait_and_collect_flush(umo, b2.token, policy=pol))
            await asyncio.sleep(0.01)
            cancel_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancel_task
            assert await coord.release_flight(umo, b1.token) is True
            b3 = await coord.admit_or_coalesce(umo, CapturedMessage("x3", clk()), policy=pol)
            assert b3.mode == "run_now"
            assert await coord.release_flight(umo, b3.token) is True
        finally:
            await coord.close()

        # 3. Streaming responses keep lock on STREAMING_RESULT and release on STREAMING_FINISH
        plugin = BubbleFlowPlugin(_FakeContext([]), {"enabled": True})
        try:
            ev = _FakeEvent(umo, "流式测试")
            await plugin.handle_interleave_intake(ev)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is True

            res_stream = MessageEventResult()
            res_stream.result_content_type = ResultContentType.STREAMING_RESULT
            ev.set_result(res_stream)
            await plugin.shape_outgoing_bubbles(ev)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is True

            res_done = MessageEventResult()
            res_done.result_content_type = ResultContentType.STREAMING_FINISH
            ev.set_result(res_done)
            await plugin.shape_outgoing_bubbles(ev)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
        finally:
            await plugin.terminate()

    asyncio.run(_run())


def test_idle_preemption_url_cn_punct_dunder_and_tokenless_after_sent() -> None:
    from astrbot_plugin_bubble_flow import clean_plain_bubble_text

    # 1. URL followed immediately by Chinese period & dunder identifiers
    assert (
        clean_plain_bubble_text(
            "看这个 https://example.com/a。很好笑",
            strip_markdown=True,
            strip_trailing_period=True,
            split_on_cn_period=True,
        )
        == "看这个 https://example.com/a\n很好笑"
    )
    assert (
        clean_plain_bubble_text(
            "看这个 https://example.com/a。",
            strip_trailing_period=True,
        )
        == "看这个 https://example.com/a"
    )
    assert (
        clean_plain_bubble_text(
            "修改 __init__ 和 __main__ 以及 **加粗**",
            strip_markdown=True,
        )
        == "修改 __init__ 和 __main__ 以及 加粗"
    )

    # 2. Idle preemption: m2 with debounce=0 joins active idle window instead of stealing flight
    async def _run() -> None:
        clk = _FakeClock(3000.0)
        coord = SessionFlightCoordinator(clock=clk)
        pol_idle = BubblePolicy(enabled=True, idle_debounce_seconds=0.05)
        pol_zero = BubblePolicy(enabled=True, idle_debounce_seconds=0.0)
        umo = "elle:FriendMessage:10086"
        try:
            a1 = await coord.admit_or_coalesce(umo, CapturedMessage("m1", clk()), policy=pol_idle)
            assert a1.mode == "wait_idle"
            assert coord.snapshot(umo)["idle_active"] is True

            a2 = await coord.admit_or_coalesce(umo, CapturedMessage("m2", clk()), policy=pol_zero)
            assert a2.mode == "swallowed"

            clk.advance(0.1)
            tok, batch = await coord.wait_idle_and_promote(umo, a1.token, policy=pol_idle)
            assert tok != ""
            assert batch is not None and batch.merged_text == "1. m1\n2. m2"
            assert coord.snapshot(umo)["idle_active"] is False
            assert await coord.release_flight(umo, tok) is True
        finally:
            await coord.close()

        # 3. Tokenless event in on_after_message_sent is a safe no-op
        plugin = BubbleFlowPlugin(_FakeContext([]), {"enabled": True})
        try:
            ev = _FakeEvent(umo, "无token事件")
            await plugin.on_after_message_sent(ev)
            assert plugin.extension_api.get_session_state(umo)["in_flight"] is False
        finally:
            await plugin.terminate()

    asyncio.run(_run())
