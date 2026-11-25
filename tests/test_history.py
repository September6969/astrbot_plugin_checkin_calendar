"""覆盖判定事务、分页统计和群聊与私聊入口。"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from astrbot.api.message_components import At, Image, Plain

from astrbot_plugin_checkin_calendar.history import history_payload, parse_history_args
from astrbot_plugin_checkin_calendar.store import CalendarStore
from test_commands import event, plugin


ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 1, 0, 2, tzinfo=ZONE)
ACTOR = json.dumps(["qq-1", "bot1", "alice"])


def test_success_blast_cooldowns_replay_undo_and_restart(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    store = CalendarStore(path)
    draw = Mock(return_value=0)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", draw)
    first = store.change(ACTOR, "first", "add", NOW)
    assert (first.status, first.risk_percent) == ("added", 0)
    assert store.change(ACTOR, "ordinary", "add", NOW + timedelta(minutes=1)).status == "cooldown"
    when = NOW + timedelta(hours=2)
    blast = store.change(ACTOR, "blast", "add", when)
    assert blast.status == "blasted"
    assert store.change(ACTOR, "replay-cooldown", "add", when + timedelta(minutes=1)).status == "blast_cooldown"
    replay = store.change(ACTOR, "blast", "add", when + timedelta(days=3))
    assert replay.replay and replay.risk_percent == blast.risk_percent
    assert store.change(ACTOR, "undo", "undo", when + timedelta(minutes=2)).status == "undone"
    snapshot = CalendarStore(path).history(ACTOR, "2026-11", False, 1)
    exploded, added = snapshot.records
    assert exploded.risk_percent == blast.risk_percent
    assert exploded.cooldown_until == blast.available_at
    assert added.undone and added.risk_percent == 0
    assert snapshot.stats.total == 2 and snapshot.stats.undone == snapshot.stats.blasted == 1
    assert snapshot.stats.average == blast.risk_percent / 2
    assert snapshot.stats.highest == snapshot.stats.lowest_blast == blast.risk_percent
    assert history_payload(snapshot, "Alice", ZONE)["metrics"][3] == ("实际炸膛率", "50.0%")
    draw.assert_called_once()


@pytest.mark.parametrize("expected", ["added", "blasted"])
def test_history_write_error_rolls_back_everything(tmp_path, monkeypatch, expected):
    store = CalendarStore(tmp_path / "test.db", cooldown_minutes=0)
    if expected == "blasted":
        store.change(ACTOR, "seed", "add", NOW)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", lambda _: 0)
    with sqlite3.connect(store.path) as con:
        before = tuple(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("entries", "receipts", "blast_cooldowns", "draw_history"))
        con.execute("""CREATE TRIGGER history_fail BEFORE INSERT ON draw_history
                       BEGIN SELECT RAISE(ABORT, 'history write failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="history write failed"):
        store.change(ACTOR, "failed", "add", NOW + timedelta(minutes=1))
    with sqlite3.connect(store.path) as con:
        assert before == tuple(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                               for table in ("entries", "receipts", "blast_cooldowns", "draw_history"))
        con.execute("DROP TRIGGER history_fail")
    assert store.change(ACTOR, "failed", "add", NOW + timedelta(minutes=1)).status == expected


def test_simultaneous_deliveries_only_store_one_draw(tmp_path):
    store = CalendarStore(tmp_path / "test.db")
    store.change(ACTOR, "seed", "add", NOW)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: store.change(ACTOR, "same", "add", NOW + timedelta(hours=2)), range(12)))
    assert sum(not row.replay for row in results) == 1
    assert len({(row.status, row.risk_percent) for row in results}) == 1
    assert store.history(ACTOR, None, False, 1).stats.total == 2


