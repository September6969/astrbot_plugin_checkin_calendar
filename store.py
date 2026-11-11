"""按用户保存打卡，事务内处理计数、撤销和消息去重。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class MonthSnapshot:
    month: str
    counts: dict[int, int]
    total: int
    days: int


@dataclass(frozen=True)
class RankEntry:
    actor: str
    user_id: str
    name: str
    total: int
    days: int


@dataclass(frozen=True)
class RankingSnapshot:
    month: str
    total: int
    participants: int
    entries: list[RankEntry]


def blast_probability(today_count: int, recent_count: int) -> int:
    """按本次尝试前的成功次数计算；这是娱乐规则，不代表健康风险。"""
    return min(80, max(0, today_count - 1) * 10 + max(0, recent_count - 4) * 3)


@dataclass(frozen=True)
class RiskState:
    today_count: int
    recent_count: int
    risk_percent: int
    available_at: str | None = None
    blast_cooling: bool = False


@dataclass(frozen=True)
class Change:
    status: str
    day: str | None = None
    replay: bool = False
    available_at: str | None = None
    risk_percent: int = 0


class CalendarStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0 CHECK(undone IN (0,1))
                );
                CREATE INDEX IF NOT EXISTS entries_actor_day ON entries(actor, day, undone);
                CREATE INDEX IF NOT EXISTS entries_actor_recent ON entries(actor, id DESC);
                CREATE TABLE IF NOT EXISTS blast_cooldowns (
                    actor TEXT PRIMARY KEY,
                    available_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS display_names (
                    actor TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS entries_month ON entries(day, undone, actor);
                CREATE TABLE IF NOT EXISTS receipts (
                    actor TEXT NOT NULL,
                    token TEXT NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY(actor, token)
                );
            """)
            con.commit()
        finally:
            con.close()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.execute("PRAGMA busy_timeout=15000")
        return con

    def change(self, actor: str, token: str | None, action: str, now: datetime) -> Change:
        if action not in {"add", "undo"}:
            raise ValueError("不支持的记录操作")
        if not actor or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("缺少用户身份或时区")
        # 每次操作使用独立连接；写锁、去重和撤销必须处于同一事务。
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if token:
                previous = con.execute("SELECT result FROM receipts WHERE actor=? AND token=?", (actor, token)).fetchone()
                if previous:
                    con.commit()
                    return Change(**json.loads(previous[0]), replay=True)
            day = now.date().isoformat()
            if action == "add":
                state = self._risk(con, actor, now)
                if state.available_at:
                    result = Change("blast_cooldown" if state.blast_cooling else "cooldown", day,
                                    available_at=state.available_at, risk_percent=state.risk_percent)
                elif state.risk_percent and secrets.randbelow(100) < state.risk_percent:
                    until = (now.astimezone(timezone.utc) + timedelta(hours=12)).isoformat()
                    con.execute("INSERT INTO blast_cooldowns(actor,available_at) VALUES (?,?) ON CONFLICT(actor) DO UPDATE SET available_at=excluded.available_at", (actor, until))
                    result = Change("blasted", day, available_at=until, risk_percent=state.risk_percent)
                else:
                    con.execute("INSERT INTO entries(actor,day,created_at) VALUES (?,?,?)", (actor, day, now.isoformat()))
                    result = Change("added", day, risk_percent=state.risk_percent)
            else:
                # 只撤销今天最后一次，避免跨日误删历史记录。
                row = con.execute("SELECT id FROM entries WHERE actor=? AND day=? AND undone=0 ORDER BY id DESC LIMIT 1", (actor, day)).fetchone()
                if row:
                    con.execute("UPDATE entries SET undone=1 WHERE id=?", (row[0],))
                result = Change("undone" if row else "empty", day)
            if token:
                con.execute("INSERT INTO receipts(actor,token,result) VALUES (?,?,?)", (actor, token, json.dumps({"status": result.status, "day": result.day, "available_at": result.available_at, "risk_percent": result.risk_percent})))
            con.commit()
            return result
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _timestamp(value):
        try:
            value = datetime.fromisoformat(value)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("无时区")
            return value.astimezone(timezone.utc)
        except (ValueError, TypeError):
            raise sqlite3.DataError("历史时间无效或缺少时区") from None

    def _risk(self, con, actor, now):
        utc_now = now.astimezone(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        recent_start = utc_now - timedelta(hours=72)
        # 保留已撤销次数用于风险计算；绝对时间窗口不会在午夜整体清零。
        recent, today = con.execute("""SELECT COUNT(*), COALESCE(SUM(julianday(created_at)>=julianday(?)),0)
            FROM entries WHERE actor=? AND julianday(created_at)>julianday(?)
            AND julianday(created_at)<=julianday(?)""",
            (day_start.isoformat(), actor, recent_start.isoformat(), utc_now.isoformat())).fetchone()
        deadline = None
        blast_cooling = False
        blast = con.execute("SELECT available_at FROM blast_cooldowns WHERE actor=?", (actor,)).fetchone()
        if blast and self._timestamp(blast[0]) > utc_now:
            deadline = self._timestamp(blast[0])
            blast_cooling = True
        last = con.execute("SELECT created_at FROM entries WHERE actor=? ORDER BY id DESC LIMIT 1", (actor,)).fetchone()
        if last:
            ordinary = self._timestamp(last[0]) + timedelta(hours=2)
            if ordinary > utc_now and (deadline is None or ordinary > deadline):
                deadline = ordinary
        return RiskState(today, recent, blast_probability(today, recent),
                         deadline.isoformat() if deadline else None, blast_cooling)

    def risk(self, actor, now):
        if not actor or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("缺少用户身份或时区")
        con = self._connect()
        try:
            con.execute("BEGIN")
            return self._risk(con, actor, now)
        finally:
            con.close()

    def month(self, actor: str, month: str) -> MonthSnapshot:
        start = date.fromisoformat(month + "-01")
        if f"{start.year:04d}-{start.month:02d}" != month:
            raise ValueError("月份格式应为 YYYY-MM")
        end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
        con = self._connect()
        try:
            rows = con.execute("SELECT day, COUNT(*) FROM entries WHERE actor=? AND day>=? AND day<? AND undone=0 GROUP BY day", (actor, start.isoformat(), end.isoformat())).fetchall()
        finally:
            con.close()
        counts = {int(day[-2:]): count for day, count in rows}
        return MonthSnapshot(month, counts, sum(counts.values()), len(counts))

    def save_name(self, actor: str, name: str):
        if not name.strip():
            return
        con = self._connect()
        try:
            con.execute("INSERT INTO display_names(actor,name) VALUES (?,?) ON CONFLICT(actor) DO UPDATE SET name=excluded.name", (actor, name))
            con.commit()
        finally:
            con.close()

    def ranking(self, platform: str, bot: str, month: str) -> RankingSnapshot:
        start = date.fromisoformat(month + "-01")
        end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
        con = self._connect()
        try:
            # 单次聚合得到完整总数与前十，昵称只用于展示，不作为分组键。
            rows = con.execute("""SELECT e.actor, COALESCE(n.name,''), COUNT(*), COUNT(DISTINCT e.day)
                FROM entries e LEFT JOIN display_names n ON n.actor=e.actor
                WHERE e.day>=? AND e.day<? AND e.undone=0 GROUP BY e.actor""",
                (start.isoformat(), end.isoformat())).fetchall()
        finally:
            con.close()
        entries = []
        for actor, name, total, days in rows:
            try:
                identity = json.loads(actor)
            except (ValueError, TypeError):
                continue
            if not isinstance(identity, list) or len(identity) != 3 or identity[:2] != [platform, bot]:
                continue
            if not isinstance(identity[2], str) or not identity[2]:
                continue
            entries.append(RankEntry(actor, identity[2], name, total, days))
        entries.sort(key=lambda entry: (-entry.total, -entry.days, entry.user_id))
        return RankingSnapshot(month, sum(e.total for e in entries), len(entries), entries[:10])
