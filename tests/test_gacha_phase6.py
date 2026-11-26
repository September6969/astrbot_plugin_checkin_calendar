"""Phase 6 Part A+B 单一 GIF 合成及消息发送/等待/撤回/降级编排测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from astrbot.core.message.message_event_result import MessageEventResult
from astrbot_plugin_checkin_calendar.gacha.assets import PortraitAssetResolver
from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.presentation import (
    DEFAULT_INTRO_DIR,
    GachaGifRenderer,
    orchestrate_ten_pull_presentation,
)
from astrbot_plugin_checkin_calendar.gacha.repository import GachaRepository
from astrbot_plugin_checkin_calendar.gacha.service import GachaService

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
TZ = ZoneInfo("Asia/Shanghai")
ACTOR = '["aiocqhttp", "bot1", "60001"]'


def test_prebuilt_intros_exist_and_decode():
    for tier in ("r", "sr", "ssr"):
        path = DEFAULT_INTRO_DIR / f"intro_{tier}.gif"
        assert path.is_file()
        with Image.open(path) as im:
            assert im.format == "GIF"
            assert im.size == (360, 640)
            assert getattr(im, "n_frames", 1) >= 20


@pytest.mark.parametrize(
    "rarities",
    [
        ("R",) * 10,
        ("SR",) * 10,
        ("R", "R", "R", "R", "SSR", "R", "R", "R", "R", "R"),
        ("SSR", "R", "SR", "SSR", "R", "R", "SSR", "SR", "R", "R"),
        ("SSR",) * 10,
    ],
)
def test_combined_gif_generation_size_and_cache(rarities):
    renderer = GachaGifRenderer()
    data, duration_sec = renderer.render_bytes(rarities)
    assert len(data) <= 500_000
    assert duration_sec > 3.0
    with Image.open(BytesIO(data)) as im:
        assert im.format == "GIF"
        assert im.size == (360, 640)
        assert getattr(im, "n_frames", 1) >= 35

    # 同一序列命中缓存且返回一致字节流
    cached_data, cached_dur = renderer.render_bytes(rarities)
    assert cached_data is data
    assert cached_dur == duration_sec


def _prepare_ten_pull_record(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)
               VALUES (?, 4, 0, 0, 0, '2026-11-26T00:00:00+00:00')""",
            (ACTOR,),
        )
        con.commit()
    finally:
        con.close()
    catalog = CatalogManager(BUILTIN_SNAPSHOT)
    service = GachaService(catalog, repo, engine=PullEngine())
    record = service.ten_pull(ACTOR, "orch-1", datetime(2026, 11, 26, 15, 0, tzinfo=TZ))
    return service, record


def _make_onebot_event(bot):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message_id="msg-1", raw_message={"self_id": "999"}),
        unified_msg_origin="group-888",
        get_platform_name=lambda: "aiocqhttp",
        get_group_id=lambda: "88888",
        get_sender_id=lambda: "60001",
        get_sender_name=lambda: "测试玩家",
        bot=bot,
        make_result=MessageEventResult,
    )


