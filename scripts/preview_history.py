"""生成用于审阅历史判定页面的本地截图。"""

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_checkin_calendar.history import history_payload, history_text
from astrbot_plugin_checkin_calendar.presentation import font_faces
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer
from astrbot_plugin_checkin_calendar.store import CalendarStore


def seed(path, actor, now):
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript("""
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY, actor TEXT NOT NULL, day TEXT NOT NULL,
                created_at TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE receipts (
                actor TEXT NOT NULL, token TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(actor,token)
            );
        """)
        con.execute("INSERT INTO entries VALUES (1, ?, '2026-11-10', ?, 0)",
                    (actor, (now - timedelta(days=8)).isoformat()))
        con.execute("INSERT INTO receipts VALUES (?, 'legacy', ?)", (actor, json.dumps({
            "status": "blasted", "day": "2026-11-12", "risk_percent": 5.4,
        })))
    store = CalendarStore(path, cooldown_minutes=0, blast_cooldown_minutes=1)
    with patch("astrbot_plugin_checkin_calendar.store.secrets.randbelow", return_value=999999):
        store.change(actor, "first", "add", now)
        store.change(actor, "another", "add", now + timedelta(hours=3))
        store.change(actor, "undo", "undo", now + timedelta(hours=3, minutes=2))
    with patch("astrbot_plugin_checkin_calendar.store.secrets.randbelow", return_value=0):
        store.change(actor, "blast", "add", now + timedelta(hours=6))
    return store


async def main(output):
    output.mkdir(parents=True, exist_ok=True)
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 11, 18, 8, 12, 17, 450000, tzinfo=zone)
    actor = json.dumps(["preview", "bot", "viewer"])
    template = (ROOT / "templates/history.html").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        store = seed(Path(tmp) / "sample.db", actor, now)
        renderer = CalendarRenderer(Path(tmp) / "rendered")
        try:
            for filename, owner, blast_only, name in [
                ("history-normal.png", actor, False, "阿尔卡娜"),
                ("history-blast.png", actor, True, "阿尔卡娜"),
                ("history-empty.png", "empty", False, "阿尔卡娜"),
                ("history-undone.png", actor, False, "阿尔卡娜的个人打卡记录与月度历史统计测试昵称" * 3),
            ]:
                snapshot = store.history(owner, "2026-11", blast_only, 1)
                data = history_payload(snapshot, name, zone)
                data["fonts"] = font_faces(history_text(data) + "判定列表范围统计")
                start = time.perf_counter()
                path = await renderer.render(template, data, {})
                shutil.copyfile(path, output / filename)
                print(f"{filename}: {time.perf_counter() - start:.3f}s, {(output / filename).stat().st_size:,} bytes")
        finally:
            await renderer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
