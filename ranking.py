"""机器人月排行的昵称补全、图片数据与同快照文字回退。"""
import asyncio
import hashlib
from dataclasses import replace

from .presentation import clean_name, font_faces


async def resolve_names(snapshot, event, store):
    """仅补全榜上缺失昵称；查询失败保留匿名标识，不阻止出榜。"""
    platform_name = getattr(event, 'get_platform_name', lambda: '')()
    client = getattr(event, 'bot', None)
    if platform_name != 'aiocqhttp' or client is None:
        return snapshot
    semaphore = asyncio.Semaphore(3)
    async def resolve(entry):
        if entry.name or not entry.user_id.isdecimal():
            return entry
        try:
            async with semaphore:
                result = await asyncio.wait_for(client.get_stranger_info(user_id=int(entry.user_id)), 2)
            name = result.get('nickname') if isinstance(result, dict) else None
            if not isinstance(name, str) or not name.strip():
                return entry
            name = clean_name(name)
            await asyncio.to_thread(store.save_name, entry.actor, name)
            return replace(entry, name=name)
        except Exception:
            return entry
    return replace(snapshot, entries=list(await asyncio.gather(*(resolve(e) for e in snapshot.entries))))


def ranking_payload(snapshot):
    year, month = map(int, snapshot.month.split('-'))
    rows = [{
        'rank': i + 1,
        'name': clean_name(e.name) if e.name else '用户·' + hashlib.sha256(e.actor.encode('utf-8')).hexdigest()[:8],
        'total': e.total, 'days': e.days,
        'color': ['#be252a','#c63832','#d54d39','#df6543','#e67c4c','#ed9558','#efac67','#f3bd74','#f6ce83','#f6da92'][i],
    } for i, e in enumerate(snapshot.entries)]
    title = f'{year}年{month}月 🦌馆排行榜'
    text = title + '总次数参与人数次天暂无记录机器人全部用户仅展示前十名按次数活跃天数排序匿名昵称暂未获取' + ''.join(r['name'] for r in rows)
    return {'title':title, 'month':snapshot.month, 'total':snapshot.total, 'participants':snapshot.participants,
            'rows':rows, 'fonts':font_faces(text), 'unknown_names':any(not e.name for e in snapshot.entries)}


def ranking_text(data):
    rows = [f"{r['rank']}. {r['name']}：{r['total']} 次 · {r['days']} 天" for r in data['rows']]
    return '\n'.join([data['title'], f"总次数 {data['total']} 次 · 参与人数 {data['participants']} 人", *rows,
                      '机器人全部用户 · 仅展示前十名' if rows else '本月暂无记录',
                      '按次数、活跃天数排序；同分按稳定用户标识排列。'])
