"""抽卡概率模型接口、nikke-balance-v1 双向仓检模型及十连顺序执行引擎。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol

from .catalog import PoolSnapshot

RATE_SCALE = 1_000_000
TEN_PULL_SIZE = 10

BALANCE_V1_VERSION = "nikke-balance-v1"
BALANCE_V1_DENOMINATOR = 12_500
BALANCE_V1_BASE_NUMERATOR = 500
BALANCE_V1_MIN_NUMERATOR = 325
BALANCE_V1_MAX_NUMERATOR = 1000
SR_CONDITIONAL_NUMERATOR = 43
NON_SSR_CONDITIONAL_DENOMINATOR = 96


@dataclass(frozen=True)
class PullRates:
    """单抽概率（百万分比定点整数，总和严格等于 1_000_000）。"""

    p_ssr_ppm: int
    p_sr_ppm: int
    p_r_ppm: int

    def __post_init__(self) -> None:
        for name, val in (
            ("p_ssr_ppm", self.p_ssr_ppm),
            ("p_sr_ppm", self.p_sr_ppm),
            ("p_r_ppm", self.p_r_ppm),
        ):
            if type(val) is not int or val < 0:
                raise ValueError(f"{name} 必须为非负整数: {val!r}")
        if self.p_ssr_ppm + self.p_sr_ppm + self.p_r_ppm != RATE_SCALE:
            raise ValueError("概率总和必须严格等于 1_000_000")

    @property
    def p_ssr(self) -> float:
        return self.p_ssr_ppm / RATE_SCALE

    @property
    def p_sr(self) -> float:
        return self.p_sr_ppm / RATE_SCALE

    @property
    def p_r(self) -> float:
        return self.p_r_ppm / RATE_SCALE


@dataclass(frozen=True)
class PullSlotResult:
    slot: int
    character_id: int
    rarity: str
    p_ssr_ppm: int
    p_sr_ppm: int
    p_r_ppm: int
    n_before: int
    s_before: int


@dataclass(frozen=True)
class TenPullOutcome:
    algorithm_version: str
    slots: tuple[PullSlotResult, ...]
    final_n: int
    final_s: int
    ssr_count: int
    sr_count: int
    r_count: int


class RandomSource(Protocol):
    def randbelow(self, upper_bound: int) -> int:
        ...


class ProbabilityModel(Protocol):
    algorithm_version: str

    def compute_rates(self, total_pulls: int, total_ssrs: int) -> PullRates:
        ...


class _SecretsRandomSource:
    def randbelow(self, upper_bound: int) -> int:
        return secrets.randbelow(upper_bound)


def ssr_numerator_v1(total_pulls: int, total_ssrs: int) -> int:
    """nikke-balance-v1 的 SSR 分子（分母 12500）：2.6% ~ 8.0%，基准 4.0%。"""
    if type(total_pulls) is not int or type(total_ssrs) is not int:
        raise ValueError("累计抽数与 SSR 数必须为整数")
    if total_pulls < 0 or total_ssrs < 0 or total_ssrs > total_pulls:
        raise ValueError(f"非法的累计抽卡统计: N={total_pulls}, S={total_ssrs}")
    deficit = total_pulls - 25 * total_ssrs
    return min(
        BALANCE_V1_MAX_NUMERATOR,
        max(BALANCE_V1_MIN_NUMERATOR, BALANCE_V1_BASE_NUMERATOR + 2 * deficit),
    )


class NikkeBalanceV1Model:
    """来自 NIKKE_招募概率研究/recruit.py 的正式双向动态仓检模型。"""

    algorithm_version = BALANCE_V1_VERSION

    def compute_rates(self, total_pulls: int, total_ssrs: int) -> PullRates:
        numerator = ssr_numerator_v1(total_pulls, total_ssrs)
        p_ssr_ppm = numerator * (RATE_SCALE // BALANCE_V1_DENOMINATOR)
        rem = RATE_SCALE - p_ssr_ppm
        p_sr_ppm = round(rem * SR_CONDITIONAL_NUMERATOR / NON_SSR_CONDITIONAL_DENOMINATOR)
        p_r_ppm = rem - p_sr_ppm
        return PullRates(p_ssr_ppm=p_ssr_ppm, p_sr_ppm=p_sr_ppm, p_r_ppm=p_r_ppm)

    def draw_rarity(
        self,
        total_pulls: int,
        total_ssrs: int,
        rng: RandomSource,
    ) -> tuple[str, PullRates]:
        """无舍入误差的精确两阶段抽取：12500 判 SSR，96 判 SR(43)/R(53)。"""
        numerator = ssr_numerator_v1(total_pulls, total_ssrs)
        rates = self.compute_rates(total_pulls, total_ssrs)

        roll = rng.randbelow(BALANCE_V1_DENOMINATOR)
        if roll < 0 or roll >= BALANCE_V1_DENOMINATOR:
            raise ValueError(f"随机源返回值越界: {roll}")
        if roll < numerator:
            return "SSR", rates

        cond_roll = rng.randbelow(NON_SSR_CONDITIONAL_DENOMINATOR)
        if cond_roll < 0 or cond_roll >= NON_SSR_CONDITIONAL_DENOMINATOR:
            raise ValueError(f"随机源返回值越界: {cond_roll}")
        rarity = "SR" if cond_roll < SR_CONDITIONAL_NUMERATOR else "R"
        return rarity, rates


class PlaceholderFlatRateModel:
    """保留向后兼容的固定 4% 模型（供对比测试用）。"""

    algorithm_version = "placeholder-flat-4pct-v0"

    def compute_rates(self, total_pulls: int, total_ssrs: int) -> PullRates:
        if total_pulls < 0 or total_ssrs < 0 or total_ssrs > total_pulls:
            raise ValueError(f"非法的累计抽卡统计: N={total_pulls}, S={total_ssrs}")
        return PullRates(p_ssr_ppm=40_000, p_sr_ppm=430_000, p_r_ppm=530_000)


class PullEngine:
    """逐抽顺序执行引擎，在同稀有度有效池内均匀抽取角色。"""

    def __init__(
        self,
        model: ProbabilityModel | None = None,
        rng: RandomSource | None = None,
    ):
        self.model: ProbabilityModel = model if model is not None else NikkeBalanceV1Model()
        self.rng: RandomSource = rng if rng is not None else _SecretsRandomSource()

    def execute_ten_pull(
        self,
        snapshot: PoolSnapshot,
        initial_n: int,
        initial_s: int,
    ) -> TenPullOutcome:
        if type(initial_n) is not int or type(initial_s) is not int:
            raise ValueError("累计抽数与 SSR 数必须为整数")
        if initial_n < 0 or initial_s < 0 or initial_s > initial_n:
            raise ValueError(f"非法的累计抽卡统计: N={initial_n}, S={initial_s}")

        for rarity in ("R", "SR", "SSR"):
            if not snapshot.recruit_pool.get(rarity):
                raise ValueError(f"卡池缺少 {rarity} 候选角色，拒绝执行十连")

        n = initial_n
        s = initial_s
        slots: list[PullSlotResult] = []
        ssr_count = 0
        sr_count = 0
        r_count = 0

        for slot_idx in range(1, TEN_PULL_SIZE + 1):
            draw_fn = getattr(self.model, "draw_rarity", None)
            if callable(draw_fn):
                rarity, rates = draw_fn(n, s, self.rng)
            else:
                rates = self.model.compute_rates(n, s)
                roll = self.rng.randbelow(RATE_SCALE)
                if roll < 0 or roll >= RATE_SCALE:
                    raise ValueError(f"随机源返回值越界: {roll}")
                if roll < rates.p_ssr_ppm:
                    rarity = "SSR"
                elif roll < rates.p_ssr_ppm + rates.p_sr_ppm:
                    rarity = "SR"
                else:
                    rarity = "R"

            if rarity == "SSR":
                ssr_count += 1
            elif rarity == "SR":
                sr_count += 1
            else:
                r_count += 1

            candidates = snapshot.recruit_pool[rarity]
            pick_idx = self.rng.randbelow(len(candidates))
            if pick_idx < 0 or pick_idx >= len(candidates):
                raise ValueError(f"随机源角色下标越界: {pick_idx}")
            character_id = candidates[pick_idx]

            slots.append(
                PullSlotResult(
                    slot=slot_idx,
                    character_id=character_id,
                    rarity=rarity,
                    p_ssr_ppm=rates.p_ssr_ppm,
                    p_sr_ppm=rates.p_sr_ppm,
                    p_r_ppm=rates.p_r_ppm,
                    n_before=n,
                    s_before=s,
                )
            )
            n += 1
            if rarity == "SSR":
                s += 1

        return TenPullOutcome(
            algorithm_version=self.model.algorithm_version,
            slots=tuple(slots),
            final_n=n,
            final_s=s,
            ssr_count=ssr_count,
            sr_count=sr_count,
            r_count=r_count,
        )
