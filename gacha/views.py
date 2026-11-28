"""图鉴、统计、商店的图片卡片数据（失败时调用方退回纯文本）。"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..presentation import font_faces, shared_css, theme_class
from .assets import PortraitAssetResolver
from .catalog import PoolUnavailableError
from .collection import enhancement_sort_key, format_collection_rank
from .commands import CORPORATION_ALIASES, CORPORATION_LABELS, RARITY_ORDER, _fmt_time
from .service import GachaService

STATIC_TEXT = (
    "妮姬收藏图鉴收藏已收录未解锁未拥有筛选第页共位统计个人招募里程与铸模商店"
    "可用十连资格打卡进度待抵扣累计正常招募抽实际出率理论期望偏差下一抽仓检概率算法"
    "连续未出当前历史最长单次最多完美铸模开启次黄金里程兑换金票躯体标签张截至暂无已核验当期名单"
    "SSRSRR★☆%·/:+-.()"
)


def _collect_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "".join(_collect_text(v) for k, v in obj.items() if k not in {"fonts", "base_css"})
    if isinstance(obj, (list, tuple)):
        return "".join(_collect_text(v) for v in obj)
    if isinstance(obj, (int, float)):
        return str(obj)
    return ""


def _finish(data: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    data["base_css"] = shared_css()
    data["theme_class"] = theme_class(now)
    data["fonts"] = font_faces(_collect_text(data) + STATIC_TEXT)
    return data


def _portrait(resolver: PortraitAssetResolver, resource_id: int) -> tuple[str | None, str]:
    res = resolver.resolve(resource_id)
    if res.path is None:
        return None, "placeholder"
    try:
        raw = res.path.read_bytes()
    except OSError:
        return None, "placeholder"
    return "data:image/webp;base64," + base64.b64encode(raw).decode("ascii"), res.kind


def build_pokedex_payload(
    service: GachaService,
    actor: str,
    args: list[str],
    resolver: PortraitAssetResolver,
    *,
    page_size: int = 20,
) -> dict[str, Any] | None:
    """仅处理列表类图鉴；详情、错误与空结果返回 None，由文字路径给出提示。"""
    chars = service.catalog.snapshot.characters
    inv = service.repo.get_inventory(actor)

    page = 1
    rarity_filter = corp_filter = None
    unowned_only = False
    if args:
        if len(args) == 1 and args[0].isdecimal():
            page = int(args[0])
        else:
            first = args[0].strip()
            upper = first.upper()
            if upper in {"SSR", "SR", "R"}:
                rarity_filter = upper
            elif first in CORPORATION_ALIASES or upper in CORPORATION_ALIASES:
                corp_filter = CORPORATION_ALIASES.get(first) or CORPORATION_ALIASES[upper]
            elif first == "未拥有":
                unowned_only = True
            else:
                return None
            if len(args) == 2 and args[1].isdecimal():
                page = int(args[1])
            elif len(args) > 1:
                return None
    if page < 1:
        return None

    total_by = {"SSR": 0, "SR": 0, "R": 0}
    owned_by = {"SSR": 0, "SR": 0, "R": 0}
    for c in chars:
        total_by[c.rarity] += 1
        if c.character_id in inv:
            owned_by[c.rarity] += 1

    filtered = []
    for c in chars:
        owned = c.character_id in inv
        if unowned_only == owned:
            continue
        if rarity_filter and c.rarity != rarity_filter:
            continue
        if corp_filter and c.corporation != corp_filter:
            continue
        filtered.append(c)
    if unowned_only:
        filtered.sort(key=lambda c: (RARITY_ORDER[c.rarity], c.character_id))
    else:
        filtered.sort(
            key=lambda c: (
                RARITY_ORDER[c.rarity],
                -enhancement_sort_key(c.rarity, inv[c.character_id].total_obtained),
                c.character_id,
            )
        )
    total_pages = (len(filtered) + page_size - 1) // page_size
    if not filtered or page > total_pages:
        return None

    cards = []
    for c in filtered[(page - 1) * page_size : page * page_size]:
        uri, kind = _portrait(resolver, c.resource_id)
        item = inv.get(c.character_id)
        rank = format_collection_rank(c.rarity, item.total_obtained) if item else "未解锁"
        cards.append(
            {
                "name": c.name_cn,
                "rarity": c.rarity,
                "corp": CORPORATION_LABELS.get(c.corporation, c.corporation),
                "owned": item is not None,
                "rank": rank,
                "count": item.total_obtained if item else 0,
                "uri": uri,
                "kind": kind,
            }
        )
    if unowned_only:
        flt = f"未拥有 · {len(filtered)} 位"
    elif rarity_filter:
        flt = f"{rarity_filter} · 已拥有 {len(filtered)} 位"
    elif corp_filter:
        flt = f"{CORPORATION_LABELS.get(corp_filter, corp_filter)} · 已拥有 {len(filtered)} 位"
    else:
        flt = f"全部已收录 · {len(filtered)} 位"
    progress = [
        {"rarity": r, "owned": owned_by[r], "total": total_by[r],
         "pct": round(owned_by[r] / total_by[r] * 100, 1) if total_by[r] else 0}
        for r in ("SSR", "SR", "R")
    ]
    return _finish(
        {
            "owned_total": len(inv),
            "all_total": len(chars),
            "progress": progress,
            "filter": flt,
            "page": page,
            "pages": total_pages,
            "cards": cards,
        }
    )


def build_stats_payload(service: GachaService, actor: str) -> dict[str, Any]:
    stats = service.repo.get_pull_stats(actor)
    wallet = service.repo.get_wallet(actor)
    nxt = service.engine.model.compute_rates(stats.total_pulls, stats.total_ssr)
    expected = 0.04 * stats.total_pulls
    total = stats.total_pulls
    actual = f"{stats.total_ssr / total * 100:.2f}%" if total else "—"

    def pct(n: int) -> float:
        return round(n / total * 100, 2) if total else 0

    return _finish(
        {
            "tickets": wallet.available_tickets,
            "checkin_progress": wallet.checkin_progress,
            "debt": wallet.debt_tickets,
            "total_pulls": total,
            "ssr": stats.total_ssr,
            "sr": stats.total_sr,
            "r": stats.total_r,
            "ssr_pct": pct(stats.total_ssr),
            "sr_pct": pct(stats.total_sr),
            "r_pct": pct(stats.total_r),
            "actual_rate": actual,
            "expected": f"{expected:.2f}",
            "deviation": f"{expected - stats.total_ssr:+.2f}",
            "next_rate": f"{nxt.p_ssr * 100:.2f}%",
            "algorithm": service.engine.model.algorithm_version,
            "streak": stats.current_no_ssr_streak,
            "max_streak": stats.max_no_ssr_streak,
            "max_ssr_ten": stats.max_ssr_in_ten_pull,
            "mold_count": stats.mold_count,
            "exchange_count": stats.mileage_exchange_count,
        }
    )


def build_shop_payload(
    service: GachaService,
    actor: str,
    now: datetime,
    tz: ZoneInfo,
    resolver: PortraitAssetResolver,
) -> dict[str, Any]:
    wallet = service.repo.get_wallet(actor)
    note = ""
    ups: list[dict[str, Any]] = []
    try:
        for entry in service.catalog.get_active_golden_mileage(now):
            char = service.catalog.get_character(entry.character_id)
            uri, kind = _portrait(resolver, char.resource_id) if char else (None, "placeholder")
            ups.append(
                {
                    "name": char.name_cn if char else f"ID:{entry.character_id}",
                    "banner": entry.banner_name,
                    "until": _fmt_time(entry.effective_until, tz),
                    "uri": uri,
                    "kind": kind,
                }
            )
        if not ups:
            note = "当前暂无已核验的当期 UP 兑换名单。"
    except PoolUnavailableError as exc:
        note = f"黄金里程兑换暂不可用（{exc}）"
    return _finish(
        {
            "mileage": wallet.golden_mileage,
            "labels": wallet.body_labels,
            "tickets": wallet.available_tickets,
            "checkin_progress": wallet.checkin_progress,
            "debt": wallet.debt_tickets,
            "mold_pct": round(min(100, wallet.body_labels / 6000 * 100), 1),
            "mileage_pct": round(min(100, wallet.golden_mileage / 200 * 100), 1),
            "ups": ups,
            "note": note,
        },
        now,
    )
