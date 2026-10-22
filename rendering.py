"""复用宿主渲染协议，给每个节点设置独立超时。"""

import asyncio
from copy import copy

from astrbot.api import logger


class CalendarRenderer:
    def __init__(self, native_render, network_strategy, endpoint_timeout=12):
        self.native_render = native_render
        self.network_strategy = network_strategy
        self.endpoint_timeout = endpoint_timeout
        self.preferred_endpoint = None

    async def render(self, template, data, options):
        strategy = self.network_strategy
        endpoints = getattr(strategy, 'endpoints', None)
        if not isinstance(endpoints, (list, tuple)) or not endpoints or not callable(getattr(strategy, 'render_custom_template', None)):
            # 宿主内部结构变动时保留原生入口，由命令层限制总时长。
            return await self.native_render(template, data, return_url=False, options=options)
        endpoints = list(dict.fromkeys(endpoint for endpoint in endpoints if isinstance(endpoint, str) and endpoint))
        if not endpoints:
            return await self.native_render(template, data, return_url=False, options=options)
        if self.preferred_endpoint in endpoints:
            endpoints.remove(self.preferred_endpoint)
            endpoints.insert(0, self.preferred_endpoint)
        last_error = None
        for index, endpoint in enumerate(endpoints):
            # 每次请求复制策略，仅替换副本的节点列表，避免影响宿主及并发用户。
            isolated = copy(strategy)
            isolated.endpoints = [endpoint]
            try:
                timeout = self.endpoint_timeout if len(endpoints) > 1 else 30
                image = await asyncio.wait_for(
                    isolated.render_custom_template(template, data, return_url=False, options=options), timeout,
                )
                if not isinstance(image, str) or not image.strip():
                    raise ValueError('渲染器未返回图片')
                self.preferred_endpoint = endpoint
                return image
            except Exception as exc:
                # 不记录昵称、模板或响应正文；取消继续向上传播。
                last_error = exc
                logger.warning('[打卡月历] 渲染节点 %s/%s 失败：%s', index + 1, len(endpoints), type(exc).__name__)
        raise RuntimeError('全部月历渲染节点失败') from last_error
