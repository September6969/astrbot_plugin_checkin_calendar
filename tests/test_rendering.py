"""本地浏览器的隔离、取消、故障恢复和生命周期回归。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    page = SimpleNamespace(set_default_timeout=Mock(), set_content=AsyncMock(), evaluate=AsyncMock(), screenshot=AsyncMock())
    context = SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(is_connected=Mock(return_value=True), new_context=AsyncMock(return_value=context), close=AsyncMock())
    driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    factory = SimpleNamespace(start=AsyncMock(return_value=driver))
    monkeypatch.setattr('astrbot_plugin_checkin_calendar.rendering.async_playwright', lambda: factory)
    renderer = CalendarRenderer(tmp_path)
    return renderer, page, context, browser, driver, factory


@pytest.mark.asyncio
async def test_reuses_browser_but_creates_isolated_contexts(runtime):
    renderer, page, context, browser, driver, factory = runtime
    first = await renderer.render('<b>{{name}}</b>', {'name': '<script>x</script>'}, {})
    second = await renderer.render('<b>{{name}}</b>', {'name': '乙'}, {})
    assert first != second
    assert factory.start.await_count == driver.chromium.launch.await_count == 1
    assert browser.new_context.await_count == context.close.await_count == 2
    assert '&lt;script&gt;' in page.set_content.call_args_list[0].args[0]
    assert '乙' in page.set_content.call_args_list[1].args[0]
    assert browser.new_context.call_args.kwargs['offline'] is True
    route = SimpleNamespace(abort=AsyncMock())
    await context.route.call_args.args[1](route)
    route.abort.assert_awaited_once()
    await renderer.close()
    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError('浏览器崩溃'), asyncio.CancelledError()])
async def test_failure_or_cancel_closes_browser_and_next_request_recovers(runtime, failure):
    renderer, page, context, browser, driver, factory = runtime
    page.screenshot.side_effect = failure
    with pytest.raises(type(failure)):
        await renderer.render('{{name}}', {'name':'甲'}, {})
    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    assert not list(renderer.output_dir.glob('*.png'))
    page.screenshot.side_effect = None
    await renderer.render('{{name}}', {'name':'乙'}, {})
    assert factory.start.await_count == 2
    await renderer.close()


@pytest.mark.asyncio
async def test_launch_failure_stops_driver(runtime):
    renderer, page, context, browser, driver, factory = runtime
    driver.chromium.launch.side_effect = RuntimeError('未安装浏览器')
    with pytest.raises(RuntimeError):
        await renderer.render('t', {}, {})
    driver.stop.assert_awaited_once()
    context.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_requests_are_serial_and_waiter_cancel_does_not_close_active(runtime):
    renderer, page, context, browser, driver, factory = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    async def screenshot(**kwargs):
        entered.set()
        await release.wait()
    page.screenshot.side_effect = screenshot
    first = asyncio.create_task(renderer.render('{{name}}', {'name':'甲'}, {}))
    await entered.wait()
    second = asyncio.create_task(renderer.render('{{name}}', {'name':'乙'}, {}))
    await asyncio.sleep(0)
    assert browser.new_context.await_count == 1
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    browser.close.assert_not_awaited()
    release.set()
    await first
    await renderer.close()


@pytest.mark.asyncio
async def test_disconnected_browser_restarts(runtime):
    renderer, page, context, browser, driver, factory = runtime
    await renderer.render('t', {}, {})
    browser.is_connected.return_value = False
    await renderer.render('t', {}, {})
    assert factory.start.await_count == 2
    await renderer.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_rejects_new_renders(runtime):
    renderer, page, context, browser, driver, factory = runtime
    await renderer.render('t', {}, {})
    await renderer.close()
    await renderer.close()
    with pytest.raises(RuntimeError, match='已关闭'):
        await renderer.render('t', {}, {})
    browser.close.assert_awaited_once()


def test_cleanup_only_old_owned_images(tmp_path):
    import os, time
    old = tmp_path / 'calendar-old.png'
    recent = tmp_path / 'calendar-recent.png'
    other = tmp_path / 'other.png'
    for path in (old, recent, other):
        path.write_bytes(b'test')
    for path in (old, other):
        os.utime(path, (time.time()-90000, time.time()-90000))
    CalendarRenderer(tmp_path)._prepare('test', {})
    assert not old.exists()
    assert recent.exists() and other.exists()
