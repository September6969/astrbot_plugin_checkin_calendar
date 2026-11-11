"""按用户保存打卡，事务内处理计数、撤销和消息去重。"""

from __future__ import annotations

import json
import math
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


SHORT_HALF_LIFE_HOURS = 6.0
LONG_HALF_LIFE_HOURS = 36.0
RISK_SCALE = 4.0
DRAW_RESOLUTION = 1_000_000
DEFAULT_COOLDOWN_MINUTES = 120
DEFAULT_BLAST_COOLDOWN_MINUTES = 720
DEFAULT_BLAST_MAX_PERCENT = 80
MAX_COOLDOWN_MINUTES = 10_080
HISTORY_PAGE_SIZE = 10


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """配置超出范围时限制在边界，格式无效时使用默认值。"""
    if type(value) not in (int, str):
        return default
    try:
        return min(maximum, max(minimum, int(value)))
    except ValueError:
        return default


def blast_probability(short_load: float, long_load: float, max_percent: float = DEFAULT_BLAST_MAX_PERCENT) -> float:
    """双时间尺度负荷经 Weibull 累积风险映射，使用 expm1 保留低风险精度。"""
    load = 0.75 * short_load + 0.25 * long_load
    return max_percent * -math.expm1(-(load / RISK_SCALE) ** 2)


@dataclass(frozen=True)
class RiskState:
    today_count: int
    recent_count: int
    risk_percent: float
    available_at: str | None = None
    blast_cooling: bool = False


@dataclass(frozen=True)
class Change:
    status: str
    day: str | None = None
    replay: bool = False
    available_at: str | None = None
    risk_percent: float = 0


@dataclass(frozen=True)
class DrawRecord:
    id: int
    day: str
    created_at: str | None
    risk_percent: float | None
    result: str
    undone: bool
    cooldown_until: str | None
    source: str


@dataclass(frozen=True)
class HistoryStats:
    total: int
    blasted: int
    undone: int
    known_risks: int
    average: float | None
    highest: float | None
    lowest_blast: float | None
    migrated: int
    unknown_times: int


@dataclass(frozen=True)
class HistorySnapshot:
    month: str | None
    blast_only: bool
    page: int
    pages: int
    matching: int
    stats: HistoryStats
    records: list[DrawRecord]


