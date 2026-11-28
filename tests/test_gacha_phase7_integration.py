"""Phase 7 插件入口端到端路由、老库迁移备份演练及无回归集成测试。"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from astrbot.api.message_components import Image
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot_plugin_checkin_calendar.main import CheckinCalendarPlugin
from astrbot_plugin_checkin_calendar.store import CalendarStore

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


def _make_plugin(tmp_path: Path, monkeypatch, *, cooldown_minutes: int = 0, blast_max_percent: int = 0):
    data_dir = tmp_path / "plugin_data"
    monkeypatch.setattr(
        "astrbot_plugin_checkin_calendar.main.StarTools.get_data_dir",
        lambda _name: data_dir,
    )
    plugin = CheckinCalendarPlugin(
        SimpleNamespace(),
        config={
            "timezone": "Asia/Shanghai",
            "cooldown": cooldown_minutes,
            "blast_max_percent": blast_max_percent,
            "roulette_enabled": False,
            "gacha_gif_enabled": True,
            "gacha_gif_buffer_seconds": 0.0,
        },
    )
    fake_png = tmp_path / "fake.png"
    fake_png.write_bytes(b"fake")
    fake_webp = tmp_path / "fake.webp"
    fake_webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPfake")

    async def fake_render(template, data, options):
        if options.get("output_format") in {"webp", "jpeg"}:
            return str(fake_webp)
        return str(fake_png)

    plugin.calendar_renderer = SimpleNamespace(render=AsyncMock(side_effect=fake_render), close=AsyncMock())
    return plugin, data_dir


def _event(text: str, msg_id: str, user: str = "123456", group_id: str = "987654"):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message_id=msg_id, raw_message={"self_id": "10000"}),
        unified_msg_origin=f"group-{group_id}",
        get_message_str=lambda: text,
        get_platform_name=lambda: "aiocqhttp",
        get_platform_id=lambda: "qq-prod",
        get_self_id=lambda: "10000",
        get_sender_id=lambda: user,
        get_sender_name=lambda: "指挥官",
        get_group_id=lambda: group_id,
        get_messages=lambda: [],
        bot=SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 555}), delete_msg=AsyncMock()),
        plain_result=lambda t: SimpleNamespace(chain=[SimpleNamespace(text=t)]),
        make_result=MessageEventResult,
    )


async def _run_cmd(plugin: CheckinCalendarPlugin, text: str, msg_id: str):
    return [r async for r in plugin.checkin(_event(text, msg_id))]


def _extract_text(reply) -> str:
    return "".join(getattr(c, "text", "") for c in getattr(reply, "chain", []))


@pytest.mark.asyncio
async def test_legacy_db_migration_backup_and_end_to_end_flow(tmp_path: Path, monkeypatch):
    # 1. 先创建老版本数据库并写入 3 条历史打卡记录。
    data_dir = tmp_path / "plugin_data"
    db_path = data_dir / "calendar.sqlite3"
    legacy = CalendarStore(db_path, cooldown_minutes=0, blast_max_percent=0)
    old_time = datetime(2026, 11, 20, 9, 0, tzinfo=TZ)
    for i in range(3):
        assert legacy.change('["qq-prod", "10000", "123456"]', f"legacy-{i}", "add", old_time + timedelta(minutes=i)).status == "added"

    # 2. 升级前备份数据库，再初始化完整插件（执行迁移）。
    backup_path = tmp_path / "calendar.sqlite3.bak"
    shutil.copy2(db_path, backup_path)
    plugin, _ = _make_plugin(tmp_path, monkeypatch)

    # 老记录仍完整保留，但未补发十连资格。
    month_snap = plugin.store.month('["qq-prod", "10000", "123456"]', "2026-11")
    assert month_snap.total == 3
    wallet0 = plugin.gacha_repo.get_wallet('["qq-prod", "10000", "123456"]')
    assert (wallet0.checkin_count, wallet0.available_tickets) == (0, 0)

    # 3. 确认“🦌 招募”绝不进入招募大厅，仅返回帮助。
    hall_replies = await _run_cmd(plugin, "🦌 招募", "m-hall")
    assert "🦌 十连" in _extract_text(hall_replies[0])
    assert "🦌 帮助" in _extract_text(hall_replies[0])

    # 4. 两次有效打卡 -> 附带招募进度与余额提示，最终获得 1 张十连资格。
    await _run_cmd(plugin, "🦌", "m-c1")
    assert "招募进度 1/2 · 可用十连 0 次" in plugin.calendar_renderer.render.call_args_list[-1].args[1]["message"]

    await _run_cmd(plugin, "🦌", "m-c2")
    assert "招募进度 0/2 · 可用十连 1 次" in plugin.calendar_renderer.render.call_args_list[-1].args[1]["message"]

    # 5. 十连快速模式 -> 图鉴 -> 抽卡历史 -> 指定编号 -> 统计 -> 商店 -> 帮助
    pull_replies = await _run_cmd(plugin, "🦌 十连 快速", "m-pull1")
    assert len(pull_replies) == 1
    assert any(isinstance(seg, Image) for seg in pull_replies[0].chain)

    dex_reply = (await _run_cmd(plugin, "🦌 图鉴", "m-dex"))[0]
    assert any(isinstance(seg, Image) for seg in dex_reply.chain)
    assert plugin.calendar_renderer.render.call_args_list[-1].args[1]["owned_total"] > 0

    hist_text = _extract_text((await _run_cmd(plugin, "🦌 抽卡历史", "m-gh"))[0])
    assert "#0001" in hist_text

    detail_text = _extract_text((await _run_cmd(plugin, "🦌 抽卡历史 0001", "m-gh-1"))[0])
    assert "【十连招募结果 #0001】" in detail_text

    stats_reply = (await _run_cmd(plugin, "🦌 统计", "m-stat"))[0]
    assert any(isinstance(seg, Image) for seg in stats_reply.chain)
    assert plugin.calendar_renderer.render.call_args_list[-1].args[1]["total_pulls"] == 10

    shop_reply = (await _run_cmd(plugin, "🦌 商店", "m-shop"))[0]
    assert any(isinstance(seg, Image) for seg in shop_reply.chain)
    assert plugin.calendar_renderer.render.call_args_list[-1].args[1]["mileage"] == 10

    help_text = _extract_text((await _run_cmd(plugin, "🦌 帮助", "m-help"))[0])
    assert "🦌 十连 快速" in help_text
    assert "🦌 铸模" in help_text

    # 6. 验证既有打卡历史（🦌 历史）与月历（🦌 月历）无回归。
    legacy_hist = await _run_cmd(plugin, "🦌 历史", "m-old-hist")
    assert len(legacy_hist) == 1
    legacy_cal = await _run_cmd(plugin, "🦌 月历", "m-old-cal")
    assert len(legacy_cal) == 1
    assert plugin.calendar_renderer.render.call_args_list[-1].args[1]["total"] >= 3

    await plugin.terminate()
