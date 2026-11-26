"""Phase 3 收藏突破、Core+7、溢出标签、完美铸模与黄金里程兑换测试。"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager, compute_snapshot_hash
from astrbot_plugin_checkin_calendar.gacha.collection import apply_acquisition, format_collection_rank
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.repository import (
    ExchangeUnavailableError,
    GachaRepository,
    InsufficientLabelsError,
    InsufficientMileageError,
    MoldPoolMaxedError,
)
from astrbot_plugin_checkin_calendar.gacha.rewards import apply_checkin_event
from astrbot_plugin_checkin_calendar.gacha.service import GachaService
from astrbot_plugin_checkin_calendar.store import CalendarStore

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
JST = timezone(timedelta(hours=9))
ACTOR = '["aiocqhttp", "bot1", "30001"]'


class FixedChoiceRng:
    def __init__(self, idx: int = 0):
        self.idx = idx

    def randbelow(self, upper_bound: int) -> int:
        return self.idx % upper_bound


class DuplicateCharTenPullRng:
    """全抽指定稀有度并全部命中索引 0，用于测试同十连内重复角色顺序突破与溢出。"""

    def __init__(self, rarity: str):
        self.rarity = rarity
        self.stage = "ssr"

    def randbelow(self, upper_bound: int) -> int:
        if self.stage == "ssr":
            if self.rarity == "SSR":
                self.stage = "char"
                return 0
            self.stage = "sr"
            return upper_bound - 1
        if self.stage == "sr":
            self.stage = "char"
            return 0 if self.rarity == "SR" else upper_bound - 1
        self.stage = "ssr"
        return 0


def _seed_wallet(db_path: Path, *, checkins: int = 0, mileage: int = 0, labels: int = 0) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)
               VALUES (?, ?, 0, ?, ?, '2026-11-26T00:00:00+00:00')
               ON CONFLICT(actor) DO UPDATE SET
                   checkin_count=excluded.checkin_count,
                   golden_mileage=excluded.golden_mileage,
                   body_labels=excluded.body_labels""",
            (ACTOR, checkins, mileage, labels),
        )
        con.commit()
    finally:
        con.close()


def test_collection_thresholds_for_all_rarities():
    # R: 1=NEW, 2+=150 标签
    r1 = apply_acquisition("R", 0)
    r2 = apply_acquisition("R", 1)
    assert (r1.outcome_tag, r1.labels_gained) == ("NEW", 0)
    assert (r2.outcome_tag, r2.labels_gained) == ("OVERFLOW", 150)
    assert format_collection_rank("R", 5) == "已收录"

    # SR: 1=NEW, 2=LB1, 3=LB2(MLB), 4+=200 标签
    sr_outcomes = [apply_acquisition("SR", i) for i in range(4)]
    assert [o.outcome_tag for o in sr_outcomes] == ["NEW", "LB1", "LB2", "OVERFLOW"]
    assert [o.labels_gained for o in sr_outcomes] == [0, 0, 0, 200]
    assert sr_outcomes[2].reached_mlb is True
    assert format_collection_rank("SR", 10) == "★★"

    # SSR: 1=NEW, 2..4=LB1..LB3, 5..11=CORE+1..7, 12+=6000 标签
    ssr_outcomes = [apply_acquisition("SSR", i) for i in range(13)]
    assert [o.outcome_tag for o in ssr_outcomes[:5]] == ["NEW", "LB1", "LB2", "LB3", "CORE+1"]
    assert ssr_outcomes[3].reached_mlb is True
    assert ssr_outcomes[10].outcome_tag == "CORE+7"
    assert ssr_outcomes[10].reached_core_max is True
    assert ssr_outcomes[10].labels_gained == 0
    assert (ssr_outcomes[11].outcome_tag, ssr_outcomes[11].labels_gained) == ("OVERFLOW", 6000)
    assert (ssr_outcomes[12].outcome_tag, ssr_outcomes[12].labels_gained) == ("OVERFLOW", 6000)
    assert format_collection_rank("SSR", 15) == "★★★ CORE+7"


