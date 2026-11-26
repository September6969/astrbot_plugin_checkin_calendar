"""Phase 4 纯文本命令（图鉴、抽卡历史、统计、商店、铸模、兑换）及隔离测试。"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager, compute_snapshot_hash
from astrbot_plugin_checkin_calendar.gacha.commands import (
    format_checkin_gacha_hint,
    format_gacha_history,
    format_pokedex,
    format_shop,
    format_stats,
    handle_exchange_command,
    handle_mold_command,
)
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.repository import GachaRepository
from astrbot_plugin_checkin_calendar.gacha.service import GachaService

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
TZ = ZoneInfo("Asia/Shanghai")
ACTOR_A = '["aiocqhttp", "bot1", "11111"]'
ACTOR_B = '["aiocqhttp", "bot1", "22222"]'


class ControlledRng:
    def __init__(self, ssr_slots: set[int] | None = None):
        self._ssr_slots = ssr_slots or set()
        self._draw = 0
        self._stage = "ssr"

    def randbelow(self, upper_bound: int) -> int:
        if self._stage == "ssr":
            self._draw += 1
            if self._draw in self._ssr_slots:
                self._stage = "char"
                return 0
            self._stage = "sr"
            return upper_bound - 1
        if self._stage == "sr":
            self._stage = "char"
            return 0 if self._draw == 3 else upper_bound - 1
        self._stage = "ssr"
        return 0


def _seed(db_path: Path, actor: str, *, checkins: int = 0, mileage: int = 0, labels: int = 0) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)
               VALUES (?, ?, 0, ?, ?, '2026-11-26T00:00:00+00:00')
               ON CONFLICT(actor) DO UPDATE SET
                   checkin_count=excluded.checkin_count,
                   golden_mileage=excluded.golden_mileage,
                   body_labels=excluded.body_labels""",
            (actor, checkins, mileage, labels),
        )
        con.commit()
    finally:
        con.close()


def test_empty_pokedex_history_and_zero_pull_stats(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)
    catalog = CatalogManager(BUILTIN_SNAPSHOT)
    service = GachaService(catalog, repo)
    now = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)

    pokedex = format_pokedex(service, ACTOR_A, [], TZ)
    assert "收藏：0/200" in pokedex
    assert "暂无已收录" in pokedex

    unowned = format_pokedex(service, ACTOR_A, ["未拥有", "1"], TZ)
    assert "筛选：未拥有（共 200 位）" in unowned
    assert "第1/10页" in unowned

    history = format_gacha_history(service, ACTOR_A, [], TZ)
    assert "暂无十连记录" in history

    stats = format_stats(service, ACTOR_A, TZ)
    assert "实际 SSR 出率：未产生统计" in stats
    assert "下一抽 SSR 仓检概率：4.00%" in stats

    shop = format_shop(service, ACTOR_A, now, TZ)
    assert "黄金里程券：0 张" in shop
    assert "暂无已核验的当期 UP" in shop

    hint = format_checkin_gacha_hint(repo.get_wallet(ACTOR_A))
    assert "招募进度 0/2 · 可用十连 0 次" in hint


def test_pokedex_filters_pagination_details_and_cross_actor_isolation(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)

    base = json.loads(BUILTIN_SNAPSHOT.read_text(encoding="utf-8"))
    up_char = next(c for c in base["characters"] if c["character_key"] == "neon_vision_eye")
    v2 = copy.deepcopy(base)
    v2["golden_mileage_shop"] = [
        {
            "character_id": up_char["character_id"],
            "banner_name": "透视之眼 UP",
            "effective_from": "2026-11-26T00:00:00+09:00",
            "effective_until": "2026-12-03T05:00:00+09:00",
            "source_note": "test",
        }
    ]
    v2["content_sha256"] = compute_snapshot_hash(v2)
    catalog = CatalogManager(BUILTIN_SNAPSHOT, runtime_dir=tmp_path / "cat")
    catalog.install_snapshot(json.dumps(v2, ensure_ascii=False).encode("utf-8"))

    engine = PullEngine(rng=ControlledRng({1, 2}))
    service = GachaService(catalog, repo, engine=engine)
    now = datetime(2026, 11, 26, 14, 0, tzinfo=TZ)

    _seed(db_path, ACTOR_A, checkins=4, mileage=200, labels=6000)
    res = service.ten_pull(ACTOR_A, "a-pull-1", now)

    # 通过全渠道（十连 + 铸模 + 金票兑换）产生记录
    mold_text = handle_mold_command(service, ACTOR_A, "a-mold-1", now + timedelta(minutes=1), TZ)
    assert "【完美铸模开启结果】" in mold_text
    ex_text = handle_exchange_command(
        service, ACTOR_A, "a-ex-1", ["尼恩：透视之眼"], now + timedelta(minutes=2), TZ
    )
    assert "【黄金里程兑换结果】" in ex_text
    assert "尼恩：透视之眼" in ex_text

    # 图鉴筛选与详情
    ssr_list = format_pokedex(service, ACTOR_A, ["SSR"], TZ)
    assert "筛选：SSR" in ssr_list
    corp_list = format_pokedex(service, ACTOR_A, ["米西利斯"], TZ)
    assert "筛选：米西利斯" in corp_list
    detail_owned = format_pokedex(service, ACTOR_A, ["尼恩：透视之眼"], TZ)
    assert "首次获得：" in detail_owned
    detail_ambig = format_pokedex(service, ACTOR_A, ["尼恩："], TZ)
    assert "匹配到多位角色" in detail_ambig
    detail_unowned = format_pokedex(service, ACTOR_A, ["红莲：暗影"], TZ)
    assert "状态：未解锁" in detail_unowned

    # 抽卡历史列表、指定编号、角色全渠道历史
    hist_list = format_gacha_history(service, ACTOR_A, [], TZ)
    assert f"#{res.session_id:04d}" in hist_list
    hist_detail = format_gacha_history(service, ACTOR_A, [f"{res.session_id:04d}"], TZ)
    assert f"【十连招募结果 #{res.session_id:04d}】" in hist_detail
    assert "10." in hist_detail

    char_hist = format_gacha_history(service, ACTOR_A, ["尼恩：透视之眼"], TZ)
    assert "黄金里程兑换" in char_hist

    # 统计与原始抽卡对账一致（不含铸模和金票兑换的 SSR）
    stats_text = format_stats(service, ACTOR_A, TZ)
    assert "累计正常招募：10 抽（SSR 2 / SR 1 / R 7）" in stats_text
    assert "完美铸模开启：1 次 · 黄金里程兑换：1 次" in stats_text

    # 严格跨用户隔离：ACTOR_B 无法猜测编号读取 ACTOR_A 的十连记录，图鉴也是独立的。
    b_guess = format_gacha_history(service, ACTOR_B, [str(res.session_id)], TZ)
    assert "未找到本人编号" in b_guess
    b_pokedex = format_pokedex(service, ACTOR_B, [], TZ)
    assert "收藏：0/200" in b_pokedex

    # 页码非法/越界校验
    assert "页码必须为大于等于 1" in format_pokedex(service, ACTOR_A, ["0"], TZ)
    assert "页码超出范围" in format_pokedex(service, ACTOR_A, ["99"], TZ)
    assert "页码超出范围" in format_gacha_history(service, ACTOR_A, ["页", "99"], TZ)
