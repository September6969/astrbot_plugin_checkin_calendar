"""Phase 2 打卡奖励账本、版本迁移、撤销抵扣、幂等与并发事务测试。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.repository import (
    GachaRepository,
    InsufficientTicketsError,
)
from astrbot_plugin_checkin_calendar.gacha.rewards import apply_checkin_event
from astrbot_plugin_checkin_calendar.store import CalendarStore

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
TZ = timezone(timedelta(hours=8))
ACTOR = '["aiocqhttp", "bot1", "10001"]'


class FixedRng:
    """确定性 RNG：前 `ssr_slots` 抽落入 SSR，其余落入 R。"""

    def __init__(self, ssr_slots: set[int] | None = None):
        self._ssr_slots = ssr_slots or set()
        self._draw_idx = 0
        self._stage = "ssr"

    def randbelow(self, upper_bound: int) -> int:
        if self._stage == "ssr":
            self._draw_idx += 1
            if self._draw_idx in self._ssr_slots:
                self._stage = "char"
                return 0
            self._stage = "sr"
            return upper_bound - 1
        if self._stage == "sr":
            self._stage = "char"
            return upper_bound - 1
        self._stage = "ssr"
        return 0


def _make_stores(db_path: Path, *, cooldown_minutes: int = 120, blast_max_percent: int = 0):
    repo = GachaRepository(db_path)
    store = CalendarStore(
        db_path,
        cooldown_minutes=cooldown_minutes,
        blast_max_percent=blast_max_percent,
        on_entry_event=apply_checkin_event,
    )
    return store, repo


def test_pre_launch_history_cutoff_and_idempotent_restart(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    legacy_store = CalendarStore(db_path, cooldown_minutes=0, blast_max_percent=0)
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=TZ)
    for i in range(5):
        assert legacy_store.change(ACTOR, f"old-{i}", "add", t0 + timedelta(minutes=i)).status == "added"

    # 上线 Gacha 迁移：上线前 5 次历史打卡不补发资格。
    store, repo = _make_stores(db_path, cooldown_minutes=0, blast_max_percent=0)
    wallet = repo.get_wallet(ACTOR)
    assert wallet.checkin_count == 0
    assert wallet.available_tickets == 0
    assert wallet.checkin_progress == 0
    assert wallet.debt_tickets == 0

    # 撤销上线前记录不产生债务。
    assert store.change(ACTOR, "undo-old", "undo", t0 + timedelta(hours=1)).status == "undone"
    assert repo.get_wallet(ACTOR) == wallet

    # 重启再次初始化仓库不追溯历史。
    repo2 = GachaRepository(db_path)
    assert repo2.get_wallet(ACTOR) == wallet


def test_checkin_progression_cross_month_and_non_counting_events(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "calendar.sqlite3"
    store, repo = _make_stores(db_path, cooldown_minutes=120, blast_max_percent=80)

    # 第 1 次有效打卡（月底 11-30），奇数进度保留。
    t1 = datetime(2026, 11, 30, 10, 0, tzinfo=TZ)
    assert store.change(ACTOR, "m1", "add", t1).status == "added"
    w1 = repo.get_wallet(ACTOR)
    assert (w1.checkin_count, w1.checkin_progress, w1.available_tickets) == (1, 1, 0)

    # 冷却拒绝、重复投递均不增加进度。
    assert store.change(ACTOR, "m1", "add", t1).replay is True
    assert store.change(ACTOR, "cd1", "add", t1 + timedelta(minutes=10)).status == "cooldown"
    assert repo.get_wallet(ACTOR) == w1

    # 跨月 12-01 第 2 次有效打卡 -> 满 2 次发放 1 张十连资格。
    t2 = datetime(2026, 12, 1, 10, 0, tzinfo=TZ)
    assert store.change(ACTOR, "m2", "add", t2).status == "added"
    w2 = repo.get_wallet(ACTOR)
    assert (w2.checkin_count, w2.checkin_progress, w2.available_tickets) == (2, 0, 1)

    # 强制触发炸膛：炸膛及炸膛冷却均不增加进度。
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.store.secrets.randbelow", lambda n: 0)
    t3 = datetime(2026, 12, 1, 12, 1, tzinfo=TZ)
    assert store.change(ACTOR, "blast1", "add", t3).status == "blasted"
    assert store.change(ACTOR, "blast-cd", "add", t3 + timedelta(minutes=5)).status == "blast_cooldown"
    assert repo.get_wallet(ACTOR) == w2


def test_undo_reclaim_and_debt_offset_prevents_farming(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    store, repo = _make_stores(db_path, cooldown_minutes=0, blast_max_percent=0)
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    day1 = datetime(2026, 11, 26, 9, 0, tzinfo=TZ)

    # 2 次打卡 -> 1 张资格；撤销 1 次（未消费前）-> 回收资格，无债务。
    assert store.change(ACTOR, "c1", "add", day1).status == "added"
    assert store.change(ACTOR, "c2", "add", day1 + timedelta(minutes=1)).status == "added"
    assert repo.get_wallet(ACTOR).available_tickets == 1
    assert store.change(ACTOR, "u1", "undo", day1 + timedelta(minutes=2)).status == "undone"
    w_reclaimed = repo.get_wallet(ACTOR)
    assert (w_reclaimed.checkin_count, w_reclaimed.available_tickets, w_reclaimed.debt_tickets) == (1, 0, 0)

    # 再打卡 1 次恢复至 1 张资格并消费十连。
    assert store.change(ACTOR, "c3", "add", day1 + timedelta(minutes=3)).status == "added"
    rec = repo.execute_ten_pull(
        ACTOR, "pull-1", snap, PullEngine(rng=FixedRng()), day1 + timedelta(minutes=4)
    )
    assert rec.wallet.available_tickets == 0
    assert rec.wallet.consumed_tickets == 1
    assert rec.wallet.golden_mileage == 10

    # 消费后撤销今天一次打卡 -> C=1, E=0, U=1 -> 待抵扣债务 = 1。
    assert store.change(ACTOR, "u2", "undo", day1 + timedelta(minutes=5)).status == "undone"
    w_debt = repo.get_wallet(ACTOR)
    assert (w_debt.checkin_count, w_debt.available_tickets, w_debt.debt_tickets) == (1, 0, 1)

    # 用完资格后试图再抽必须被拒绝。
    with pytest.raises(InsufficientTicketsError):
        repo.execute_ten_pull(ACTOR, "pull-fail", snap, PullEngine(rng=FixedRng()), day1 + timedelta(minutes=6))

    # 重新打卡 1 次：C=2, E=1, U=1 -> 恰好抵扣债务，余额仍为 0（防反复套取）。
    assert store.change(ACTOR, "c4", "add", day1 + timedelta(minutes=7)).status == "added"
    w_even = repo.get_wallet(ACTOR)
    assert (w_even.checkin_count, w_even.available_tickets, w_even.debt_tickets) == (2, 0, 0)

    # 再打卡 2 次：C=4, E=2, U=1 -> 恢复至 1 张可用。
    assert store.change(ACTOR, "c5", "add", day1 + timedelta(minutes=8)).status == "added"
    assert store.change(ACTOR, "c6", "add", day1 + timedelta(minutes=9)).status == "added"
    w_recovered = repo.get_wallet(ACTOR)
    assert (w_recovered.checkin_count, w_recovered.available_tickets, w_recovered.debt_tickets) == (4, 1, 0)


def test_ten_pull_idempotency_streaks_and_rollback(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    store, repo = _make_stores(db_path, cooldown_minutes=0, blast_max_percent=0)
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    t = datetime(2026, 11, 26, 10, 0, tzinfo=TZ)

    # 准备 2 张十连资格。
    for i in range(4):
        store.change(ACTOR, f"add-{i}", "add", t + timedelta(minutes=i))

    # 注入提交前故障 -> 必须全量回滚。
    def _boom(_stage: str) -> None:
        raise RuntimeError("simulated disk error")

    with pytest.raises(RuntimeError, match="simulated disk error"):
        repo.execute_ten_pull(
            ACTOR, "pull-boom", snap, PullEngine(rng=FixedRng({1})), t + timedelta(minutes=10), fault_injector=_boom
        )

    assert repo.get_wallet(ACTOR).available_tickets == 2
    assert repo.get_wallet(ACTOR).golden_mileage == 0
    assert repo.get_pull_stats(ACTOR).total_pulls == 0
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM gacha_sessions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM gacha_results").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM gacha_acquisitions").fetchone()[0] == 0
    finally:
        con.close()

    # 正常第 1 次十连（第 2 抽中 SSR，后面 8 抽未中 -> 结尾连续未出金 8）。
    r1 = repo.execute_ten_pull(
        ACTOR, "pull-ok-1", snap, PullEngine(rng=FixedRng({2})), t + timedelta(minutes=11)
    )
    assert r1.replay is False
    assert len(r1.slots) == 10
    assert r1.ssr_count == 1
    assert r1.mileage_gained == 10
    assert r1.wallet.available_tickets == 1
    assert r1.wallet.golden_mileage == 10
    assert r1.stats.current_no_ssr_streak == 8
    assert r1.stats.max_no_ssr_streak == 8

    # 幂等重放：同一幂等键返回原记录且不重复扣券、不加金票。
    r1_replay = repo.execute_ten_pull(
        ACTOR, "pull-ok-1", snap, PullEngine(rng=FixedRng({1, 2, 3})), t + timedelta(minutes=12)
    )
    assert r1_replay.replay is True
    assert r1_replay.session_id == r1.session_id
    assert r1_replay.slots == r1.slots
    assert repo.get_wallet(ACTOR).available_tickets == 1
    assert repo.get_wallet(ACTOR).golden_mileage == 10

    # 第 2 次十连（第 4、5 抽中 SSR -> 跨十连最长连续未出金为 8 + 3 = 11，单次十连最多 SSR = 2）。
    r2 = repo.execute_ten_pull(
        ACTOR, "pull-ok-2", snap, PullEngine(rng=FixedRng({4, 5})), t + timedelta(minutes=13)
    )
    assert r2.stats.total_pulls == 20
    assert r2.stats.total_ssr == 3
    assert r2.stats.max_no_ssr_streak == 11
    assert r2.stats.current_no_ssr_streak == 5
    assert r2.stats.max_ssr_in_ten_pull == 2


def test_concurrent_ten_pull_and_checkin_undo_consistency(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    store, repo = _make_stores(db_path, cooldown_minutes=0, blast_max_percent=0)
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    t = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)

    # 仅准备 1 张十连资格。
    store.change(ACTOR, "c1", "add", t)
    store.change(ACTOR, "c2", "add", t + timedelta(minutes=1))

    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def _worker(key: str) -> None:
        barrier.wait()
        try:
            res = repo.execute_ten_pull(ACTOR, key, snap, PullEngine(rng=FixedRng()), t + timedelta(minutes=2))
            with lock:
                outcomes.append(res)
        except InsufficientTicketsError as exc:
            with lock:
                outcomes.append(exc)

    t1 = threading.Thread(target=_worker, args=("race-1",))
    t2 = threading.Thread(target=_worker, args=("race-2",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    successes = [o for o in outcomes if not isinstance(o, Exception)]
    failures = [o for o in outcomes if isinstance(o, InsufficientTicketsError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert repo.get_wallet(ACTOR).consumed_tickets == 1
    assert repo.get_wallet(ACTOR).available_tickets == 0