def test_same_ten_pull_duplicates_progression_and_overflow(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)
    _seed_wallet(db_path, checkins=4)  # 2 张十连资格
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    t1 = datetime(2026, 11, 26, 12, 0, tzinfo=JST)

    # 第 1 次十连：全部抽中同一个 SSR（1..10 次），顺序经历 NEW, LB1..LB3, CORE+1..6，0 标签
    r1 = repo.execute_ten_pull(ACTOR, "ten-1", snap, PullEngine(rng=DuplicateCharTenPullRng("SSR")), t1)
    assert [s.outcome_tag for s in r1.slots] == [
        "NEW", "LB1", "LB2", "LB3", "CORE+1", "CORE+2", "CORE+3", "CORE+4", "CORE+5", "CORE+6"
    ]
    assert r1.labels_gained == 0

    # 第 2 次十连：继续抽中同一 SSR（11..20 次），第 11 张达成 CORE+7 (0标签)，第 12~20 张共 9 次溢出 = 54000 标签
    r2 = repo.execute_ten_pull(
        ACTOR, "ten-2", snap, PullEngine(rng=DuplicateCharTenPullRng("SSR")), t1 + timedelta(minutes=5)
    )
    assert r2.slots[0].outcome_tag == "CORE+7"
    assert r2.slots[0].labels_gained == 0
    assert all(s.outcome_tag == "OVERFLOW" and s.labels_gained == 6000 for s in r2.slots[1:])
    assert r2.labels_gained == 9 * 6000
    assert r2.wallet.body_labels == 54000

    inv = repo.get_inventory(ACTOR)[r1.slots[0].character_id]
    assert inv.total_obtained == 20
    assert inv.first_obtained_at is not None
    assert inv.mlb_at is not None
    assert inv.core_max_at is not None


def test_perfect_mold_thresholds_overflow_and_all_maxed_protection(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)
    catalog = CatalogManager(BUILTIN_SNAPSHOT)
    service = GachaService(catalog, repo)
    now = datetime(2026, 11, 26, 12, 0, tzinfo=JST)

    # 5999 标签不足
    _seed_wallet(db_path, labels=5999)
    with pytest.raises(InsufficientLabelsError):
        service.open_mold(ACTOR, "mold-low", now, rng=FixedChoiceRng(0))

    # 6000 标签成功开启：不改变 N/S，不增加金票
    _seed_wallet(db_path, labels=6000)
    res1 = service.open_mold(ACTOR, "mold-1", now, rng=FixedChoiceRng(0))
    assert res1.outcome_tag == "NEW"
    assert res1.net_labels_delta == -6000
    assert res1.wallet.body_labels == 0
    assert res1.wallet.golden_mileage == 0
    assert repo.get_pull_stats(ACTOR).total_pulls == 0
    assert repo.get_pull_stats(ACTOR).mold_count == 1

    # 幂等重放不重复扣标签
    res1_replay = service.open_mold(ACTOR, "mold-1", now, rng=FixedChoiceRng(1))
    assert res1_replay.replay is True
    assert res1_replay.character_id == res1.character_id
    assert repo.get_pull_stats(ACTOR).mold_count == 1

    # 将该角色补到 11 张 (CORE+7) 后再次用铸模抽中它 -> 返还 6000 标签，净消耗为 0
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE gacha_inventory SET total_obtained=11 WHERE actor=? AND character_id=?",
            (ACTOR, res1.character_id),
        )
        con.commit()
    finally:
        con.close()
    _seed_wallet(db_path, labels=6000)
    res_ov = service.open_mold(ACTOR, "mold-overflow", now, rng=FixedChoiceRng(0))
    assert res_ov.outcome_tag == "OVERFLOW"
    assert res_ov.labels_refunded == 6000
    assert res_ov.net_labels_delta == 0
    assert res_ov.wallet.body_labels == 6000

    # 将整个铸模池所有角色均设为 >= 11 -> 直接拒绝且不扣除标签
    con = sqlite3.connect(db_path)
    try:
        for cid in catalog.snapshot.mold_pool:
            con.execute(
                """INSERT INTO gacha_inventory(actor, character_id, total_obtained, first_obtained_at, last_obtained_at)
                   VALUES (?, ?, 11, '2026-11-26T00:00:00+00:00', '2026-11-26T00:00:00+00:00')
                   ON CONFLICT(actor, character_id) DO UPDATE SET total_obtained=11""",
                (ACTOR, cid),
            )
        con.commit()
    finally:
        con.close()

    with pytest.raises(MoldPoolMaxedError):
        service.open_mold(ACTOR, "mold-all-max", now, rng=FixedChoiceRng(0))
    assert repo.get_wallet(ACTOR).body_labels == 6000


