"""排行榜正式模板与生产渲染器的合成预览。"""
import argparse,asyncio,json,sys,shutil
from pathlib import Path
from jinja2 import Environment
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from astrbot_plugin_checkin_calendar.store import RankEntry,RankingSnapshot
from astrbot_plugin_checkin_calendar.ranking import ranking_payload
from astrbot_plugin_checkin_calendar.rendering import CalendarRenderer

async def main(output):
    renderer=CalendarRenderer(output)
    template=(ROOT/'templates/ranking.html').read_text(encoding='utf-8')
    cases={
      'normal':RankingSnapshot('2026-10',1112,100,[RankEntry(str(i),str(i),name,count,days) for i,(name,count,days) in enumerate(zip(['小鹿','阿尔卡娜','TenShI','大妮','Trusted','railgun10314','星辰大海','ハイボール','阿尘','6.22'],[70,62,61,61,56,53,50,47,46,40],[16,17,18,18,17,18,16,16,17,14]))]),
      'empty':RankingSnapshot('2026-10',0,0,[]),
      'long':RankingSnapshot('2026-10',999999999999,10,[RankEntry(str(i),str(i),'W'*40 if i%2 else '<script>alert(1)</script>长名字'*3,9999999999,31) for i in range(10)]),
    }
    checks=[]
    try:
      for case,snapshot in cases.items():
        data=ranking_payload(snapshot)
        image=await renderer.render(template,data,{})
        shutil.copyfile(image,output/f'{case}.png')
        page=await renderer._browser.new_page(viewport={'width':720,'height':1})
        try:
          await page.set_content(Environment(autoescape=True).from_string(template).render(**data))
          await page.evaluate('document.fonts.ready')
          result=await page.evaluate('''() => ({errors:[...document.querySelectorAll('.row,.name,.count,.days,h1,.summary')].filter(e=>e.scrollWidth>e.clientWidth+1||e.getBoundingClientRect().right>720).map(e=>e.className),rows:document.querySelectorAll('.row').length,scripts:document.scripts.length,height:document.documentElement.scrollHeight})''')
          assert not result['errors'] and not result['scripts'] and result['rows']==len(snapshot.entries),result
          checks.append({'case':case,**result})
        finally: await page.close()
    finally: await renderer.close()
    (output/'layout.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(checks,ensure_ascii=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args().output))
