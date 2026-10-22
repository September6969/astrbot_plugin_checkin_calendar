"""使用已安装 AstrBot SDK 验证命令、渲染回退和身份隔离。"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from astrbot.api.message_components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageEventResult

from astrbot_plugin_checkin_calendar.main import CheckinCalendarPlugin
from astrbot_plugin_checkin_calendar.store import CalendarStore
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer


@pytest.fixture
def plugin(tmp_path):
    instance = CheckinCalendarPlugin.__new__(CheckinCalendarPlugin)
    instance.store = CalendarStore(tmp_path / "calendar.sqlite3")
    instance.timezone_name = "Asia/Shanghai"
    instance.timezone = ZoneInfo(instance.timezone_name)
    instance.template = (Path(__file__).parents[1] / "templates/calendar.html").read_text(encoding="utf-8")
    instance.html_render = AsyncMock(return_value="rendered.png")
    instance.calendar_renderer = CalendarRenderer(instance.html_render, None)
    return instance


def event(message="1", user="alice", bot="bot1", platform="qq-1", session="group-1", text="🦌"):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message_id=message), unified_msg_origin=session, get_message_str=lambda: text,
        get_platform_id=lambda: platform, get_self_id=lambda: bot,
        get_sender_id=lambda: user, get_sender_name=lambda: "测试用户",
        plain_result=lambda text: ("text", text), image_result=lambda path: ("image", path),
        make_result=MessageEventResult,
    )


async def replies(plugin, e, action="", value=""):
    e.get_message_str = lambda: " ".join(x for x in ("🦌", action, value) if x)
    return [item async for item in plugin.checkin(e)]


@pytest.mark.asyncio
async def test_add_query_undo_and_replayed_delivery(plugin):
    result = (await replies(plugin, event()))[0]
    assert [type(item) for item in result.chain] == [At, Plain, Image]
    assert result.chain[0].qq == "alice"
    assert plugin.html_render.call_args.args[1]["total"] == 1
    await replies(plugin, event())
    assert plugin.html_render.call_args.args[1]["total"] == 1
    assert "已处理" in plugin.html_render.call_args.args[1]["message"]
    await replies(plugin, event("query"), "月历")
    assert plugin.html_render.call_args.args[1]["total"] == 1
    await replies(plugin, event("undo"), "撤销")
    assert plugin.html_render.call_args.args[1]["total"] == 0


@pytest.mark.asyncio
async def test_render_failure_preserves_saved_record_and_text(plugin):
    plugin.html_render.side_effect = RuntimeError("模拟渲染失败")
    result = await replies(plugin, event())
    assert [type(item) for item in result[0].chain] == [Plain]
    assert "累计 1 次" in result[0].chain[0].text
    assert "图片暂时生成失败" in result[0].chain[0].text
    assert result[0].use_t2i_ is False
    await replies(plugin, event())
    assert plugin.store.month('["qq-1", "bot1", "alice"]', datetime.now(plugin.timezone).strftime("%Y-%m")).total == 1


@pytest.mark.asyncio
async def test_empty_render_path_uses_text(plugin):
    plugin.html_render.return_value = ""
    assert "图片暂时生成失败" in (await replies(plugin, event()))[0].chain[0].text


@pytest.mark.asyncio
async def test_cancellation_propagates_but_message_cannot_duplicate_write(plugin):
    plugin.html_render.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await replies(plugin, event())
    plugin.html_render.side_effect = None
    await replies(plugin, event())
    assert plugin.html_render.call_args.args[1]["total"] == 1


@pytest.mark.asyncio
async def test_groups_share_user_record_bots_and_platforms_are_isolated(plugin):
    await replies(plugin, event("m1", session="group-a"))
    second = await replies(plugin, event("m2", session="group-b"))
    assert plugin.html_render.call_args.args[1]["total"] == 1
    assert "每120分钟只能记录一次哦" in second[0].chain[0].text
    for kwargs in ({"user": "bob"}, {"bot": "bot2"}, {"platform": "qq-2"}):
        await replies(plugin, event(**kwargs), "月历")
        assert plugin.html_render.call_args.args[1]["total"] == 0


@pytest.mark.asyncio
async def test_help_bad_month_and_bad_arguments_do_not_write(plugin):
    for action, value in (("帮助", ""), ("月历", "2026-13"), ("撤销", "2026-10"), ("其他", "")):
        assert (await replies(plugin, event(), action, value))[0][0] == "text"
    assert not plugin.html_render.called
    assert plugin.store.month('["qq-1", "bot1", "alice"]', "2026-10").total == 0


@pytest.mark.asyncio
async def test_missing_identity_never_uses_shared_fallback(plugin):
    assert "身份" in (await replies(plugin, event(user="")))[0][1]
    assert not plugin.html_render.called


@pytest.mark.asyncio
async def test_write_failure_does_not_claim_success_or_retry(plugin):
    plugin.store.change = Mock(side_effect=sqlite3.OperationalError("模拟数据库异常"))
    result = await replies(plugin, event())
    assert "无法确认" in result[0][1]
    assert plugin.store.change.call_count == 1
    assert not plugin.html_render.called


@pytest.mark.asyncio
async def test_snapshot_failure_does_not_show_zero_after_success(plugin):
    plugin.store.month = Mock(side_effect=sqlite3.OperationalError("模拟读取异常"))
    result = await replies(plugin, event())
    assert "记录成功" in result[0][1] and "读取失败" in result[0][1]


@pytest.mark.parametrize("text", ["🦌", "🦌️", " 🦌 ", "🦌 月历", "🦌 月历 2026-10", "🦌 撤销", "🦌 帮助"])
def test_real_regex_filter_accepts_bare_emoji_without_wake_prefix(text):
    from astrbot.core.star.filter.regex import RegexFilter
    from astrbot_plugin_checkin_calendar.main import TRIGGER_PATTERN
    assert RegexFilter(TRIGGER_PATTERN).filter(SimpleNamespace(get_message_str=lambda: text, is_at_or_wake_command=False), {})


@pytest.mark.parametrize("text", ["/打卡", "/🦌", "今天🦌", "你好 🦌", "🦌🦌", "🦌abc", "普通聊天"])
def test_regex_does_not_trigger_on_old_command_or_embedded_emoji(text):
    from astrbot.core.star.filter.regex import RegexFilter
    from astrbot_plugin_checkin_calendar.main import TRIGGER_PATTERN
    assert not RegexFilter(TRIGGER_PATTERN).filter(SimpleNamespace(get_message_str=lambda: text), {})


@pytest.mark.asyncio
async def test_emoji_arguments_and_extra_tokens_are_read_only(plugin):
    await replies(plugin, event(), "月历", "2026-08")
    assert plugin.html_render.call_args.args[1]["month"] == "2026-08"
    result = [item async for item in plugin.checkin(event(text="🦌 月历 2026-08 多余参数"))]
    assert result[0][0] == "text"
    assert plugin.store.month('["qq-1", "bot1", "alice"]', "2026-08").total == 0


@pytest.mark.asyncio
async def test_interval_uses_exact_requested_prompt_without_increment(plugin):
    await replies(plugin, event("one"))
    response = (await replies(plugin, event("two")))[0]
    data = plugin.html_render.call_args.args[1]
    assert data["total"] == 1
    assert [type(item) for item in response.chain] == [Plain]
    assert "注意身体，歇会儿吧！（每120分钟只能记录一次哦）" in response.chain[0].text
    assert "还需等待" in response.chain[0].text
    assert data["heading"] == "测试用户本月的🦌馆足迹"
    assert response.use_t2i_ is False


@pytest.mark.asyncio
async def test_cooldown_skips_renderer_and_snapshot_entirely(plugin):
    await replies(plugin, event("one"))
    plugin.html_render.reset_mock()
    plugin.html_render.side_effect = RuntimeError("模拟图片失败")
    plugin.store.month = Mock(side_effect=AssertionError("冷却不应读取月历"))
    result = (await replies(plugin, event("two")))[0]
    assert "每120分钟只能记录一次哦" in result.chain[0].text
    assert "记录成功" not in result.chain[0].text
    plugin.html_render.assert_not_awaited()
    plugin.store.month.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,value", [("", ""), ("月历", ""), ("月历", "2026-08"), ("状态", ""), ("撤销", "")])
@pytest.mark.parametrize("image", ["https://example.invalid/calendar.png", "rendered.png"])
async def test_calendar_mentions_exact_sender_with_image_in_same_chain(plugin, action, value, image):
    plugin.html_render.return_value = image
    result = (await replies(plugin, event(user="123456789"), action, value))[0]
    assert [type(item) for item in result.chain] == [At, Plain, Image]
    assert result.chain[0].toDict() == {"type": "at", "data": {"qq": "123456789"}}
    assert result.chain[1].text == "\n"
    assert result.use_t2i_ is False


@pytest.mark.asyncio
async def test_replayed_cooldown_remains_text_and_does_not_render(plugin):
    await replies(plugin, event("one"))
    await replies(plugin, event("two"))
    plugin.html_render.reset_mock()
    response = (await replies(plugin, event("two")))[0]
    assert [type(item) for item in response.chain] == [Plain]
    assert response.use_t2i_ is False
    plugin.html_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_retry_only_renders_same_snapshot_and_writes_once(plugin):
    class Strategy:
        endpoints = ['slow', 'healthy']
        calls = []

        async def render_custom_template(self, template, data, return_url, options):
            self.calls.append((self.endpoints[0], data))
            if self.endpoints == ['slow']:
                await asyncio.Event().wait()
            return 'rendered.png'

    strategy = Strategy()
    plugin.calendar_renderer = CalendarRenderer(plugin.html_render, strategy, endpoint_timeout=0.01)
    change = plugin.store.change
    plugin.store.change = Mock(wraps=change)
    response = (await replies(plugin, event()))[0]
    assert [type(item) for item in response.chain] == [At, Plain, Image]
    assert [endpoint for endpoint, _ in strategy.calls] == ['slow', 'healthy']
    assert strategy.calls[0][1] is strategy.calls[1][1]
    assert strategy.calls[1][1]['total'] == 1
    assert plugin.store.change.call_count == 1
    plugin.html_render.assert_not_awaited()
