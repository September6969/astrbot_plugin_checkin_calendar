"""在本机 Chromium 渲染月历，不调用公共图片服务。"""

import asyncio
import time
import uuid
from pathlib import Path

from jinja2 import Environment
from playwright.async_api import async_playwright
from astrbot.api import logger


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
        for path in self.output_dir.glob('calendar-*.png'):
            try:
                if path.stat().st_mtime < time.time() - 86400:
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
            path = self.output_dir / f'calendar-{uuid.uuid4().hex}.png'
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
                stage = '截图'
                await page.screenshot(path=str(path), type='png', full_page=True, animations='disabled')
                await context.close()
                context = None
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
