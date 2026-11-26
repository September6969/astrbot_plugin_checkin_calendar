"""统一角色收藏解锁、自动突破、核心强化与溢出躯体标签结算。"""

from __future__ import annotations

from dataclasses import dataclass

OVERFLOW_LABELS = {
    "R": 150,
    "SR": 200,
    "SSR": 6000,
}

MAX_NON_OVERFLOW_COUNT = {
    "R": 1,
    "SR": 3,
    "SSR": 11,
}


@dataclass(frozen=True)
class AcquisitionOutcome:
    before_count: int
    after_count: int
    outcome_tag: str
    labels_gained: int
    reached_mlb: bool
    reached_core_max: bool
    display_status: str


def apply_acquisition(rarity: str, before_count: int) -> AcquisitionOutcome:
    """按稀有度与当前累计获取次数结算单次角色获取状态。"""
    if rarity not in OVERFLOW_LABELS:
        raise ValueError(f"未知稀有度: {rarity}")
    if type(before_count) is not int or before_count < 0:
        raise ValueError(f"非法的累计获取次数: {before_count}")

    after_count = before_count + 1
    if rarity == "R":
        if after_count == 1:
            return AcquisitionOutcome(before_count, after_count, "NEW", 0, False, False, "NEW")
        return AcquisitionOutcome(
            before_count, after_count, "OVERFLOW", OVERFLOW_LABELS["R"], False, False, "溢出 +150 标签"
        )

    if rarity == "SR":
        if after_count == 1:
            return AcquisitionOutcome(before_count, after_count, "NEW", 0, False, False, "NEW")
        if after_count == 2:
            return AcquisitionOutcome(before_count, after_count, "LB1", 0, False, False, "★☆")
        if after_count == 3:
            return AcquisitionOutcome(before_count, after_count, "LB2", 0, True, False, "★★ (满破)")
        return AcquisitionOutcome(
            before_count, after_count, "OVERFLOW", OVERFLOW_LABELS["SR"], False, False, "溢出 +200 标签"
        )

    # SSR: 1=解锁, 2..4=1~3星(满破), 5..11=CORE+1..7, >=12=溢出
    if after_count == 1:
        return AcquisitionOutcome(before_count, after_count, "NEW", 0, False, False, "NEW")
    if after_count <= 4:
        stars = after_count - 1
        star_str = "★" * stars + "☆" * (3 - stars)
        reached_mlb = after_count == 4
        display = f"{star_str} (满破)" if reached_mlb else star_str
        return AcquisitionOutcome(before_count, after_count, f"LB{stars}", 0, reached_mlb, False, display)
    if after_count <= 11:
        core = after_count - 4
        reached_core_max = after_count == 11
        return AcquisitionOutcome(
            before_count,
            after_count,
            f"CORE+{core}",
            0,
            False,
            reached_core_max,
            f"★★★ CORE+{core}",
        )
    return AcquisitionOutcome(
        before_count, after_count, "OVERFLOW", OVERFLOW_LABELS["SSR"], False, False, "溢出 +6000 标签"
    )


def format_collection_rank(rarity: str, total_obtained: int) -> str:
    """格式化图鉴中角色的当前突破/核心强化状态（上限截断，不显示 R 虚构星级）。"""
    if total_obtained <= 0:
        return "未解锁"
    if rarity == "R":
        return "已收录"
    if rarity == "SR":
        capped = min(total_obtained, 3)
        stars = capped - 1
        return "★" * stars + "☆" * (2 - stars)
    if rarity == "SSR":
        capped = min(total_obtained, 11)
        if capped <= 4:
            stars = capped - 1
            return "★" * stars + "☆" * (3 - stars)
        return f"★★★ CORE+{capped - 4}"
    raise ValueError(f"未知稀有度: {rarity}")


def enhancement_sort_key(rarity: str, total_obtained: int) -> int:
    if total_obtained <= 0:
        return 0
    return min(total_obtained, MAX_NON_OVERFLOW_COUNT[rarity])