def test_golden_mileage_exchange_rules(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)

    base = json.loads(BUILTIN_SNAPSHOT.read_text(encoding="utf-8"))
    char_a = next(c for c in base["characters"] if c["character_key"] == "neon_vision_eye")
    char_b = next(c for c in base["characters"] if c["character_key"] == "anis_star")
    char_expired = next(c for c in base["characters"] if c["character_key"] == "rapi_red_hood")

    v2 = copy.deepcopy(base)
    v2["snapshot_version"] = "jp-test-mileage-v2"
    v2["golden_mileage_shop"] = [
        {
            "character_id": char_a["character_id"],
            "banner_name": "UP A",
            "effective_from": "2026-11-26T00:00:00+09:00",
            "effective_until": "2026-12-03T05:00:00+09:00",
            "source_note": "test",
        },
        {
            "character_id": char_b["character_id"],
            "banner_name": "UP B",
            "effective_from": "2026-11-26T00:00:00+09:00",
            "effective_until": "2026-12-03T05:00:00+09:00",
            "source_note": "test",
        },
        {
            "character_id": char_expired["character_id"],
            "banner_name": "Expired",
            "effective_from": "2026-11-10T00:00:00+09:00",
            "effective_until": "2026-11-25T05:00:00+09:00",
            "source_note": "expired",
        },
    ]
    v2["content_sha256"] = compute_snapshot_hash(v2)

    catalog = CatalogManager(BUILTIN_SNAPSHOT, runtime_dir=tmp_path / "cat")
    catalog.install_snapshot(json.dumps(v2, ensure_ascii=False).encode("utf-8"))
    service = GachaService(catalog, repo)
    now = datetime(2026, 11, 27, 10, 0, tzinfo=JST)

    # 199 金票不足
    _seed_wallet(db_path, mileage=199)
    with pytest.raises(InsufficientMileageError):
        service.exchange_golden_mileage(ACTOR, "ex-low", "尼恩：透视之眼", now)

    # 过期 UP / 模糊名拒绝
    _seed_wallet(db_path, mileage=400)
    with pytest.raises(ExchangeUnavailableError, match="不在当前黄金里程"):
        service.exchange_golden_mileage(ACTOR, "ex-exp", "拉毗：小红帽", now)
    with pytest.raises(ExchangeUnavailableError, match="多个"):
        service.exchange_golden_mileage(ACTOR, "ex-ambig", "：", now)

    # 正常兑换（不计入 N/S）
    res = service.exchange_golden_mileage(ACTOR, "ex-1", "尼恩：透视之眼", now)
    assert res.character_id == char_a["character_id"]
    assert res.net_mileage_delta == -200
    assert res.wallet.golden_mileage == 200
    assert repo.get_pull_stats(ACTOR).total_pulls == 0
    assert repo.get_pull_stats(ACTOR).mileage_exchange_count == 1

    # 幂等重放不重复扣金票
    res_replay = service.exchange_golden_mileage(ACTOR, "ex-1", "尼恩：透视之眼", now)
    assert res_replay.replay is True
    assert repo.get_wallet(ACTOR).golden_mileage == 200

    # 满 Core+7 后兑换仍返还 6000 标签
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE gacha_inventory SET total_obtained=11 WHERE actor=? AND character_id=?",
            (ACTOR, char_a["character_id"]),
        )
        con.commit()
    finally:
        con.close()
    res_ov = service.exchange_golden_mileage(ACTOR, "ex-ov", "尼恩：透视之眼", now)
    assert res_ov.outcome_tag == "OVERFLOW"
    assert res_ov.labels_refunded == 6000
    assert res_ov.wallet.golden_mileage == 0
    assert res_ov.wallet.body_labels == 6000
