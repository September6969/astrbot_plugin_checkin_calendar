"""绘制打卡结果轮盘 GIF。"""

import math
import random
import time
import uuid
from hashlib import sha256
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .presentation import format_probability

ASSETS = Path(__file__).parent / "assets"
ARCANA_ASSET = ASSETS / "arcana-roulette.png"

FONT_CANDIDATES = (
    ASSETS / "roulette-sans.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

BG_TOP = (18, 24, 36)
BG_BOTTOM = (25, 34, 52)
CARD_BORDER = (56, 72, 98)
PANEL_BG = (22, 30, 46)
SAFE_SECTOR = (29, 104, 92)
BLAST_CRIMSON = (239, 58, 86)
GOLD = (245, 196, 81)
GOLD_DIM = (176, 138, 54)
EMERALD = (52, 211, 153)
WHITE = (244, 247, 254)
MUTED = (158, 174, 198)
DARK_INK = (15, 23, 42)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _has_cjk_font() -> bool:
    return any(Path(candidate).is_file() for candidate in FONT_CANDIDATES)


POINTER_ANGLE = 270.0
ROULETTE_MIN_VIEW_SECONDS = 4.5
FINAL_HOLD_MS = 2220


def _landing_fraction(animation_key: str) -> float:
    if not animation_key:
        return 0.5
    # 由消息标识取得可复现的落点，离扇区两边各保留 12%。
    bits = int.from_bytes(sha256(animation_key.encode("utf-8")).digest()[:4], "big")
    return 0.12 + 0.76 * (bits / (1 << 32))


def _wheel_geometry(risk_percent: float, blasted: bool, landing_fraction: float = 0.5) -> tuple[float, float]:
    """返回炸膛扇区的圆心角及符合已确定结果的最终转角。"""
    blast_arc = risk_percent * 360.0 / 100.0
    start, width = (0.0, blast_arc) if blasted else (blast_arc, 360.0 - blast_arc)
    target = start + width * landing_fraction
    return blast_arc, 3 * 360.0 + (POINTER_ANGLE - target) % 360.0


def _draw_sectors(radius: int, rotation: float, blast_arc: float) -> Image.Image:
    diameter = radius * 2 + 1
    image = Image.new("RGBA", (diameter, diameter))
    draw = ImageDraw.Draw(image)
    bounds = (0, 0, diameter - 1, diameter - 1)
    if blast_arc >= 360:
        draw.ellipse(bounds, fill=BLAST_CRIMSON)
    else:
        draw.ellipse(bounds, fill=SAFE_SECTOR)
        if blast_arc > 0:
            draw.pieslice(bounds, rotation, rotation + blast_arc, fill=BLAST_CRIMSON)
    return image


def _build_backdrop(cw: int, ch: int) -> Image.Image:
    """绘制卡片底图、柔光和阿尔卡娜贴图。"""
    img = Image.new("RGBA", (cw, ch), BG_TOP + (255,))
    draw = ImageDraw.Draw(img)

    # 垂直渐变
    for y in range(ch):
        t = y / max(1, ch - 1)
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line((0, y, cw, y), fill=(r, g, b, 255))

    # 人物背后的青蓝柔光 & 轮盘背后的暗金柔光
    glow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow)
    g_draw.ellipse((int(-cw * 0.05), int(ch * 0.08), int(cw * 0.45), int(ch * 0.92)), fill=(56, 189, 248, 42))
    g_draw.ellipse((int(cw * 0.44), int(ch * 0.12), int(cw * 0.94), int(ch * 0.88)), fill=(245, 196, 81, 24))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(12, cw // 18)))
    img = Image.alpha_composite(img, glow)

    # 极细半透明战术网格线装饰
    grid = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    grid_draw = ImageDraw.Draw(grid)
    step = max(36, cw // 16)
    for x in range(step, cw, step):
        grid_draw.line((x, 0, x, ch), fill=(255, 255, 255, 16), width=1)
    for y in range(step, ch, step):
        grid_draw.line((0, y, cw, y), fill=(255, 255, 255, 16), width=1)
    img = Image.alpha_composite(img, grid)

    # 完整保留用户提供的 PNG 画布和宽高比。
    if ARCANA_ASSET.is_file():
        with Image.open(ARCANA_ASSET) as src:
            portrait = src.convert("RGBA")
            portrait.thumbnail((int(cw * 0.57), int(ch * 0.72)), Image.Resampling.LANCZOS)
            px = round(cw * 0.225 - portrait.width / 2)
            py = round(ch * 0.94 - portrait.height)
            img.alpha_composite(portrait, (px, py))

    return img


def _build_wheel_lighting(diameter: int) -> Image.Image:
    mask = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mask)
    for i in range(14):
        alpha = int(85 * ((14 - i) / 14) ** 2)
        draw.ellipse((i, i, diameter - 1 - i, diameter - 1 - i), outline=(8, 12, 22, alpha), width=2)
    return mask


def _draw_explosion_overlay(img: Image.Image, progress: float, cw: int, ch: int, cx: int, cy: int) -> Image.Image:
    """炸膛全屏爆炸特效（冲击波、火球星芒、火星与烟雾暗角）。"""
    ease = 1.0 - (1.0 - progress) ** 2

    # 1. 全屏烟雾暗角（独立层叠加，不覆盖后续元素）
    smoke = Image.new("RGBA", (cw, ch), (24, 12, 14, int(165 * min(1.0, progress * 1.35))))
    img = Image.alpha_composite(img, smoke)

    # 2. 扩张冲击波 + 爆炸火球星芒 + 飞溅火星
    layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    max_r = int(math.hypot(cw, ch) * 0.95)
    for delay, col, w in ((0.0, (255, 220, 120), 10), (0.14, (255, 100, 40), 7)):
        p = max(0.0, min(1.0, (progress - delay) / max(0.01, 1.0 - delay)))
        if 0.0 < p < 1.0:
            rr = int(max_r * (1.0 - (1.0 - p) ** 2))
            al = int(225 * (1.0 - p))
            draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=col + (al,), width=w)

    burst_scale = 0.30 + 1.25 * ease
    fade = max(0.0, 1.0 - max(0.0, progress - 0.35) / 0.65)
    if fade > 0.02:
        spikes = 16
        for r_mul, color in (
            (1.0, (235, 64, 36, int(235 * fade))),
            (0.74, (255, 158, 38, int(245 * fade))),
            (0.46, (255, 242, 190, int(250 * fade))),
        ):
            pts = []
            base_len = int(ch * 0.72 * burst_scale * r_mul)
            for i in range(spikes * 2):
                ang = i * math.pi / spikes + (0.15 if r_mul < 0.8 else 0.0)
                mod = 1.0 if i % 2 == 0 else (0.42 + 0.14 * math.sin(i * 2.3))
                dist = base_len * mod
                pts.append((cx + math.cos(ang) * dist, cy + math.sin(ang) * dist))
            draw.polygon(pts, fill=color)

    rng = random.Random(42)
    for i in range(28):
        ang = rng.uniform(0, math.tau)
        speed = rng.uniform(0.35, 1.30)
        dist = int(ch * 0.90 * ease * speed)
        px = int(cx + math.cos(ang) * dist)
        py = int(cy + math.sin(ang) * dist + (progress ** 2) * ch * 0.12)
        pr = max(3, int(cw * rng.uniform(0.007, 0.018) * (1.0 - progress * 0.45)))
        al = int(245 * (1.0 - progress * 0.65))
        col = (255, 210, 90, al) if i % 2 == 0 else (255, 95, 50, al)
        draw.ellipse((px - pr, py - pr, px + pr, py + pr), fill=col)

    img = Image.alpha_composite(img, layer)

    # 3. 初爆白橙强闪光（独立层叠加）
    if progress < 0.42:
        flash_al = int(175 * (1.0 - progress / 0.42) ** 1.6)
        flash = Image.new("RGBA", (cw, ch), (255, 236, 196, flash_al))
        img = Image.alpha_composite(img, flash)

    return img


