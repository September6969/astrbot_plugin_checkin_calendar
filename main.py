"""AstrBot 打卡月历入口。"""

import asyncio
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .presentation import clean_name, parse_month, payload, text_summary
from .rendering import CalendarRenderer
from .store import CalendarStore

TRIGGER_PATTERN = r"\A🦌\ufe0f?(?:\s+[\s\S]*)?\Z"
HELP = "🦌馆月历\n🦌：记录一次并查看本月月历\n🦌 月历：只查看，不增加次数\n🦌 月历 2026-10：查看历史月份\n🦌 撤销：撤销今天最近一次\n🦌 帮助：查看说明\n直接发送即可，无需斜杠。每次记录至少间隔2小时，撤销不重置间隔。按本人独立保存，不开放他人查询。"


class CheckinCalendarPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.timezone_name = str((config or {}).get("timezone", "Asia/Shanghai"))
        self.timezone = ZoneInfo(self.timezone_name)
        self.store = CalendarStore(StarTools.get_data_dir("astrbot_plugin_checkin_calendar") / "calendar.sqlite3")
        self.template = (Path(__file__).parent / "templates/calendar.html").read_text(encoding="utf-8")
        from astrbot.core import html_renderer
        self.calendar_renderer = CalendarRenderer(self.html_render, getattr(html_renderer, "network_strategy", None))

    @filter.regex(TRIGGER_PATTERN)
    async def checkin(self, event: AstrMessageEvent):
        text = event.get_message_str().strip()
        if not re.fullmatch(TRIGGER_PATTERN, text):
            return
        parts = text.split()
        if len(parts) > 3:
            yield event.plain_result(HELP)
            return
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""
        async for reply in self._handle(event, action, value):
            yield reply

    async def _handle(self, event: AstrMessageEvent, action: str, value: str):
        if action == "帮助":
            yield event.plain_result(HELP)
            return
        if action not in {"", "月历", "状态", "撤销"} or (value and action != "月历"):
            yield event.plain_result(HELP)
            return
        platform = str(event.get_platform_id() or "")
        bot = str(event.get_self_id() or "")
        user = str(event.get_sender_id() or "")
        if not platform or not bot or not user:
            yield event.plain_result("未取得完整用户身份，暂时无法读取或记录。")
            return
        actor = json.dumps([platform, bot, user], ensure_ascii=False)
        now = datetime.now(self.timezone)
        try:
            month = parse_month(value, now.date())
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        message = ""
        if action in {"", "撤销"}:
            message_id = str(getattr(event.message_obj, "message_id", "") or "")
            # 消息 ID 不存在时使用本次调用唯一标记；不通过消息文本猜测重复。
            token = json.dumps([event.unified_msg_origin, message_id]) if message_id else str(uuid.uuid4())
            try:
                result = await asyncio.to_thread(self.store.change, actor, token, "undo" if action else "add", now)
            except (sqlite3.Error, OSError):
                logger.exception("[打卡月历] 保存记录异常")
                yield event.plain_result("保存结果暂时无法确认，请先用 🦌 月历 核对次数，再决定是否重新记录。")
                return
            if result.status == "cooldown":
                remaining = max(1, math.ceil((datetime.fromisoformat(result.available_at) - now).total_seconds()))
                hours, seconds = divmod(remaining, 3600)
                minutes, seconds = divmod(seconds, 60)
                wait = (f"{hours}小时" if hours else "") + (f"{minutes}分钟" if minutes else "") + (f"{seconds}秒" if seconds else "")
                message = f"注意身体，歇会儿吧！（每120分钟只能记录一次哦）\n还需等待{wait}。"
            else:
                message = {"added": "记录成功", "undone": "已撤销今天最近一次", "empty": "今天没有可撤销的记录"}[result.status]
            if result.replay:
                message = "这条消息已处理，未重复计数或撤销"
            if result.status == "cooldown":
                # 冷却只发送文字，并禁用宿主自动文转图；不读取月历或调用渲染器。
                yield event.make_result().message(message).use_t2i(False)
                return
        try:
            snapshot = await asyncio.to_thread(self.store.month, actor, month)
        except (sqlite3.Error, OSError):
            logger.exception("[打卡月历] 读取月历异常")
            yield event.plain_result((message + "。" if message else "") + "月历暂时读取失败，请稍后用 🦌 月历 查询。")
            return
        name = event.get_sender_name() or "我"
        fallback = text_summary(snapshot, name, self.timezone_name, message)
        try:
            data = payload(snapshot, name, now.date(), self.timezone_name, message)
            options = {"type": "png", "quality": None, "full_page": True, "viewport": {"width": 720, "height": 1}, "animations": "disabled"}
            image = await asyncio.wait_for(self.calendar_renderer.render(self.template, data, options), timeout=30)
            if not isinstance(image, str) or not image.strip():
                raise ValueError("渲染器未返回图片")
        except Exception as exc:
            logger.warning("[打卡月历] 图片生成失败：%s", type(exc).__name__)
            yield event.make_result().message("月历图片暂时生成失败，可发送 🦌 月历 重试（不会增加次数）。\n" + fallback).use_t2i(False)
            return
        # 原生艾特放在图片之前，使用平台用户 ID，避免昵称同名造成误艾特。
        reply = event.make_result().use_t2i(False).at(clean_name(name), user).message("\n")
        yield reply.url_image(image) if image.startswith("http") else reply.file_image(image)
