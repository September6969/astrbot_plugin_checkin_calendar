"""十连招募静态结算图、Part A+B 单一 GIF 合成与消息发送/撤回编排。"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFilter

from ..presentation import clean_name, font_faces
from .assets import PortraitAssetResolver
from .collection import format_collection_rank
from .commands import format_ten_pull_text
from .repository import SessionSlotRecord, TenPullRecord
from .service import GachaService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTRO_DIR = ROOT / "assets" / "gacha"
DEFAULT_RESULT_TEMPLATE = ROOT / "templates" / "gacha_result.html"

ANIMATION_VERSION = "nikke-gacha-anim-v2"
CANVAS_W = 360
CANVAS_H = 640
CHARCOAL_BG = (34, 32, 30)  # #22201E
HOLD_FINAL_MS = 3500
RECALL_HOLD_MS = 900

RARITY_PART_B_COLORS = {
    "R": {
        "fill": (132, 206, 255),
        "glow": (42, 132, 248),
        "beam": (64, 154, 255),
        "rim": (110, 190, 255),
    },
    "SR": {
        "fill": (228, 174, 255),
        "glow": (162, 64, 240),
        "beam": (182, 86, 252),
        "rim": (212, 146, 255),
    },
    "SSR": {
        "fill": (255, 232, 118),
        "glow": (240, 152, 42),
        "beam": (236, 134, 46),
        "rim": (232, 148, 68),
    },
}

GIF_RENDER_TIMEOUT = 12.0
RESULT_RENDER_TIMEOUT = 30.0
ONEBOT_SEND_TIMEOUT = 8.0
ONEBOT_RECALL_TIMEOUT = 3.0
MAX_PLAY_WAIT_SECONDS = 10.0


def _slot_status_badge(slot: SessionSlotRecord) -> tuple[str, str]:
    if slot.outcome_tag == "OVERFLOW":
        return f"溢出 +{slot.labels_gained}", "status-overflow"
    if slot.outcome_tag == "NEW":
        return "NEW", "status-new"
    rank = format_collection_rank(slot.rarity, slot.after_count)
    if (slot.rarity == "SSR" and slot.after_count == 4) or (
        slot.rarity == "SR" and slot.after_count == 3
    ):
        return f"{rank} 满破", "status-rank"
    return rank, "status-rank"


def build_result_image_payload(
    service: GachaService,
    record: TenPullRecord,
    owner_name: str,
    tz: ZoneInfo,
    resolver: PortraitAssetResolver,
) -> dict[str, Any]:
    """构建 2×5 十连静态结算图模板数据（严格按第 1~10 抽顺序，零网络读取）。"""
    safe_owner = clean_name(owner_name)
    created_local = datetime.fromisoformat(record.created_at).astimezone(tz).strftime(
        "%Y-%m-%d %H:%M"
    )
    cards: list[dict[str, Any]] = []
    text_samples: list[str] = [
        safe_owner,
        record.snapshot_version,
        "招募结果金票躯体标签余票累计出率溢出满破已收录",
    ]

    for slot in record.slots:
        char = service.catalog.get_character(slot.character_id)
        name = char.name_cn if char is not None else f"ID:{slot.character_id}"
        res = resolver.resolve(char.resource_id) if char is not None else None
        portrait_data_uri: str | None = None
        portrait_kind = "placeholder"
        if res is not None and res.path is not None:
            raw_bytes = res.path.read_bytes()
            portrait_data_uri = "data:image/webp;base64," + base64.b64encode(raw_bytes).decode(
                "ascii"
            )
            portrait_kind = res.kind

        status_label, status_class = _slot_status_badge(slot)
        text_samples.append(name)
        text_samples.append(status_label)
        cards.append(
            {
                "slot": slot.slot,
                "character_id": slot.character_id,
                "name": name,
                "rarity": slot.rarity,
                "portrait_data_uri": portrait_data_uri,
                "portrait_kind": portrait_kind,
                "status_label": status_label,
                "status_class": status_class,
            }
        )

    total_pulls = record.stats.total_pulls
    rate_str = (
        f"{(record.stats.total_ssr / total_pulls) * 100:.2f}%"
        if total_pulls > 0
        else "0.00%"
    )

    return {
        "session_code": f"{record.session_id:04d}",
        "owner_name": safe_owner,
        "created_at_local": created_local,
        "snapshot_version": record.snapshot_version,
        "cards": cards,
        "ssr_count": record.ssr_count,
        "mileage_gained": record.mileage_gained,
        "labels_gained": record.labels_gained,
        "available_tickets": record.wallet.available_tickets,
        "golden_mileage": record.wallet.golden_mileage,
        "body_labels": record.wallet.body_labels,
        "cumulative_ssr_rate": rate_str,
        "fonts": font_faces("".join(text_samples)),
    }


def _build_silhouette_mask(w: int = 24, h: int = 72) -> Image.Image:
    """绘制极简圆头圆润整体人形遮罩，贴合参考图比例。"""
    scale = 4
    sw, sh = w * scale, h * scale
    mask = Image.new("L", (sw, sh), 0)
    d = ImageDraw.Draw(mask)
    cx = sw // 2
    # 头部圆形
    head_r = int(5.4 * scale)
    head_cy = int(8 * scale)
    d.ellipse((cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r), fill=255)
    # 圆润躯干与腿部整体轮廓（微上宽下收胶囊）
    top_y = int(16.5 * scale)
    bot_y = int(67 * scale)
    # 肩部圆弧 + 梯形身体 + 底部圆弧
    shoulder_hw = int(9.2 * scale)
    hip_hw = int(7.8 * scale)
    foot_hw = int(5.8 * scale)
    mid_y = int(42 * scale)
    d.rounded_rectangle((cx - shoulder_hw, top_y, cx + shoulder_hw, mid_y + 10), radius=int(6.5 * scale), fill=255)
    d.polygon(
        [
            (cx - shoulder_hw + 2, top_y + int(10 * scale)),
            (cx + shoulder_hw - 2, top_y + int(10 * scale)),
            (cx + foot_hw, bot_y - int(6 * scale)),
            (cx - foot_hw, bot_y - int(6 * scale)),
        ],
        fill=255,
    )
    d.ellipse((cx - foot_hw, bot_y - int(11 * scale), cx + foot_hw, bot_y), fill=255)
    # 两侧腰线略微收顺
    d.rounded_rectangle((cx - hip_hw, mid_y - 8, cx + hip_hw, bot_y - int(4 * scale)), radius=int(5.2 * scale), fill=255)
    return mask.resize((w, h), Image.Resampling.LANCZOS)


def _dilate_mask(mask: Image.Image, radius: int = 2) -> Image.Image:
    return mask.filter(ImageFilter.MaxFilter(radius * 2 + 1))


class GachaGifRenderer:
    """合成 Part A（预制机械门闸）+ Part B（横向十小人垂直扫描揭晓）单一 GIF。"""

    def __init__(
        self,
        intro_dir: Path = DEFAULT_INTRO_DIR,
        *,
        cache_capacity: int = 32,
        output_dir: Path | None = None,
    ):
        self.intro_dir = Path(intro_dir)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.cache_capacity = max(1, cache_capacity)
        self._cache: OrderedDict[tuple[tuple[str, ...], str], tuple[bytes, float]] = OrderedDict()
        self._intro_cache: dict[str, tuple[list[Image.Image], list[int]]] = {}
        self._base_bg = Image.new("RGBA", (CANVAS_W, CANVAS_H), CHARCOAL_BG + (255,))
        self._lane_centers = [int(22 + i * 35.1) for i in range(10)]  # 单排 10 个等距
        self._fig_cy = int(CANVAS_H * 0.49)
        self._fig_w = 24
        self._fig_h = 72
        self._beam_w = 28
        self._beam_h = 260
        self._init_sprites()

    def _init_sprites(self) -> None:
        mask = _build_silhouette_mask(self._fig_w, self._fig_h)
        pad = 12
        cw, ch = self._fig_w + pad * 2, self._fig_h + pad * 2
        padded_mask = Image.new("L", (cw, ch), 0)
        padded_mask.paste(mask, (pad, pad))

        # 暗色未点亮小人
        unlit = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        unlit_fill = Image.new("RGBA", (cw, ch), (74, 62, 56, 255))
        unlit.paste(unlit_fill, (0, 0), padded_mask)
        self._unlit_sprite = unlit
        self._sprite_pad = pad

        self._lit_sprites: dict[str, Image.Image] = {}
        self._preglow_sprites: dict[str, Image.Image] = {}
        self._beam_strips: dict[str, Image.Image] = {}

        rim_mask = _dilate_mask(padded_mask, radius=1)
        for rarity, cols in RARITY_PART_B_COLORS.items():
            # 点亮小人 + 柔和外发光
            glow_layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            glow_fill = Image.new("RGBA", (cw, ch), cols["glow"] + (210,))
            glow_layer.paste(glow_fill, (0, 0), padded_mask)
            glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=4.5))

            body_layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            body_fill = Image.new("RGBA", (cw, ch), cols["fill"] + (255,))
            body_layer.paste(body_fill, (0, 0), padded_mask)
            self._lit_sprites[rarity] = Image.alpha_composite(glow_layer, body_layer)

            # 预点亮描边轮廓（光柱刚接触时的暗底亮边状态）
            pre = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            rim_fill = Image.new("RGBA", (cw, ch), cols["rim"] + (210,))
            pre.paste(rim_fill, (0, 0), rim_mask)
            dark_inner = Image.new("RGBA", (cw, ch), (116, 62, 32, 255) if rarity == "SSR" else (60, 62, 74, 255))
            pre.paste(dark_inner, (0, 0), padded_mask)
            self._preglow_sprites[rarity] = pre

            # 竖直渐变扫描光柱（中间最亮，上下平滑衰减至透明）
            strip = Image.new("RGBA", (self._beam_w, self._beam_h), (0, 0, 0, 0))
            sd = ImageDraw.Draw(strip)
            half_h = self._beam_h / 2.0
            for y in range(self._beam_h):
                dist = abs(y - half_h) / half_h
                alpha = int(195 * ((1.0 - dist) ** 1.7))
                sd.line((0, y, self._beam_w - 1, y), fill=cols["beam"] + (alpha,))
            self._beam_strips[rarity] = strip

    def _load_intro(self, tier: str) -> tuple[list[Image.Image], list[int]]:
        if tier in self._intro_cache:
            frames, durs = self._intro_cache[tier]
            return [f.copy() for f in frames], list(durs)
        path = self.intro_dir / f"intro_{tier}.gif"
        if not path.is_file():
            raise FileNotFoundError(f"缺少预制公共开场动画: {path}")
        frames: list[Image.Image] = []
        durations: list[int] = []
        with Image.open(path) as gif:
            idx = 0
            while True:
                try:
                    gif.seek(idx)
                except EOFError:
                    break
                frames.append(gif.convert("RGB"))
                durations.append(max(20, int(gif.info.get("duration", 90))))
                idx += 1
        self._intro_cache[tier] = (frames, durations)
        return [f.copy() for f in frames], list(durations)

    def _render_part_b_frames(
        self,
        rarities: tuple[str, ...],
    ) -> tuple[list[Image.Image], list[int]]:
        frames: list[Image.Image] = []
        durations: list[int] = []
        cy = self._fig_cy
        beam_top = cy - self._beam_h // 2
        fig_top = cy - self._fig_h // 2 - self._sprite_pad

        def _compose(lit_up_to: int, beam_intensities: dict[int, float], preglow_idx: int | None) -> Image.Image:
            canvas = self._base_bg.copy()
            # 1. 先铺设竖直渐变扫描光柱层
            if beam_intensities:
                beam_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
                for lane, intensity in beam_intensities.items():
                    if 0 <= lane < 10 and intensity > 0.02:
                        strip = self._beam_strips[rarities[lane]]
                        if intensity < 0.99:
                            r, g, b, a = strip.split()
                            a = a.point(lambda v, f=intensity: int(v * f))
                            strip = Image.merge("RGBA", (r, g, b, a))
                        bx = self._lane_centers[lane] - self._beam_w // 2
                        beam_layer.alpha_composite(strip, (bx, beam_top))
                canvas = Image.alpha_composite(canvas, beam_layer)

            # 2. 单排十个等距小人
            for i in range(10):
                fx = self._lane_centers[i] - self._fig_w // 2 - self._sprite_pad
                if i <= lit_up_to:
                    sprite = self._lit_sprites[rarities[i]]
                elif preglow_idx is not None and i == preglow_idx:
                    sprite = self._preglow_sprites[rarities[i]]
                else:
                    sprite = self._unlit_sprite
                canvas.alpha_composite(sprite, (fx, fig_top))
            return canvas.convert("RGB")

        # 初始全暗帧
        frames.append(_compose(-1, {}, 0))
        durations.append(80)

        # 从左向右逐个扫过 0..9
        for active in range(10):
            beams = {
                active: 1.0,
                active - 1: 0.56,
                active - 2: 0.22,
            }
            if active + 1 < 10:
                beams[active + 1] = 0.34
            frames.append(_compose(active, beams, active + 1 if active + 1 < 10 else None))
            durations.append(100)

        # 光柱退去，十人全亮定格保持 3500ms（播放一次不重播，等待结果图送达后撤回）
        frames.append(_compose(9, {9: 0.35, 8: 0.14}, None))
        durations.append(90)
        frames.append(_compose(9, {}, None))
        durations.append(HOLD_FINAL_MS)
        return frames, durations

    def render_bytes(self, rarities: Sequence[str]) -> tuple[bytes, float]:
        """生成 Part A + Part B 合成单一 GIF 字节流（仅播一次不循环），返回适合衔接撤回的等待秒数。"""
        seq = tuple(rarities)
        if len(seq) != 10 or any(r not in RARITY_PART_B_COLORS for r in seq):
            raise ValueError(f"十连动画需要 10 位有效稀有度: {seq!r}")

        key = (seq, ANIMATION_VERSION)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        tier = "ssr" if "SSR" in seq else ("sr" if "SR" in seq else "r")
        part_a_frames, part_a_durs = self._load_intro(tier)
        part_b_frames, part_b_durs = self._render_part_b_frames(seq)

        all_frames = part_a_frames + part_b_frames
        all_durs = part_a_durs + part_b_durs
        # 在最终十人定格期间（扫完后约 0.9s，远早于 3500ms 定格结束）触发结果发送与撤回，避免重新播放。
        wait_seconds = (sum(all_durs[:-1]) + RECALL_HOLD_MS) / 1000.0

        # 逐段自适应量化，同时保持 Part A/B 各帧色相准确、体积可控；省略 loop 参数以禁止循环重播。
        quantized = [
            f.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            for f in all_frames
        ]
        buf = BytesIO()
        quantized[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=quantized[1:],
            duration=all_durs,
            optimize=True,
            disposal=2,
        )
        gif_bytes = buf.getvalue()

        # 若体积超过 500 KB 目标，自动用 48 色再重采样一次（不修改序列或时长）。
        if len(gif_bytes) > 500_000:
            quantized_compact = [
                f.quantize(colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
                for f in all_frames
            ]
            buf_compact = BytesIO()
            quantized_compact[0].save(
                buf_compact,
                format="GIF",
                save_all=True,
                append_images=quantized_compact[1:],
                duration=all_durs,
                optimize=True,
                disposal=2,
            )
            if len(buf_compact.getvalue()) < len(gif_bytes):
                gif_bytes = buf_compact.getvalue()

        self._cache[key] = (gif_bytes, wait_seconds)
        while len(self._cache) > self.cache_capacity:
            self._cache.popitem(last=False)
        return gif_bytes, wait_seconds


async def _send_onebot_segments(
    bot: Any,
    group_id: str,
    sender_id: str,
    routing: dict[str, Any],
    segments: list[dict[str, Any]],
) -> int | None:
    if group_id and str(group_id).isdigit():
        resp = await asyncio.wait_for(
            bot.send_group_msg(group_id=int(group_id), message=segments, **routing),
            timeout=ONEBOT_SEND_TIMEOUT,
        )
    elif sender_id and str(sender_id).isdigit():
        resp = await asyncio.wait_for(
            bot.send_private_msg(user_id=int(sender_id), message=segments, **routing),
            timeout=ONEBOT_SEND_TIMEOUT,
        )
    else:
        raise ValueError("缺少有效数字群号或 QQ 号")
    return resp.get("message_id") if isinstance(resp, dict) else None


async def orchestrate_ten_pull_presentation(
    *,
    record: TenPullRecord,
    event: Any,
    service: GachaService,
    calendar_renderer: Any,
    gif_renderer: GachaGifRenderer | None,
    resolver: PortraitAssetResolver,
    owner_name: str,
    tz: ZoneInfo,
    fast_mode: bool = False,
    gif_enabled: bool = True,
    gif_buffer_seconds: float = 1.0,
    result_format: str = "webp",
    result_template_path: Path = DEFAULT_RESULT_TEMPLATE,
):
    """编排十连视觉演出：GIF 播放等待期间并行渲染结果图，发送结果图成功后撤回 GIF；异常逐级降级。"""
    template_source = await asyncio.to_thread(
        result_template_path.read_text, encoding="utf-8"
    )
    user = str(event.get_sender_id() or "")
    safe_owner = clean_name(owner_name)
    text_fallback = format_ten_pull_text(service, record, tz)

    async def _render_result_image() -> str | None:
        try:
            data = await asyncio.to_thread(
                build_result_image_payload, service, record, owner_name, tz, resolver
            )
            fmt = result_format if result_format in {"webp", "jpeg"} else "webp"
            options = {
                "prefix": "gacha-result",
                "output_format": fmt,
                "target_bytes": 150_000,
                "soft_cap_bytes": 250_000,
            }
            path = await asyncio.wait_for(
                calendar_renderer.render(template_source, data, options),
                timeout=RESULT_RENDER_TIMEOUT,
            )
            if isinstance(path, str) and path.strip():
                return path
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[妮姬招募] 十连结果图生成失败：%s", type(exc).__name__)
        return None

    is_aiocqhttp = getattr(event, "get_platform_name", lambda: "")() == "aiocqhttp"
    bot = getattr(event, "bot", None)
    group_id = str(event.get_group_id() or "") if hasattr(event, "get_group_id") else ""
    sender_id = str(event.get_sender_id() or "") if hasattr(event, "get_sender_id") else ""
    can_onebot_orchestrate = bool(
        not fast_mode
        and gif_enabled
        and gif_renderer is not None
        and is_aiocqhttp
        and bot is not None
        and hasattr(bot, "delete_msg")
        and ((group_id and group_id.isdigit()) or (sender_id and sender_id.isdigit()))
    )

    # 快速模式或非 OneBot 适配器：跳过 GIF，直接渲染静态结果图（失败退回纯文本）。
    if not can_onebot_orchestrate:
        image_path = await _render_result_image()
        if image_path is not None:
            reply = event.make_result().use_t2i(False).at(safe_owner, user).message("\n")
            return reply.file_image(image_path)
        return event.make_result().use_t2i(False).at(safe_owner, user).message("\n" + text_fallback)

    raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
    self_id = raw_event.get("self_id") if hasattr(raw_event, "get") else None
    routing = {"self_id": self_id} if self_id else {}

    # 1. 并行启动静态结算图渲染，不等待 GIF 播完再开始。
    result_task = asyncio.create_task(_render_result_image())

    gif_msg_id: int | None = None
    gif_duration = 0.0
    gif_sent_at = 0.0
    rarities = tuple(slot.rarity for slot in record.slots)

    # 2. 合成并发送单一完整 GIF（Part A + Part B）。
    try:
        gif_bytes, gif_duration = await asyncio.wait_for(
            asyncio.to_thread(gif_renderer.render_bytes, rarities),
            timeout=GIF_RENDER_TIMEOUT,
        )
        gif_segments = [
            {
                "type": "image",
                "data": {"file": "base64://" + base64.b64encode(gif_bytes).decode("ascii")},
            }
        ]
        gif_msg_id = await _send_onebot_segments(bot, group_id, sender_id, routing, gif_segments)
        gif_sent_at = time.monotonic()
    except asyncio.CancelledError:
        result_task.cancel()
        raise
    except Exception as exc:
        logger.warning("[妮姬招募] 招募动画合成或发送失败，直接发送结果：%s", type(exc).__name__)
        gif_msg_id = None

    # 3. GIF 发送成功后，按实际编码时长 + 缓冲等待，再等待并取得静态结果图。
    if gif_msg_id is not None:
        wait_budget = min(MAX_PLAY_WAIT_SECONDS, max(0.0, gif_duration + max(0.0, gif_buffer_seconds)))
        elapsed = time.monotonic() - gif_sent_at
        remaining_wait = wait_budget - elapsed
        if remaining_wait > 0:
            await asyncio.sleep(remaining_wait)

    image_path = await result_task

    # 4. 构造结果消息并发送；确认结果发送成功后再撤回本次 GIF。
    result_sent = False
    if image_path is not None:
        try:
            img_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
            result_segments = [
                {"type": "at", "data": {"qq": sender_id}},
                {"type": "text", "data": {"text": "\n"}},
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(img_bytes).decode("ascii")},
                },
            ]
            await _send_onebot_segments(bot, group_id, sender_id, routing, result_segments)
            result_sent = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[妮姬招募] 十连结果图 OneBot 发送失败：%s", type(exc).__name__)
    else:
        try:
            text_segments = [
                {"type": "at", "data": {"qq": sender_id}},
                {"type": "text", "data": {"text": "\n" + text_fallback}},
            ]
            await _send_onebot_segments(bot, group_id, sender_id, routing, text_segments)
            result_sent = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[妮姬招募] 十连纯文本结果 OneBot 发送失败：%s", type(exc).__name__)

    if result_sent and gif_msg_id is not None:
        try:
            await asyncio.wait_for(
                bot.delete_msg(message_id=gif_msg_id, **routing),
                timeout=ONEBOT_RECALL_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[妮姬招募] 招募动画撤回失败：%s", type(exc).__name__)

    if result_sent:
        return None
    # OneBot 主动发送结果失败时，保留已发 GIF，并通过标准事件返回值再试一次文本提示（含编号）。
    return event.make_result().use_t2i(False).at(safe_owner, user).message(
        "\n" + text_fallback
    )