def _draw_liquid_overlay(img: Image.Image, progress: float, cw: int, ch: int) -> Image.Image:
    """正常通过全屏白色液体泼溅与流淌覆盖特效。"""
    layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    ease = 1.0 - (1.0 - progress) ** 2.2

    # 1. 顶部向下流淌的粘稠白液帘（带 7 条波浪垂滴）
    drop_base_y = int(ch * (0.12 + 0.96 * ease))
    top_pts = [(0, 0), (cw, 0)]
    steps = 48
    for i in range(steps, -1, -1):
        x = int(cw * i / steps)
        t = i / steps
        wave = (
            math.sin(t * math.pi * 7.0 + 0.4) * ch * 0.11
            + math.cos(t * math.pi * 3.0) * ch * 0.06
        ) * min(1.0, (1.0 - progress * 0.35))
        y = max(0, min(ch + 40, int(drop_base_y + wave)))
        top_pts.append((x, y))
    # 浅蓝灰投影边缘 + 纯白高光主体
    shadow_pts = [(x, min(ch + 40, y + 8)) for x, y in top_pts]
    draw.polygon(shadow_pts, fill=(214, 226, 242, 220))
    draw.polygon(top_pts, fill=(248, 251, 255, 242))

    # 2. 底部向上汇聚的液面
    pool_y = int(ch * (1.02 - 0.34 * ease))
    pool_pts = [(0, ch), (cw, ch)]
    for i in range(steps, -1, -1):
        x = int(cw * i / steps)
        t = i / steps
        wave = math.sin(t * math.pi * 5.0 + progress * 3.0) * ch * 0.035
        pool_pts.append((x, max(0, min(ch, int(pool_y + wave)))))
    draw.polygon(pool_pts, fill=(244, 248, 255, 235))

    # 3. 飞溅水滴与光泽高光
    rng = random.Random(77)
    for i in range(18):
        sx = int(cw * rng.uniform(0.08, 0.92))
        sy = int(ch * (rng.uniform(0.15, 0.85) * ease + rng.uniform(0.0, 0.2)))
        sr = max(4, int(cw * rng.uniform(0.012, 0.032)))
        draw.ellipse((sx - sr, sy - sr, sx + sr, sy + int(sr * 1.3)), fill=(252, 254, 255, 235))
        hl_r = max(2, sr // 3)
        draw.ellipse((sx - sr // 2, sy - sr // 2, sx - sr // 2 + hl_r, sy - sr // 2 + hl_r), fill=(255, 255, 255, 255))

    return Image.alpha_composite(img, layer)


class RouletteRenderer:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)

    def _cleanup_old(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 86400
        for path in self.output_dir.glob("roulette-*"):
            if path.suffix not in {".gif", ".tmp"}:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def render(
        self, risk_percent: float, blasted: bool, *, size: int = 400, animation_key: str = "",
    ) -> str:
        """同步生成正方形转盘 GIF，返回文件路径。"""
        self._cleanup_old()
        # 消息标识只选择扇区内的演出落点，缓存复用相同概率和落点的 GIF。
        fraction = 0.5 if risk_percent in (0, 100) else _landing_fraction(animation_key)
        cache_key = sha256(f"5:{size}:{float(risk_percent).hex()}:{blasted}:{fraction.hex()}".encode()).hexdigest()[:24]
        out_path = self.output_dir / f"roulette-{cache_key}.gif"
        if out_path.is_file():
            return str(out_path)
        cw = ch = (size * 3 + 1) // 2

        backdrop = _build_backdrop(cw, ch)

        # 右侧轮盘参数
        cx = int(cw * 0.705)
        cy = int(ch * 0.48)
        outer_r = int(ch * 0.25)
        wheel_r = int(ch * 0.223)
        hub_r = int(ch * 0.057)
        lighting = _build_wheel_lighting(wheel_r * 2 + 1)
        blast_arc, final_rotation = _wheel_geometry(risk_percent, blasted, fraction)

        cjk_ok = _has_cjk_font()
        tag_font = _load_font(int(ch * 0.033))
        risk_font = _load_font(int(ch * 0.069))
        status_font = _load_font(int(ch * 0.048))
        risk_label = format_probability(risk_percent, decimal_places=2)

        spin_frames = 26
        hold_frames = 4
        finale_frames = 9

        frames: list[Image.Image] = []
        durations: list[int] = []

        finale_end = spin_frames + hold_frames + finale_frames
        for idx in range(finale_end + 1):
            is_hold = idx >= spin_frames
            is_finale = spin_frames + hold_frames <= idx < finale_end
            progress = min(1.0, idx / (spin_frames - 1))
            rotation = final_rotation * (1 - (1 - progress) ** 3)

            img = backdrop.copy()
            draw = ImageDraw.Draw(img)

            # 外圈金属框 + 刻度点
            bezel_outline = (BLAST_CRIMSON if (idx - spin_frames) % 2 == 0 else GOLD) if (is_hold and blasted) else GOLD_DIM
            draw.ellipse((cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r), fill=PANEL_BG + (255,), outline=bezel_outline + (255,), width=4)
            tick_r = int((outer_r + wheel_r) / 2)
            for tick in range(16):
                rad = math.radians(rotation + tick * 22.5)
                tx = cx + math.cos(rad) * tick_r
                ty = cy + math.sin(rad) * tick_r
                dot_r = 3 if tick % 2 == 0 else 2
                draw.ellipse((tx - dot_r, ty - dot_r, tx + dot_r, ty + dot_r), fill=(GOLD if tick % 2 == 0 else MUTED) + (230,))

            # 炸膛区的面积比例直接来自后台概率，边界不另加描边。
            disc = Image.alpha_composite(_draw_sectors(wheel_r, rotation, blast_arc), lighting)
            img.alpha_composite(disc, (cx - wheel_r, cy - wheel_r))

            # 轮毂标记固定，概率统一放在独立信息区。
            draw = ImageDraw.Draw(img)
            draw.ellipse((cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r), fill=DARK_INK + (255,), outline=GOLD + (255,), width=3)
            marker = hub_r // 3
            draw.polygon(((cx, cy - marker), (cx + marker, cy), (cx, cy + marker), (cx - marker, cy)), fill=GOLD + (255,))

            # 结算全屏特效（炸膛爆炸 / 安全白液覆盖）
            if is_finale:
                fin_p = (idx - spin_frames - hold_frames) / max(1, finale_frames - 1)
                if blasted:
                    img = _draw_explosion_overlay(img, fin_p, cw, ch, cx, cy)
                else:
                    img = _draw_liquid_overlay(img, fin_p, cw, ch)
                draw = ImageDraw.Draw(img)

            # 指针始终固定在正上方，刻度与盘面同步旋转。
            pointer_base = cy - outer_r - int(ch * 0.016)
            pointer_tip = cy - wheel_r + int(ch * 0.016)
            pointer_half_width = int(ch * 0.019)
            draw.polygon(
                ((cx - pointer_half_width, pointer_base), (cx + pointer_half_width, pointer_base), (cx, pointer_tip)),
                fill=GOLD, outline=DARK_INK, width=2,
            )

            # 概率和状态放在结算特效上层，便于读数。
            panel_left, panel_right = int(cw * 0.45), int(cw * 0.96)
            panel_center = (panel_left + panel_right) // 2
            draw.rounded_rectangle(
                (panel_left, int(ch * 0.08), panel_right, int(ch * 0.205)),
                radius=12, fill=PANEL_BG + (255,), outline=GOLD_DIM + (255,), width=2,
            )
            draw.text((panel_center, int(ch * 0.113)), "本次炸膛概率" if cjk_ok else "BLAST CHANCE", fill=MUTED + (255,), font=tag_font, anchor="mm")
            draw.text((panel_center, int(ch * 0.166)), risk_label, fill=WHITE + (255,), font=risk_font, anchor="mm")

            bx1, by1, bx2, by2 = int(cw * 0.45), int(ch * 0.865), int(cw * 0.94), int(ch * 0.955)
            if is_hold:
                if blasted:
                    bar_fill, bar_outline = (58, 20, 32), BLAST_CRIMSON
                    bar_txt, txt_col = ("炸膛了！触发冷却" if cjk_ok else "BLASTED!"), (255, 118, 134)
                else:
                    bar_fill, bar_outline = (18, 48, 46), EMERALD
                    bar_txt, txt_col = ("安全通过！" if cjk_ok else "SAFE!"), EMERALD
            else:
                bar_fill, bar_outline = PANEL_BG, CARD_BORDER
                bar_txt, txt_col = ("正在揭晓结果" if cjk_ok else "REVEALING"), MUTED
            draw.rounded_rectangle((bx1, by1, bx2, by2), radius=12, fill=bar_fill + (245,), outline=bar_outline + (255,), width=3)
            draw.text(((bx1 + bx2) // 2, (by1 + by2) // 2), bar_txt, fill=txt_col + (255,), font=status_font, anchor="mm")

            # 外卡片描边
            card_outline = (BLAST_CRIMSON if (is_hold and blasted) else (EMERALD if is_hold else CARD_BORDER))
            draw.rectangle((0, 0, cw - 1, ch - 1), outline=card_outline + (255,), width=4)

            downsampled = img.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
            frames.append(downsampled)
            if idx == finale_end:
                durations.append(FINAL_HOLD_MS)
            else:
                durations.append(80 if is_finale else (60 if is_hold else 50))

        # 共用调色板，让未变化的像素保留相同色号，GIF 可只写入变化区域。
        sample_indices = (0, spin_frames, *(range(spin_frames + hold_frames, finale_end, 2)), finale_end)
        samples = Image.new("RGB", (96 * len(sample_indices), 96))
        for column, index in enumerate(sample_indices):
            samples.paste(frames[index].resize((96, 96), Image.Resampling.LANCZOS), (96 * column, 0))
        palette = samples.quantize(colors=256)
        frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]

        temp_path = out_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            frames[0].save(
                temp_path, format="GIF", save_all=True, append_images=frames[1:],
                duration=durations, loop=1, optimize=True,
            )
            temp_path.replace(out_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return str(out_path)
