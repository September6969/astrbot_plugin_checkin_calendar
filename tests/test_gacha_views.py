"""测试北京时间日夜主题切换、图鉴/统计/商店图片载荷及打包完整性。"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_checkin_calendar.gacha.assets import PortraitAssetResolver
from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager, PoolUnavailableError
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.repository import GachaRepository
from astrbot_plugin_checkin_calendar.gacha.service import GachaService
from astrbot_plugin_checkin_calendar.gacha.views import (
    build_pokedex_payload,
    build_shop_payload,
    build_stats_payload,
)
from astrbot_plugin_checkin_calendar.presentation import is_night_beijing, theme_class
from scripts.package import build as build_package
from scripts.verify_package import verify as verify_package

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
TZ = ZoneInfo("Asia/Shanghai")
ACTOR = '["aiocqhttp", "bot1", "50001"]'


@pytest.mark.parametrize(
    ("dt", "expected_night"),
    [
        (datetime(2026, 11, 27, 6, 0, tzinfo=TZ), False),
        (datetime(2026, 11, 27, 12, 30, tzinfo=TZ), False),
        (datetime(2026, 11, 27, 17, 59, tzinfo=TZ), False),
        (datetime(2026, 11, 27, 18, 0, tzinfo=TZ), True),
        (datetime(2026, 11, 27, 23, 30, tzinfo=TZ), True),
        (datetime(2026, 11, 27, 5, 59, tzinfo=TZ), True),
        # UTC 04:00 == 北京时间 12:00（白天）
        (datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc), False),
        # UTC 14:00 == 北京时间 22:00（夜间）
        (datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc), True),
    ],
)
def test_beijing_day_night_theme_class(dt: datetime, expected_night: bool):
    assert is_night_beijing(dt) is expected_night
    assert theme_class(dt) == ("theme-dark" if expected_night else "theme-light")


def _setup_service(tmp_path: Path) -> tuple[GachaService, PortraitAssetResolver]:
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)
               VALUES (?, 6, 0, 210, 6200, '2026-11-26T00:00:00+00:00')""",
            (ACTOR,),
        )
        con.commit()
    finally:
        con.close()
    catalog = CatalogManager(BUILTIN_SNAPSHOT)
    service = GachaService(catalog, repo, engine=PullEngine())
    resolver = PortraitAssetResolver(tmp_path / "assets")
    return service, resolver


def test_pokedex_stats_and_shop_payloads(tmp_path: Path):
    service, resolver = _setup_service(tmp_path)
    now_day = datetime(2026, 11, 26, 13, 0, tzinfo=TZ)

    # 0 抽时的统计边界
    empty_stats = build_stats_payload(service, ACTOR)
    assert empty_stats["total_pulls"] == 0
    assert empty_stats["actual_rate"] == "—"
    assert empty_stats["tickets"] == 3

    # 抽一次十连后测试图鉴筛选与分页
    service.ten_pull(ACTOR, "views-1", now_day)
    dex_all = build_pokedex_payload(service, ACTOR, [], resolver)
    assert dex_all is not None
    assert dex_all["owned_total"] > 0
    assert dex_all["page"] == 1

    dex_unowned = build_pokedex_payload(service, ACTOR, ["未拥有"], resolver)
    assert dex_unowned is not None
    assert "未拥有" in dex_unowned["filter"]

    # 单角色名查询、无效页码、页码越界应返回 None 以回退文本提示
    assert build_pokedex_payload(service, ACTOR, ["拉吡"], resolver) is None
    assert build_pokedex_payload(service, ACTOR, ["0"], resolver) is None
    assert build_pokedex_payload(service, ACTOR, ["9999"], resolver) is None

    stats = build_stats_payload(service, ACTOR)
    assert stats["total_pulls"] == 10
    assert stats["ssr_pct"] + stats["sr_pct"] + stats["r_pct"] == pytest.approx(100.0, abs=0.05)

    # 商店载荷（含白天浅色主题与 PoolUnavailableError 分支）
    shop_day = build_shop_payload(service, ACTOR, now_day, TZ, resolver)
    assert shop_day["theme_class"] == "theme-light"
    assert shop_day["mold_pct"] == 100.0
    assert shop_day["mileage_pct"] == 100.0

    def _raise_unavailable(_now):
        raise PoolUnavailableError("卡池尚未同步")

    service.catalog.get_active_golden_mileage = _raise_unavailable  # type: ignore[method-assign]
    shop_err = build_shop_payload(service, ACTOR, datetime(2026, 11, 26, 22, 0, tzinfo=TZ), TZ, resolver)
    assert shop_err["theme_class"] == "theme-dark"
    assert "暂不可用" in shop_err["note"]


def test_package_includes_shared_css_and_new_templates(tmp_path: Path):
    archive = tmp_path / "pkg.zip"
    build_package(archive)
    verify_package(archive)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "astrbot_plugin_checkin_calendar/templates/_card_base.css" in names
    assert "astrbot_plugin_checkin_calendar/templates/pokedex.html" in names
    assert "astrbot_plugin_checkin_calendar/templates/stats.html" in names
    assert "astrbot_plugin_checkin_calendar/templates/shop.html" in names
    assert "astrbot_plugin_checkin_calendar/gacha/views.py" in names
