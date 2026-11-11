"""以合成记录验证生产本地渲染器，不发送聊天消息或写入账号记录。"""
import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer
from astrbot_plugin_checkin_calendar.presentation import payload
from astrbot_plugin_checkin_calendar.store import MonthSnapshot


async def main(output):
    renderer = CalendarRenderer(output)
    template = (ROOT / 'templates/calendar.html').read_text(encoding='utf-8')
    rows = []
    async def render(name, month, counts):
        start = time.monotonic()
        image = await renderer.render(template, payload(MonthSnapshot(month, counts, sum(counts.values()), len(counts)), name, date(2026,10,26), 'Asia/Shanghai', '记录成功'), {})
        raw = Path(image).read_bytes()
        assert raw.startswith(b'\x89PNG\r\n\x1a\n')
        rows.append({'name':name, 'seconds':round(time.monotonic()-start,3), 'bytes':len(raw), 'image':image})
        assert not renderer._browser.contexts
    try:
        await render('本地渲染测试', '2026-10', {1:1,26:2})
        await render('本地渲染测试', '2026-10', {1:1,26:2})
        await render('W'*40, '2026-08', {1:1,31:3})
        await asyncio.gather(render('并发甲', '2026-10', {}), render('并发乙', '2026-10', {26:2}))
        await renderer._browser.close()
        await render('浏览器重启恢复', '2026-10', {26:1})
    finally:
        await renderer.close()
    (output / 'local-render-results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(rows,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(main(parser.parse_args().output))