def test_page_month_filter_ties_and_actor_isolation(tmp_path):
    store = CalendarStore(tmp_path / "test.db", cooldown_minutes=0, blast_max_percent=0)
    store.change(ACTOR, "previous", "add", NOW - timedelta(minutes=3))
    for i in range(12):
        store.change(ACTOR, str(i), "add", NOW)
    for identity in (["qq-2", "bot1", "alice"], ["qq-1", "bot2", "alice"], ["qq-1", "bot1", "bob"]):
        other = json.dumps(identity)
        store.change(other, "other", "add", NOW)
        assert store.history(other, None, False, 1).stats.total == 1
    first = store.history(ACTOR, "2026-11", False, 1)
    second = store.history(ACTOR, "2026-11", False, 2)
    assert first.pages == second.pages == 2
    assert len(first.records) == 10 and len(second.records) == 2
    ids = [row.id for row in first.records + second.records]
    assert ids == sorted(set(ids), reverse=True)
    assert first.stats == second.stats
    assert store.history(ACTOR, "2026-10", False, 1).matching == 1
    assert store.history(ACTOR, None, False, 1).matching == 13
    filtered = store.history(ACTOR, "2026-11", True, 1)
    assert filtered.stats == first.stats and filtered.matching == filtered.pages == 0
    assert store.history(ACTOR, "2026-11", False, 10**50).records == []
    local = history_payload(first, "A", ZoneInfo("America/Los_Angeles"))
    assert local["rows"][0]["time"].startswith("2026-10-31")


def test_absolute_time_order_across_timezones(tmp_path):
    store = CalendarStore(tmp_path / "test.db", cooldown_minutes=0, blast_max_percent=0)
    local = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    store.change(ACTOR, "later", "add", local + timedelta(minutes=10))
    store.change(ACTOR, "earlier", "add", NOW)
    all_rows = store.history(ACTOR, None, False, 1).records
    assert all_rows[0].created_at > all_rows[1].created_at
    assert [row.day for row in all_rows] == ["2026-10-31", "2026-11-01"]


def test_blast_only_pagination_uses_all_draws_for_statistics(tmp_path, monkeypatch):
    store = CalendarStore(tmp_path / "test.db", cooldown_minutes=0, blast_cooldown_minutes=1)
    store.change(ACTOR, "seed", "add", NOW)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", lambda _: 0)
    for minute in range(1, 12):
        assert store.change(ACTOR, f"blast-{minute}", "add", NOW + timedelta(minutes=minute)).status == "blasted"
    all_draws = store.history(ACTOR, "2026-11", False, 1)
    part = store.history(ACTOR, "2026-11", True, 2)
    assert part.pages == 2 and len(part.records) == 1 and part.matching == 11
    assert part.records[0].result == "blasted"
    assert part.stats == all_draws.stats
    assert history_payload(part, "Alice", ZONE)["metrics"][3] == ("实际炸膛率", "91.7%")


@pytest.mark.parametrize("args,expected", [
    ([], (None, False, 1)), (["2"], (None, False, 2)),
    (["2026-11"], ("2026-11", False, 1)), (["2026-11", "2"], ("2026-11", False, 2)),
    (["炸膛"], (None, True, 1)), (["炸膛", "2"], (None, True, 2)),
    (["2026-11", "炸膛"], ("2026-11", True, 1)),
    (["2026-11", "炸膛", "2"], ("2026-11", True, 2)),
])
def test_history_args(args, expected):
    assert parse_history_args(args, NOW.date()) == expected


@pytest.mark.parametrize("args", [
    ["2026-13"], ["2026-1"], ["0000-01"], ["-2"], ["1.5"], ["0"], ["1"*19],
    ["2", "3"], ["炸膛", "炸膛"], ["2026-11", "2026-12"], ["别人"], ["@12345"],
])
def test_history_rejects_invalid_arguments(args):
    with pytest.raises(ValueError):
        parse_history_args(args, NOW.date())


async def call_history(plugin, text="历史", *, private=True, components=(), user="alice"):
    message = event(text="🦌 " + text, user=user)
    message.is_private_chat = lambda: private
    message.get_messages = lambda: components
    return (await collect(plugin, message))[0]


