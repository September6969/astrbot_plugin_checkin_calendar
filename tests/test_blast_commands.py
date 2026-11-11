"""实际命令链上的炸膛、重复投递与状态只读验证。"""
import json
from datetime import datetime,timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import pytest
from astrbot.api.message_components import At,Plain
from test_commands import plugin,event,replies

NOW=datetime(2026,11,10,12,tzinfo=ZoneInfo('Asia/Shanghai'))

@pytest.mark.asyncio
async def test_blast_message_at_and_status_and_duplicate_do_not_render_or_reroll(plugin,monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.main.datetime',Clock)
    actor=json.dumps(['qq-1','bot1','alice'],ensure_ascii=False)
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',lambda n:999999)
    plugin.store.change(actor,'old1','add',NOW-timedelta(hours=4))
    plugin.store.change(actor,'old2','add',NOW-timedelta(hours=2))
    draw=Mock(return_value=0)
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.store.secrets.randbelow',draw)
    result=(await replies(plugin,event('blast')))[0]
    assert [type(x) for x in result.chain]==[At,Plain]
    assert '炸膛了' in result.chain[1].text and '12小时' in result.chain[1].text and '11.0%' in result.chain[1].text
    assert result.use_t2i_ is False and str(result.chain[0].qq)=='alice'
    plugin.html_render.assert_not_awaited()
    again=(await replies(plugin,event('blast')))[0]
    assert '不会重新抽签或延长冷却' in again.chain[1].text
    blocked=(await replies(plugin,event('new')))[0]
    assert '还在炸膛冷却中' in blocked.chain[1].text
    await replies(plugin,event('state'),'状态')
    assert '炸膛冷却至' in plugin.html_render.call_args.args[1]['message']
    assert plugin.store.month(actor,'2026-11').total==2
    draw.assert_called_once_with(1_000_000)


@pytest.mark.asyncio
async def test_expired_blast_replay_does_not_report_a_new_lock(plugin,monkeypatch):
    from astrbot_plugin_checkin_calendar.store import Change
    plugin.store.change=Mock(return_value=Change('blasted','2020-01-01',True,'2020-01-02T00:00:00+00:00',20))
    result=(await replies(plugin,event()))[0]
    assert '该次冷却已结束' in result.chain[1].text
    assert '不会重新抽签或延长冷却' in result.chain[1].text
    plugin.html_render.assert_not_awaited()
