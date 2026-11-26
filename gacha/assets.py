"""BLA MI/SI 本地缓存立绘解析器（运行时零网络请求）。"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

from PIL import Image, UnidentifiedImageError

from .catalog import CharacterRecord


@dataclass(frozen=True)
class PortraitResolution:
    resource_id: int
    kind: str  # "mi" | "si" | "placeholder"
    path: Path | None
    is_fallback: bool
    reason: str | None = None


def _is_valid_webp(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    try:
        with Image.open(BytesIO(data)) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


class PortraitAssetResolver:
    """严格按 resource_id 解析本地缓存的 BLA MI/SI WebP 素材。"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)

    def mi_path(self, resource_id: int) -> Path:
        return self.cache_dir / "character" / "mi" / f"mi_c{resource_id:03d}_00_s.webp"

    def si_path(self, resource_id: int) -> Path:
        return self.cache_dir / "character" / "si" / f"si_c{resource_id:03d}_00_s.webp"

    def resolve(self, resource_id: int) -> PortraitResolution:
        """优先返回 MI (256x512)，缺失或损坏时回退至同 ID 的 SI (128x128) 或占位标记。"""
        if type(resource_id) is not int or resource_id < 0:
            return PortraitResolution(
                resource_id=-1,
                kind="placeholder",
                path=None,
                is_fallback=True,
                reason="invalid_resource_id",
            )

        mi_file = self.mi_path(resource_id)
        mi_exists = mi_file.is_file()
        if mi_exists and _is_valid_webp(mi_file):
            return PortraitResolution(
                resource_id=resource_id,
                kind="mi",
                path=mi_file,
                is_fallback=False,
                reason=None,
            )

        si_file = self.si_path(resource_id)
        si_exists = si_file.is_file()
        if si_exists and _is_valid_webp(si_file):
            return PortraitResolution(
                resource_id=resource_id,
                kind="si",
                path=si_file,
                is_fallback=True,
                reason="mi_corrupt_using_si" if mi_exists else "mi_missing_using_si",
            )

        reason = "asset_corrupt" if (mi_exists or si_exists) else "asset_missing"
        return PortraitResolution(
            resource_id=resource_id,
            kind="placeholder",
            path=None,
            is_fallback=True,
            reason=reason,
        )

    def coverage_report(self, characters: Iterable[CharacterRecord | int]) -> dict[str, object]:
        res_ids: list[int] = []
        for item in characters:
            res_ids.append(item.resource_id if isinstance(item, CharacterRecord) else int(item))

        mi_cached = 0
        si_cached = 0
        missing_mi_ids: list[int] = []
        missing_both_ids: list[int] = []

        for rid in res_ids:
            has_mi = _is_valid_webp(self.mi_path(rid))
            has_si = _is_valid_webp(self.si_path(rid))
            if has_mi:
                mi_cached += 1
            else:
                missing_mi_ids.append(rid)
            if has_si:
                si_cached += 1
            if not has_mi and not has_si:
                missing_both_ids.append(rid)

        return {
            "total": len(res_ids),
            "mi_cached": mi_cached,
            "si_cached": si_cached,
            "missing_mi_ids": missing_mi_ids,
            "missing_both_ids": missing_both_ids,
        }
