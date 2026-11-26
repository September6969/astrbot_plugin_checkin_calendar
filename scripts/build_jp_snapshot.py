"""从已核验的本地参考数据生成日本服务器（JP）版本化卡池快照。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gacha.catalog import compute_snapshot_hash, parse_and_validate_snapshot

DEFAULT_MASTER_PATH = Path(r"E:\Nikke_Plugin\assets\data\character_master.json")
DEFAULT_INVENTORY_PATH = Path(
    r"E:\Nikke_Plugin\docs\archive\evidence\blabla_static_assets\static_asset_inventory.json"
)
DEFAULT_OUTPUT_PATH = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"

PERMANENT_SR_RESOURCE_IDS = frozenset({10, 11, 12, 20, 60, 61, 120, 161, 291})
WELFARE_SR_RESOURCE_IDS = frozenset({193, 351})

LIBERATION_SSR_RESOURCE_IDS = frozenset({261, 400, 401, 402})
WELFARE_SSR_RESOURCE_IDS = frozenset({224, 226, 331, 361, 392, 590})
LIMITED_SEASONAL_SSR_RESOURCE_IDS = frozenset({
    14, 15, 62, 73, 75, 121, 132, 182, 183, 194, 203, 234, 283, 284, 322, 353, 502, 515
})
OVERSPEC_SSR_RESOURCE_IDS = frozenset({16, 17, 18, 162})


def _extract_asset_map(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    mapping: dict[int, dict[str, Any]] = {}
    for item in items:
        rel_path = str(item.get("relative_path", ""))
        match = re.search(r"c(\d+)_(\d+)", rel_path)
        if not match:
            raise ValueError(f"无法从素材路径解析 resource_id: {rel_path}")
        res_id = int(match.group(1))
        if res_id in mapping:
            raise ValueError(f"素材清单存在重复 resource_id: {res_id}")
        mapping[res_id] = item
    return mapping


def build_jp_snapshot(
    master_path: Path,
    inventory_path: Path,
    output_path: Path,
    *,
    verified_at: str = "2026-11-26T12:00:00+09:00",
    effective_from: str = "2026-11-26T00:00:00+09:00",
) -> dict[str, Any]:
    master = json.loads(master_path.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))

    mi_map = _extract_asset_map(inventory["categories"]["portraits_mi_default"]["items"])
    si_map = _extract_asset_map(inventory["categories"]["portraits_si_default"]["items"])

    raw_characters = master["characters"]
    characters_payload: list[dict[str, Any]] = []
    recruit_r: list[int] = []
    recruit_sr: list[int] = []
    recruit_ssr: list[int] = []
    mold_ssr: list[int] = []

    for entry in sorted(raw_characters, key=lambda c: int(c["id"])):
        cid = int(entry["id"])
        rid = int(entry["resource_id"])
        rarity = str(entry["rare"])
        corp = str(entry["corporation"])

        if rid not in mi_map or rid not in si_map:
            raise ValueError(f"角色 {cid} (resource_id={rid}) 缺少 MI/SI 素材映射")

        mi_item = mi_map[rid]
        si_item = si_map[rid]
        characters_payload.append({
            "character_id": cid,
            "resource_id": rid,
            "character_key": str(entry["character_key"]),
            "name_cn": str(entry["name_cn"]),
            "name_zh_tw": str(entry["name_zh_tw"]),
            "name_en": str(entry["name_en"]),
            "rarity": rarity,
            "corporation": corp,
            "mi_url": str(mi_item["url"]),
            "si_url": str(si_item["url"]),
            "mi_rel_path": str(mi_item["relative_path"]).lstrip("/"),
            "si_rel_path": str(si_item["relative_path"]).lstrip("/"),
        })

        if rarity == "R":
            recruit_r.append(cid)
        elif rarity == "SR":
            if corp == "ABNORMAL" or rid in WELFARE_SR_RESOURCE_IDS:
                continue
            if rid in PERMANENT_SR_RESOURCE_IDS:
                recruit_sr.append(cid)
            else:
                raise ValueError(f"未分类 SR 角色 resource_id={rid} ({entry['name_cn']})")
        elif rarity == "SSR":
            if (
                corp == "ABNORMAL"
                or rid in LIBERATION_SSR_RESOURCE_IDS
                or rid in WELFARE_SSR_RESOURCE_IDS
                or rid in LIMITED_SEASONAL_SSR_RESOURCE_IDS
            ):
                continue
            recruit_ssr.append(cid)
            if corp != "PILGRIM" and rid not in OVERSPEC_SSR_RESOURCE_IDS:
                mold_ssr.append(cid)

    payload: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_version": "jp-2026.11.26-v1",
        "server": "JP",
        "timezone": "Asia/Tokyo",
        "source": "blablalink_official_directory + static_asset_inventory + jp_eligibility_audit",
        "verified_at": verified_at,
        "effective_from": effective_from,
        "effective_until": None,
        "characters": characters_payload,
        "recruit_pool": {
            "R": recruit_r,
            "SR": recruit_sr,
            "SSR": recruit_ssr,
        },
        "mold_pool": mold_ssr,
        "golden_mileage_shop": [],
        "eligibility_notes": {
            "server_scope": "JP (Asia/Tokyo)",
            "recruit_r": f"All {len(recruit_r)} mass-produced R Nikkes.",
            "recruit_sr": (
                f"{len(recruit_sr)} permanent SR Nikkes; excludes ABNORMAL collab SRs "
                f"and welfare SRs (resource_ids={sorted(WELFARE_SR_RESOURCE_IDS)})."
            ),
            "recruit_ssr": (
                f"{len(recruit_ssr)} verified permanent JP SSR Nikkes (including permanent Pilgrims & Overspec); "
                f"excludes ABNORMAL collabs, Liberation (261,400,401,402), "
                f"Welfare (224,226,331,361,392,590), and Seasonal Limiteds."
            ),
            "mold_pool": (
                f"{len(mold_ssr)} High-Quality Mold SSRs (Elysion/Missilis/Tetra permanent SSRs); "
                "excludes PILGRIM, Overspec (16,17,18,162), ABNORMAL, Limited, Liberation, and Welfare."
            ),
            "golden_mileage_shop": (
                "Empty in initial baseline: JP active UP banner (Sky / スカイ, 2026-11-19~2026-12-03 JST) "
                "is not present in the verified 200-character BlaBlaLink master catalog; "
                "mileage exchange remains paused until an audited UP snapshot entry is added."
            ),
        },
    }
    payload["content_sha256"] = compute_snapshot_hash(payload)

    # 写出前先用领域校验器自检。
    parse_and_validate_snapshot(payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并校验日本服务器 NIKKE 卡池快照")
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER_PATH)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    payload = build_jp_snapshot(args.master, args.inventory, args.output)
    print(
        f"已生成 {args.output}: "
        f"characters={len(payload['characters'])}, "
        f"R={len(payload['recruit_pool']['R'])}, "
        f"SR={len(payload['recruit_pool']['SR'])}, "
        f"SSR={len(payload['recruit_pool']['SSR'])}, "
        f"mold={len(payload['mold_pool'])}, "
        f"sha256={payload['content_sha256'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
