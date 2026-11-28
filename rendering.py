"""在本机 Chromium 渲染月历，不调用公共图片服务。"""

import asyncio
import time
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageColor
from jinja2 import Environment
from playwright.async_api import async_playwright
from astrbot.api import logger


def _encode_static_image(
    png_bytes: bytes,
    *,
    output_format: str,
    quality_steps: tuple[int, ...] = (85, 80, 75),
    target_bytes: int = 150_000,
    soft_cap_bytes: int = 250_000,
    fallback_width: int = 640,
) -> bytes:
    """从无损 PNG 源单次编码为不透明 RGB WebP/JPEG，按目标体积自适应降档而不二次有损叠加。"""
    fmt = "JPEG" if output_format.lower() in {"jpg", "jpeg"} else "WEBP"
    with Image.open(BytesIO(png_bytes)) as src:
        rgb = src.convert("RGB")

    def _try_encode(img: Image.Image, quality: int) -> bytes:
        buf = BytesIO()
        if fmt == "WEBP":
            img.save(buf, format="WEBP", quality=quality, method=6)
        else:
            img.save(buf, format="JPEG", quality=quality, subsampling=0, optimize=True)
        return buf.getvalue()

    best: bytes | None = None
    for q in quality_steps:
        candidate = _try_encode(rgb, q)
        if best is None or len(candidate) < len(best):
            best = candidate
        if len(candidate) <= target_bytes:
            return candidate

    if best is not None and len(best) <= soft_cap_bytes:
        return best

    if rgb.width > fallback_width:
        ratio = fallback_width / rgb.width
        resized = rgb.resize(
            (fallback_width, max(1, round(rgb.height * ratio))),
            Image.Resampling.LANCZOS,
        )
        for q in (80, 75):
            candidate = _try_encode(resized, q)
            if best is None or len(candidate) < len(best):
                best = candidate
            if len(candidate) <= target_bytes:
                return candidate

    return best if best is not None else png_bytes


def _compact_calendar_png(original: bytes, css_colors: list[str]) -> bytes:
    """压缩月历 PNG，调色板保留原本的日期色阶、文字色和底色。"""
    try:
        with Image.open(BytesIO(original)) as source:
            if source.mode != 'RGB':
                return original
            fixed = list(dict.fromkeys(ImageColor.getrgb(color) for color in css_colors))
            adaptive = 256 - len(fixed)
            palette = source.quantize(colors=adaptive)
            entries = palette.getpalette()[:adaptive * 3]
            fixed_start = len(entries) // 3
            entries.extend(component for color in fixed for component in color)
            palette.putpalette(entries)
            indexed = source.quantize(palette=palette, dither=Image.Dither.NONE)
            # Pillow 的邻近查色按颜色桶近似。将原本就是关键色的像素显式映射到对应色号。
            red, green, blue = source.split()
            for index, (r, g, b) in enumerate(fixed, start=fixed_start):
                matching_rg = ImageChops.multiply(red.point(lambda value: 255 * (value == r)),
                                                 green.point(lambda value: 255 * (value == g)))
                matching = ImageChops.multiply(matching_rg, blue.point(lambda value: 255 * (value == b)))
                indexed.paste(index, mask=matching)
            with BytesIO() as output:
                indexed.save(output, format='PNG', optimize=True)
                compressed = output.getvalue()
    except (OSError, ValueError):
        return original
    return compressed if len(compressed) < len(original) else original


class CalendarRenderer:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._closed = False
        self._template_source = None
        self._template = None

    async def _start(self):
        if self._browser and self._browser.is_connected():
            return
        await self._stop()
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True, args=['--disable-dev-shm-usage'],
            )
        except BaseException:
            await self._stop()
            raise

    async def _stop(self):
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = None
        try:
            if browser:
                await browser.close()
        finally:
            if playwright:
                await playwright.stop()

    def _prepare(self, template, data):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 只清理本渲染器一天前的临时图，给消息上传留出时间。
        cutoff = time.time() - 86400
        for pattern in ('calendar-*.png', 'gacha-result-*', 'gacha-card-*'):
            for path in self.output_dir.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass
        if template != self._template_source:
            self._template = Environment(autoescape=True).from_string(template)
            self._template_source = template
        return self._template.render(**data)

    async def render(self, template, data, options):
        # 串行限制浏览器内存；每次新建隔离上下文，不能串用用户页面。
        async with self._lock:
            if self._closed:
                raise RuntimeError('月历渲染器已关闭')
            html = await asyncio.to_thread(self._prepare, template, data)
            output_format = str(options.get('output_format', 'png')).lower()
            prefix = str(options.get('prefix', 'calendar'))
            ext = 'jpg' if output_format in {'jpg', 'jpeg'} else ('webp' if output_format == 'webp' else 'png')
            path = self.output_dir / f'{prefix}-{uuid.uuid4().hex}.{ext}'
            context = None
            started = time.monotonic()
            stage = '启动浏览器'
            try:
                await self._start()
                stage = '创建页面'
                context = await self._browser.new_context(
                    viewport={'width': 720, 'height': 1}, device_scale_factor=1,
                    service_workers='block', offline=True,
                )
                # 模板资源全部内嵌，禁止意外外链请求。
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                page.set_default_timeout(15000)
                stage = '加载模板和资源'
                await page.set_content(html, wait_until='load')
                await page.evaluate('document.fonts.ready')
                await page.evaluate('''async () => {
                    await Promise.all([...document.images].map(img => img.decode()));
                }''')
                compact_png = options.get('compact_png', False)
                use_lossy = output_format in {'webp', 'jpg', 'jpeg'}
                colors = []
                if compact_png and not use_lossy:
                    colors = await page.evaluate("""() =>
                        [...document.querySelectorAll('body, .status, .legend .swatch')].flatMap(element => {
                            const style = getComputedStyle(element);
                            return [style.color, style.backgroundColor];
                        })
                    """)
                stage = '截图'
                png = await page.screenshot(
                    path=None if (compact_png or use_lossy) else str(path), type='png', full_page=True, animations='disabled',
                )
                await context.close()
                context = None
                if use_lossy:
                    stage = '编码静态结果图'
                    encoded = await asyncio.to_thread(
                        _encode_static_image,
                        png,
                        output_format=output_format,
                        target_bytes=int(options.get('target_bytes', 150_000)),
                        soft_cap_bytes=int(options.get('soft_cap_bytes', 250_000)),
                    )
                    path.write_bytes(encoded)
                elif compact_png:
                    stage = '压缩月历图片'
                    png = await asyncio.to_thread(_compact_calendar_png, png, colors)
                    path.write_bytes(png)
                logger.info('[打卡月历] 本地渲染成功，耗时 %.2f 秒', time.monotonic() - started)
                return str(path)
            except BaseException as exc:
                logger.warning('[打卡月历] 本地渲染在%s失败：%s，耗时 %.2f 秒',
                               stage, type(exc).__name__, time.monotonic() - started)
                path.unlink(missing_ok=True)
                # 崩溃或取消后丢弃浏览器，下次只重试渲染，绝不重新写入记录。
                await self._stop()
                raise
            finally:
                if context and self._browser:
                    await context.close()

    async def close(self):
        async with self._lock:
            self._closed = True
            await self._stop()
