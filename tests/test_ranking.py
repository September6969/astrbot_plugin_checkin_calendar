"""排行范围、同名隔离、历史兼容和只读命令回归。"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from astrbot.api.message_components import Image, Plain
from astrbot_plugin_checkin_calendar.store import CalendarStore
from astrbot_plugin_checkin_calendar.ranking import ranking_payload, ranking_text, resolve_names
from test_commands import plugin, event, replies

NOW=datetime(2026,10,1,8,tzinfo=ZoneInfo('Asia/Shanghai'))
def actor(user, bot='bot', platform='qq'):
    return json.dumps([platform,bot,str(user)],ensure_ascii=False)


def test_totals_cover_all_users_but_only_ten_rows_and_scope_isolated(tmp_path):
    store=CalendarStore(tmp_path/'test.db')
    for i in range(12):
        store.change(actor(i),str(i),'add',NOW)
        store.save_name(actor(i),'同名')
    store.change(actor('outside','other'),'x','add',NOW)
    store.change(actor('outside',platform='other'),'x','add',NOW)
    store.change('broken','x','add',NOW)
    snapshot=store.ranking('qq','bot','2026-10')
    assert snapshot.total==snapshot.participants==12
    assert len(snapshot.entries)==10
    assert len({e.actor for e in snapshot.entries})==10
    assert all(e.name=='同名' for e in snapshot.entries)
    assert snapshot==store.ranking('qq','bot','2026-10')


def test_undo_month_boundary_and_active_days_sort(tmp_path):
    store=CalendarStore(tmp_path/'test.db')
    for user,when in [('a',NOW),('a',NOW+timedelta(hours=2)),('b',NOW),('b',NOW+timedelta(days=1)),('c',NOW)]:
        store.change(actor(user),when.isoformat(),'add',when)
    store.change(actor('c'),'undo','undo',NOW)
    store.change(actor('b'),'november','add',NOW.replace(month=11))
    result=store.ranking('qq','bot','2026-10')
    assert result.total==4 and result.participants==2
    assert [(e.user_id,e.total,e.days) for e in result.entries]==[('b',2,2),('a',2,1)]


def test_old_database_keeps_records_without_nicknames(tmp_path):
    path=tmp_path/'old.db'
    con=sqlite3.connect(path)
    con.execute('CREATE TABLE entries(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,day TEXT NOT NULL,created_at TEXT NOT NULL,undone INTEGER NOT NULL DEFAULT 0)')
    con.execute('INSERT INTO entries(actor,day,created_at) VALUES (?,?,?)',(actor(123),'2026-10-01',NOW.isoformat()))
    con.commit();con.close()
    store=CalendarStore(path)
    data=ranking_payload(store.ranking('qq','bot','2026-10'))
    assert data['total']==1 and data['rows'][0]['name'].startswith('用户·')
    assert data['unknown_names']
    assert store.month(actor(123),'2026-10').total==1


@pytest.mark.asyncio
async def test_names_resolved_once_failure_anonymous_and_same_names_keep_identity(tmp_path):
    store=CalendarStore(tmp_path/'test.db')
    for uid in (1,2,3): store.change(actor(uid),'x','add',NOW)
    api=AsyncMock(side_effect=[{'nickname':'同名'},{'nickname':'同名'},RuntimeError('失败')])
    e=SimpleNamespace(get_platform_name=lambda:'aiocqhttp',bot=SimpleNamespace(get_stranger_info=api))
    result=await resolve_names(store.ranking('qq','bot','2026-10'),e,store)
    assert [r.name for r in result.entries]==['同名','同名','']
    assert len({r.actor for r in result.entries})==3
    assert api.await_count==3
    assert [r.name for r in store.ranking('qq','bot','2026-10').entries]==['同名','同名','']


@pytest.mark.asyncio
async def test_ranking_command_readonly_and_same_dto_fallback(plugin):
    await replies(plugin,event())
    plugin.store.change=Mock(side_effect=AssertionError('排行不能写入打卡'))
    response=(await replies(plugin,event('rank'), '排行'))[0]
    assert [type(x) for x in response.chain]==[Image]
    data=plugin.html_render.call_args.args[1]
    assert data['total']==1 and data['participants']==1
    assert data['rows'][0]['name']=='测试用户'
    plugin.html_render.side_effect=RuntimeError('渲染失败')
    response=(await replies(plugin,event('rank2'), '排行'))[0]
    assert [type(x) for x in response.chain]==[Plain]
    assert response.chain[0].text==ranking_text(data) and response.use_t2i_ is False
    plugin.store.change.assert_not_called()


@pytest.mark.asyncio
async def test_empty_historical_and_bad_month(plugin):
    await replies(plugin,event(), '排行', '2024-02')
    assert plugin.html_render.call_args.args[1]['month']=='2024-02'
    assert plugin.html_render.call_args.args[1]['participants']==0
    plugin.html_render.reset_mock()
    await replies(plugin,event(), '排行', '2024-13')
    plugin.html_render.assert_not_awaited()
