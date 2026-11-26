"""Phase 2 & 3 概率模型、nikke-balance-v1 双向仓检与十连顺序引擎测试。"""

from __future__ import annotations

import copy
import random
from pathlib import Path

import pytest

from astrbot_plugin_checkin_calendar.gacha.catalog import CatalogManager
from astrbot_plugin_checkin_calendar.gacha.engine import (
    BALANCE_V1_DENOMINATOR,
    RATE_SCALE,
    NikkeBalanceV1Model,
    PlaceholderFlatRateModel,
    PullEngine,
    PullRates,
    ssr_numerator_v1,
)

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SNAPSHOT = ROOT / "assets" / "gacha" / "jp_pool_snapshot.json"


class SequenceRng:
    def __init__(self, values: list[int]):
        self._values = list(values)
        self._idx = 0

    def randbelow(self, upper_bound: int) -> int:
        val = self._values[self._idx]
        self._idx += 1
        return val % upper_bound


class StdlibRandomAdapter:
    def __init__(self, seed: int):
        self._rng = random.Random(seed)

    def randbelow(self, upper_bound: int) -> int:
        return self._rng.randrange(upper_bound)


class RecordingDynamicModel:
    """验证 PullEngine 逐抽传入最新 (N, S)。"""

    algorithm_version = "mock-dynamic-v1"

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def compute_rates(self, total_pulls: int, total_ssrs: int) -> PullRates:
        self.calls.append((total_pulls, total_ssrs))
        p_ssr = min(80_000, max(20_000, 40_000 + 400 * total_pulls - 10_000 * total_ssrs))
        rem = RATE_SCALE - p_ssr
        p_sr = (rem * 43) // 96
        p_r = rem - p_sr
        return PullRates(p_ssr_ppm=p_ssr, p_sr_ppm=p_sr, p_r_ppm=p_r)


def test_nikke_balance_v1_exact_bounds_and_rates():
    model = NikkeBalanceV1Model()
    assert model.algorithm_version == "nikke-balance-v1"

    # 基准点：N=0, S=0 -> 500/12500 = 4.0%
    assert ssr_numerator_v1(0, 0) == 500
    r0 = model.compute_rates(0, 0)
    assert r0.p_ssr_ppm == 40_000
    assert r0.p_ssr_ppm + r0.p_sr_ppm + r0.p_r_ppm == RATE_SCALE

    # 缺金上调：每缺 1 个理论期望（deficit = 25），+50/12500 (+0.4% = +2*25/12500)
    assert ssr_numerator_v1(25, 0) == 550
    assert ssr_numerator_v1(50, 0) == 600
    # 上限封顶：1000/12500 = 8.0%
    assert ssr_numerator_v1(500, 0) == 1000
    assert model.compute_rates(500, 0).p_ssr_ppm == 80_000

    # 超金下调：下限封底 325/12500 = 2.6%
    assert ssr_numerator_v1(10, 1) == 470  # deficit = -15 -> 500 - 30 = 470
    assert ssr_numerator_v1(10, 10) == 325
    assert model.compute_rates(10, 10).p_ssr_ppm == 26_000


def test_nikke_balance_v1_smoke_mean_near_4_percent():
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    engine = PullEngine(model=NikkeBalanceV1Model(), rng=StdlibRandomAdapter(20261126))
    total_pulls = 0
    total_ssrs = 0
    # 2,000 个独立 100 抽轨迹（共 200,000 抽）快速烟雾验证长期均值接近 4%
    for _ in range(2000):
        n, s = 0, 0
        for _ten in range(10):
            out = engine.execute_ten_pull(snap, n, s)
            n, s = out.final_n, out.final_s
        total_pulls += n
        total_ssrs += s
    mean_rate = total_ssrs / total_pulls
    assert 0.0385 <= mean_rate <= 0.0415


def test_placeholder_flat_rate_model_kept_valid():
    model = PlaceholderFlatRateModel()
    rates = model.compute_rates(0, 0)
    assert (rates.p_ssr_ppm, rates.p_sr_ppm, rates.p_r_ppm) == (40_000, 430_000, 530_000)
    with pytest.raises(ValueError):
        PullRates(50_000, 500_000, 500_000)


def test_pull_engine_sequential_update_duplicates_and_multi_ssr():
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    model = RecordingDynamicModel()
    rng = SequenceRng([
        0, 0,          # slot 1: SSR idx 0
        100, 0,        # slot 2: SSR idx 0 (允许同一十连重复角色与多金)
        100_000, 1,    # slot 3: SR idx 1
        900_000, 0,    # slot 4: R idx 0
        900_000, 1,    # slot 5: R idx 1
        900_000, 2,    # slot 6: R idx 2
        900_000, 3,    # slot 7: R idx 3
        900_000, 4,    # slot 8: R idx 4
        900_000, 5,    # slot 9: R idx 5
        900_000, 6,    # slot 10: R idx 6
    ])
    engine = PullEngine(model=model, rng=rng)
    outcome = engine.execute_ten_pull(snap, initial_n=20, initial_s=0)

    assert outcome.algorithm_version == "mock-dynamic-v1"
    assert outcome.ssr_count == 2
    assert outcome.sr_count == 1
    assert outcome.r_count == 7
    assert outcome.final_n == 30
    assert outcome.final_s == 2
    assert len(outcome.slots) == 10
    assert outcome.slots[0].character_id == outcome.slots[1].character_id
    assert model.calls == [
        (20, 0), (21, 1), (22, 2), (23, 2), (24, 2),
        (25, 2), (26, 2), (27, 2), (28, 2), (29, 2),
    ]


def test_pull_engine_rejects_empty_pool_without_reallocating():
    snap = CatalogManager(BUILTIN_SNAPSHOT).snapshot
    broken = copy.copy(snap)
    object.__setattr__(broken, "recruit_pool", {
        "R": snap.recruit_pool["R"],
        "SR": snap.recruit_pool["SR"],
        "SSR": (),
    })
    engine = PullEngine(rng=SequenceRng([0] * 30))
    with pytest.raises(ValueError, match="SSR"):
        engine.execute_ten_pull(broken, 0, 0)
