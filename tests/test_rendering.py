"""复现单节点卡住与消息路径的故障切换。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer


class Strategy:
    def __init__(self, outcomes):
        self.endpoints = list(outcomes)
        self.outcomes = outcomes
        self.calls = []

    async def render_custom_template(self, template, data, return_url, options):
        endpoint, = self.endpoints
        self.calls.append((endpoint, template, data, return_url, options))
        outcome = self.outcomes[endpoint]
        if outcome == 'hang':
            await asyncio.Event().wait()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_hanging_endpoint_switches_and_success_is_preferred():
    strategy = Strategy({'slow': 'hang', 'healthy': '/tmp/calendar.png'})
    native = AsyncMock()
    renderer = CalendarRenderer(native, strategy, endpoint_timeout=0.01)
    data = {'name': '测试', 'total': 1}
    assert await asyncio.wait_for(renderer.render('template', data, {'type': 'png'}), 1) == '/tmp/calendar.png'
    assert [c[0] for c in strategy.calls] == ['slow', 'healthy']
    assert strategy.endpoints == ['slow', 'healthy']
    assert all(c[2] is data and c[3] is False for c in strategy.calls)
    strategy.calls.clear()
    assert await renderer.render('template', data, {}) == '/tmp/calendar.png'
    assert [c[0] for c in strategy.calls] == ['healthy']
    native.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError('上游异常'), '', None])
async def test_failed_or_invalid_result_switches(failure):
    strategy = Strategy({'bad': failure, 'good': '/tmp/good.png'})
    renderer = CalendarRenderer(AsyncMock(), strategy)
    assert await renderer.render('t', {}, {}) == '/tmp/good.png'
    assert [c[0] for c in strategy.calls] == ['bad', 'good']


@pytest.mark.asyncio
async def test_all_endpoints_fail_once_without_native_duplicate():
    strategy = Strategy({'a': 'hang', 'b': 'hang'})
    native = AsyncMock()
    with pytest.raises(RuntimeError, match='全部月历渲染节点失败'):
        await CalendarRenderer(native, strategy, endpoint_timeout=0.01).render('t', {}, {})
    assert [c[0] for c in strategy.calls] == ['a', 'b']
    native.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_without_switching():
    strategy = Strategy({'a': asyncio.CancelledError(), 'b': '/tmp/good.png'})
    with pytest.raises(asyncio.CancelledError):
        await CalendarRenderer(AsyncMock(), strategy).render('t', {}, {})
    assert [c[0] for c in strategy.calls] == ['a']


@pytest.mark.asyncio
async def test_missing_sdk_strategy_uses_native_with_download():
    native = AsyncMock(return_value='/tmp/native.png')
    assert await CalendarRenderer(native, object()).render('t', {'n': 1}, {'type': 'png'}) == '/tmp/native.png'
    native.assert_awaited_once_with('t', {'n': 1}, return_url=False, options={'type': 'png'})


@pytest.mark.asyncio
async def test_concurrent_requests_keep_endpoint_selection_and_data_isolated():
    strategy = Strategy({'slow': 'hang', 'good': '/tmp/good.png'})
    renderer = CalendarRenderer(AsyncMock(), strategy, endpoint_timeout=0.01)
    data = [{'name': '甲'}, {'name': '乙'}]
    result = await asyncio.gather(*(renderer.render('t', value, {}) for value in data))
    assert result == ['/tmp/good.png', '/tmp/good.png']
    assert strategy.endpoints == ['slow', 'good']
    assert sum(c[2] is data[0] for c in strategy.calls) == 2
    assert sum(c[2] is data[1] for c in strategy.calls) == 2


@pytest.mark.asyncio
async def test_successful_endpoint_survives_restart(tmp_path):
    path = tmp_path / 'render-preference.json'
    strategy = Strategy({'slow': 'hang', 'good': '/tmp/good.png'})
    renderer = CalendarRenderer(AsyncMock(), strategy, endpoint_timeout=0.01, preference_path=path)
    await renderer.render('t', {}, {})
    strategy.calls.clear()
    restarted = CalendarRenderer(AsyncMock(), strategy, preference_path=path)
    assert await restarted.render('t', {}, {}) == '/tmp/good.png'
    assert [c[0] for c in strategy.calls] == ['good']


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['bad json', '[]', '{"endpoint":"retired"}'])
async def test_invalid_or_removed_cached_endpoint_never_overrides_host(tmp_path, content):
    path = tmp_path / 'render-preference.json'
    path.write_text(content, encoding='utf-8')
    strategy = Strategy({'configured': '/tmp/good.png'})
    assert await CalendarRenderer(AsyncMock(), strategy, preference_path=path).render('t', {}, {}) == '/tmp/good.png'
    assert [c[0] for c in strategy.calls] == ['configured']


@pytest.mark.asyncio
async def test_unwritable_preference_does_not_lose_rendered_image(tmp_path):
    path = tmp_path / 'directory'
    path.mkdir()
    strategy = Strategy({'good': '/tmp/good.png'})
    assert await CalendarRenderer(AsyncMock(), strategy, preference_path=path).render('t', {}, {}) == '/tmp/good.png'
