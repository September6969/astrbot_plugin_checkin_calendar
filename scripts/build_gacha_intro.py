"""离线基于门闸参考图制作 NIKKE 公共招募往前推进场 GIF（Part A：蓝/紫/金，360x640）。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "assets" / "gacha"
GATE_REF_PATH = OUT_DIR / "gate_reference.png"
CANVAS_W = 360
CANVAS_H = 640
END_CHARCOAL = (34, 32, 30)  # 与 Part B 十小人背景 #22201E 无缝衔接

RARITY_THEMES = {
    "r": {
        "core": (160, 226, 255),
        "glow": (40, 134, 248),
        "beam": (88, 174, 255),
    },
    "sr": {
        "core": (236, 186, 255),
        "glow": (158, 62, 238),
        "beam": (196, 122, 255),
    },
    "ssr": {
        "core": (255, 244, 174),
        "glow": (244, 162, 30),
        "beam": (255, 212, 72),
    },
}


def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _prepare_gate_layers(ref_path: Path = GATE_REF_PATH):
    with Image.open(ref_path) as src:
        gate = src.convert("RGBA")
    gw, gh = gate.size
    px = gate.load()

    # 提取中缝像素与斜交分割边界 split_x[y]，用于左右门扇物理分离与中缝充能发光
    seam_mask = Image.new("L", (gw, gh), 0)
    bolt_mask = Image.new("L", (gw, gh), 0)
    sm_px = seam_mask.load()
    bm_px = bolt_mask.load()

    split_x: list[int] = []
    for y in range(gh):
        dark_xs = [
            x
            for x in range(44, 116)
            if px[x, y][0] < 24 and px[x, y][1] < 24 and px[x, y][2] < 24
        ]
        if dark_xs:
            cx_row = round(sum(dark_xs) / len(dark_xs))
        else:
            # 中心斜段按线性过渡插值
            if y < 128:
                cx_row = 64 + round((y - 96) * 0.5) if y >= 96 else 80
            else:
                cx_row = 80 + round((y - 128) * 0.5) if y <= 160 else 80
        split_x.append(cx_row)

        for x in range(gw):
            r, g, b, _ = px[x, y]
            if r < 26 and g < 26 and b < 26 and abs(x - cx_row) <= 22:
                sm_px[x, y] = 255
            elif r > 180 and 100 <= g <= 220 and b < 115:
                bm_px[x, y] = 255

    left_door = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
    right_door = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
    lp = left_door.load()
    rp = right_door.load()
    for y in range(gh):
        sx = split_x[y]
        for x in range(gw):
            if x < sx:
                lp[x, y] = px[x, y]
            else:
                rp[x, y] = px[x, y]

    return gate, left_door, right_door, seam_mask, bolt_mask


def _build_vignette(w: int, h: int) -> Image.Image:
    # 轻量级纯色边角暗框（避免平滑渐变导致像素门闸 GIF 体积膨胀）
    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(vig)
    d.rectangle((0, 0, w - 1, h - 1), outline=(10, 12, 16, 130), width=10)
    return vig


def _render_push_frame(
    layers,
    vignette: Image.Image,
    theme: dict[str, tuple[int, int, int]],
    frame_idx: int,
    total_frames: int,
) -> Image.Image:
    gate, left_door, right_door, seam_mask, bolt_mask = layers
    gw, gh = gate.size

    # 0..8：镜头向前推进（scale 2.50 -> 4.05）+ 第 4 帧起阶梯中缝按真实最高稀有度点亮
    # 9..12：左右门扇沿阶梯中缝向两侧分开，背后透出稀有度光柱
    # 13：平滑融暗至 Part B 精确底色 #22201E
    push_t = min(1.0, frame_idx / 8.0)
    ease_push = 1.0 - (1.0 - push_t) ** 1.8
    charge_t = max(0.0, min(1.0, (frame_idx - 3) / 5.0))
    open_t = max(0.0, min(1.0, (frame_idx - 8) / 4.5))
    fade_t = max(0.0, min(1.0, (frame_idx - 12) / max(1, total_frames - 1 - 12)))

    scale = 2.50 + 1.55 * ease_push + 0.30 * open_t
    sw = int(round(gw * scale))
    sh = int(round(gh * scale))

    # 1. 背景能量光（门开启时透出）
    bg_col = _lerp_color((18, 20, 26), theme["glow"], min(1.0, open_t * 1.1))
    if fade_t > 0:
        bg_col = _lerp_color(bg_col, END_CHARCOAL, fade_t)
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg_col + (255,))

    if open_t > 0 and fade_t < 1.0:
        flare = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(flare)
        beam_hw = max(14, int(CANVAS_W * (0.16 + 0.52 * open_t)))
        cx = CANVAS_W // 2
        alpha_f = int(225 * (1.0 - fade_t))
        fd.rectangle((cx - beam_hw, 0, cx + beam_hw, CANVAS_H), fill=theme["glow"] + (alpha_f,))
        fd.rectangle((cx - beam_hw // 2, 0, cx + beam_hw // 2, CANVAS_H), fill=theme["core"] + (alpha_f,))
        canvas = Image.alpha_composite(canvas, flare)

    # 2. 合拢推进期 vs 分裂开启期
    ox = (CANVAS_W - sw) // 2
    oy = (CANVAS_H - sh) // 2

    if open_t <= 0.0:
        door_layer = gate.copy()
        if charge_t > 0.0:
            # 点亮阶梯中缝与 "+" 指示灯
            glow_seam = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
            glow_seam.paste(Image.new("RGBA", (gw, gh), theme["beam"] + (int(225 * charge_t),)), (0, 0), seam_mask)
            glow_seam.paste(Image.new("RGBA", (gw, gh), theme["core"] + (int(240 * charge_t),)), (0, 0), bolt_mask)
            door_layer = Image.alpha_composite(door_layer, glow_seam)
        scaled = door_layer.resize((sw, sh), Image.Resampling.NEAREST)
        canvas.alpha_composite(scaled, (ox, oy))
    else:
        split_px = int((CANVAS_W * 0.62) * (open_t ** 1.35))
        left_scaled = left_door.resize((sw, sh), Image.Resampling.NEAREST)
        right_scaled = right_door.resize((sw, sh), Image.Resampling.NEAREST)
        canvas.alpha_composite(left_scaled, (ox - split_px, oy))
        canvas.alpha_composite(right_scaled, (ox + split_px, oy))

    canvas = Image.alpha_composite(canvas, vignette)

    if fade_t > 0.0:
        fade_overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), END_CHARCOAL + (int(255 * fade_t),))
        canvas = Image.alpha_composite(canvas, fade_overlay)

    return canvas.convert("RGB")


def build_all_intros(out_dir: Path = OUT_DIR, ref_path: Path = GATE_REF_PATH) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = _prepare_gate_layers(ref_path)
    vignette = _build_vignette(CANVAS_W, CANVAS_H)
    total_frames = 14
    durations = [95] * total_frames
    sizes: dict[str, int] = {}

    for tier, theme in RARITY_THEMES.items():
        frames = [
            _render_push_frame(layers, vignette, theme, idx, total_frames)
            for idx in range(total_frames)
        ]
        quantized = [
            f.quantize(colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            for f in frames
        ]
        dest = out_dir / f"intro_{tier}.gif"
        quantized[0].save(
            dest,
            format="GIF",
            save_all=True,
            append_images=quantized[1:],
            duration=durations,
            optimize=True,
            disposal=2,
        )
        sizes[dest.name] = dest.stat().st_size
    return sizes


if __name__ == "__main__":
    result = build_all_intros()
    for name, byte_len in result.items():
        print(f"{name}: {byte_len:,} bytes")
