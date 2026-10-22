"""离线构建中文字体分片；运行时只读取当前页面所需分片。"""

import argparse
import hashlib
import json
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]


def ranges(points):
    groups = []
    for point in sorted(points):
        if groups and groups[-1][1] + 1 == point:
            groups[-1][1] = point
        else:
            groups.append([point, point])
    return ','.join(f'U+{a:X}' if a == b else f'U+{a:X}-{b:X}' for a, b in groups)


def build(source, output):
    output.mkdir(parents=True, exist_ok=True)
    with TTFont(source) as font:
        supported = set(font.getBestCmap())
    # 静态文案来自正式源码；动态昵称按 Unicode 页选择其余字形。
    text = ''.join((ROOT / name).read_text(encoding='utf-8') for name in
                   ('templates/calendar.html', 'main.py', 'presentation.py'))
    common = ({ord(c) for c in text} | set(range(32, 127))) & supported
    groups = {'base': common}
    for point in sorted(supported - common):
        groups.setdefault(str(point // 256), set()).add(point)
    manifest = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'supported_count': len(supported), 'base_codepoints': sorted(common), 'blocks': {}}
    for key, points in groups.items():
        name = 'base.woff2' if key == 'base' else f'block-{int(key):04x}.woff2'
        with TTFont(source, recalcTimestamp=False, recalcBBoxes=False) as font:
            worker = subset.Subsetter()
            worker.populate(unicodes=points)
            worker.subset(font)
            font.save(output / name)
        entry = {'file': name, 'unicode_range': ranges(points)}
        if key == 'base':
            manifest['base'] = entry
        else:
            manifest['blocks'][key] = entry
    (output / 'index.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(groups)} 个分片，完整保留 {len(supported)} 个字符；基本分片 {(output / "base.woff2").stat().st_size} bytes。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'assets/fonts')
    args = parser.parse_args()
    build(args.source, args.output)
