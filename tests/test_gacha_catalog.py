"""Phase 1 卡池快照契约、JP 池口径与原子切换测试。"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrbot_plugin_checkin_calendar.gacha.catalog import (
    CatalogManager,
    PoolUnavailableError,
    SnapshotValidationError,
    compute_snapshot_hash,
    parse_and_validate_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"
JST = timezone(timedelta(hours=9))


def _load_builtin_raw() -> dict:
    return json.loads(BUILTIN_SNAPSHOT.read_text(encoding="utf-8"))


def _resign(raw: dict) -> dict:
    updated = copy.deepcopy(raw)
    updated["content_sha256"] = compute_snapshot_hash(updated)
    return updated


def test_builtin_jp_snapshot_integrity_and_pool_rules():
    manager = CatalogManager(BUILTIN_SNAPSHOT)
    now = datetime(2026, 11, 26, 13, 0, 0, tzinfo=JST)
    snap = manager.get_recruit_snapshot(now)
    mold_snap = manager.get_mold_snapshot(now)

    assert snap.schema_version == 1
    assert snap.server == "JP"
    assert snap.timezone == "Asia/Tokyo"
    assert len(snap.characters) == 200
    assert len(snap.recruit_pool["R"]) == 9
    assert len(snap.recruit_pool["SR"]) == 9

    by_id = {c.character_id: c for c in snap.characters}

    # 验证异常/联动、解放、活动赠送及季节限定均不在统一招募池中。
    excluded_res = {
        193, 351,  # welfare SR
        261, 400, 401, 402,  # liberation
        224, 226, 331, 361, 392, 590,  # welfare SSR
        14, 15, 62, 73, 75, 121, 132, 182, 183, 194, 203, 234, 283, 284, 322, 353, 502, 515,  # limited
    }
    for rarity in ("R", "SR", "SSR"):
        for cid in snap.recruit_pool[rarity]:
            char = by_id[cid]
            assert char.rarity == rarity
            assert char.corporation != "ABNORMAL"
            assert char.resource_id not in excluded_res

    # 完美铸模仅包含三大常驻企业普通常驻 SSR，不含朝圣者、异常与超标准（Overspec）。
    overspec_res = {16, 17, 18, 162}
    for cid in mold_snap.mold_pool:
        char = by_id[cid]
        assert char.rarity == "SSR"
        assert char.corporation in {"ELYSION", "MISSILIS", "TETRA"}
        assert char.resource_id not in excluded_res
        assert char.resource_id not in overspec_res

    # 同一角色不同独立实装版本必须正确区分。
    neon_exact = manager.find_characters_by_name("尼恩")
    assert [c.character_key for c in neon_exact] == ["neon"]

    neon_partial = manager.find_characters_by_name("尼恩：")
    assert {c.character_key for c in neon_partial} == {"neon_blue_ocean", "neon_vision_eye"}

    snow_partial = manager.find_characters_by_name("白雪公主：")
    assert {c.character_key for c in snow_partial} == {
        "snow_white_innocent_days",
        "snow_white_heavy_arms",
    }
    assert manager.get_active_golden_mileage(now) == ()


def test_snapshot_validation_rejects_corrupt_or_invalid_rules():
    base = _load_builtin_raw()

    tampered = copy.deepcopy(base)
    tampered["server"] = "GLOBAL"
    with pytest.raises(SnapshotValidationError, match="content_sha256"):
        parse_and_validate_snapshot(tampered)

    dup_id = copy.deepcopy(base)
    dup_id["characters"][1]["character_id"] = dup_id["characters"][0]["character_id"]
    with pytest.raises(SnapshotValidationError, match="重复的 character_id"):
        parse_and_validate_snapshot(_resign(dup_id))

    empty_pool = copy.deepcopy(base)
    empty_pool["recruit_pool"]["SSR"] = []
    with pytest.raises(SnapshotValidationError, match="非空列表"):
        parse_and_validate_snapshot(_resign(empty_pool))

    wrong_rarity = copy.deepcopy(base)
    wrong_rarity["recruit_pool"]["R"] = [wrong_rarity["recruit_pool"]["SR"][0]]
    with pytest.raises(SnapshotValidationError, match="实际稀有度"):
        parse_and_validate_snapshot(_resign(wrong_rarity))

    pilgrim_id = next(c["character_id"] for c in base["characters"] if c["corporation"] == "PILGRIM")
    pilgrim_in_mold = copy.deepcopy(base)
    pilgrim_in_mold["mold_pool"].append(pilgrim_id)
    with pytest.raises(SnapshotValidationError, match="PILGRIM"):
        parse_and_validate_snapshot(_resign(pilgrim_in_mold))


def test_snapshot_expiry_and_golden_mileage_window(tmp_path: Path):
    base = _load_builtin_raw()
    up_ssr = base["recruit_pool"]["SSR"][0]
    other_ssr = base["recruit_pool"]["SSR"][1]

    v2 = copy.deepcopy(base)
    v2["snapshot_version"] = "jp-2026.11.26-v2"
    v2["effective_from"] = "2026-11-26T00:00:00+09:00"
    v2["effective_until"] = "2026-12-03T05:00:00+09:00"
    v2["golden_mileage_shop"] = [
        {
            "character_id": up_ssr,
            "banner_name": "当期UP",
            "effective_from": "2026-11-26T00:00:00+09:00",
            "effective_until": "2026-12-01T05:00:00+09:00",
            "source_note": "test-active",
        },
        {
            "character_id": other_ssr,
            "banner_name": "已结束UP",
            "effective_from": "2026-11-10T00:00:00+09:00",
            "effective_until": "2026-11-25T05:00:00+09:00",
            "source_note": "test-expired",
        },
    ]
    v2 = _resign(v2)

    runtime_dir = tmp_path / "catalog_runtime"
    manager = CatalogManager(BUILTIN_SNAPSHOT, runtime_dir=runtime_dir)
    installed = manager.install_snapshot(json.dumps(v2, ensure_ascii=False).encode("utf-8"))
    assert installed.snapshot_version == "jp-2026.11.26-v2"
    assert (runtime_dir / "current.json").is_file()
    assert list((runtime_dir / "versions").glob("*.json"))

    active_time = datetime(2026, 11, 27, 10, 0, 0, tzinfo=JST)
    active_mileage = manager.get_active_golden_mileage(active_time)
    assert [e.character_id for e in active_mileage] == [up_ssr]

    with pytest.raises(PoolUnavailableError):
        manager.get_recruit_snapshot(datetime(2026, 11, 25, 23, 59, 0, tzinfo=JST))
    with pytest.raises(PoolUnavailableError):
        manager.get_mold_snapshot(datetime(2026, 12, 3, 5, 0, 0, tzinfo=JST))

    # 损坏或未通过校验的新快照不得破坏当前已生效版本。
    with pytest.raises(SnapshotValidationError):
        manager.install_snapshot(b"{not-valid-json")
    assert manager.snapshot.snapshot_version == "jp-2026.11.26-v2"
