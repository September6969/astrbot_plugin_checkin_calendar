"""用合成数据渲染全部新版展示图（抽卡结果、图鉴、统计、商店、排行、历史），输出 PNG 供人工查看。

用法：python scripts/preview_redesign.py --output <目录>
（需从包名为 astrbot_plugin_checkin_calendar 的父目录可导入，同 preview.py。）
"""

import argparse
import asyncio
import random
import sqlite3
import sys
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from datetime import timedelta

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_checkin_calendar.gacha.assets import PortraitAssetResolver
from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.presentation import build_result_image_payload
from astrbot_plugin_checkin_calendar.gacha.repository import GachaRepository
from astrbot_plugin_checkin_calendar.gacha.service import GachaService
from astrbot_plugin_checkin_calendar.gacha.views import (
    build_pokedex_payload, build_shop_payload, build_stats_payload,
)
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer
from astrbot_plugin_checkin_calendar.history import history_payload, history_text
from astrbot_plugin_checkin_calendar.presentation import font_faces, payload as calendar_payload
from astrbot_plugin_checkin_calendar.ranking import ranking_payload
from astrbot_plugin_checkin_calendar.store import CalendarStore, MonthSnapshot

TZ = ZoneInfo("Asia/Shanghai")
ACTOR = '["aiocqhttp", "bot1", "50001"]'


class SeqRng:
    def __init__(self, seed=7):
        self.r = random.Random(seed)

    def randbelow(self, n):
        return self.r.randrange(n)


def fake_portrait(path: Path, size, seed):
    path.parent.mkdir(parents=True, exist_ok=True)
    r = random.Random(seed)
    base = tuple(r.randrange(60, 200) for _ in range(3))
    img = Image.new("RGB", size, base)
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse((w * .3, h * .1, w * .7, h * .1 + w * .4), fill=(245, 230, 220))
    d.rounded_rectangle((w * .22, h * .38, w * .78, h * .95), radius=w // 6, fill=tuple(c // 2 for c in base))
    img.save(path, format="WEBP")


async def main(output: Path, assets_dir: Path | None = None):
    output.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "calendar.sqlite3"
    repo = GachaRepository(db)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)"
        " VALUES (?, 8, 0, 130, 2300, '2026-11-26T00:00:00+00:00')", (ACTOR,))
    con.commit()
    con.close()
    catalog = CatalogManager(ROOT / "assets/gacha/jp_pool_snapshot.json")
    service = GachaService(catalog, repo, engine=PullEngine(rng=SeqRng()))
    resolver = PortraitAssetResolver(assets_dir if assets_dir is not None else tmp / "assets")
    now = datetime(2026, 11, 26, 15, 0, tzinfo=TZ)
    records = [service.ten_pull(ACTOR, f"pv-{i}", now) for i in range(3)]
    if assets_dir is None:
        for c in catalog.snapshot.characters:
            if c.character_id % 3:
                fake_portrait(resolver.mi_path(c.resource_id), (256, 512), c.character_id)
            elif c.character_id % 2:
                fake_portrait(resolver.si_path(c.resource_id), (128, 128), c.character_id)

    # 找一个含 SSR 的十连用于展示金色效果。
    ssr_record = records[-1]
    for seed in range(1, 400):
        svc2 = GachaService(catalog, GachaRepository(tmp / f"s{seed}.sqlite3"), engine=PullEngine(rng=SeqRng(seed)))
        c2 = sqlite3.connect(tmp / f"s{seed}.sqlite3")
        c2.execute("INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)"
                   " VALUES (?, 4, 0, 0, 0, '2026-11-26T00:00:00+00:00')", (ACTOR,))
        c2.commit(); c2.close()
        rec = svc2.ten_pull(ACTOR, "x", now)
        if rec.ssr_count >= 2:
            ssr_record, ssr_service = rec, svc2
            break
    # 打卡数据：排行榜与历史。
    store = CalendarStore(tmp / "cal.sqlite3", cooldown_minutes=0, blast_max_percent=40)
    base = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
    users = {"1001": ("指挥官A", 31), "1002": ("Rapi", 22), "1003": ("红莲", 17), "1004": ("长名字用户" * 3, 9), "1005": ("Anis", 4), "1006": ("", 2)}
    for uid, (nm, n) in users.items():
        actor = f'["qq", "bot", "{uid}"]'
        if nm:
            store.save_name(actor, nm)
        for i in range(n):
            store.change(actor, f"{uid}-{i}", "add", base + timedelta(days=i % 28, minutes=i * 7))
    rank = ranking_payload(store.ranking("qq", "bot", "2026-10"))
    hist = history_payload(store.history('["qq", "bot", "1001"]', "2026-10", False, 1), "指挥官A", TZ)
    hist["fonts"] = font_faces(history_text(hist) + "判定列表范围统计")

    ssr_payload = build_result_image_payload(ssr_service, ssr_record, "测试指挥官", TZ, resolver)
    cal_payload = calendar_payload(MonthSnapshot("2026-10", {3: 1, 7: 2, 12: 3, 16: 4, 21: 5}, 15, 5), "指挥官A", now.date(), "Asia/Shanghai", "记录成功")

    jobs = {
        "gacha-result-ssr-dark": ("gacha_result.html", {**ssr_payload, "theme_class": "theme-dark"}),
        "gacha-result-ssr-light": ("gacha_result.html", {**ssr_payload, "theme_class": "theme-light"}),
        "ranking-light": ("ranking.html", {**rank, "theme_class": "theme-light"}),
        "history-light": ("history.html", {**hist, "theme_class": "theme-light"}),
        "calendar-dark": ("calendar.html", {**cal_payload, "theme_class": "theme-dark"}),
        "calendar-light": ("calendar.html", {**cal_payload, "theme_class": "theme-light"}),
        "pokedex-light": ("pokedex.html", {**build_pokedex_payload(service, ACTOR, [], resolver), "theme_class": "theme-light"}),
        "pokedex-unowned-dark": ("pokedex.html", {**build_pokedex_payload(service, ACTOR, ["未拥有"], resolver), "theme_class": "theme-dark"}),
        "stats-light": ("stats.html", {**build_stats_payload(service, ACTOR), "theme_class": "theme-light"}),
        "shop-light": ("shop.html", {**build_shop_payload(service, ACTOR, now, TZ, resolver), "theme_class": "theme-light"}),
    }
    renderer = CalendarRenderer(output / "tmp")
    try:
        for name, (tpl, data) in jobs.items():
            assert data is not None, name
            path = await renderer.render((ROOT / "templates" / tpl).read_text(encoding="utf-8"), data, {"output_format": "png"})
            (output / f"{name}.png").write_bytes(Path(path).read_bytes())
            print("ok", name)
    finally:
        await renderer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.assets))
