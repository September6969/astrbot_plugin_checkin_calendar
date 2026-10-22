"""字体分片覆盖、实际昵称字形和传输预算。"""

import base64
import io
import json
from datetime import date
from pathlib import Path

from fontTools.ttLib import TTFont
from astrbot_plugin_checkin_calendar.presentation import font_faces, font_index, payload
from astrbot_plugin_checkin_calendar.store import MonthSnapshot


def points(face):
    with TTFont(io.BytesIO(base64.b64decode(face['data'].split(',', 1)[1]))) as font:
        return set(font.getBestCmap())


def test_nicknames_load_their_glyphs_without_full_font():
    text = '预览用户皇冠亚特曼哈蘭阿尔卡娜龘齉WW'
    faces = font_faces(text)
    available = set().union(*(points(face) for face in faces))
    assert {ord(char) for char in text} <= available
    assert len(faces) < len(font_index()['blocks'])
    assert sum(len(face['data']) for face in faces) < 1_000_000


def test_all_original_supported_characters_remain_available():
    index = font_index()
    root = Path(__file__).parents[1] / 'assets/fonts'
    seen = set()
    for face in [index['base'], *index['blocks'].values()]:
        with TTFont(root / face['file']) as font:
            current = set(font.getBestCmap())
        assert not seen & current
        seen |= current
    assert len(seen) == index['supported_count'] == 21113


def test_static_template_and_messages_have_font_coverage():
    root = Path(__file__).parents[1]
    text = ''.join((root / p).read_text(encoding='utf-8') for p in ('templates/calendar.html', 'main.py', 'presentation.py'))
    # 校验页面使用的汉字，源码中新注释不属于实际页面文案。
    visible = '本月的馆足迹打卡月历本月累计打卡天数今日记录查看范围整月周一开始方框标记今天二三四五六日这个月还没有记录从一次打卡开始记录成功已撤销今天最近一次今天没有可撤销的记录这条消息已处理未重复计数或撤销'
    assert all(char in text for char in visible)
    assert {ord(char) for char in visible} <= points(font_faces('')[0])


def test_regular_payload_stays_below_three_megabytes():
    data = payload(MonthSnapshot('2026-10', {22: 1}, 1, 1), '预览用户', date(2026, 10, 22), 'Asia/Shanghai', '记录成功')
    assert len(json.dumps(data).encode('utf-8')) < 3_000_000
    assert data['background_art'].startswith('data:image/webp;base64,')
