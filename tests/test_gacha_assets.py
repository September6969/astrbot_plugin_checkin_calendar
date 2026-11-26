"""Phase 1 BLA MI/SI 本地缓存解析与同步校验测试。"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from astrbot_plugin_checkin_calendar.gacha.assets import PortraitAssetResolver
from astrbot_plugin_checkin_calendar.scripts.sync_nikke_assets import (
    AssetDownloadError,
    sync_assets,
    validate_cdn_url,
    validate_downloaded_image,
)

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"


def _make_webp_bytes(size: tuple[int, int], color: tuple[int, int, int, int] = (255, 200, 60, 255)) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", size, color).save(buf, format="WEBP")
    return buf.getvalue()


def test_local_portrait_resolver_and_fallbacks(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    resolver = PortraitAssetResolver(cache_dir)

    mi_10 = resolver.mi_path(10)
    mi_10.parent.mkdir(parents=True, exist_ok=True)
    mi_10.write_bytes(_make_webp_bytes((256, 512)))

    si_11 = resolver.si_path(11)
    si_11.parent.mkdir(parents=True, exist_ok=True)
    si_11.write_bytes(_make_webp_bytes((128, 128)))

    # resource_id 12 同时存在损坏 MI 与有效 SI
    mi_12 = resolver.mi_path(12)
    mi_12.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 corrupted")
    si_12 = resolver.si_path(12)
    si_12.write_bytes(_make_webp_bytes((128, 128)))

    # 1. 正常命中 MI
    res_10 = resolver.resolve(10)
    assert res_10.kind == "mi"
    assert res_10.path == mi_10
    assert res_10.is_fallback is False

    # 2. MI 缺失回退 SI
    res_11 = resolver.resolve(11)
    assert res_11.kind == "si"
    assert res_11.path == si_11
    assert res_11.is_fallback is True
    assert res_11.reason == "mi_missing_using_si"

    # 3. MI 损坏回退 SI
    res_12 = resolver.resolve(12)
    assert res_12.kind == "si"
    assert res_12.path == si_12
    assert res_12.is_fallback is True
    assert res_12.reason == "mi_corrupt_using_si"

    # 4. 不存在的相邻 resource_id 绝不串图或猜图
    res_13 = resolver.resolve(13)
    assert res_13.kind == "placeholder"
    assert res_13.path is None
    assert res_13.is_fallback is True
    assert res_13.reason == "asset_missing"

    report = resolver.coverage_report([10, 11, 12, 13])
    assert report == {
        "total": 4,
        "mi_cached": 1,
        "si_cached": 2,
        "missing_mi_ids": [11, 12, 13],
        "missing_both_ids": [13],
    }


def test_sync_validation_rejects_untrusted_urls_and_corrupt_payloads(tmp_path: Path):
    assert (
        validate_cdn_url("https://sg-tools-cdn.blablalink.com/a/b.webp")
        == "https://sg-tools-cdn.blablalink.com/a/b.webp"
    )
    for bad_url in (
        "",
        "http://sg-tools-cdn.blablalink.com/a/b.webp",
        "https://evil.example.com/a/b.webp",
        "file:///etc/passwd",
        "https://sg-tools-cdn.blablalink.com/",
    ):
        with pytest.raises(AssetDownloadError):
            validate_cdn_url(bad_url)

    valid_mi = _make_webp_bytes((256, 512))
    sha, size = validate_downloaded_image(valid_mi, "mi", content_type="image/webp")
    assert len(sha) == 64
    assert size == (256, 512)

    with pytest.raises(AssetDownloadError, match="Content-Type"):
        validate_downloaded_image(valid_mi, "mi", content_type="text/html")
    with pytest.raises(AssetDownloadError, match="RIFF/WEBP"):
        validate_downloaded_image(b"not-a-webp", "mi", content_type="image/webp")
    with pytest.raises(AssetDownloadError, match="尺寸不匹配"):
        validate_downloaded_image(_make_webp_bytes((128, 128)), "mi", content_type="image/webp")
    with pytest.raises(AssetDownloadError, match="体积超限"):
        validate_downloaded_image(valid_mi, "mi", max_bytes=16)

    # 确认损坏内容不会污染缓存目录（通过直接调用 sync_assets 的 audit_only 路径）。
    audit = asyncio.run(sync_assets(BUILTIN_SNAPSHOT, tmp_path / "empty_cache", kind="mi", download=False, sample=5))
    assert audit["mode"] == "audit_only"
    assert audit["targets"] == 5
    assert audit["coverage"]["mi_cached"] == 0
