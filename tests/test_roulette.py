"""转盘 GIF 渲染与发送-停顿-撤回流程测试。"""

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageSequence

from astrbot.api.message_components import At, Image as MsgImage, Plain
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot_plugin_checkin_calendar.presentation import format_probability
from astrbot_plugin_checkin_calendar.roulette import BLAST_CRIMSON, LANDING_FRACTIONS, POINTER_ANGLE, RouletteRenderer, _draw_sectors, _wheel_geometry
from test_commands import plugin, replies


NOW = datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize("risk,blasted", [(0, False), (0.003, False), (12.54, True), (99.5, True)])
def test_roulette_renderer_produces_valid_animated_gif(tmp_path, risk, blasted):
    renderer = RouletteRenderer(tmp_path)
    out = renderer.render(risk, blasted, size=240)
    with Image.open(out) as im:
        assert im.format == "GIF"
        assert im.is_animated is True
        assert im.n_frames >= 20
        assert im.size == (240, 240)
        assert sum(frame.info["duration"] for frame in ImageSequence.Iterator(im)) == 4480
        im.seek(im.n_frames - 1)
        assert im.info["duration"] == 2220


@pytest.mark.parametrize("risk,label", [(0, "0%"), (0.003, "<0.01%"), (12.54, "12.54%")])
def test_roulette_probability_label_keeps_decimals(risk, label):
    assert format_probability(risk, decimal_places=2) == label


@pytest.mark.parametrize("risk,blasted", [(0, False), (0.003, False), (7.31, True), (24.85, False), (100, True)])
@pytest.mark.parametrize("fraction", LANDING_FRACTIONS)
def test_pointer_lands_in_selected_sector(risk, blasted, fraction):
    arc, rotation = _wheel_geometry(risk, blasted, fraction)
    assert arc == pytest.approx(risk * 3.6)
    local_pointer = (POINTER_ANGLE - rotation) % 360
    assert (local_pointer < arc) is blasted
    start, width = (0, arc) if blasted else (arc, 360 - arc)
    assert local_pointer == pytest.approx(start + width * fraction)


@pytest.mark.parametrize("risk", [0, 0.5, 7.31, 24.85, 99.5, 100])
def test_disc_area_matches_probability(risk):
    arc, rotation = _wheel_geometry(risk, False)
    disc = _draw_sectors(180, rotation, arc)
    colors = {color: count for count, color in disc.getcolors()}
    red = colors.get((*BLAST_CRIMSON, 255), 0)
    area = sum(count for color, count in colors.items() if color[3])
    assert 100 * red / area == pytest.approx(risk, abs=0.2)


def test_roulette_renderer_cleans_up_old_gifs(tmp_path):
    old_file = tmp_path / "roulette-old.gif"
    recent_file = tmp_path / "roulette-recent.gif"
    old_file.write_bytes(b"old")
    recent_file.write_bytes(b"recent")
    stale = time.time() - 90000
    os.utime(old_file, (stale, stale))
    RouletteRenderer(tmp_path).render(20.0, False, size=180)
    assert not old_file.exists()
    assert recent_file.exists()


def _bot_event(msg_id="m1", group_id="123456", sender_id="987654", bot=None):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message_id=msg_id, raw_message={"self_id": 111222}),
        unified_msg_origin=f"group-{group_id}",
        get_message_str=lambda: "🦌",
        get_platform_id=lambda: "qq-1",
        get_platform_name=lambda: "aiocqhttp",
        get_self_id=lambda: "bot1",
        get_sender_id=lambda: sender_id,
        get_sender_name=lambda: "测试用户",
        get_group_id=lambda: group_id,
        bot=bot,
        plain_result=lambda text: ("text", text),
        image_result=lambda path: ("image", path),
        make_result=MessageEventResult,
    )


