"""官方角色主数据、四类池快照合同、校验与原子切换管理。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

VALID_RARITIES = frozenset({"R", "SR", "SSR"})
VALID_CORPORATIONS = frozenset({"ELYSION", "MISSILIS", "TETRA", "PILGRIM", "ABNORMAL"})
MOLD_EXCLUDED_CORPORATIONS = frozenset({"PILGRIM", "ABNORMAL"})


class SnapshotValidationError(ValueError):
    """卡池快照结构、完整性或业务规则校验失败。"""


class PoolUnavailableError(RuntimeError):
    """当前没有有效的已校验卡池快照或卡池已过期。"""


@dataclass(frozen=True)
class CharacterRecord:
    character_id: int
    resource_id: int
    character_key: str
    name_cn: str
    name_zh_tw: str
    name_en: str
    rarity: str
    corporation: str
    mi_url: str | None = None
    si_url: str | None = None
    mi_rel_path: str | None = None
    si_rel_path: str | None = None


@dataclass(frozen=True)
class GoldenMileageEntry:
    character_id: int
    banner_name: str
    effective_from: str
    effective_until: str
    source_note: str = ""


@dataclass(frozen=True)
class PoolSnapshot:
    schema_version: int
    snapshot_version: str
    server: str
    timezone: str
    source: str
    verified_at: str
    effective_from: str
    effective_until: str | None
    content_sha256: str
    characters: tuple[CharacterRecord, ...]
    recruit_pool: dict[str, tuple[int, ...]]
    mold_pool: tuple[int, ...]
    golden_mileage_shop: tuple[GoldenMileageEntry, ...]
    eligibility_notes: dict[str, str]


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SnapshotValidationError(f"字段 {field} 必须为 >= {minimum} 的整数")
    return value


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotValidationError(f"字段 {field} 必须为非空字符串")
    return value.strip()


def _require_optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SnapshotValidationError(f"字段 {field} 必须为字符串或 null")
    stripped = value.strip()
    return stripped or None


def _parse_tz_datetime(value: Any, field: str) -> datetime:
    text = _require_nonempty_str(value, field)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotValidationError(f"字段 {field} 不是有效的 ISO-8601 时间") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise SnapshotValidationError(f"字段 {field} 必须包含显式时区偏移")
    return dt


def compute_snapshot_hash(payload: dict[str, Any]) -> str:
    """计算快照规范 JSON（剔除 content_sha256）的 SHA-256 十六进制摘要。"""
    if not isinstance(payload, dict):
        raise SnapshotValidationError("快照顶层必须为 JSON 对象")
    canonical_body = {k: v for k, v in payload.items() if k != "content_sha256"}
    encoded = json.dumps(
        canonical_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_and_validate_snapshot(raw: dict[str, Any]) -> PoolSnapshot:
    """解析并严格校验官方卡池快照。"""
    if not isinstance(raw, dict):
        raise SnapshotValidationError("快照必须为 JSON 对象")

    schema_version = _require_int(raw.get("schema_version"), "schema_version", minimum=1)
    if schema_version != 1:
        raise SnapshotValidationError(f"不支持的 schema_version: {schema_version}")

    snapshot_version = _require_nonempty_str(raw.get("snapshot_version"), "snapshot_version")
    server = _require_nonempty_str(raw.get("server"), "server")
    timezone_name = _require_nonempty_str(raw.get("timezone"), "timezone")
    source = _require_nonempty_str(raw.get("source"), "source")

    verified_at = _require_nonempty_str(raw.get("verified_at"), "verified_at")
    _parse_tz_datetime(verified_at, "verified_at")

    effective_from = _require_nonempty_str(raw.get("effective_from"), "effective_from")
    eff_from_dt = _parse_tz_datetime(effective_from, "effective_from")

    effective_until = _require_optional_str(raw.get("effective_until"), "effective_until")
    if effective_until is not None:
        eff_until_dt = _parse_tz_datetime(effective_until, "effective_until")
        if eff_from_dt >= eff_until_dt:
            raise SnapshotValidationError("effective_from 必须早于 effective_until")

    content_sha256 = _require_nonempty_str(raw.get("content_sha256"), "content_sha256")
    expected_sha256 = compute_snapshot_hash(raw)
    if content_sha256 != expected_sha256:
        raise SnapshotValidationError("content_sha256 校验失败，快照内容可能已被篡改")

    raw_characters = raw.get("characters")
    if not isinstance(raw_characters, list) or not raw_characters:
        raise SnapshotValidationError("characters 必须为非空列表")

    characters: list[CharacterRecord] = []
    by_char_id: dict[int, CharacterRecord] = {}
    seen_res_ids: set[int] = set()
    seen_keys: set[str] = set()

    for idx, item in enumerate(raw_characters):
        if not isinstance(item, dict):
            raise SnapshotValidationError(f"characters[{idx}] 必须为对象")
        cid = _require_int(item.get("character_id"), f"characters[{idx}].character_id", minimum=1)
        rid = _require_int(item.get("resource_id"), f"characters[{idx}].resource_id", minimum=0)
        ckey = _require_nonempty_str(item.get("character_key"), f"characters[{idx}].character_key")
        name_cn = _require_nonempty_str(item.get("name_cn"), f"characters[{idx}].name_cn")
        name_zh_tw = _require_nonempty_str(item.get("name_zh_tw"), f"characters[{idx}].name_zh_tw")
        name_en = _require_nonempty_str(item.get("name_en"), f"characters[{idx}].name_en")
        rarity = _require_nonempty_str(item.get("rarity"), f"characters[{idx}].rarity")
        corporation = _require_nonempty_str(item.get("corporation"), f"characters[{idx}].corporation")

        if rarity not in VALID_RARITIES:
            raise SnapshotValidationError(f"characters[{idx}].rarity 无效: {rarity}")
        if corporation not in VALID_CORPORATIONS:
            raise SnapshotValidationError(f"characters[{idx}].corporation 无效: {corporation}")
        if cid in by_char_id:
            raise SnapshotValidationError(f"重复的 character_id: {cid}")
        if rid in seen_res_ids:
            raise SnapshotValidationError(f"重复的 resource_id: {rid}")
        if ckey in seen_keys:
            raise SnapshotValidationError(f"重复的 character_key: {ckey}")

        record = CharacterRecord(
            character_id=cid,
            resource_id=rid,
            character_key=ckey,
            name_cn=name_cn,
            name_zh_tw=name_zh_tw,
            name_en=name_en,
            rarity=rarity,
            corporation=corporation,
            mi_url=_require_optional_str(item.get("mi_url"), f"characters[{idx}].mi_url"),
            si_url=_require_optional_str(item.get("si_url"), f"characters[{idx}].si_url"),
            mi_rel_path=_require_optional_str(item.get("mi_rel_path"), f"characters[{idx}].mi_rel_path"),
            si_rel_path=_require_optional_str(item.get("si_rel_path"), f"characters[{idx}].si_rel_path"),
        )
        characters.append(record)
        by_char_id[cid] = record
        seen_res_ids.add(rid)
        seen_keys.add(ckey)

    raw_recruit = raw.get("recruit_pool")
    if not isinstance(raw_recruit, dict) or set(raw_recruit.keys()) != VALID_RARITIES:
        raise SnapshotValidationError("recruit_pool 必须且只能包含 R、SR、SSR 三个集合")

    recruit_pool: dict[str, tuple[int, ...]] = {}
    for rarity in ("R", "SR", "SSR"):
        ids = raw_recruit.get(rarity)
        if not isinstance(ids, list) or not ids:
            raise SnapshotValidationError(f"recruit_pool[{rarity}] 必须为非空列表")
        seen_in_pool: set[int] = set()
        validated_ids: list[int] = []
        for pos, raw_id in enumerate(ids):
            cid = _require_int(raw_id, f"recruit_pool.{rarity}[{pos}]", minimum=1)
            if cid in seen_in_pool:
                raise SnapshotValidationError(f"recruit_pool[{rarity}] 包含重复角色 ID: {cid}")
            char = by_char_id.get(cid)
            if char is None:
                raise SnapshotValidationError(f"recruit_pool[{rarity}] 引用了未知角色 ID: {cid}")
            if char.rarity != rarity:
                raise SnapshotValidationError(
                    f"recruit_pool[{rarity}] 中角色 {cid} ({char.name_cn}) 实际稀有度为 {char.rarity}"
                )
            seen_in_pool.add(cid)
            validated_ids.append(cid)
        recruit_pool[rarity] = tuple(validated_ids)

    raw_mold = raw.get("mold_pool")
    if not isinstance(raw_mold, list) or not raw_mold:
        raise SnapshotValidationError("mold_pool 必须为非空列表")
    seen_mold: set[int] = set()
    mold_ids: list[int] = []
    for pos, raw_id in enumerate(raw_mold):
        cid = _require_int(raw_id, f"mold_pool[{pos}]", minimum=1)
        if cid in seen_mold:
            raise SnapshotValidationError(f"mold_pool 包含重复角色 ID: {cid}")
        char = by_char_id.get(cid)
        if char is None:
            raise SnapshotValidationError(f"mold_pool 引用了未知角色 ID: {cid}")
        if char.rarity != "SSR":
            raise SnapshotValidationError(f"mold_pool 中角色 {cid} 稀有度必须为 SSR")
        if char.corporation in MOLD_EXCLUDED_CORPORATIONS:
            raise SnapshotValidationError(
                f"mold_pool 不得包含 {char.corporation} 企业角色 {cid} ({char.name_cn})"
            )
        seen_mold.add(cid)
        mold_ids.append(cid)

    raw_mileage = raw.get("golden_mileage_shop")
    if not isinstance(raw_mileage, list):
        raise SnapshotValidationError("golden_mileage_shop 必须为列表")
    mileage_entries: list[GoldenMileageEntry] = []
    for idx, item in enumerate(raw_mileage):
        if not isinstance(item, dict):
            raise SnapshotValidationError(f"golden_mileage_shop[{idx}] 必须为对象")
        cid = _require_int(item.get("character_id"), f"golden_mileage_shop[{idx}].character_id", minimum=1)
        char = by_char_id.get(cid)
        if char is None:
            raise SnapshotValidationError(f"golden_mileage_shop[{idx}] 引用了未知角色 ID: {cid}")
        if char.rarity != "SSR":
            raise SnapshotValidationError(f"golden_mileage_shop[{idx}] 兑换角色 {cid} 必须为 SSR")
        banner_name = _require_nonempty_str(item.get("banner_name"), f"golden_mileage_shop[{idx}].banner_name")
        m_from = _require_nonempty_str(item.get("effective_from"), f"golden_mileage_shop[{idx}].effective_from")
        m_until = _require_nonempty_str(item.get("effective_until"), f"golden_mileage_shop[{idx}].effective_until")
        if _parse_tz_datetime(m_from, f"golden_mileage_shop[{idx}].effective_from") >= _parse_tz_datetime(
            m_until, f"golden_mileage_shop[{idx}].effective_until"
        ):
            raise SnapshotValidationError(f"golden_mileage_shop[{idx}] effective_from 必须早于 effective_until")
        source_note = item.get("source_note", "")
        if not isinstance(source_note, str):
            raise SnapshotValidationError(f"golden_mileage_shop[{idx}].source_note 必须为字符串")
        mileage_entries.append(
            GoldenMileageEntry(
                character_id=cid,
                banner_name=banner_name,
                effective_from=m_from,
                effective_until=m_until,
                source_note=source_note.strip(),
            )
        )

    raw_notes = raw.get("eligibility_notes", {})
    if not isinstance(raw_notes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_notes.items()
    ):
        raise SnapshotValidationError("eligibility_notes 必须为字符串键值映射")

    return PoolSnapshot(
        schema_version=schema_version,
        snapshot_version=snapshot_version,
        server=server,
        timezone=timezone_name,
        source=source,
        verified_at=verified_at,
        effective_from=effective_from,
        effective_until=effective_until,
        content_sha256=content_sha256,
        characters=tuple(characters),
        recruit_pool=recruit_pool,
        mold_pool=tuple(mold_ids),
        golden_mileage_shop=tuple(mileage_entries),
        eligibility_notes=dict(raw_notes),
    )


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(payload)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _sanitize_version_token(version: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in version)
    return cleaned or "snapshot"


class CatalogManager:
    """管理官方池快照的加载、校验、版本保留与原子切换。"""

    def __init__(self, builtin_path: Path, runtime_dir: Path | None = None):
        self.builtin_path = Path(builtin_path)
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self._snapshot: PoolSnapshot | None = None
        self._by_id: dict[int, CharacterRecord] = {}
        self.reload()

    @property
    def current_path(self) -> Path:
        if self.runtime_dir is not None:
            candidate = self.runtime_dir / "current.json"
            if candidate.is_file():
                return candidate
        return self.builtin_path

    @property
    def snapshot(self) -> PoolSnapshot:
        if self._snapshot is None:
            raise PoolUnavailableError("尚未加载有效卡池快照")
        return self._snapshot

    def reload(self) -> PoolSnapshot:
        path = self.current_path
        if not path.is_file():
            raise PoolUnavailableError(f"卡池快照文件不存在: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError(f"读取卡池快照失败: {path}") from exc
        parsed = parse_and_validate_snapshot(raw)
        self._snapshot = parsed
        self._by_id = {c.character_id: c for c in parsed.characters}
        return parsed

    def install_snapshot(self, raw_bytes: bytes) -> PoolSnapshot:
        """校验新快照并原子切换，失败时完整保留当前有效快照。"""
        if self.runtime_dir is None:
            raise ValueError("未配置 runtime_dir，无法安装运行期快照")
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError("新快照不是合法 UTF-8 JSON") from exc

        parsed = parse_and_validate_snapshot(raw)
        canonical_bytes = (
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        safe_ver = _sanitize_version_token(parsed.snapshot_version)
        version_file = (
            self.runtime_dir / "versions" / f"{safe_ver}-{parsed.content_sha256[:12]}.json"
        )
        _atomic_write_bytes(version_file, canonical_bytes)
        _atomic_write_bytes(self.runtime_dir / "current.json", canonical_bytes)

        self._snapshot = parsed
        self._by_id = {c.character_id: c for c in parsed.characters}
        return parsed

    def _require_active(self, now: datetime) -> PoolSnapshot:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("校验卡池有效期需要带时区的 datetime")
        snap = self.snapshot
        eff_from = datetime.fromisoformat(snap.effective_from)
        if now < eff_from:
            raise PoolUnavailableError(f"当前卡池快照尚未生效（{snap.effective_from}）")
        if snap.effective_until is not None:
            eff_until = datetime.fromisoformat(snap.effective_until)
            if now >= eff_until:
                raise PoolUnavailableError(
                    f"当前卡池快照已过期（{snap.effective_until}），请更新已核验快照"
                )
        return snap

    def get_recruit_snapshot(self, now: datetime) -> PoolSnapshot:
        return self._require_active(now)

    def get_mold_snapshot(self, now: datetime) -> PoolSnapshot:
        return self._require_active(now)

    def get_active_golden_mileage(self, now: datetime) -> tuple[GoldenMileageEntry, ...]:
        snap = self._require_active(now)
        active: list[GoldenMileageEntry] = []
        for entry in snap.golden_mileage_shop:
            start = datetime.fromisoformat(entry.effective_from)
            end = datetime.fromisoformat(entry.effective_until)
            if start <= now < end:
                active.append(entry)
        return tuple(active)

    def get_character(self, character_id: int) -> CharacterRecord | None:
        return self._by_id.get(character_id)

    def find_characters_by_name(self, query: str) -> list[CharacterRecord]:
        """按中文/繁中/英文/key 精确匹配优先；无精确命中时返回所有子串匹配供消歧。"""
        needle = query.strip().casefold()
        if not needle:
            return []
        exact = [
            char
            for char in self.snapshot.characters
            if needle
            in {
                char.name_cn.casefold(),
                char.name_zh_tw.casefold(),
                char.name_en.casefold(),
                char.character_key.casefold(),
            }
        ]
        if exact:
            return sorted(exact, key=lambda c: c.character_id)
        partial = [
            char
            for char in self.snapshot.characters
            if needle in char.name_cn.casefold()
            or needle in char.name_zh_tw.casefold()
            or needle in char.name_en.casefold()
            or needle in char.character_key.casefold()
        ]
        return sorted(partial, key=lambda c: c.character_id)
