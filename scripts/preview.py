"""用正式 HTML 模板生成合成月历与浏览器布局证据。"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from jinja2 import Environment
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_checkin_calendar.presentation import payload
from astrbot_plugin_checkin_calendar.store import MonthSnapshot


async def main(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    cases = {
        "normal": ("2026-10", "那一个少年", {3: 1, 4: 1, 7: 2, 8: 2, 9: 1, 12: 3, 14: 2, 16: 4, 21: 2}, "记录成功"),
        "empty": ("2026-10", "新朋友", {}, ""),
        "long-name": ("2026-10", "W" * 40, {1: 1, 21: 10}, "记录成功"),
        "six-weeks": ("2026-08", "每一份努力都有回响", {1: 2, 15: 3, 31: 5}, ""),
        "leap-year": ("2024-02", "闰年也要记录", {1: 2, 29: 4}, ""),
        "large-count": ("2026-10", "次数不会被颜色截断", {1: 9999, 21: 999999999}, ""),
        "injection": ("2026-10", '<script>alert("X")</script>用户', {21: 1}, "记录成功"),
    }
    template = Environment(autoescape=True).from_string((ROOT / "templates/calendar.html").read_text(encoding="utf-8"))
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 720, "height": 1}, device_scale_factor=1)
            for case, (month, name, counts, message) in cases.items():
                snapshot = MonthSnapshot(month, counts, sum(counts.values()), len(counts))
                await page.set_content(template.render(**payload(snapshot, name, date(2026,10,21), "Asia/Shanghai", message)))
                await page.evaluate("document.fonts.ready")
                evidence = await page.evaluate("""() => {
                    const errors=[];
                    for(const e of document.querySelectorAll('.name,.stat,.cell,.status')) {
                        const r=e.getBoundingClientRect();
                        if(r.left<0 || r.right>720.5 || e.scrollWidth>e.clientWidth+1 || e.scrollHeight>e.clientHeight+1) errors.push(e.className);
                    }
                    return {errors,width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,
                        cells:[...document.querySelectorAll('[data-day]')].map(e=>({day:Number(e.dataset.day),count:Number(e.dataset.count)})),
                        artwork:[...document.querySelectorAll('.watermark img')].map(e=>({loaded:e.complete&&e.naturalWidth>0,left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right})),
                        footer_nodes:document.querySelectorAll('.footer').length,
                        fonts:[...document.fonts].map(f=>({family:f.family,status:f.status})),script_nodes:document.scripts.length};
                }""")
                assert not evidence["errors"], (case, evidence)
                assert evidence["width"] == 720 and evidence["script_nodes"] == 0
                assert evidence["footer_nodes"] == 0
                assert len(evidence["artwork"]) == 1 and evidence["artwork"][0]["loaded"]
                assert {c["day"]:c["count"] for c in evidence["cells"] if c["count"]} == counts
                await page.screenshot(path=str(output / f"{case}.png"), full_page=True)
                phone = await browser.new_page(viewport={"width": 720, "height": 1}, device_scale_factor=0.6)
                try:
                    await phone.set_content(template.render(**payload(snapshot, name, date(2026,10,21), "Asia/Shanghai", message)))
                    await phone.evaluate("document.fonts.ready")
                    await phone.screenshot(path=str(output / f"{case}-432.png"), full_page=True)
                finally:
                    await phone.close()
                results.append({"case": case, **evidence})
        finally:
            await browser.close()
    (output / "layout-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(results)} 个正式模板场景通过布局和计数检查。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