async def collect(plugin, message):
    return [reply async for reply in plugin.checkin(message)]


@pytest.mark.asyncio
async def test_targeting_is_rejected_before_any_read(plugin, monkeypatch):
    history = Mock(side_effect=AssertionError("不得查询数据库"))
    monkeypatch.setattr(plugin.store, "history", history)
    for reply in (
        await call_history(plugin, "历史 12345 Bob", private=False),
        await call_history(plugin, "历史", components=[At(qq="bob")], private=False),
        await call_history(plugin, "历史 Bob"),
    ):
        assert [type(item) for item in reply.chain] == [Plain]
        assert not reply.use_t2i_
    history.assert_not_called()
    plugin.html_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_empty_image_and_complete_fallback_after_renderer_crash(plugin):
    empty = await call_history(plugin)
    assert [type(item) for item in empty.chain] == [Image]
    assert plugin.html_render.call_args.args[1]["pagination"] == "第 0 / 0 页 · 共 0 条"
    plugin.store.change(ACTOR, "one", "add", NOW)
    plugin.store.change(ACTOR, "undo", "undo", NOW)
    plugin.html_render.side_effect = RuntimeError("模拟 Chromium 崩溃")
    result = await call_history(plugin, "历史 2026-11")
    body = result.chain[0].text
    assert "00:02:00" in body and "0%" in body and "已撤销" in body and "第 1 / 1 页" in body
    assert (await call_history(plugin, "历史 2")).chain[0].text.startswith("页码超出")
    assert "暂无有效判定" in (await call_history(plugin, user="bob")).chain[0].text
    assert not result.use_t2i_


@pytest.mark.asyncio
async def test_persisted_history_remains_queryable_after_checkin_render_failure(plugin):
    plugin.html_render.side_effect = RuntimeError("模拟打卡展示失败")
    response = (await collect(plugin, event()))[0]
    assert "图片暂时生成失败" in response.chain[0].text
    history = await call_history(plugin)
    assert "有效判定 1" in history.chain[0].text
    assert "正常通过" in history.chain[0].text


def test_admin_cooldown_reset_does_not_change_history(tmp_path):
    store = CalendarStore(tmp_path / "test.db")
    store.change(ACTOR, "first", "add", NOW)
    original = store.history(ACTOR, None, False, 1)
    assert store.change(ACTOR, "reset", "reset_all_cooldowns", NOW).status == "cooldowns_reset"
    assert store.history(ACTOR, None, False, 1) == original
    assert not store.risk(ACTOR, NOW).available_at

@pytest.mark.asyncio
async def test_repeated_own_history_in_group_and_private_during_both_cooldowns(plugin, monkeypatch):
    ordinary = plugin.store.change(ACTOR, "first", "add", NOW)
    assert ordinary.status == "added"
    clock = NOW + timedelta(minutes=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.datetime", Clock)
    draw = Mock(return_value=0)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", draw)
    send = Mock(side_effect=AssertionError("历史查询不会发送转盘"))
    monkeypatch.setattr(plugin, "_send_and_recall_roulette", send)
    for group in (True, False, True):
        assert (await call_history(plugin, private=not group)).chain
        assert plugin.html_render.call_args.args[1]["metrics"][0] == ("有效判定", "1")
    assert plugin.store.risk(ACTOR, clock).available_at
    draw.assert_not_called()
    clock = NOW + timedelta(hours=2)
    assert plugin.store.change(ACTOR, "blast", "add", clock).status == "blasted"
    clock += timedelta(minutes=1)
    assert plugin.store.risk(ACTOR, clock).blast_cooling
    for group in (True, False, True):
        assert (await call_history(plugin, "历史 炸膛", private=not group)).chain
        data = plugin.html_render.call_args.args[1]
        assert data["metrics"][0] == ("有效判定", "2")
        assert [row["result"] for row in data["rows"]] == ["炸膛"]
    draw.assert_called_once()
    send.assert_not_called()
