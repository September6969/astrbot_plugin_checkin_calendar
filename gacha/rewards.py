"""打卡与撤销的十连资格账本联动。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class WalletBalance:
    checkin_count: int
    earned_tickets: int
    consumed_tickets: int
    available_tickets: int
    debt_tickets: int
    checkin_progress: int
    golden_mileage: int
    body_labels: int


def compute_wallet_balance(
    checkin_count: int,
    consumed_tickets: int,
    golden_mileage: int,
    body_labels: int,
) -> WalletBalance:
    """根据有效打卡计数 C 与已消费资格数 U 推导可用余额与待抵扣债务。"""
    earned = checkin_count // 2
    return WalletBalance(
        checkin_count=checkin_count,
        earned_tickets=earned,
        consumed_tickets=consumed_tickets,
        available_tickets=max(earned - consumed_tickets, 0),
        debt_tickets=max(consumed_tickets - earned, 0),
        checkin_progress=checkin_count % 2,
        golden_mileage=golden_mileage,
        body_labels=body_labels,
    )


def apply_checkin_event(
    con: sqlite3.Connection,
    event_type: str,
    actor: str,
    entry_id: int,
    now: datetime,
) -> None:
    """在打卡/撤销同一写事务中原子更新奖励账本与缓存钱包。"""
    if event_type not in {"added", "undone"}:
        raise ValueError(f"不支持的打卡联动事件类型: {event_type}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("缺少时区信息")

    has_meta = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gacha_meta'"
    ).fetchone()
    if not has_meta:
        return

    cutoff_row = con.execute(
        "SELECT value FROM gacha_meta WHERE key='cutoff_entry_id'"
    ).fetchone()
    cutoff_id = int(cutoff_row[0]) if cutoff_row else 0
    if entry_id <= cutoff_id:
        return

    ledger_type = "checkin_add" if event_type == "added" else "checkin_undo"
    delta_checkins = 1 if event_type == "added" else -1
    now_iso = now.astimezone(timezone.utc).isoformat(timespec="microseconds")

    cur = con.execute(
        """INSERT OR IGNORE INTO gacha_reward_ledger(
               actor, event_type, entry_id, session_id, delta_checkins, delta_tickets, created_at
           ) VALUES (?, ?, ?, NULL, ?, 0, ?)""",
        (actor, ledger_type, entry_id, delta_checkins, now_iso),
    )
    if cur.rowcount == 0:
        return

    total_row = con.execute(
        "SELECT COALESCE(SUM(delta_checkins), 0) FROM gacha_reward_ledger WHERE actor=?",
        (actor,),
    ).fetchone()
    checkin_count = max(0, int(total_row[0]))

    con.execute(
        """INSERT INTO gacha_wallet(
               actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at
           ) VALUES (?, ?, 0, 0, 0, ?)
           ON CONFLICT(actor) DO UPDATE SET
               checkin_count = excluded.checkin_count,
               updated_at = excluded.updated_at""",
        (actor, checkin_count, now_iso),
    )