@pytest.mark.asyncio
async def test_orchestration_normal_fast_and_degradation_paths(tmp_path: Path):
    service, record = _prepare_ten_pull_record(tmp_path)
    resolver = PortraitAssetResolver(tmp_path / "empty_assets")
    gif_renderer = GachaGifRenderer()

    result_file = tmp_path / "result.webp"
    result_file.write_bytes(b"RIFF\x00\x00\x00\x00WEBPfake")

    # 1. 正常模式：发 1 个 GIF -> 等播放 -> 发静态图 -> 成功后撤回该 GIF
    calls = []

    async def send_group_msg(group_id, message, **kwargs):
        calls.append(("send", group_id, message[0]["type"], message[-1]["data"].get("file", "")[:9]))
        return {"message_id": 1001 if len(calls) == 1 else 1002}

    async def delete_msg(message_id, **kwargs):
        calls.append(("delete", message_id))

    bot = SimpleNamespace(send_group_msg=send_group_msg, delete_msg=delete_msg)
    cal_renderer = SimpleNamespace(render=AsyncMock(return_value=str(result_file)))

    # 缩短等待以便单测快速运行
    res = await orchestrate_ten_pull_presentation(
        record=record,
        event=_make_onebot_event(bot),
        service=service,
        calendar_renderer=cal_renderer,
        gif_renderer=gif_renderer,
        resolver=resolver,
        owner_name="测试玩家",
        tz=TZ,
        fast_mode=False,
        gif_enabled=True,
        gif_buffer_seconds=0.0,
    )
    assert res is None
    # 第 1 次 send 是 GIF，第 2 次 send 是结果图，第 3 次操作是撤回 GIF (1001)
    assert calls[0] == ("send", 88888, "image", "base64://")
    assert calls[1][0] == "send"
    assert calls[2] == ("delete", 1001)

    # 2. 快速模式：跳过 GIF 与 delete_msg，直接返回结果图消息链
    fast_bot = SimpleNamespace(send_group_msg=AsyncMock(), delete_msg=AsyncMock())
    fast_res = await orchestrate_ten_pull_presentation(
        record=record,
        event=_make_onebot_event(fast_bot),
        service=service,
        calendar_renderer=cal_renderer,
        gif_renderer=gif_renderer,
        resolver=resolver,
        owner_name="测试玩家",
        tz=TZ,
        fast_mode=True,
    )
    assert fast_res is not None
    fast_bot.send_group_msg.assert_not_awaited()
    fast_bot.delete_msg.assert_not_awaited()

    # 3. GIF 合成失败：直接发送结果图，不重抽、不撤回
    broken_gif = SimpleNamespace(render_bytes=lambda _r: (_ for _ in ()).throw(RuntimeError("gif boom")))
    fallback_calls = []

    async def send_group_fb(group_id, message, **kwargs):
        fallback_calls.append(message)
        return {"message_id": 2001}

    fb_bot = SimpleNamespace(send_group_msg=send_group_fb, delete_msg=AsyncMock())
    await orchestrate_ten_pull_presentation(
        record=record,
        event=_make_onebot_event(fb_bot),
        service=service,
        calendar_renderer=cal_renderer,
        gif_renderer=broken_gif,
        resolver=resolver,
        owner_name="测试玩家",
        tz=TZ,
        fast_mode=False,
    )
    assert len(fallback_calls) == 1
    fb_bot.delete_msg.assert_not_awaited()

    # 4. 结算图渲染失败：发送纯文本结果后再撤回 GIF
    broken_cal = SimpleNamespace(render=AsyncMock(side_effect=RuntimeError("render boom")))
    text_calls = []

    async def send_group_text(group_id, message, **kwargs):
        text_calls.append(message)
        return {"message_id": 3001 if len(text_calls) == 1 else 3002}

    text_del = AsyncMock()
    text_bot = SimpleNamespace(send_group_msg=send_group_text, delete_msg=text_del)
    await orchestrate_ten_pull_presentation(
        record=record,
        event=_make_onebot_event(text_bot),
        service=service,
        calendar_renderer=broken_cal,
        gif_renderer=gif_renderer,
        resolver=resolver,
        owner_name="测试玩家",
        tz=TZ,
        fast_mode=False,
        gif_buffer_seconds=0.0,
    )
    assert len(text_calls) == 2
    assert "十连招募结果" in text_calls[1][1]["data"]["text"]
    text_del.assert_awaited_once_with(message_id=3001, self_id="999")

    # 5. 结果图发送失败：保留已发 GIF，不执行撤回
    send_idx = 0

    async def send_fail_second(group_id, message, **kwargs):
        nonlocal send_idx
        send_idx += 1
        if send_idx == 1:
            return {"message_id": 4001}
        raise RuntimeError("send result failed")

    no_recall = AsyncMock()
    fail_bot = SimpleNamespace(send_group_msg=send_fail_second, delete_msg=no_recall)
    ret = await orchestrate_ten_pull_presentation(
        record=record,
        event=_make_onebot_event(fail_bot),
        service=service,
        calendar_renderer=cal_renderer,
        gif_renderer=gif_renderer,
        resolver=resolver,
        owner_name="测试玩家",
        tz=TZ,
        fast_mode=False,
        gif_buffer_seconds=0.0,
    )
    assert ret is not None
    no_recall.assert_not_awaited()
