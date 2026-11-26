"""NIKKE 招募与兑换应用服务层。"""

from __future__ import annotations

from datetime import datetime

from .catalog import CatalogManager, CharacterRecord, GoldenMileageEntry
from .engine import PullEngine, RandomSource
from .repository import (
    ExchangeResultRecord,
    ExchangeUnavailableError,
    GachaRepository,
    TenPullRecord,
)


class GachaService:
    """编排快照校验与仓库原子事务的应用服务。"""

    def __init__(
        self,
        catalog: CatalogManager,
        repo: GachaRepository,
        engine: PullEngine | None = None,
    ):
        self.catalog = catalog
        self.repo = repo
        self.engine = engine if engine is not None else PullEngine()

    def ten_pull(
        self,
        actor: str,
        idempotency_key: str,
        now: datetime,
    ) -> TenPullRecord:
        snapshot = self.catalog.get_recruit_snapshot(now)
        return self.repo.execute_ten_pull(
            actor=actor,
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            engine=self.engine,
            now=now,
        )

    def open_mold(
        self,
        actor: str,
        idempotency_key: str,
        now: datetime,
        *,
        rng: RandomSource | None = None,
    ) -> ExchangeResultRecord:
        snapshot = self.catalog.get_mold_snapshot(now)
        return self.repo.execute_mold(
            actor=actor,
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            now=now,
            rng=rng,
        )

    def _resolve_active_mileage_target(
        self,
        query: str,
        now: datetime,
    ) -> tuple[GoldenMileageEntry, CharacterRecord]:
        active = self.catalog.get_active_golden_mileage(now)
        if not active:
            raise ExchangeUnavailableError(
                "当前暂无已核验的黄金里程可兑换 UP 角色，请等待卡池快照更新。"
            )
        needle = query.strip().casefold()
        if not needle:
            names = "、".join(
                self.catalog.get_character(e.character_id).name_cn
                for e in active
                if self.catalog.get_character(e.character_id) is not None
            )
            raise ExchangeUnavailableError(f"请指定兑换角色名。当前可兑换：{names}")

        candidates: list[tuple[GoldenMileageEntry, CharacterRecord]] = []
        for entry in active:
            char = self.catalog.get_character(entry.character_id)
            if char is not None:
                candidates.append((entry, char))

        exact = [
            pair
            for pair in candidates
            if needle
            in {
                pair[1].name_cn.casefold(),
                pair[1].name_zh_tw.casefold(),
                pair[1].name_en.casefold(),
                pair[1].character_key.casefold(),
                str(pair[1].character_id),
            }
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            names = "、".join(c.name_cn for _, c in exact)
            raise ExchangeUnavailableError(f"角色名存在歧义，请使用准确名称：{names}")

        partial = [
            pair
            for pair in candidates
            if needle in pair[1].name_cn.casefold()
            or needle in pair[1].name_zh_tw.casefold()
            or needle in pair[1].name_en.casefold()
            or needle in pair[1].character_key.casefold()
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = "、".join(c.name_cn for _, c in partial)
            raise ExchangeUnavailableError(f"匹配到多个可兑换角色，请使用完整名称：{names}")

        all_chars = self.catalog.find_characters_by_name(query)
        if all_chars:
            raise ExchangeUnavailableError(
                f"角色「{all_chars[0].name_cn}」不在当前黄金里程可兑换 UP 名单中。"
            )
        raise ExchangeUnavailableError(f"未找到可兑换角色：{query.strip()}")

    def exchange_golden_mileage(
        self,
        actor: str,
        idempotency_key: str,
        query: str,
        now: datetime,
    ) -> ExchangeResultRecord:
        entry, _char = self._resolve_active_mileage_target(query, now)
        return self.repo.execute_golden_mileage(
            actor=actor,
            idempotency_key=idempotency_key,
            character_id=entry.character_id,
            now=now,
        )
