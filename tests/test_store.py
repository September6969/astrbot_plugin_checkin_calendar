"""验证实际 SQLite 事务、去重、日期边界和账号隔离。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_checkin_calendar.store import CalendarStore

NOW = datetime(2026, 10, 21, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def store(tmp_path):
    return CalendarStore(tmp_path / "calendar.sqlite3")


def test_multiple_counts_undo_and_restart_preserve_history(store):
    for i, token in enumerate(("one", "two", "three")):
        store.change("alice", token, "add", NOW.replace(hour=8) + timedelta(hours=2*i))
    assert store.month("alice", "2026-10").counts == {21: 3}
    assert store.change("alice", "undo", "undo", NOW).status == "undone"
    assert CalendarStore(store.path).month("alice", "2026-10").total == 2
    assert store.change("alice", "undo", "undo", NOW).replay
    assert store.month("alice", "2026-10").total == 2


def test_duplicate_after_undo_does_not_resurrect_entry(store):
    store.change("alice", "message", "add", NOW)
    store.change("alice", "undo", "undo", NOW)
    assert store.change("alice", "message", "add", NOW).replay
    assert store.month("alice", "2026-10").total == 0


def test_user_isolation_and_midnight_no_cross_day_undo(store):
    store.change("alice", "shared-id", "add", NOW)
    store.change("bob", "shared-id", "add", NOW)
    tomorrow = NOW.replace(day=22, hour=0, minute=0)
    assert store.change("alice", "undo", "undo", tomorrow).status == "empty"
    assert store.month("alice", "2026-10").counts == {21: 1}
    assert store.month("bob", "2026-10").total == 1


def test_month_year_boundary_and_leap_day(store):
    for day in ("2024-02-29", "2025-12-31", "2026-01-01"):
        store.change("alice", day, "add", datetime.fromisoformat(day).replace(tzinfo=NOW.tzinfo))
    assert store.month("alice", "2024-02").counts == {29: 1}
    assert store.month("alice", "2025-12").counts == {31: 1}
    assert store.month("alice", "2026-01").counts == {1: 1}


def test_concurrent_delivery_deduplicates_across_store_instances(store):
    def record(_):
        return CalendarStore(store.path).change("alice", "same-message", "add", NOW)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(record, range(24)))
    assert sum(not item.replay for item in results) == 1
    assert store.month("alice", "2026-10").total == 1


def test_concurrent_distinct_messages_cannot_bypass_interval(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: store.change("alice", str(i), "add", NOW), range(24)))
    assert store.month("alice", "2026-10").total == 1


def test_reads_do_not_write_and_invalid_action_rejected(store):
    assert store.month("alice", "2026-10").total == 0
    with pytest.raises(ValueError):
        store.change("alice", "bad", "remove-all", NOW)
    with pytest.raises(ValueError):
        store.change("alice", "bad", "add", NOW.replace(tzinfo=None))
    assert store.month("alice", "2026-10").total == 0


def test_receipts_work_across_dates(store):
    store.change("alice", "one", "add", NOW)
    assert store.change("alice", "one", "add", NOW.replace(month=11, day=1)).replay
    assert store.month("alice", "2026-11").total == 0


def test_exact_two_hour_boundary_and_rejected_attempt_does_not_extend_wait(store):
    store.change("alice", "first", "add", NOW)
    blocked = store.change("alice", "early", "add", NOW + timedelta(hours=2, seconds=-1))
    assert blocked.status == "cooldown"
    assert datetime.fromisoformat(blocked.available_at) == NOW + timedelta(hours=2)
    assert store.change("alice", "on-time", "add", NOW + timedelta(hours=2)).status == "added"
    assert store.month("alice", "2026-10").total == 2


def test_undo_restart_and_midnight_cannot_reset_interval(store):
    store.change("alice", "first", "add", NOW)
    store.change("alice", "undo", "undo", NOW)
    restarted = CalendarStore(store.path)
    assert restarted.change("alice", "new", "add", NOW + timedelta(minutes=30)).status == "cooldown"
    assert restarted.month("alice", "2026-10").total == 0
    assert restarted.change("alice", "later", "add", NOW + timedelta(hours=2)).status == "added"


def test_timezone_change_and_clock_rollback_do_not_bypass_interval(store):
    store.change("alice", "one", "add", NOW)
    same_instant = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    assert store.change("alice", "tz", "add", same_instant).status == "cooldown"
    assert store.change("alice", "clock", "add", NOW - timedelta(hours=1)).status == "cooldown"
    assert store.change("alice", "two", "add", same_instant + timedelta(hours=2)).status == "added"


def test_existing_records_and_legacy_receipts_are_preserved(store):
    import sqlite3
    # 模拟旧版没有 available_at 的成功回执与历史记录。
    with sqlite3.connect(store.path) as con:
        con.execute("INSERT INTO entries(actor,day,created_at) VALUES (?,?,?)", ("alice", NOW.date().isoformat(), NOW.isoformat()))
        con.execute("INSERT INTO receipts(actor,token,result) VALUES (?,?,?)", ("alice", "legacy", '{"status":"added","day":"2026-10-21"}'))
    assert CalendarStore(store.path).change("alice", "legacy", "add", NOW).replay
    assert store.change("alice", "fresh", "add", NOW + timedelta(minutes=1)).status == "cooldown"
    assert store.month("alice", "2026-10").total == 1


def test_cooldown_receipt_is_not_replayed_as_a_late_add(store):
    store.change("alice", "one", "add", NOW)
    store.change("alice", "blocked", "add", NOW + timedelta(minutes=1))
    result = store.change("alice", "blocked", "add", NOW + timedelta(hours=3))
    assert result.replay and result.status == "cooldown"
    assert store.month("alice", "2026-10").total == 1
