"""离线预制 NIKKE 公共招募机械演出 GIF（Part A：蓝/紫/金三版本，360x640）。"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "assets" / "gacha"
CANVAS_W = 360
CANVAS_H = 640
END_CHARCOAL = (34, 32, 30)  # 与 Part B 十小人背景 #22201E 无缝衔接

RARITY_THEMES = {
    "r": {
        "core": (130, 215, 255),
        "glow": (38, 132, 245),
        "beam": (96, 178, 255),
    },
    "sr": {
        "core": (232, 180, 255),
        "glow": (158, 62, 238),
        "beam": (196, 122, 255),
    },
    "ssr": {
        "core": (255, 244, 172),
        "glow": (245, 166, 28),
        "beam": (255, 214, 72),
    },
}


def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _draw_corridor_frame(
    theme: dict[str, tuple[int, int, int]],
    frame_idx: int,
    total_frames: int,
) -> Image.Image:
    cx, cy = CANVAS_W // 2, int(CANVAS_H * 0.46)
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (16, 19, 25, 255))
    draw = ImageDraw.Draw(img)

    # 阶段划分为：0..8 镜头推进；9..16 门锁合拢与能量充能；17..23 闸门开启并渐变为 Part B 底色
    zoom_t = min(1.0, frame_idx / 8.0)
    charge_t = max(0.0, min(1.0, (frame_idx - 8) / 8.0))
    open_t = max(0.0, min(1.0, (frame_idx - 16) / max(1, total_frames - 1 - 16)))

    core_col = theme["core"]
    glow_col = theme["glow"]
    beam_col = theme["beam"]

    # 1. 背景机械通道纵深肋骨（随镜头推进放大）
    for ring in range(6):
        depth = (ring / 5.0 + zoom_t * 0.28) % 1.2
        scale = 0.25 + depth * 0.95
        hw = int(CANVAS_W * 0.56 * scale)
        hh = int(CANVAS_H * 0.52 * scale)
        shade = int(24 + 28 * depth)
        rib_col = (shade, shade + 4, shade + 10, 255)
        draw.rectangle(
            (cx - hw, cy - hh, cx + hw, cy + hh),
            outline=rib_col,
            width=max(2, int(4 * scale)),
        )
        # 通道导轨斜线
        if ring == 5:
            for sx in (-1, 1):
                for sy in (-1, 1):
                    draw.line(
                        (cx + sx * 40, cy + sy * 50, cx + sx * 220, cy + sy * 340),
                        fill=(40, 46, 58, 255),
                        width=2,
                    )

    # 2. 左右装甲闸门（open_t > 0 时向两侧滑开，露出背后光束与最终底色）
    split = int((CANVAS_W * 0.58) * (open_t ** 1.6))
    door_fill = (32, 36, 44, 255)
    door_trim = (58, 64, 76, 255)

    # 门后透出的能量光晕（开启阶段）
    if open_t > 0:
        bg_mix = _lerp_color(glow_col, END_CHARCOAL, max(0.0, (open_t - 0.35) / 0.65))
        draw.rectangle((0, 0, CANVAS_W, CANVAS_H), fill=bg_mix + (255,))
        flare_w = max(8, int(CANVAS_W * (0.18 + open_t * 0.95)))
        if open_t < 0.85:
            flare_alpha = int(235 * (1.0 - max(0.0, (open_t - 0.3) / 0.55)))
            flare = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
            fd = ImageDraw.Draw(flare)
            fd.rectangle((cx - flare_w // 2, 0, cx + flare_w // 2, CANVAS_H), fill=beam_col + (flare_alpha,))
            fd.ellipse((cx - flare_w, cy - flare_w, cx + flare_w, cy + flare_w), fill=core_col + (flare_alpha,))
            flare = flare.filter(ImageFilter.GaussianBlur(radius=18))
            img = Image.alpha_composite(img, flare)
            draw = ImageDraw.Draw(img)

    # 左右装甲门扇
    left_x2 = cx - split
    right_x1 = cx + split
    if left_x2 > 0:
        draw.rectangle((0, 0, left_x2, CANVAS_H), fill=door_fill)
        draw.line((left_x2 - 3, 0, left_x2 - 3, CANVAS_H), fill=door_trim, width=4)
        for y in range(80, CANVAS_H, 90):
            draw.rectangle((16, y, max(20, left_x2 - 18), y + 18), outline=(46, 52, 64, 255), width=2)
    if right_x1 < CANVAS_W:
        draw.rectangle((right_x1, 0, CANVAS_W, CANVAS_H), fill=door_fill)
        draw.line((right_x1 + 2, 0, right_x1 + 2, CANVAS_H), fill=door_trim, width=4)
        for y in range(80, CANVAS_H, 90):
            draw.rectangle((min(CANVAS_W - 20, right_x1 + 18), y, CANVAS_W - 16, y + 18), outline=(46, 52, 64, 255), width=2)

    # 3. 门缝垂直光柱与门锁核心（在闸门滑开前逐步点亮）
    if open_t < 0.75:
        seam_alpha = int((60 + 195 * charge_t) * (1.0 - open_t / 0.75))
        glow_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)

        # 中缝能量束
        if charge_t > 0:
            beam_hw = int(3 + 12 * charge_t)
            gd.rectangle((cx - beam_hw, 40, cx + beam_hw, CANVAS_H - 40), fill=glow_col + (seam_alpha,))
            gd.rectangle((cx - max(2, beam_hw // 3), 60, cx + max(2, beam_hw // 3), CANVAS_H - 60), fill=core_col + (seam_alpha,))

        # 门锁外环与夹紧锁扣
        lock_r = int(58 + 14 * zoom_t)
        clamp_gap = int(26 * (1.0 - min(1.0, charge_t * 1.4)))
        ring_col = _lerp_color((88, 96, 112), beam_col, charge_t) + (int(240 * (1.0 - open_t / 0.75)),)
        gd.ellipse((cx - lock_r, cy - lock_r, cx + lock_r, cy + lock_r), outline=ring_col, width=6)

        # 上下左右四向机械锁舌
        for ang_deg in (0, 90, 180, 270):
            rad = math.radians(ang_deg)
            dist = lock_r + 12 + clamp_gap
            bx = int(cx + math.cos(rad) * dist)
            by = int(cy + math.sin(rad) * dist)
            gd.rectangle((bx - 10, by - 10, bx + 10, by + 10), fill=ring_col)

        # 中央八角核心
        inner_r = int(24 + 8 * charge_t)
        core_fill = _lerp_color((48, 54, 66), core_col, charge_t) + (int(255 * (1.0 - open_t / 0.75)),)
        pts = [
            (
                cx + math.cos(math.radians(i * 45 + 22.5)) * inner_r,
                cy + math.sin(math.radians(i * 45 + 22.5)) * inner_r,
            )
            for i in range(8)
        ]
        gd.polygon(pts, fill=core_fill, outline=beam_col + (seam_alpha,))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=1.5))
        img = Image.alpha_composite(img, glow_layer)

    # 4. 末两帧平滑淡入 Part B 精确底色 #22201E，确保衔接零跳变
    if frame_idx >= total_frames - 2:
        fade_t = 0.55 if frame_idx == total_frames - 2 else 1.0
        fade_overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), END_CHARCOAL + (int(255 * fade_t),))
        img = Image.alpha_composite(img, fade_overlay)

    return img.convert("RGB")


def build_all_intros(out_dir: Path = OUT_DIR) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    total_frames = 24
    durations = [90] * (total_frames - 1) + [100]
    sizes: dict[str, int] = {}

    for tier, theme in RARITY_THEMES.items():
        frames = [_draw_corridor_frame(theme, idx, total_frames) for idx in range(total_frames)]
        quantized = [
            f.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            for f in frames
        ]
        dest = out_dir / f"intro_{tier}.gif"
        quantized[0].save(
            dest,
            format="GIF",
            save_all=True,
            append_images=quantized[1:],
            duration=durations,
            loop=1,
            optimize=True,
            disposal=2,
        )
        sizes[dest.name] = dest.stat().st_size
    return sizes


if __name__ == "__main__":
    result = build_all_intros()
    for name, byte_len in result.items():
        print(f"{name}: {byte_len:,} bytes")
