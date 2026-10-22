"""月历的星期位置、内容和 HTML 转义合同。"""

from datetime import date
from pathlib import Path

import pytest
from jinja2 import Environment

from astrbot_plugin_checkin_calendar.presentation import parse_month, payload, text_summary
from astrbot_plugin_checkin_calendar.store import MonthSnapshot


@pytest.mark.parametrize("month", ["2026-10", "2024-02", "2021-02", "2026-08", "0001-01", "9998-12"])
def test_month_cells_have_exact_days_and_correct_weekday(month):
    import calendar
    year, number = map(int, month.split("-"))
    p = payload(MonthSnapshot(month, {}, 0, 0), "用户", date(2026, 10, 21), "Asia/Shanghai")
    flat = [c for week in p["weeks"] for c in week]
    assert len(flat) == 7 * len(calendar.Calendar().monthdayscalendar(year, number))
    assert [c["day"] for c in flat if c["day"]] == list(range(1, calendar.monthrange(year, number)[1] + 1))
    assert next(i for i,c in enumerate(flat) if c["day"] == 1) == date(year, number, 1).weekday()


@pytest.mark.parametrize("value", ["2026-00", "2026-13", "2026-1", "2026/10", "9999-01", "0000-01", "<script>", "2026-10-01"])
def test_month_validation(value):
    with pytest.raises(ValueError):
        parse_month(value, date(2026, 10, 21))


def test_future_month_today_and_big_counts_do_not_fabricate_data():
    snapshot = MonthSnapshot("2026-10", {1: 1000, 21: 5}, 1005, 2)
    p = payload(snapshot, "我", date(2026, 10, 21), "Asia/Shanghai")
    flat = [c for w in p["weeks"] for c in w]
    assert next(c for c in flat if c["day"] == 1)["level"] == 5
    assert next(c for c in flat if c["day"] == 1)["count"] == 1000
    assert sum(c["today"] for c in flat) == 1
    assert p["today_count"] == 5
    assert "1005" in text_summary(snapshot, "我", "Asia/Shanghai")


def test_nickname_html_is_escaped_and_controls_removed():
    p = payload(MonthSnapshot("2026-10", {}, 0, 0), '<script>alert("X")</script>\n用户', date(2026,10,21), "Asia/Shanghai")
    template = Path(__file__).parents[1] / "templates/calendar.html"
    # 宿主即使未全局启用转义，正式模板仍须自行保护昵称内容。
    html = Environment(autoescape=False).from_string(template.read_text(encoding="utf-8")).render(**p)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "\n" not in p["name"]
