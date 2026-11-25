"""使用旧版结构验证迁移、原数据完整性和失败回滚。"""

import json
import sqlite3
from datetime import datetime

import pytest

from astrbot_plugin_checkin_calendar.store import CalendarStore
from test_history import ACTOR, ZONE


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                day TEXT NOT NULL, created_at TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE receipts (actor TEXT NOT NULL, token TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(actor,token));
            CREATE TABLE blast_cooldowns (actor TEXT PRIMARY KEY, available_at TEXT NOT NULL);
        """)
        con.executemany("INSERT INTO entries VALUES (?,?,?,?,?)", [
            (1, ACTOR, "2026-11-01", "2026-11-01T08:00:00+08:00", 1),
            (2, ACTOR, "2026-11-01", "2026-11-01T08:01:00+08:00", 0),
        ])
        for token, receipt in [
            ("success", {"status": "added", "day": "2026-11-01", "risk_percent": 11.5}),
            ("blast1", {"status": "blasted", "day": "2026-11-01", "risk_percent": 17.5,
                        "available_at": "2026-11-01T22:00:00+08:00"}),
            ("blast2", {"status": "blasted", "day": "2026-11-02"}),
            ("cooldown", {"status": "blast_cooldown", "day": "2026-11-01", "risk_percent": 95}),
        ]:
            con.execute("INSERT INTO receipts VALUES (?,?,?)", (ACTOR, token, json.dumps(receipt)))
        con.execute("INSERT INTO blast_cooldowns VALUES (?,?)", (ACTOR, "2026-11-01T22:00:00+08:00"))
    return path


def original_data(path):
    with sqlite3.connect(path) as con:
        return [con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("entries", "receipts", "blast_cooldowns")]


def test_legacy_data_is_conservative_and_migration_is_idempotent(legacy):
    before = original_data(legacy)
    store = CalendarStore(legacy)
    history = store.history(ACTOR, "2026-11", False, 1)
    assert (history.matching, history.stats.known_risks, history.stats.unknown_times) == (4, 1, 2)
    successes = [r for r in history.records if r.result == "added"]
    assert len(successes) == 2 and all(r.risk_percent is None for r in successes)
    assert [r.undone for r in successes] == [False, True]
    assert successes[0].created_at == "2026-11-01T00:01:00.000000+00:00"
    blasts = [r for r in history.records if r.result == "blasted"]
    assert [r.risk_percent for r in blasts] == [None, 17.5]
    assert all(r.created_at is None for r in blasts)
    assert blasts[1].cooldown_until == "2026-11-01T14:00:00.000000+00:00"
    assert history.stats.average == history.stats.highest == history.stats.lowest_blast == 17.5
    assert store.month(ACTOR, "2026-11").total == 1
    assert store.ranking("qq-1", "bot1", "2026-11").total == 1
    assert store.risk(ACTOR, datetime(2026, 11, 1, 10, tzinfo=ZONE)).blast_cooling
    assert CalendarStore(legacy).history(ACTOR, "2026-11", False, 1) == history
    assert original_data(legacy) == before


def test_migration_failure_rolls_back_schema_and_data(legacy, monkeypatch):
    before = original_data(legacy)
    migrate = CalendarStore._migrate_history

    def fail_after_migration(con):
        migrate(con)
        raise sqlite3.OperationalError("模拟迁移失败")

    with monkeypatch.context() as patch:
        patch.setattr(CalendarStore, "_migrate_history", staticmethod(fail_after_migration))
        with pytest.raises(sqlite3.OperationalError, match="模拟迁移失败"):
            CalendarStore(legacy)
    assert original_data(legacy) == before
    with sqlite3.connect(legacy) as con:
        assert not con.execute("SELECT name FROM sqlite_master WHERE name LIKE '%history%'").fetchall()
    assert CalendarStore(legacy).history(ACTOR, None, False, 1).matching == 4
