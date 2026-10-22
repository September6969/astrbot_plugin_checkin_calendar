"""从同一个月快照生成月历数据和文字回退，不进行打卡写操作。"""

import base64
import calendar
import re
import unicodedata
from functools import lru_cache
from datetime import date
from pathlib import Path

from .store import MonthSnapshot

ROOT = Path(__file__).resolve().parent


def clean_name(name: str) -> str:
    return "".join(c for c in str(name) if unicodedata.category(c)[0] != "C").strip()[:40] or "我"


def parse_month(value: str, today: date) -> str:
    value = value or today.strftime("%Y-%m")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("月份请填写 YYYY-MM，例如 2026-10")
    try:
        year, month = map(int, value.split("-"))
        date(year, month, 1)
    except ValueError:
        raise ValueError("月份无效，请填写 0001-01 至 9998-12") from None
    if not 1 <= year <= 9998:
        raise ValueError("年份需在 0001 至 9998 之间")
    return value


@lru_cache(maxsize=1)
def font_data() -> str:
    return "data:font/woff2;base64," + base64.b64encode((ROOT / "assets/calendar-sans.woff2").read_bytes()).decode("ascii")


@lru_cache(maxsize=1)
def background_data() -> str | None:
    """装饰立绘随包提供；素材缺失时仍可显示完整月历。"""
    path = ROOT / "assets/arcana.png"
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii") if path.is_file() else None


def payload(snapshot: MonthSnapshot, name: str, today: date, timezone: str, message: str = "") -> dict:
    year, month = map(int, snapshot.month.split("-"))
    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    return {
        "name": clean_name(name), "heading": f"{clean_name(name)}本月的🦌馆足迹", "month": snapshot.month,
        "title": f"{year} 年 {month} 月", "total": snapshot.total, "active_days": snapshot.days,
        "total_font": min(32, max(12, 240 // (len(str(snapshot.total)) + 2))),
        "today_font": min(32, max(12, 240 // (len(str(snapshot.counts.get(today.day, 0))) + 2))),
        "message": message, "timezone": timezone,
        "current_month": today.strftime("%Y-%m") == snapshot.month,
        "today_count": snapshot.counts.get(today.day, 0) if today.strftime("%Y-%m") == snapshot.month else None,
        "weekdays": ["一", "二", "三", "四", "五", "六", "日"],
        "weeks": [[{
            "day": day, "count": snapshot.counts.get(day, 0),
            "level": min(snapshot.counts.get(day, 0), 5),
            "today": bool(day and date(year, month, day) == today),
            "future": bool(day and date(year, month, day) > today),
        } for day in week] for week in weeks],
        "font": font_data(),
        "background_art": background_data(),
    }


def text_summary(snapshot: MonthSnapshot, name: str, timezone: str, message: str = "") -> str:
    details = "、".join(f"{day}日 {count}次" for day, count in sorted(snapshot.counts.items())) or "这个月还没有记录"
    return "\n".join(part for part in [message, f"{clean_name(name)}本月的🦌馆足迹 · {snapshot.month}", f"累计 {snapshot.total} 次，打卡 {snapshot.days} 天", details, f"日期时区：{timezone}", "查看：🦌 月历　撤销今天最近一次：🦌 撤销"] if part)
