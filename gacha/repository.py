"""Gacha 数据库版本化迁移、原子十连/铸模/金票事务与查询仓库。"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .catalog import PoolSnapshot
from .collection import AcquisitionOutcome, apply_acquisition
from .engine import PullEngine, RandomSource
from .rewards import WalletBalance, compute_wallet_balance

GACHA_SCHEMA_VERSION = 1
MOLD_LABEL_COST = 6000
GOLDEN_MILEAGE_COST = 200


class GachaRepositoryError(RuntimeError):
    """抽卡持久化层错误。"""


class InsufficientTicketsError(GachaRepositoryError):
    """十连招募资格不足。"""


class InsufficientLabelsError(GachaRepositoryError):
    """躯体标签不足。"""


class InsufficientMileageError(GachaRepositoryError):
    """黄金里程券不足。"""


class MoldPoolMaxedError(GachaRepositoryError):
    """铸模池内全部 SSR 已达 CORE+7。"""


class ExchangeUnavailableError(GachaRepositoryError):
    """黄金里程兑换不可用或对象歧义。"""


@dataclass(frozen=True)
class SessionSlotRecord:
    slot: int
    character_id: int
    rarity: str
    p_ssr_ppm: int
    p_sr_ppm: int
    p_r_ppm: int
    before_count: int
    after_count: int
    outcome_tag: str
    labels_gained: int


@dataclass(frozen=True)
class PullStatsRecord:
    total_pulls: int
    total_ssr: int
    total_sr: int
    total_r: int
    current_no_ssr_streak: int
    max_no_ssr_streak: int
    max_ssr_in_ten_pull: int
    mold_count: int
    mileage_exchange_count: int


@dataclass(frozen=True)
class TenPullRecord:
    session_id: int
    actor: str
    idempotency_key: str
    snapshot_version: str
    snapshot_sha256: str
    algorithm_version: str
    ssr_count: int
    sr_count: int
    r_count: int
    mileage_gained: int
    labels_gained: int
    created_at: str
    slots: tuple[SessionSlotRecord, ...]
    wallet: WalletBalance
    stats: PullStatsRecord
    replay: bool = False


@dataclass(frozen=True)
class InventoryItemRecord:
    character_id: int
    total_obtained: int
    first_obtained_at: str
    mlb_at: str | None
    core_max_at: str | None
    last_obtained_at: str


@dataclass(frozen=True)
class AcquisitionRecord:
    id: int
    actor: str
    character_id: int
    rarity: str
    source: str
    session_id: int | None
    slot: int | None
    before_count: int
    after_count: int
    outcome_tag: str
    labels_delta: int
    mileage_delta: int
    created_at: str


@dataclass(frozen=True)
class SessionSummaryRecord:
    session_id: int
    ssr_count: int
    sr_count: int
    r_count: int
    ssr_character_ids: tuple[int, ...]
    created_at: str


@dataclass(frozen=True)
class ExchangeResultRecord:
    acquisition_id: int
    actor: str
    source: str
    idempotency_key: str
    character_id: int
    rarity: str
    before_count: int
    after_count: int
    outcome_tag: str
    cost_amount: int
    labels_refunded: int
    net_labels_delta: int
    net_mileage_delta: int
    wallet: WalletBalance
    created_at: str
    replay: bool = False


def _now_utc_iso(now: datetime | None = None) -> str:
    if now is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("缺少时区信息")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


def ensure_gacha_schema(con: sqlite3.Connection, now_iso: str) -> None:
    """在调用方事务内执行幂等 Gacha 表结构迁移与上线基线边界记录。"""
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_migrations (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_meta (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_wallet (
               actor TEXT PRIMARY KEY,
               checkin_count INTEGER NOT NULL DEFAULT 0 CHECK(checkin_count >= 0),
               consumed_tickets INTEGER NOT NULL DEFAULT 0 CHECK(consumed_tickets >= 0),
               golden_mileage INTEGER NOT NULL DEFAULT 0 CHECK(golden_mileage >= 0),
               body_labels INTEGER NOT NULL DEFAULT 0 CHECK(body_labels >= 0),
               updated_at TEXT NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_reward_ledger (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               actor TEXT NOT NULL,
               event_type TEXT NOT NULL CHECK(event_type IN ('checkin_add','checkin_undo','ten_pull_consume')),
               entry_id INTEGER,
               session_id INTEGER,
               delta_checkins INTEGER NOT NULL DEFAULT 0,
               delta_tickets INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL,
               UNIQUE(event_type, entry_id)
           )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS gacha_reward_ledger_actor_idx ON gacha_reward_ledger(actor, id)"
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_pull_stats (
               actor TEXT PRIMARY KEY,
               total_pulls INTEGER NOT NULL DEFAULT 0 CHECK(total_pulls >= 0),
               total_ssr INTEGER NOT NULL DEFAULT 0 CHECK(total_ssr >= 0),
               total_sr INTEGER NOT NULL DEFAULT 0 CHECK(total_sr >= 0),
               total_r INTEGER NOT NULL DEFAULT 0 CHECK(total_r >= 0),
               current_no_ssr_streak INTEGER NOT NULL DEFAULT 0 CHECK(current_no_ssr_streak >= 0),
               max_no_ssr_streak INTEGER NOT NULL DEFAULT 0 CHECK(max_no_ssr_streak >= 0),
               max_ssr_in_ten_pull INTEGER NOT NULL DEFAULT 0 CHECK(max_ssr_in_ten_pull >= 0),
               mold_count INTEGER NOT NULL DEFAULT 0 CHECK(mold_count >= 0),
               mileage_exchange_count INTEGER NOT NULL DEFAULT 0 CHECK(mileage_exchange_count >= 0),
               updated_at TEXT NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_inventory (
               actor TEXT NOT NULL,
               character_id INTEGER NOT NULL,
               total_obtained INTEGER NOT NULL CHECK(total_obtained >= 1),
               first_obtained_at TEXT NOT NULL,
               mlb_at TEXT,
               core_max_at TEXT,
               last_obtained_at TEXT NOT NULL,
               PRIMARY KEY(actor, character_id)
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_sessions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               actor TEXT NOT NULL,
               idempotency_key TEXT NOT NULL,
               snapshot_version TEXT NOT NULL,
               snapshot_sha256 TEXT NOT NULL,
               algorithm_version TEXT NOT NULL,
               ssr_count INTEGER NOT NULL CHECK(ssr_count >= 0),
               sr_count INTEGER NOT NULL CHECK(sr_count >= 0),
               r_count INTEGER NOT NULL CHECK(r_count >= 0),
               mileage_gained INTEGER NOT NULL DEFAULT 10,
               labels_gained INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL,
               CHECK(ssr_count + sr_count + r_count = 10),
               UNIQUE(actor, idempotency_key)
           )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS gacha_sessions_actor_idx ON gacha_sessions(actor, id DESC)"
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_results (
               session_id INTEGER NOT NULL REFERENCES gacha_sessions(id),
               slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 10),
               character_id INTEGER NOT NULL,
               rarity TEXT NOT NULL CHECK(rarity IN ('R','SR','SSR')),
               p_ssr_ppm INTEGER NOT NULL,
               p_sr_ppm INTEGER NOT NULL,
               p_r_ppm INTEGER NOT NULL,
               before_count INTEGER NOT NULL CHECK(before_count >= 0),
               after_count INTEGER NOT NULL CHECK(after_count >= 1),
               outcome_tag TEXT NOT NULL,
               labels_gained INTEGER NOT NULL DEFAULT 0 CHECK(labels_gained >= 0),
               PRIMARY KEY(session_id, slot)
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gacha_acquisitions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               actor TEXT NOT NULL,
               character_id INTEGER NOT NULL,
               rarity TEXT NOT NULL CHECK(rarity IN ('R','SR','SSR')),
               source TEXT NOT NULL CHECK(source IN ('ten_pull','mold','golden_mileage')),
               session_id INTEGER REFERENCES gacha_sessions(id),
               slot INTEGER,
               idempotency_key TEXT,
               before_count INTEGER NOT NULL CHECK(before_count >= 0),
               after_count INTEGER NOT NULL CHECK(after_count >= 1),
               outcome_tag TEXT NOT NULL,
               labels_delta INTEGER NOT NULL DEFAULT 0,
               mileage_delta INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL
           )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS gacha_acquisitions_char_idx ON gacha_acquisitions(actor, character_id, id DESC)"
    )
    con.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS gacha_acquisitions_idem_idx
           ON gacha_acquisitions(actor, source, idempotency_key)
           WHERE idempotency_key IS NOT NULL"""
    )

    applied = con.execute(
        "SELECT 1 FROM gacha_migrations WHERE version=?",
        (GACHA_SCHEMA_VERSION,),
    ).fetchone()
    if not applied:
        has_entries = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
        ).fetchone()
        cutoff_id = 0
        if has_entries:
            row = con.execute("SELECT COALESCE(MAX(id), 0) FROM entries").fetchone()
            cutoff_id = int(row[0]) if row else 0
        con.execute(
            "INSERT OR IGNORE INTO gacha_meta(key, value) VALUES ('cutoff_entry_id', ?)",
            (str(cutoff_id),),
        )
        con.execute(
            "INSERT INTO gacha_migrations(version, applied_at) VALUES (?, ?)",
            (GACHA_SCHEMA_VERSION, now_iso),
        )