class CalendarStore:
    def __init__(
        self, path: Path, *,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        blast_cooldown_minutes: int = DEFAULT_BLAST_COOLDOWN_MINUTES,
        blast_max_percent: int = DEFAULT_BLAST_MAX_PERCENT,
    ):
        self.cooldown_minutes = bounded_int(cooldown_minutes, DEFAULT_COOLDOWN_MINUTES, 0, MAX_COOLDOWN_MINUTES)
        self.blast_cooldown_minutes = bounded_int(
            blast_cooldown_minutes, DEFAULT_BLAST_COOLDOWN_MINUTES, 1, MAX_COOLDOWN_MINUTES,
        )
        self.blast_max_percent = bounded_int(blast_max_percent, DEFAULT_BLAST_MAX_PERCENT, 0, 100)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.executescript("""
                BEGIN IMMEDIATE;
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
                CREATE TABLE IF NOT EXISTS cooldown_resets (
                    actor TEXT PRIMARY KEY,
                    last_entry_id INTEGER NOT NULL
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
                CREATE TABLE IF NOT EXISTS draw_history (
                    id INTEGER PRIMARY KEY,
                    actor TEXT NOT NULL,
                    day TEXT NOT NULL,
                    created_at TEXT,
                    risk_percent REAL CHECK(risk_percent BETWEEN 0 AND 100),
                    result TEXT NOT NULL CHECK(result IN ('added','blasted')),
                    entry_id INTEGER UNIQUE REFERENCES entries(id),
                    cooldown_until TEXT,
                    source TEXT NOT NULL CHECK(source IN ('native','legacy_entry','legacy_receipt')),
                    CHECK((result='added') = (entry_id IS NOT NULL))
                );
                CREATE INDEX IF NOT EXISTS draw_history_order
                    ON draw_history(actor, COALESCE(created_at, day) DESC, id DESC);
                CREATE INDEX IF NOT EXISTS draw_history_result_order
                    ON draw_history(actor, result, COALESCE(created_at, day) DESC, id DESC);
                CREATE INDEX IF NOT EXISTS draw_history_month
                    ON draw_history(actor, day, result);
                CREATE TABLE IF NOT EXISTS history_migrations (version INTEGER PRIMARY KEY);
            """)
            self._migrate_history(con)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @classmethod
    def _legacy_timestamp(cls, value):
        try:
            return cls._timestamp(value).isoformat(timespec="microseconds")
        except sqlite3.DataError:
            return None

    @classmethod
    def _migrate_history(cls, con):
        if con.execute("SELECT 1 FROM history_migrations WHERE version=1").fetchone():
            return
        # 成功回执没有可靠的关联键，只迁移真实打卡记录。
        entries = con.execute("SELECT id, actor, day, created_at FROM entries ORDER BY id")
        con.executemany(
            """INSERT INTO draw_history(actor, day, created_at, result, entry_id, source)
               VALUES (?, ?, ?, 'added', ?, 'legacy_entry')""",
            ((actor, day, cls._legacy_timestamp(when), entry) for entry, actor, day, when in entries),
        )
        # 炸膛回执本身就是一次判定；旧冷却表只记录截止时间。
        for actor, raw in con.execute("SELECT actor, result FROM receipts ORDER BY actor, token"):
            try:
                receipt = json.loads(raw)
                if not isinstance(receipt, dict) or receipt.get("status") != "blasted":
                    continue
                day = date.fromisoformat(receipt["day"]).isoformat()
            except (ValueError, TypeError, KeyError):
                continue
            risk = receipt.get("risk_percent")
            if type(risk) not in (int, float) or not 0 <= risk <= 100:
                risk = None
            con.execute(
                """INSERT INTO draw_history(actor, day, risk_percent, result, cooldown_until, source)
                   VALUES (?, ?, ?, 'blasted', ?, 'legacy_receipt')""",
                (actor, day, risk, cls._legacy_timestamp(receipt.get("available_at"))),
            )
        con.execute("INSERT INTO history_migrations(version) VALUES (1)")

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def change(self, actor: str, token: str | None, action: str, now: datetime) -> Change:
        if action not in {"add", "undo", "reset_cooldown"}:
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
            entry_id = None
            if action == "add":
                state = self._risk(con, actor, now)
                if state.available_at:
                    result = Change("blast_cooldown" if state.blast_cooling else "cooldown", day,
                                    available_at=state.available_at, risk_percent=state.risk_percent)
                elif state.risk_percent and secrets.randbelow(DRAW_RESOLUTION) / (DRAW_RESOLUTION / 100) < state.risk_percent:
                    until = (now.astimezone(timezone.utc) + timedelta(minutes=self.blast_cooldown_minutes)).isoformat()
                    con.execute("INSERT INTO blast_cooldowns(actor,available_at) VALUES (?,?) ON CONFLICT(actor) DO UPDATE SET available_at=excluded.available_at", (actor, until))
                    result = Change("blasted", day, available_at=until, risk_percent=state.risk_percent)
                else:
                    entry_id = con.execute("INSERT INTO entries(actor,day,created_at) VALUES (?,?,?)",
                                           (actor, day, now.isoformat())).lastrowid
                    result = Change("added", day, risk_percent=state.risk_percent)
            elif action == "reset_cooldown":
                # 仅豁免重置前的记录，保留计数和概率使用的历史。
                con.execute(
                    """INSERT INTO cooldown_resets(actor,last_entry_id)
                    SELECT ?, COALESCE(MAX(id), 0) FROM entries WHERE actor=?
                    ON CONFLICT(actor) DO UPDATE SET last_entry_id=excluded.last_entry_id""",
                    (actor, actor),
                )
                con.execute("DELETE FROM blast_cooldowns WHERE actor=?", (actor,))
                result = Change("cooldown_reset", day)
            else:
                # 只撤销今天最后一次，避免跨日误删历史记录。
                row = con.execute("SELECT id FROM entries WHERE actor=? AND day=? AND undone=0 ORDER BY id DESC LIMIT 1", (actor, day)).fetchone()
                if row:
                    con.execute("UPDATE entries SET undone=1 WHERE id=?", (row[0],))
                result = Change("undone" if row else "empty", day)
            if result.status in {"added", "blasted"}:
                con.execute(
                    """INSERT INTO draw_history(actor, day, created_at, risk_percent, result, entry_id, cooldown_until, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'native')""",
                    (actor, day, now.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                     result.risk_percent, result.status, entry_id, result.available_at),
                )
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
        # 全部成功记录按绝对时间衰减，不按午夜或72小时硬截断；撤销不消除负荷。
        short_terms, long_terms = [], []
        today = recent = 0
        for (created_at,) in con.execute("SELECT created_at FROM entries WHERE actor=?", (actor,)):
            when = self._timestamp(created_at)
            if when > utc_now:
                continue
            age_hours = (utc_now - when).total_seconds() / 3600
            short_terms.append(2.0 ** (-age_hours / SHORT_HALF_LIFE_HOURS))
            long_terms.append(2.0 ** (-age_hours / LONG_HALF_LIFE_HOURS))
            today += when >= day_start
            recent += when > recent_start
        probability = blast_probability(math.fsum(short_terms), math.fsum(long_terms), self.blast_max_percent)
        deadline = None
        blast_cooling = False
        blast = con.execute("SELECT available_at FROM blast_cooldowns WHERE actor=?", (actor,)).fetchone()
        if blast and self._timestamp(blast[0]) > utc_now:
            deadline = self._timestamp(blast[0])
            blast_cooling = True
        last = con.execute(
            """SELECT created_at FROM entries WHERE actor=?
            AND id > COALESCE((SELECT last_entry_id FROM cooldown_resets WHERE actor=?), 0)
            ORDER BY id DESC LIMIT 1""",
            (actor, actor),
        ).fetchone()
        if last and self.cooldown_minutes:
            ordinary = self._timestamp(last[0]) + timedelta(minutes=self.cooldown_minutes)
            if ordinary > utc_now and (deadline is None or ordinary > deadline):
                deadline = ordinary
                blast_cooling = False
        return RiskState(today, recent, probability,
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

    def history(self, actor: str, month: str | None, blast_only: bool, page: int) -> HistorySnapshot:
        """同一读事务中查询统计和单页记录，避免并发产生口径差异。"""
        if not actor or type(page) is not int or page < 1:
            raise ValueError("历史查询缺少身份或有效页码")
        where = "h.actor=?"
        params = [actor]
        if month is not None:
            start = date.fromisoformat(month + "-01")
            if start.isoformat()[:7] != month:
                raise ValueError("月份格式应为 YYYY-MM")
            end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
            where += " AND h.day>=? AND h.day<?"
            params.extend((start.isoformat(), end.isoformat()))
        join = "FROM draw_history h LEFT JOIN entries e ON e.id=h.entry_id AND e.actor=h.actor"
        con = self._connect()
        try:
            con.execute("BEGIN")
            stats = HistoryStats(*con.execute(
                f"""SELECT COUNT(*), COUNT(CASE WHEN h.result='blasted' THEN 1 END),
                           COUNT(CASE WHEN e.undone=1 THEN 1 END), COUNT(h.risk_percent),
                           AVG(h.risk_percent), MAX(h.risk_percent),
                           MIN(CASE WHEN h.result='blasted' THEN h.risk_percent END),
                           COUNT(CASE WHEN h.source!='native' THEN 1 END),
                           COUNT(*)-COUNT(h.created_at)
                    {join} WHERE {where}""", params,
            ).fetchone())
            matching = stats.blasted if blast_only else stats.total
            pages = (matching + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
            records = []
            if page <= pages:
                if blast_only:
                    where += " AND h.result='blasted'"
                records = [DrawRecord(*row) for row in con.execute(
                    f"""SELECT h.id, h.day, h.created_at, h.risk_percent, h.result,
                               COALESCE(e.undone, 0), h.cooldown_until, h.source
                        {join} WHERE {where}
                        ORDER BY COALESCE(h.created_at, h.day) DESC, h.id DESC LIMIT ? OFFSET ?""",
                    (*params, HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE),
                )]
            return HistorySnapshot(month, blast_only, page, pages, matching, stats, records)
        finally:
            con.close()

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
