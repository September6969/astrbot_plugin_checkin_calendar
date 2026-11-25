"""历史指令参数与图片、文字共用的展示数据。"""

import re
from datetime import date, datetime

from .presentation import clean_name, format_probability, parse_month
from .store import HistorySnapshot

TITLE = "历史判定记录"
LEGACY_NOTE = "旧版成功记录的概率可能未知，旧版炸膛记录可能缺少精确时间。未留下回执的旧炸膛无法恢复。"
TIME_NOTE = "月份按判定当时所属日期筛选，具体时间按当前配置时区显示。"


def parse_history_args(args: list[str], today: date) -> tuple[str | None, bool, int]:
    month = page = None
    blast_only = False
    for arg in args:
        if arg == "炸膛":
            if blast_only:
                raise ValueError("结果筛选只能填写一次。")
            blast_only = True
        elif re.fullmatch(r"[0-9]+", arg):
            if not arg.lstrip("0") or len(arg) > 18:
                raise ValueError("页码须为大于 0 的整数，且最多 18 位。")
            if page is not None:
                raise ValueError("页码只能填写一次。")
            page = int(arg)
        elif re.fullmatch(r"[0-9]{4}-.*", arg):
            if month is not None:
                raise ValueError("月份只能填写一次。")
            month = parse_month(arg, today)
        elif re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", arg):
            raise ValueError("页码须为大于 0 的整数。")
        else:
            raise ValueError("历史参数仅支持月份 YYYY-MM、炸膛筛选和页码。")
    return month, blast_only, page or 1


def history_payload(snapshot: HistorySnapshot, name: str, display_zone) -> dict:
    def risk(value, missing="—"):
        return missing if value is None else format_probability(value)

    stats = snapshot.stats
    metrics = [
        ("有效判定", str(stats.total)),
        ("正常通过", str(stats.total - stats.blasted)),
        ("炸膛次数", str(stats.blasted)),
        ("实际炸膛率", risk(stats.blasted / stats.total * 100 if stats.total else None)),
        ("已撤销", str(stats.undone)),
        ("平均风险", risk(stats.average)),
        ("最高风险", risk(stats.highest)),
        ("最低炸膛概率", risk(stats.lowest_blast)),
    ]
    rows = []
    for record in snapshot.records:
        when = datetime.fromisoformat(record.created_at).astimezone(display_zone) if record.created_at else None
        clock = "时间未知" if when is None else when.time().isoformat(timespec="seconds")
        if when is not None and when.microsecond:
            clock += f".{when.microsecond:06d}".rstrip("0")
        if when is not None and when.date().isoformat() != record.day:
            clock = when.date().isoformat() + " " + clock
        rows.append({
            "day": record.day, "time": clock,
            "state": "blast" if record.result == "blasted" else ("undone" if record.undone else "passed"),
            "result": "炸膛" if record.result == "blasted" else "正常通过",
            "undone": bool(record.undone), "risk": risk(record.risk_percent, "未知"),
        })
    return {
        "title": TITLE, "name": clean_name(name), "range": snapshot.month or "全部日期",
        "filter": "仅炸膛" if snapshot.blast_only else "全部判定", "metrics": metrics, "rows": rows,
        "pagination": f"第 {snapshot.page if snapshot.pages else 0} / {snapshot.pages} 页 · 共 {snapshot.matching} 条",
        "coverage": f"已知概率 {stats.known_risks}/{stats.total} · 旧版迁移 {stats.migrated} 条 · 时间未知 {stats.unknown_times} 条",
        "empty": "所选月份没有炸膛记录" if snapshot.month and snapshot.blast_only else (
            "暂无炸膛记录" if snapshot.blast_only else "所选范围暂无有效判定"),
        "notes": [f"时区 {display_zone}。{TIME_NOTE}", LEGACY_NOTE],
    }


def history_text(data: dict) -> str:
    rows = [
        f"{row['day']} · {row['time']} · 炸膛概率 {row['risk']} · {row['result']}" + (" · 已撤销" if row["undone"] else "")
        for row in data["rows"]
    ]
    metrics = " · ".join(f"{label} {value}" for label, value in data["metrics"])
    return "\n".join([
        f"{data['title']} · {data['name']}", f"{data['range']} / {data['filter']}",
        metrics, data["coverage"], *(rows or [data["empty"]]), data["pagination"], *data["notes"],
    ])
