"""NIKKE 招募与集卡小游戏纯文本命令解析与格式化输出。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .catalog import CharacterRecord, PoolUnavailableError
from .collection import apply_acquisition, enhancement_sort_key, format_collection_rank
from .repository import (
    ExchangeUnavailableError,
    InsufficientLabelsError,
    InsufficientMileageError,
    MoldPoolMaxedError,
    TenPullRecord,
)
from .rewards import WalletBalance
from .service import GachaService

CORPORATION_ALIASES = {
    "极乐净土": "ELYSION",
    "ELYSION": "ELYSION",
    "米西利斯": "MISSILIS",
    "MISSILIS": "MISSILIS",
    "泰特拉": "TETRA",
    "TETRA": "TETRA",
    "朝圣者": "PILGRIM",
    "PILGRIM": "PILGRIM",
    "反常": "ABNORMAL",
    "ABNORMAL": "ABNORMAL",
}

CORPORATION_LABELS = {
    "ELYSION": "极乐净土",
    "MISSILIS": "米西利斯",
    "TETRA": "泰特拉",
    "PILGRIM": "朝圣者",
    "ABNORMAL": "反常",
}

RARITY_ORDER = {"SSR": 0, "SR": 1, "R": 2}
SOURCE_LABELS = {
    "ten_pull": "十连招募",
    "mold": "完美铸模",
    "golden_mileage": "黄金里程兑换",
}


def _fmt_time(utc_iso: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(utc_iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _parse_page_token(token: str) -> int:
    if not token.isdecimal() or int(token) < 1:
        raise ValueError("页码必须为大于等于 1 的正整数。")
    return int(token)


def format_checkin_gacha_hint(wallet: WalletBalance) -> str:
    """打卡或撤销成功后附带的简短资格进度提示。"""
    hint = f"招募进度 {wallet.checkin_progress}/2 · 可用十连 {wallet.available_tickets} 次"
    if wallet.debt_tickets > 0:
        hint += f"（待抵扣 {wallet.debt_tickets} 次）"
    return hint


def format_ten_pull_text(service: GachaService, record: TenPullRecord, tz: ZoneInfo) -> str:
    """纯文本十连结果（用于结果图失败降级及测试核对）。"""
    lines = [f"【十连招募结果 #{record.session_id:04d}】"]
    if record.replay:
        lines.append("（这条消息已处理，以下为当时结果，未重复消耗资格）")
    for slot in record.slots:
        char = service.catalog.get_character(slot.character_id)
        name = char.name_cn if char is not None else f"ID:{slot.character_id}"
        if slot.outcome_tag == "OVERFLOW":
            status = f"溢出 +{slot.labels_gained} 标签"
        elif slot.outcome_tag == "NEW":
            status = "NEW"
        else:
            status = format_collection_rank(slot.rarity, slot.after_count)
            if slot.rarity == "SSR" and slot.after_count == 4:
                status += " (满破)"
            elif slot.rarity == "SR" and slot.after_count == 3:
                status += " (满破)"
        lines.append(f"{slot.slot:02d}. [{slot.rarity}] {name} · {status}")
    lines.append(
        f"汇总：SSR {record.ssr_count} · SR {record.sr_count} · R {record.r_count}"
    )
    lines.append(
        f"收益：金票 +{record.mileage_gained} · 标签 +{record.labels_gained} "
        f"| 余额：十连 {record.wallet.available_tickets} · 金票 {record.wallet.golden_mileage} · 标签 {record.wallet.body_labels}"
    )
    lines.append(f"时间：{_fmt_time(record.created_at, tz)}")
    return "\n".join(lines)


def _format_character_detail(
    char: CharacterRecord,
    inv_item,
    tz: ZoneInfo,
) -> str:
    corp = CORPORATION_LABELS.get(char.corporation, char.corporation)
    lines = [
        f"【图鉴详情 · {char.name_cn}】",
        f"稀有度：{char.rarity} · 企业：{corp} · ID：{char.character_id}",
    ]
    if inv_item is None:
        lines.append("状态：未解锁")
        return "\n".join(lines)

    rank = format_collection_rank(char.rarity, inv_item.total_obtained)
    status_text = f"{rank} · " if rank and rank != "已收录" else ""
    lines.append(f"状态：{status_text}累计获得 {inv_item.total_obtained} 次")
    lines.append(f"首次获得：{_fmt_time(inv_item.first_obtained_at, tz)}")
    if inv_item.mlb_at is not None and char.rarity in {"SR", "SSR"}:
        lines.append(f"满破达成：{_fmt_time(inv_item.mlb_at, tz)}")
    if inv_item.core_max_at is not None and char.rarity == "SSR":
        lines.append(f"CORE+7 达成：{_fmt_time(inv_item.core_max_at, tz)}")
    lines.append(f"最近获得：{_fmt_time(inv_item.last_obtained_at, tz)}")
    return "\n".join(lines)


def format_pokedex(
    service: GachaService,
    actor: str,
    args: list[str],
    tz: ZoneInfo,
    *,
    page_size: int = 20,
) -> str:
    """处理 `🦌 图鉴` 及筛选、分页、单角色详情（纯文本）。"""
    chars = service.catalog.snapshot.characters
    inv = service.repo.get_inventory(actor)

    total_by_rarity = {"SSR": 0, "SR": 0, "R": 0}
    owned_by_rarity = {"SSR": 0, "SR": 0, "R": 0}
    for c in chars:
        total_by_rarity[c.rarity] += 1
        if c.character_id in inv:
            owned_by_rarity[c.rarity] += 1

    header = [
        "【妮姬收藏图鉴】",
        f"收藏：{len(inv)}/{len(chars)}",
        f"SSR：{owned_by_rarity['SSR']}/{total_by_rarity['SSR']} · "
        f"SR：{owned_by_rarity['SR']}/{total_by_rarity['SR']} · "
        f"R：{owned_by_rarity['R']}/{total_by_rarity['R']}",
    ]

    page = 1
    rarity_filter: str | None = None
    corp_filter: str | None = None
    unowned_only = False

    if args:
        if len(args) == 1 and args[0].isdecimal():
            try:
                page = _parse_page_token(args[0])
            except ValueError as exc:
                return str(exc)
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
                query = " ".join(args).strip()
                matches = service.catalog.find_characters_by_name(query)
                if not matches:
                    return f"图鉴中未找到角色：{query}"
                if len(matches) > 1:
                    names = "、".join(c.name_cn for c in matches[:10])
                    more = f" 等共 {len(matches)} 位" if len(matches) > 10 else ""
                    return f"匹配到多位角色，请指定完整名称：{names}{more}"
                char = matches[0]
                return _format_character_detail(char, inv.get(char.character_id), tz)

            if len(args) == 2:
                try:
                    page = _parse_page_token(args[1])
                except ValueError as exc:
                    return str(exc)
            elif len(args) > 2:
                return "用法：🦌 图鉴 [页码 | SSR/SR/R | 企业 | 未拥有 | 角色名]"

    filtered: list[CharacterRecord] = []
    for c in chars:
        is_owned = c.character_id in inv
        if unowned_only:
            if is_owned:
                continue
        elif not is_owned:
            continue
        if rarity_filter is not None and c.rarity != rarity_filter:
            continue
        if corp_filter is not None and c.corporation != corp_filter:
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

    if not filtered:
        if unowned_only:
            return "\n".join(header + ["恭喜，当前已收录图鉴中全部角色！"])
        return "\n".join(header + ["当前筛选暂无已收录角色，可用 🦌 图鉴 未拥有 查看未解锁角色。"])

    total_pages = (len(filtered) + page_size - 1) // page_size
    if page > total_pages:
        return f"页码超出范围，当前筛选共 {total_pages} 页。"

    start = (page - 1) * page_size
    slice_chars = filtered[start : start + page_size]

    lines = list(header)
    if unowned_only:
        lines.append(f"筛选：未拥有（共 {len(filtered)} 位）")
    elif rarity_filter or corp_filter:
        label = rarity_filter or CORPORATION_LABELS.get(corp_filter or "", corp_filter)
        lines.append(f"筛选：{label}（已拥有 {len(filtered)} 位）")

    for c in slice_chars:
        if unowned_only:
            corp = CORPORATION_LABELS.get(c.corporation, c.corporation)
            lines.append(f"[{c.rarity}] {c.name_cn}（{corp}）")
        else:
            rank = format_collection_rank(c.rarity, inv[c.character_id].total_obtained)
            suffix = f" {rank}" if rank and rank != "已收录" else ""
            lines.append(f"[{c.rarity}] {c.name_cn}{suffix}")

    lines.append(f"第{page}/{total_pages}页")
    return "\n".join(lines)


def format_gacha_history(
    service: GachaService,
    actor: str,
    args: list[str],
    tz: ZoneInfo,
    *,
    session_page_size: int = 5,
    acq_page_size: int = 10,
) -> str:
    """处理 `🦌 抽卡历史`、指定编号、页码及角色全渠道获取历史（严格隔离 actor）。"""
    if not args:
        page = 1
    elif len(args) == 1 and args[0].isdecimal():
        session_id = int(args[0])
        if session_id < 1:
            return "招募编号必须为正整数。"
        detail = service.repo.get_session_detail(actor, session_id)
        if detail is None:
            return f"未找到本人编号为 #{session_id:04d} 的十连记录。"
        return format_ten_pull_text(service, detail, tz)
    elif args[0] == "页":
        if len(args) != 2:
            return "用法：🦌 抽卡历史 页 <页码>"
        try:
            page = _parse_page_token(args[1])
        except ValueError as exc:
            return str(exc)
    else:
        # 角色全渠道获取历史：[角色名..., 可选页码]
        acq_page = 1
        name_tokens = list(args)
        if len(name_tokens) >= 3 and name_tokens[-2] == "页":
            try:
                acq_page = _parse_page_token(name_tokens[-1])
            except ValueError as exc:
                return str(exc)
            name_tokens = name_tokens[:-2]
        query = " ".join(name_tokens).strip()
        if not query:
            return "用法：🦌 抽卡历史 [编号 | 页 <页码> | 角色名]"
        matches = service.catalog.find_characters_by_name(query)
        if not matches:
            return f"未找到角色：{query}"
        if len(matches) > 1:
            names = "、".join(c.name_cn for c in matches[:10])
            more = f" 等共 {len(matches)} 位" if len(matches) > 10 else ""
            return f"匹配到多位角色，请指定完整名称：{names}{more}"
        char = matches[0]
        total, records = service.repo.list_character_acquisitions(
            actor, char.character_id, page=acq_page, page_size=acq_page_size
        )
        if total == 0:
            return f"【{char.name_cn} 获取历史】\n暂无任何获取记录。"
        total_pages = (total + acq_page_size - 1) // acq_page_size
        if acq_page > total_pages:
            return f"页码超出范围，该角色获取历史共 {total_pages} 页。"
        lines = [f"【{char.name_cn} 全渠道获取历史】（共 {total} 次）"]
        for rec in records:
            src = SOURCE_LABELS.get(rec.source, rec.source)
            if rec.session_id is not None:
                src = f"{src} #{rec.session_id:04d}"
            state = (
                f"溢出 +{rec.labels_delta} 标签"
                if rec.outcome_tag == "OVERFLOW"
                else rec.outcome_tag
            )
            lines.append(
                f"{_fmt_time(rec.created_at, tz)} · {src} · {state}（累计第 {rec.after_count} 张）"
            )
        lines.append(f"第{acq_page}/{total_pages}页")
        return "\n".join(lines)

    total, sessions = service.repo.list_sessions(actor, page=page, page_size=session_page_size)
    if total == 0:
        return "【十连招募历史】\n暂无十连记录。每两次有效 🦌 打卡可获得一次十连资格。"
    total_pages = (total + session_page_size - 1) // session_page_size
    if page > total_pages:
        return f"页码超出范围，当前共 {total_pages} 页。"

    lines = [f"【十连招募历史】（共 {total} 次）"]
    for sess in sessions:
        ssr_names = [
            (service.catalog.get_character(cid).name_cn if service.catalog.get_character(cid) else f"ID:{cid}")
            for cid in sess.ssr_character_ids
        ]
        ssr_desc = "、".join(ssr_names) if ssr_names else "无"
        lines.append(
            f"#{sess.session_id:04d} · {_fmt_time(sess.created_at, tz)} · "
            f"SSR {sess.ssr_count}/SR {sess.sr_count}/R {sess.r_count} · SSR：{ssr_desc}"
        )
    lines.append(f"第{page}/{total_pages}页 · 查看详情：🦌 抽卡历史 编号")
    return "\n".join(lines)


def format_stats(service: GachaService, actor: str, tz: ZoneInfo) -> str:
    """处理 `🦌 统计`（仅正常十连参与 SSR 出率统计，铸模与兑换独立展示）。"""
    stats = service.repo.get_pull_stats(actor)
    wallet = service.repo.get_wallet(actor)
    next_rates = service.engine.model.compute_rates(stats.total_pulls, stats.total_ssr)

    if stats.total_pulls == 0:
        actual_rate_text = "未产生统计"
    else:
        actual_rate_text = f"{(stats.total_ssr / stats.total_pulls) * 100:.2f}%"

    expected_ssr = 0.04 * stats.total_pulls
    deviation = expected_ssr - stats.total_ssr

    lines = [
        "【个人招募统计】",
        f"可用十连资格：{wallet.available_tickets} 次（打卡进度 {wallet.checkin_progress}/2"
        + (f"，待抵扣 {wallet.debt_tickets} 次" if wallet.debt_tickets else "")
        + "）",
        f"累计正常招募：{stats.total_pulls} 抽（SSR {stats.total_ssr} / SR {stats.total_sr} / R {stats.total_r}）",
        f"实际 SSR 出率：{actual_rate_text} · 理论期望 0.04N：{expected_ssr:.2f} · 期望偏差：{deviation:+.2f}",
        f"下一抽 SSR 仓检概率：{next_rates.p_ssr * 100:.2f}%（算法：{service.engine.model.algorithm_version}）",
        f"连续未出 SSR：当前 {stats.current_no_ssr_streak} 抽 · 历史最长 {stats.max_no_ssr_streak} 抽",
        f"单次十连最多 SSR：{stats.max_ssr_in_ten_pull} 位",
        f"完美铸模开启：{stats.mold_count} 次 · 黄金里程兑换：{stats.mileage_exchange_count} 次",
    ]
    return "\n".join(lines)


def format_shop(service: GachaService, actor: str, now: datetime, tz: ZoneInfo) -> str:
    """处理 `🦌 商店`（纯文本）。"""
    wallet = service.repo.get_wallet(actor)
    lines = [
        "【里程与铸模商店】",
        f"黄金里程券：{wallet.golden_mileage} 张 · 躯体标签：{wallet.body_labels}",
        f"可用十连资格：{wallet.available_tickets} 次（打卡进度 {wallet.checkin_progress}/2"
        + (f"，待抵扣 {wallet.debt_tickets} 次" if wallet.debt_tickets else "")
        + "）",
        "1) 完美铸模：6000 躯体标签 -> 必得 1 位铸模池 SSR（指令：🦌 铸模）",
    ]

    try:
        active_mileage = service.catalog.get_active_golden_mileage(now)
    except PoolUnavailableError as exc:
        lines.append(f"2) 黄金里程兑换（200 金票）：暂不可用（{exc}）")
        return "\n".join(lines)

    if not active_mileage:
        lines.append("2) 黄金里程兑换（200 金票）：当前暂无已核验的当期 UP 兑换名单。")
    else:
        lines.append("2) 黄金里程兑换（200 金票/位，指令：🦌 兑换 角色名）：")
        for entry in active_mileage:
            char = service.catalog.get_character(entry.character_id)
            name = char.name_cn if char is not None else f"ID:{entry.character_id}"
            until = _fmt_time(entry.effective_until, tz)
            lines.append(f"   - {name}（{entry.banner_name}，截至 {until}）")
    return "\n".join(lines)


def handle_mold_command(
    service: GachaService,
    actor: str,
    idempotency_key: str,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """处理 `🦌 铸模`（纯文本）。"""
    try:
        res = service.open_mold(actor, idempotency_key, now)
    except (PoolUnavailableError, MoldPoolMaxedError, InsufficientLabelsError) as exc:
        return str(exc)

    char = service.catalog.get_character(res.character_id)
    name = char.name_cn if char is not None else f"ID:{res.character_id}"
    rank = format_collection_rank("SSR", res.after_count)
    lines = ["【完美铸模开启结果】"]
    if res.replay:
        lines.append("（这条消息已处理，以下为当时结果，未重复扣除标签）")
    lines.append(f"获得 SSR：{name}（{res.outcome_tag} · {rank}）")
    if res.labels_refunded:
        lines.append(
            f"消耗：6000 标签 · 溢出返还：+{res.labels_refunded} 标签 · 净变化：{res.net_labels_delta:+d} 标签"
        )
    else:
        lines.append(f"消耗：6000 标签 · 净变化：{res.net_labels_delta:+d} 标签")
    lines.append(f"剩余躯体标签：{res.wallet.body_labels} · 时间：{_fmt_time(res.created_at, tz)}")
    return "\n".join(lines)


def handle_exchange_command(
    service: GachaService,
    actor: str,
    idempotency_key: str,
    args: list[str],
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """处理 `🦌 兑换 角色名`（纯文本）。"""
    query = " ".join(args).strip()
    try:
        res = service.exchange_golden_mileage(actor, idempotency_key, query, now)
    except (PoolUnavailableError, ExchangeUnavailableError, InsufficientMileageError) as exc:
        return str(exc)

    char = service.catalog.get_character(res.character_id)
    name = char.name_cn if char is not None else f"ID:{res.character_id}"
    rank = format_collection_rank("SSR", res.after_count)
    lines = ["【黄金里程兑换结果】"]
    if res.replay:
        lines.append("（这条消息已处理，以下为当时结果，未重复扣除金票）")
    lines.append(f"兑换 SSR：{name}（{res.outcome_tag} · {rank}）")
    if res.labels_refunded:
        lines.append(f"消耗：200 金票 · 溢出返还：+{res.labels_refunded} 躯体标签")
    else:
        lines.append("消耗：200 金票")
    lines.append(
        f"剩余：黄金里程 {res.wallet.golden_mileage} 张 · 躯体标签 {res.wallet.body_labels} · "
        f"时间：{_fmt_time(res.created_at, tz)}"
    )
    return "\n".join(lines)
