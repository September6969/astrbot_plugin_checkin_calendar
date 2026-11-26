"""独立同步并校验 BLA 默认角色 MI/SI WebP 本地缓存（不依赖主 NIKKE 插件）。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gacha.assets import PortraitAssetResolver
from gacha.catalog import CatalogManager, CharacterRecord

ALLOWED_CDN_SCHEME = "https"
ALLOWED_CDN_HOST = "sg-tools-cdn.blablalink.com"
DEFAULT_SNAPSHOT_PATH = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
EXPECTED_DIMENSIONS = {
    "mi": (256, 512),
    "si": (128, 128),
}


class AssetDownloadError(ValueError):
    """下载来源、大小或图像校验不通过。"""


def validate_cdn_url(url: str) -> str:
    """仅允许来自已核验 HTTPS BlaBlaLink CDN 域名的下载链接。"""
    if not isinstance(url, str) or not url.strip():
        raise AssetDownloadError("素材 URL 为空")
    parsed = urlsplit(url.strip())
    if parsed.scheme != ALLOWED_CDN_SCHEME or parsed.netloc.lower() != ALLOWED_CDN_HOST:
        raise AssetDownloadError(f"拒绝非受信任来源的素材 URL: {url}")
    if not parsed.path or parsed.path == "/":
        raise AssetDownloadError(f"素材 URL 路径无效: {url}")
    return url.strip()


def validate_downloaded_image(
    data: bytes,
    kind: str,
    *,
    content_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, tuple[int, int]]:
    """校验下载字节体积、Content-Type、WebP 签名、解码与预期尺寸。"""
    if not data:
        raise AssetDownloadError("下载内容为空")
    if len(data) > max_bytes:
        raise AssetDownloadError(f"素材体积超限: {len(data)} > {max_bytes}")
    if content_type:
        ct_main = content_type.split(";", 1)[0].strip().lower()
        if ct_main not in {"image/webp", "application/octet-stream", "binary/octet-stream"}:
            raise AssetDownloadError(f"非法 Content-Type: {content_type}")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise AssetDownloadError("文件签名不是 RIFF/WEBP")

    try:
        with Image.open(BytesIO(data)) as img:
            if img.format != "WEBP":
                raise AssetDownloadError(f"解码格式不是 WEBP: {img.format}")
            size = img.size
            img.load()
    except AssetDownloadError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AssetDownloadError(f"WebP 解码失败: {exc}") from exc

    expected = EXPECTED_DIMENSIONS.get(kind)
    if expected is not None and size != expected:
        raise AssetDownloadError(f"{kind.upper()} 尺寸不匹配: 实际 {size}, 预期 {expected}")
    return hashlib.sha256(data).hexdigest(), size


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _collect_targets(
    characters: tuple[CharacterRecord, ...],
    kind: str,
    resolver: PortraitAssetResolver,
    sample: int | None,
) -> list[dict[str, Any]]:
    kinds = ("mi", "si") if kind == "both" else (kind,)
    targets: list[dict[str, Any]] = []
    selected_chars = characters[:sample] if sample is not None and sample > 0 else characters
    for char in selected_chars:
        for k in kinds:
            url = char.mi_url if k == "mi" else char.si_url
            dest = resolver.mi_path(char.resource_id) if k == "mi" else resolver.si_path(char.resource_id)
            targets.append({
                "character_id": char.character_id,
                "resource_id": char.resource_id,
                "name_cn": char.name_cn,
                "kind": k,
                "url": url,
                "dest": dest,
            })
    return targets


async def _download_one(
    client: httpx.AsyncClient,
    target: dict[str, Any],
    sem: asyncio.Semaphore,
    *,
    force: bool,
    max_bytes: int,
) -> dict[str, Any]:
    dest: Path = target["dest"]
    kind: str = target["kind"]
    if not force and dest.is_file():
        try:
            existing = dest.read_bytes()
            sha256, size = validate_downloaded_image(existing, kind, max_bytes=max_bytes)
            return {
                "resource_id": target["resource_id"],
                "kind": kind,
                "status": "cached",
                "bytes": len(existing),
                "dimensions": list(size),
                "sha256": sha256,
            }
        except AssetDownloadError:
            pass

    url = validate_cdn_url(str(target.get("url") or ""))
    async with sem:
        resp = await client.get(url, follow_redirects=False)
    if resp.status_code != 200:
        raise AssetDownloadError(f"HTTP {resp.status_code} 下载失败: {url}")
    if len(resp.content) > max_bytes:
        raise AssetDownloadError(f"响应体积超限: {len(resp.content)} > {max_bytes}")

    sha256, size = validate_downloaded_image(
        resp.content,
        kind,
        content_type=resp.headers.get("Content-Type"),
        max_bytes=max_bytes,
    )
    await asyncio.to_thread(_atomic_write_bytes, dest, resp.content)
    return {
        "resource_id": target["resource_id"],
        "kind": kind,
        "status": "downloaded",
        "bytes": len(resp.content),
        "dimensions": list(size),
        "sha256": sha256,
    }


async def sync_assets(
    snapshot_path: Path,
    cache_dir: Path,
    *,
    kind: str = "mi",
    download: bool = False,
    sample: int | None = None,
    concurrency: int = 4,
    timeout: float = 15.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    force: bool = False,
) -> dict[str, Any]:
    if kind not in {"mi", "si", "both"}:
        raise ValueError(f"不支持的素材类型: {kind}")
    bounded_concurrency = max(1, min(8, concurrency))
    catalog = CatalogManager(snapshot_path)
    resolver = PortraitAssetResolver(cache_dir)
    characters = catalog.snapshot.characters
    targets = _collect_targets(characters, kind, resolver, sample)

    if not download:
        report = resolver.coverage_report(characters[:sample] if sample else characters)
        return {
            "mode": "audit_only",
            "snapshot_version": catalog.snapshot.snapshot_version,
            "kind": kind,
            "targets": len(targets),
            "coverage": report,
        }

    sem = asyncio.Semaphore(bounded_concurrency)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": "astrbot_plugin_checkin_calendar/asset-sync"},
    ) as client:
        tasks = [
            _download_one(client, t, sem, force=force, max_bytes=max_bytes)
            for t in targets
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for target, outcome in zip(targets, outcomes):
        if isinstance(outcome, Exception):
            errors.append({
                "resource_id": target["resource_id"],
                "kind": target["kind"],
                "error": f"{type(outcome).__name__}: {outcome}",
            })
        else:
            results.append(outcome)

    manifest = {
        "snapshot_version": catalog.snapshot.snapshot_version,
        "snapshot_sha256": catalog.snapshot.content_sha256,
        "kind": kind,
        "downloaded": sum(1 for r in results if r["status"] == "downloaded"),
        "cached": sum(1 for r in results if r["status"] == "cached"),
        "failed": len(errors),
        "items": results,
        "errors": errors,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(cache_dir / "sync_manifest.json", manifest_bytes)
    manifest["coverage"] = resolver.coverage_report(characters[:sample] if sample else characters)
    manifest["mode"] = "download"
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="同步并校验 NIKKE BLA MI/SI 本地缓存")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--kind", choices=["mi", "si", "both"], default="mi")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    do_download = bool(args.download and not args.audit_only)
    summary = asyncio.run(
        sync_assets(
            args.snapshot,
            args.cache_dir,
            kind=args.kind,
            download=do_download,
            sample=args.sample,
            concurrency=args.concurrency,
            timeout=args.timeout,
            max_bytes=args.max_bytes,
            force=args.force,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