class GachaRepository:
    """SQLite Gacha 仓库，共享 calendar.sqlite3 并保持独立连接与事务边界。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            ensure_gacha_schema(con, _now_utc_iso())
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @staticmethod
    def _read_wallet(con: sqlite3.Connection, actor: str) -> WalletBalance:
        row = con.execute(
            """SELECT checkin_count, consumed_tickets, golden_mileage, body_labels
               FROM gacha_wallet WHERE actor=?""",
            (actor,),
        ).fetchone()
        if not row:
            return compute_wallet_balance(0, 0, 0, 0)
        return compute_wallet_balance(int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    @staticmethod
    def _read_stats(con: sqlite3.Connection, actor: str) -> PullStatsRecord:
        row = con.execute(
            """SELECT total_pulls, total_ssr, total_sr, total_r,
                      current_no_ssr_streak, max_no_ssr_streak,
                      max_ssr_in_ten_pull, mold_count, mileage_exchange_count
               FROM gacha_pull_stats WHERE actor=?""",
            (actor,),
        ).fetchone()
        if not row:
            return PullStatsRecord(0, 0, 0, 0, 0, 0, 0, 0, 0)
        return PullStatsRecord(*(int(x) for x in row))

    @staticmethod
    def _load_session_slots(con: sqlite3.Connection, session_id: int) -> tuple[SessionSlotRecord, ...]:
        rows = con.execute(
            """SELECT slot, character_id, rarity, p_ssr_ppm, p_sr_ppm, p_r_ppm,
                      before_count, after_count, outcome_tag, labels_gained
               FROM gacha_results WHERE session_id=? ORDER BY slot ASC""",
            (session_id,),
        ).fetchall()
        return tuple(
            SessionSlotRecord(
                slot=int(r[0]),
                character_id=int(r[1]),
                rarity=str(r[2]),
                p_ssr_ppm=int(r[3]),
                p_sr_ppm=int(r[4]),
                p_r_ppm=int(r[5]),
                before_count=int(r[6]),
                after_count=int(r[7]),
                outcome_tag=str(r[8]),
                labels_gained=int(r[9]),
            )
            for r in rows
        )

    @staticmethod
    def _record_inventory_update(
        con: sqlite3.Connection,
        actor: str,
        character_id: int,
        rarity: str,
        now_iso: str,
    ) -> AcquisitionOutcome:
        inv_row = con.execute(
            "SELECT total_obtained, mlb_at, core_max_at FROM gacha_inventory WHERE actor=? AND character_id=?",
            (actor, character_id),
        ).fetchone()
        before_count = int(inv_row[0]) if inv_row else 0
        outcome = apply_acquisition(rarity, before_count)

        if inv_row:
            mlb_at = inv_row[1] if inv_row[1] is not None else (now_iso if outcome.reached_mlb else None)
            core_max_at = (
                inv_row[2] if inv_row[2] is not None else (now_iso if outcome.reached_core_max else None)
            )
            con.execute(
                """UPDATE gacha_inventory
                   SET total_obtained=?, mlb_at=?, core_max_at=?, last_obtained_at=?
                   WHERE actor=? AND character_id=?""",
                (outcome.after_count, mlb_at, core_max_at, now_iso, actor, character_id),
            )
        else:
            con.execute(
                """INSERT INTO gacha_inventory(
                       actor, character_id, total_obtained,
                       first_obtained_at, mlb_at, core_max_at, last_obtained_at
                   ) VALUES (?, ?, 1, ?, NULL, NULL, ?)""",
                (actor, character_id, now_iso, now_iso),
            )
        return outcome

    def get_wallet(self, actor: str) -> WalletBalance:
        if not actor:
            raise ValueError("缺少用户身份")
        con = self._connect()
        try:
            con.execute("BEGIN")
            return self._read_wallet(con, actor)
        finally:
            con.close()

    def get_pull_stats(self, actor: str) -> PullStatsRecord:
        if not actor:
            raise ValueError("缺少用户身份")
        con = self._connect()
        try:
            con.execute("BEGIN")
            return self._read_stats(con, actor)
        finally:
            con.close()

    def get_inventory(self, actor: str) -> dict[int, InventoryItemRecord]:
        if not actor:
            raise ValueError("缺少用户身份")
        con = self._connect()
        try:
            con.execute("BEGIN")
            rows = con.execute(
                """SELECT character_id, total_obtained, first_obtained_at, mlb_at, core_max_at, last_obtained_at
                   FROM gacha_inventory WHERE actor=?""",
                (actor,),
            ).fetchall()
            return {
                int(r[0]): InventoryItemRecord(
                    character_id=int(r[0]),
                    total_obtained=int(r[1]),
                    first_obtained_at=str(r[2]),
                    mlb_at=str(r[3]) if r[3] is not None else None,
                    core_max_at=str(r[4]) if r[4] is not None else None,
                    last_obtained_at=str(r[5]),
                )
                for r in rows
            }
        finally:
            con.close()

    def list_sessions(
        self,
        actor: str,
        *,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[int, list[SessionSummaryRecord]]:
        if not actor or page < 1 or page_size < 1:
            raise ValueError("历史查询缺少身份或有效页码")
        con = self._connect()
        try:
            con.execute("BEGIN")
            total = int(
                con.execute("SELECT COUNT(*) FROM gacha_sessions WHERE actor=?", (actor,)).fetchone()[0]
            )
            rows = con.execute(
                """SELECT id, ssr_count, sr_count, r_count, created_at
                   FROM gacha_sessions WHERE actor=?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (actor, page_size, (page - 1) * page_size),
            ).fetchall()
            summaries: list[SessionSummaryRecord] = []
            for r in rows:
                sid = int(r[0])
                ssr_rows = con.execute(
                    "SELECT character_id FROM gacha_results WHERE session_id=? AND rarity='SSR' ORDER BY slot ASC",
                    (sid,),
                ).fetchall()
                summaries.append(
                    SessionSummaryRecord(
                        session_id=sid,
                        ssr_count=int(r[1]),
                        sr_count=int(r[2]),
                        r_count=int(r[3]),
                        ssr_character_ids=tuple(int(x[0]) for x in ssr_rows),
                        created_at=str(r[4]),
                    )
                )
            return total, summaries
        finally:
            con.close()

    def get_session_detail(self, actor: str, session_id: int) -> TenPullRecord | None:
        if not actor or session_id < 1:
            return None
        con = self._connect()
        try:
            con.execute("BEGIN")
            row = con.execute(
                """SELECT id, idempotency_key, snapshot_version, snapshot_sha256, algorithm_version,
                          ssr_count, sr_count, r_count, mileage_gained, labels_gained, created_at
                   FROM gacha_sessions WHERE id=? AND actor=?""",
                (session_id, actor),
            ).fetchone()
            if not row:
                return None
            slots = self._load_session_slots(con, int(row[0]))
            wallet = self._read_wallet(con, actor)
            stats = self._read_stats(con, actor)
            return TenPullRecord(
                session_id=int(row[0]),
                actor=actor,
                idempotency_key=str(row[1]),
                snapshot_version=str(row[2]),
                snapshot_sha256=str(row[3]),
                algorithm_version=str(row[4]),
                ssr_count=int(row[5]),
                sr_count=int(row[6]),
                r_count=int(row[7]),
                mileage_gained=int(row[8]),
                labels_gained=int(row[9]),
                created_at=str(row[10]),
                slots=slots,
                wallet=wallet,
                stats=stats,
                replay=False,
            )
        finally:
            con.close()

    def list_character_acquisitions(
        self,
        actor: str,
        character_id: int,
        *,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[int, list[AcquisitionRecord]]:
        if not actor or page < 1 or page_size < 1:
            raise ValueError("角色历史查询缺少身份或有效页码")
        con = self._connect()
        try:
            con.execute("BEGIN")
            total = int(
                con.execute(
                    "SELECT COUNT(*) FROM gacha_acquisitions WHERE actor=? AND character_id=?",
                    (actor, character_id),
                ).fetchone()[0]
            )
            rows = con.execute(
                """SELECT id, actor, character_id, rarity, source, session_id, slot,
                          before_count, after_count, outcome_tag, labels_delta, mileage_delta, created_at
                   FROM gacha_acquisitions WHERE actor=? AND character_id=?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (actor, character_id, page_size, (page - 1) * page_size),
            ).fetchall()
            records = [
                AcquisitionRecord(
                    id=int(r[0]),
                    actor=str(r[1]),
                    character_id=int(r[2]),
                    rarity=str(r[3]),
                    source=str(r[4]),
                    session_id=int(r[5]) if r[5] is not None else None,
                    slot=int(r[6]) if r[6] is not None else None,
                    before_count=int(r[7]),
                    after_count=int(r[8]),
                    outcome_tag=str(r[9]),
                    labels_delta=int(r[10]),
                    mileage_delta=int(r[11]),
                    created_at=str(r[12]),
                )
                for r in rows
            ]
            return total, records
        finally:
            con.close()

    def execute_ten_pull(
        self,
        actor: str,
        idempotency_key: str,
        snapshot: PoolSnapshot,
        engine: PullEngine,
        now: datetime,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> TenPullRecord:
        """在单一 BEGIN IMMEDIATE 事务中执行幂等检查、扣券、十连落库、突破/溢出、+10 金票与统计更新。"""
        if not actor or not idempotency_key:
            raise ValueError("缺少用户身份或幂等键")
        now_iso = _now_utc_iso(now)

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")

            existing = con.execute(
                """SELECT id, snapshot_version, snapshot_sha256, algorithm_version,
                          ssr_count, sr_count, r_count, mileage_gained, labels_gained, created_at
                   FROM gacha_sessions WHERE actor=? AND idempotency_key=?""",
                (actor, idempotency_key),
            ).fetchone()
            if existing:
                session_id = int(existing[0])
                slots = self._load_session_slots(con, session_id)
                wallet = self._read_wallet(con, actor)
                stats = self._read_stats(con, actor)
                con.commit()
                return TenPullRecord(
                    session_id=session_id,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    snapshot_version=str(existing[1]),
                    snapshot_sha256=str(existing[2]),
                    algorithm_version=str(existing[3]),
                    ssr_count=int(existing[4]),
                    sr_count=int(existing[5]),
                    r_count=int(existing[6]),
                    mileage_gained=int(existing[7]),
                    labels_gained=int(existing[8]),
                    created_at=str(existing[9]),
                    slots=slots,
                    wallet=wallet,
                    stats=stats,
                    replay=True,
                )

            wallet = self._read_wallet(con, actor)
            if wallet.available_tickets < 1:
                raise InsufficientTicketsError(
                    f"十连资格不足（当前进度 {wallet.checkin_progress}/2"
                    + (f"，待抵扣 {wallet.debt_tickets} 次" if wallet.debt_tickets else "")
                    + "）"
                )

            stats = self._read_stats(con, actor)
            outcome = engine.execute_ten_pull(snapshot, stats.total_pulls, stats.total_ssr)

            mileage_gained = 10
            session_id = con.execute(
                """INSERT INTO gacha_sessions(
                       actor, idempotency_key, snapshot_version, snapshot_sha256,
                       algorithm_version, ssr_count, sr_count, r_count,
                       mileage_gained, labels_gained, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    actor,
                    idempotency_key,
                    snapshot.snapshot_version,
                    snapshot.content_sha256,
                    outcome.algorithm_version,
                    outcome.ssr_count,
                    outcome.sr_count,
                    outcome.r_count,
                    mileage_gained,
                    now_iso,
                ),
            ).lastrowid
            assert session_id is not None

            streak = stats.current_no_ssr_streak
            max_streak = stats.max_no_ssr_streak
            total_labels_gained = 0
            persisted_slots: list[SessionSlotRecord] = []

            for slot in outcome.slots:
                acq = self._record_inventory_update(
                    con, actor, slot.character_id, slot.rarity, now_iso
                )
                total_labels_gained += acq.labels_gained

                con.execute(
                    """INSERT INTO gacha_results(
                           session_id, slot, character_id, rarity,
                           p_ssr_ppm, p_sr_ppm, p_r_ppm,
                           before_count, after_count, outcome_tag, labels_gained
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        slot.slot,
                        slot.character_id,
                        slot.rarity,
                        slot.p_ssr_ppm,
                        slot.p_sr_ppm,
                        slot.p_r_ppm,
                        acq.before_count,
                        acq.after_count,
                        acq.outcome_tag,
                        acq.labels_gained,
                    ),
                )
                con.execute(
                    """INSERT INTO gacha_acquisitions(
                           actor, character_id, rarity, source, session_id, slot,
                           idempotency_key, before_count, after_count, outcome_tag,
                           labels_delta, mileage_delta, created_at
                       ) VALUES (?, ?, ?, 'ten_pull', ?, ?, NULL, ?, ?, ?, ?, 0, ?)""",
                    (
                        actor,
                        slot.character_id,
                        slot.rarity,
                        session_id,
                        slot.slot,
                        acq.before_count,
                        acq.after_count,
                        acq.outcome_tag,
                        acq.labels_gained,
                        now_iso,
                    ),
                )
                persisted_slots.append(
                    SessionSlotRecord(
                        slot=slot.slot,
                        character_id=slot.character_id,
                        rarity=slot.rarity,
                        p_ssr_ppm=slot.p_ssr_ppm,
                        p_sr_ppm=slot.p_sr_ppm,
                        p_r_ppm=slot.p_r_ppm,
                        before_count=acq.before_count,
                        after_count=acq.after_count,
                        outcome_tag=acq.outcome_tag,
                        labels_gained=acq.labels_gained,
                    )
                )

                if slot.rarity == "SSR":
                    streak = 0
                else:
                    streak += 1
                    if streak > max_streak:
                        max_streak = streak

            if total_labels_gained:
                con.execute(
                    "UPDATE gacha_sessions SET labels_gained=? WHERE id=?",
                    (total_labels_gained, session_id),
                )

            new_consumed = wallet.consumed_tickets + 1
            new_mileage = wallet.golden_mileage + mileage_gained
            new_labels = wallet.body_labels + total_labels_gained
            con.execute(
                """INSERT INTO gacha_wallet(
                       actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(actor) DO UPDATE SET
                       consumed_tickets = excluded.consumed_tickets,
                       golden_mileage = excluded.golden_mileage,
                       body_labels = excluded.body_labels,
                       updated_at = excluded.updated_at""",
                (actor, wallet.checkin_count, new_consumed, new_mileage, new_labels, now_iso),
            )
            con.execute(
                """INSERT INTO gacha_reward_ledger(
                       actor, event_type, entry_id, session_id, delta_checkins, delta_tickets, created_at
                   ) VALUES (?, 'ten_pull_consume', NULL, ?, 0, -1, ?)""",
                (actor, session_id, now_iso),
            )

            new_stats = PullStatsRecord(
                total_pulls=stats.total_pulls + 10,
                total_ssr=stats.total_ssr + outcome.ssr_count,
                total_sr=stats.total_sr + outcome.sr_count,
                total_r=stats.total_r + outcome.r_count,
                current_no_ssr_streak=streak,
                max_no_ssr_streak=max_streak,
                max_ssr_in_ten_pull=max(stats.max_ssr_in_ten_pull, outcome.ssr_count),
                mold_count=stats.mold_count,
                mileage_exchange_count=stats.mileage_exchange_count,
            )
            con.execute(
                """INSERT INTO gacha_pull_stats(
                       actor, total_pulls, total_ssr, total_sr, total_r,
                       current_no_ssr_streak, max_no_ssr_streak, max_ssr_in_ten_pull,
                       mold_count, mileage_exchange_count, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(actor) DO UPDATE SET
                       total_pulls = excluded.total_pulls,
                       total_ssr = excluded.total_ssr,
                       total_sr = excluded.total_sr,
                       total_r = excluded.total_r,
                       current_no_ssr_streak = excluded.current_no_ssr_streak,
                       max_no_ssr_streak = excluded.max_no_ssr_streak,
                       max_ssr_in_ten_pull = excluded.max_ssr_in_ten_pull,
                       updated_at = excluded.updated_at""",
                (
                    actor,
                    new_stats.total_pulls,
                    new_stats.total_ssr,
                    new_stats.total_sr,
                    new_stats.total_r,
                    new_stats.current_no_ssr_streak,
                    new_stats.max_no_ssr_streak,
                    new_stats.max_ssr_in_ten_pull,
                    new_stats.mold_count,
                    new_stats.mileage_exchange_count,
                    now_iso,
                ),
            )

            if fault_injector is not None:
                fault_injector("before_commit")

            con.commit()
            new_wallet = compute_wallet_balance(
                wallet.checkin_count, new_consumed, new_mileage, new_labels
            )
            return TenPullRecord(
                session_id=int(session_id),
                actor=actor,
                idempotency_key=idempotency_key,
                snapshot_version=snapshot.snapshot_version,
                snapshot_sha256=snapshot.content_sha256,
                algorithm_version=outcome.algorithm_version,
                ssr_count=outcome.ssr_count,
                sr_count=outcome.sr_count,
                r_count=outcome.r_count,
                mileage_gained=mileage_gained,
                labels_gained=total_labels_gained,
                created_at=now_iso,
                slots=tuple(persisted_slots),
                wallet=new_wallet,
                stats=new_stats,
                replay=False,
            )
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def execute_mold(
        self,
        actor: str,
        idempotency_key: str,
        snapshot: PoolSnapshot,
        now: datetime,
        *,
        rng: RandomSource | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> ExchangeResultRecord:
        """消耗 6000 躯体标签开启完美铸模（必得铸模池 SSR；全满池拒绝；不计仓检、不产金票）。"""
        if not actor or not idempotency_key:
            raise ValueError("缺少用户身份或幂等键")
        if not snapshot.mold_pool:
            raise ValueError("完美铸模池为空")
        now_iso = _now_utc_iso(now)

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")

            existing = con.execute(
                """SELECT id, character_id, rarity, before_count, after_count,
                          outcome_tag, labels_delta, mileage_delta, created_at
                   FROM gacha_acquisitions
                   WHERE actor=? AND source='mold' AND idempotency_key=?""",
                (actor, idempotency_key),
            ).fetchone()
            if existing:
                wallet = self._read_wallet(con, actor)
                con.commit()
                tag = str(existing[5])
                refund = 6000 if tag == "OVERFLOW" else 0
                return ExchangeResultRecord(
                    acquisition_id=int(existing[0]),
                    actor=actor,
                    source="mold",
                    idempotency_key=idempotency_key,
                    character_id=int(existing[1]),
                    rarity=str(existing[2]),
                    before_count=int(existing[3]),
                    after_count=int(existing[4]),
                    outcome_tag=tag,
                    cost_amount=MOLD_LABEL_COST,
                    labels_refunded=refund,
                    net_labels_delta=int(existing[6]),
                    net_mileage_delta=int(existing[7]),
                    wallet=wallet,
                    created_at=str(existing[8]),
                    replay=True,
                )

            placeholders = ",".join("?" for _ in snapshot.mold_pool)
            owned_rows = con.execute(
                f"SELECT character_id, total_obtained FROM gacha_inventory WHERE actor=? AND character_id IN ({placeholders})",
                (actor, *snapshot.mold_pool),
            ).fetchall()
            owned_counts = {int(r[0]): int(r[1]) for r in owned_rows}
            if all(owned_counts.get(cid, 0) >= 11 for cid in snapshot.mold_pool):
                raise MoldPoolMaxedError(
                    "完美铸模池内全部 SSR 均已达成 CORE+7，暂无有效收藏收益，未扣除躯体标签。"
                )

            wallet = self._read_wallet(con, actor)
            if wallet.body_labels < MOLD_LABEL_COST:
                raise InsufficientLabelsError(
                    f"躯体标签不足（需要 {MOLD_LABEL_COST}，当前 {wallet.body_labels}）"
                )

            pick_idx = (
                rng.randbelow(len(snapshot.mold_pool))
                if rng is not None
                else secrets.randbelow(len(snapshot.mold_pool))
            )
            if pick_idx < 0 or pick_idx >= len(snapshot.mold_pool):
                raise ValueError(f"随机源下标越界: {pick_idx}")
            character_id = snapshot.mold_pool[pick_idx]

            acq = self._record_inventory_update(con, actor, character_id, "SSR", now_iso)
            net_labels_delta = -MOLD_LABEL_COST + acq.labels_gained
            new_labels = wallet.body_labels + net_labels_delta

            con.execute(
                """INSERT INTO gacha_wallet(
                       actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(actor) DO UPDATE SET
                       body_labels = excluded.body_labels,
                       updated_at = excluded.updated_at""",
                (actor, wallet.checkin_count, wallet.consumed_tickets, wallet.golden_mileage, new_labels, now_iso),
            )

            stats = self._read_stats(con, actor)
            con.execute(
                """INSERT INTO gacha_pull_stats(
                       actor, total_pulls, total_ssr, total_sr, total_r,
                       current_no_ssr_streak, max_no_ssr_streak, max_ssr_in_ten_pull,
                       mold_count, mileage_exchange_count, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(actor) DO UPDATE SET
                       mold_count = excluded.mold_count,
                       updated_at = excluded.updated_at""",
                (
                    actor,
                    stats.total_pulls,
                    stats.total_ssr,
                    stats.total_sr,
                    stats.total_r,
                    stats.current_no_ssr_streak,
                    stats.max_no_ssr_streak,
                    stats.max_ssr_in_ten_pull,
                    stats.mold_count + 1,
                    stats.mileage_exchange_count,
                    now_iso,
                ),
            )

            acq_id = con.execute(
                """INSERT INTO gacha_acquisitions(
                       actor, character_id, rarity, source, session_id, slot,
                       idempotency_key, before_count, after_count, outcome_tag,
                       labels_delta, mileage_delta, created_at
                   ) VALUES (?, ?, 'SSR', 'mold', NULL, NULL, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    actor,
                    character_id,
                    idempotency_key,
                    acq.before_count,
                    acq.after_count,
                    acq.outcome_tag,
                    net_labels_delta,
                    now_iso,
                ),
            ).lastrowid
            assert acq_id is not None

            if fault_injector is not None:
                fault_injector("before_commit")

            con.commit()
            new_wallet = compute_wallet_balance(
                wallet.checkin_count, wallet.consumed_tickets, wallet.golden_mileage, new_labels
            )
            return ExchangeResultRecord(
                acquisition_id=int(acq_id),
                actor=actor,
                source="mold",
                idempotency_key=idempotency_key,
                character_id=character_id,
                rarity="SSR",
                before_count=acq.before_count,
                after_count=acq.after_count,
                outcome_tag=acq.outcome_tag,
                cost_amount=MOLD_LABEL_COST,
                labels_refunded=acq.labels_gained,
                net_labels_delta=net_labels_delta,
                net_mileage_delta=0,
                wallet=new_wallet,
                created_at=now_iso,
                replay=False,
            )
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def execute_golden_mileage(
        self,
        actor: str,
        idempotency_key: str,
        character_id: int,
        now: datetime,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> ExchangeResultRecord:
        """消耗 200 黄金里程券兑换指定当期 UP SSR（不计仓检、不产金票）。"""
        if not actor or not idempotency_key:
            raise ValueError("缺少用户身份或幂等键")
        now_iso = _now_utc_iso(now)

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")

            existing = con.execute(
                """SELECT id, character_id, rarity, before_count, after_count,
                          outcome_tag, labels_delta, mileage_delta, created_at
                   FROM gacha_acquisitions
                   WHERE actor=? AND source='golden_mileage' AND idempotency_key=?""",
                (actor, idempotency_key),
            ).fetchone()
            if existing:
                wallet = self._read_wallet(con, actor)
                con.commit()
                return ExchangeResultRecord(
                    acquisition_id=int(existing[0]),
                    actor=actor,
                    source="golden_mileage",
                    idempotency_key=idempotency_key,
                    character_id=int(existing[1]),
                    rarity=str(existing[2]),
                    before_count=int(existing[3]),
                    after_count=int(existing[4]),
                    outcome_tag=str(existing[5]),
                    cost_amount=GOLDEN_MILEAGE_COST,
                    labels_refunded=int(existing[6]),
                    net_labels_delta=int(existing[6]),
                    net_mileage_delta=int(existing[7]),
                    wallet=wallet,
                    created_at=str(existing[8]),
                    replay=True,
                )

            wallet = self._read_wallet(con, actor)
            if wallet.golden_mileage < GOLDEN_MILEAGE_COST:
                raise InsufficientMileageError(
                    f"黄金里程券不足（需要 {GOLDEN_MILEAGE_COST}，当前 {wallet.golden_mileage}）"
                )

            acq = self._record_inventory_update(con, actor, character_id, "SSR", now_iso)
            new_mileage = wallet.golden_mileage - GOLDEN_MILEAGE_COST
            new_labels = wallet.body_labels + acq.labels_gained

            con.execute(
                """INSERT INTO gacha_wallet(
                       actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(actor) DO UPDATE SET
                       golden_mileage = excluded.golden_mileage,
                       body_labels = excluded.body_labels,
                       updated_at = excluded.updated_at""",
                (actor, wallet.checkin_count, wallet.consumed_tickets, new_mileage, new_labels, now_iso),
            )

            stats = self._read_stats(con, actor)
            con.execute(
                """INSERT INTO gacha_pull_stats(
                       actor, total_pulls, total_ssr, total_sr, total_r,
                       current_no_ssr_streak, max_no_ssr_streak, max_ssr_in_ten_pull,
                       mold_count, mileage_exchange_count, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(actor) DO UPDATE SET
                       mileage_exchange_count = excluded.mileage_exchange_count,
                       updated_at = excluded.updated_at""",
                (
                    actor,
                    stats.total_pulls,
                    stats.total_ssr,
                    stats.total_sr,
                    stats.total_r,
                    stats.current_no_ssr_streak,
                    stats.max_no_ssr_streak,
                    stats.max_ssr_in_ten_pull,
                    stats.mold_count,
                    stats.mileage_exchange_count + 1,
                    now_iso,
                ),
            )

            acq_id = con.execute(
                """INSERT INTO gacha_acquisitions(
                       actor, character_id, rarity, source, session_id, slot,
                       idempotency_key, before_count, after_count, outcome_tag,
                       labels_delta, mileage_delta, created_at
                   ) VALUES (?, ?, 'SSR', 'golden_mileage', NULL, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    actor,
                    character_id,
                    idempotency_key,
                    acq.before_count,
                    acq.after_count,
                    acq.outcome_tag,
                    acq.labels_gained,
                    -GOLDEN_MILEAGE_COST,
                    now_iso,
                ),
            ).lastrowid
            assert acq_id is not None

            if fault_injector is not None:
                fault_injector("before_commit")

            con.commit()
            new_wallet = compute_wallet_balance(
                wallet.checkin_count, wallet.consumed_tickets, new_mileage, new_labels
            )
            return ExchangeResultRecord(
                acquisition_id=int(acq_id),
                actor=actor,
                source="golden_mileage",
                idempotency_key=idempotency_key,
                character_id=character_id,
                rarity="SSR",
                before_count=acq.before_count,
                after_count=acq.after_count,
                outcome_tag=acq.outcome_tag,
                cost_amount=GOLDEN_MILEAGE_COST,
                labels_refunded=acq.labels_gained,
                net_labels_delta=acq.labels_gained,
                net_mileage_delta=-GOLDEN_MILEAGE_COST,
                wallet=new_wallet,
                created_at=now_iso,
                replay=False,
            )
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