@pytest.mark.asyncio
async def test_roulette_send_pause_recall_before_blast_result(plugin, tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.datetime", Clock)
    plugin.roulette_renderer = RouletteRenderer(tmp_path)
    plugin.roulette_enabled = True
    plugin.roulette_delay_seconds = 2.5

    actor = json.dumps(["qq-1", "bot1", "987654"], ensure_ascii=False)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", lambda n: 999999)
    plugin.store.change(actor, "old1", "add", NOW - timedelta(hours=4))
    plugin.store.change(actor, "old2", "add", NOW - timedelta(hours=2))
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", Mock(return_value=0))

    order = []

    async def fake_send_group_msg(**kwargs):
        order.append(("send", kwargs["group_id"], kwargs["self_id"]))
        return {"message_id": 555666}

    async def fake_delete_msg(**kwargs):
        order.append(("delete", kwargs["message_id"], kwargs["self_id"]))

    async def fake_sleep(sec):
        order.append(("sleep", sec))

    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.asyncio.sleep", fake_sleep)
    bot = SimpleNamespace(send_group_msg=fake_send_group_msg, delete_msg=fake_delete_msg)

    res = (await replies(plugin, _bot_event("blast-1", bot=bot)))[0]
    assert order == [
        ("send", 123456, 111222),
        ("sleep", 4.5),
        ("delete", 555666, 111222),
    ]
    assert [type(x) for x in res.chain] == [At, Plain]
    assert "炸膛了" in res.chain[1].text

    # 重复投递不重发转盘
    order.clear()
    await replies(plugin, _bot_event("blast-1", bot=bot))
    assert order == []


@pytest.mark.asyncio
async def test_roulette_recall_failure_still_returns_calendar_result(plugin, tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.datetime", Clock)
    plugin.roulette_renderer = RouletteRenderer(tmp_path)
    plugin.roulette_enabled = True
    plugin.roulette_delay_seconds = 1.0

    actor = json.dumps(["qq-1", "bot1", "987654"], ensure_ascii=False)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", lambda n: 999999)
    plugin.store.change(actor, "old1", "add", NOW - timedelta(hours=4))

    async def fake_sleep(_):
        pass

    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.asyncio.sleep", fake_sleep)
    bot = SimpleNamespace(
        send_private_msg=AsyncMock(return_value={"message_id": 777}),
        delete_msg=AsyncMock(side_effect=RuntimeError("撤回权限不足")),
    )

    res = (await replies(plugin, _bot_event("safe-1", group_id="", bot=bot)))[0]
    bot.send_private_msg.assert_awaited_once()
    packet = bot.send_private_msg.await_args.kwargs["message"][0]["data"]["file"]
    assert packet.startswith("base64://R0lGOD")
    bot.delete_msg.assert_awaited_once()
    assert [type(x) for x in res.chain] == [At, Plain, MsgImage]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["render", "send_timeout", "busy"])
async def test_roulette_failure_still_persists_history(plugin, tmp_path, monkeypatch, failure):
    import asyncio
    from datetime import timezone

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.datetime", Clock)
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.main.ROULETTE_SEND_TIMEOUT", 0.01)
    gif = tmp_path / "sample.gif"
    gif.write_bytes(b"GIF89a")
    render = Mock(return_value=str(gif))
    if failure == "render":
        render.side_effect = TimeoutError
    plugin.roulette_renderer = SimpleNamespace(render=render)
    busy = None
    if failure == "busy":
        busy = asyncio.get_running_loop().create_future()
        plugin._roulette_task = busy

    async def delayed_send(**kwargs):
        await asyncio.sleep(60)

    bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=delayed_send), delete_msg=AsyncMock())
    try:
        reply = (await replies(plugin, _bot_event(msg_id="failure", bot=bot)))[0]
    finally:
        if busy is not None:
            busy.cancel()
    assert [type(x) for x in reply.chain] == [At, Plain, MsgImage]
    actor = json.dumps(["qq-1", "bot1", "987654"])
    records = plugin.store.history(actor, None, False, 1).records
    assert len(records) == 1
    assert records[0].risk_percent == 0
    assert datetime.fromisoformat(records[0].created_at) == NOW.astimezone(timezone.utc)
    assert bot.send_group_msg.await_count == int(failure == "send_timeout")
    bot.delete_msg.assert_not_called()


def test_gif_cache_keys_exact_area_and_outcome(tmp_path):
    renderer = RouletteRenderer(tmp_path)
    first = renderer.render(7.3101, False, size=120)
    assert renderer.render(7.3101, False, size=120) == first
    assert renderer.render(7.3102, False, size=120) != first
    assert renderer.render(7.3101, True, size=120) != first

def test_animation_key_varies_landing_and_stable_keys_reuse_gif(tmp_path):
    renderer = RouletteRenderer(tmp_path)
    risk = 24.85
    variants = {
        hashlib.sha256(str(i).encode()).digest()[0] % len(LANDING_FRACTIONS): str(i)
        for i in range(50)
    }
    assert len(variants) == len(LANDING_FRACTIONS)
    first = renderer.render(risk, True, size=160, animation_key=variants[0])
    second = renderer.render(risk, True, size=160, animation_key=variants[1])
    assert first != second
    assert renderer.render(risk, True, size=160, animation_key=variants[1]) == second
    with Image.open(first) as left, Image.open(second) as right:
        left.seek(left.n_frames - 1)
        right.seek(right.n_frames - 1)
        assert left.convert("RGB").tobytes() != right.convert("RGB").tobytes()
