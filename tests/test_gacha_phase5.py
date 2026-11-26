"""Phase 5 十连 2×5 静态结算图载荷、自适应 WebP/JPEG 压缩与渲染测试。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from astrbot_plugin_checkin_calendar.gacha.assets import PortraitAssetResolver
from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager
from astrbot_plugin_checkin_calendar.gacha.engine import PullEngine
from astrbot_plugin_checkin_calendar.gacha.presentation import build_result_image_payload
from astrbot_plugin_checkin_calendar.gacha.repository import GachaRepository
from astrbot_plugin_checkin_calendar.gacha.service import GachaService
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer, _encode_static_image

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
TZ = ZoneInfo("Asia/Shanghai")
ACTOR = '["aiocqhttp", "bot1", "50001"]'


class MixedTenPullRng:
    """构造含 SSR、SR、R 及同角色重复的十连。"""

    def __init__(self):
        self.draw = 0
        self.stage = "ssr"

    def randbelow(self, upper_bound: int) -> int:
        if self.stage == "ssr":
            self.draw += 1
            if self.draw in {1, 2, 7}:
                self.stage = "char"
                return 0
            self.stage = "sr"
            return upper_bound - 1
        if self.stage == "sr":
            self.stage = "char"
            return 0 if self.draw in {3, 4} else upper_bound - 1
        self.stage = "ssr"
        return 0


def _make_webp_bytes(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", size, color).save(buf, format="WEBP")
    return buf.getvalue()


def test_adaptive_static_encoder_webp_and_jpeg():
    # 构造带噪点的高熵图像验证自适应降档与宽度回退。
    img = Image.new("RGB", (720, 660), (240, 242, 246))
    draw = ImageDraw.Draw(img)
    for y in range(0, 660, 4):
        for x in range(0, 720, 4):
            draw.point((x, y), fill=((x * 13) % 255, (y * 17) % 255, ((x + y) * 7) % 255))
    raw_png = BytesIO()
    img.save(raw_png, format="PNG")
    png_bytes = raw_png.getvalue()

    webp_bytes = _encode_static_image(png_bytes, output_format="webp", target_bytes=150_000, soft_cap_bytes=250_000)
    with Image.open(BytesIO(webp_bytes)) as out_webp:
        assert out_webp.format == "WEBP"
        assert out_webp.mode == "RGB"
        assert len(webp_bytes) <= 250_000

    jpeg_bytes = _encode_static_image(png_bytes, output_format="jpeg", target_bytes=150_000, soft_cap_bytes=250_000)
    with Image.open(BytesIO(jpeg_bytes)) as out_jpg:
        assert out_jpg.format == "JPEG"
        assert out_jpg.mode == "RGB"
        assert len(jpeg_bytes) <= 250_000


def test_result_image_payload_order_badges_and_asset_fallbacks(tmp_path: Path):
    db_path = tmp_path / "calendar.sqlite3"
    repo = GachaRepository(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """INSERT INTO gacha_wallet(actor, checkin_count, consumed_tickets, golden_mileage, body_labels, updated_at)
               VALUES (?, 4, 0, 0, 0, '2026-11-26T00:00:00+00:00')""",
            (ACTOR,),
        )
        con.commit()
    finally:
        con.close()

    catalog = CatalogManager(BUILTIN_SNAPSHOT)
    service = GachaService(catalog, repo, engine=PullEngine(rng=MixedTenPullRng()))
    now = datetime(2026, 11, 26, 15, 0, tzinfo=TZ)
    record = service.ten_pull(ACTOR, "p5-ten-1", now)

    cache_dir = tmp_path / "assets"
    resolver = PortraitAssetResolver(cache_dir)
    ssr_char = catalog.get_character(record.slots[0].character_id)
    sr_char = catalog.get_character(record.slots[2].character_id)
    assert ssr_char is not None and sr_char is not None

    # SSR 用 MI，SR 用 SI 回退，R 保留 placeholder。
    mi_path = resolver.mi_path(ssr_char.resource_id)
    mi_path.parent.mkdir(parents=True, exist_ok=True)
    mi_path.write_bytes(_make_webp_bytes((256, 512), (240, 190, 60, 255)))

    si_path = resolver.si_path(sr_char.resource_id)
    si_path.parent.mkdir(parents=True, exist_ok=True)
    si_path.write_bytes(_make_webp_bytes((128, 128), (180, 90, 240, 255)))

    payload = build_result_image_payload(service, record, "测试指挥官", TZ, resolver)
    cards = payload["cards"]
    assert [c["slot"] for c in cards] == list(range(1, 11))
    assert cards[0]["portrait_kind"] == "mi"
    assert cards[0]["status_label"] == "NEW"
    assert cards[1]["status_label"] == "★☆☆"
    assert cards[6]["status_label"] == "★★☆"
    assert cards[2]["portrait_kind"] == "si"
    assert cards[2]["status_label"] == "NEW"
    assert cards[3]["status_label"] == "★☆"
    assert cards[4]["portrait_kind"] == "placeholder"
    assert cards[4]["status_label"] == "NEW"
    # R 溢出显示标签而不是星级。
    assert "溢出 +150" in cards[5]["status_label"]


@pytest.mark.asyncio
async def test_calendar_renderer_outputs_webp_and_jpeg(tmp_path: Path, monkeypatch):
    sample_img = Image.new("RGB", (720, 600), (235, 238, 244))
    png_buf = BytesIO()
    sample_img.save(png_buf, format="PNG")

    page = SimpleNamespace(
        set_default_timeout=Mock(),
        set_content=AsyncMock(),
        evaluate=AsyncMock(),
        screenshot=AsyncMock(return_value=png_buf.getvalue()),
    )
    context = SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(is_connected=Mock(return_value=True), new_context=AsyncMock(return_value=context), close=AsyncMock())
    driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    factory = SimpleNamespace(start=AsyncMock(return_value=driver))
    monkeypatch.setattr("astrbot_plugin_checkin_calendar.rendering.async_playwright", lambda: factory)

    renderer = CalendarRenderer(tmp_path)
    out_webp = Path(await renderer.render("<b>{{x}}</b>", {"x": 1}, {"prefix": "gacha-result", "output_format": "webp"}))
    out_jpg = Path(await renderer.render("<b>{{x}}</b>", {"x": 2}, {"prefix": "gacha-result", "output_format": "jpeg"}))
    assert out_webp.suffix == ".webp" and out_webp.is_file()
    assert out_jpg.suffix == ".jpg" and out_jpg.is_file()
    await renderer.close()
