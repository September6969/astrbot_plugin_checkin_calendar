"""核对 AstrBot 安装包结构、版本与逐文件哈希。"""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from astrbot.core.star.updater import _PluginUpdater


def verify(path: Path):
    metadata = _PluginUpdater.inspect_plugin_archive(str(path))["metadata"]
    assert metadata["name"] == "astrbot_plugin_checkin_calendar"
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == set(manifest)
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
        assert not any("__pycache__" in n or ".sqlite" in n for n in archive.namelist())
        assert "astrbot_plugin_checkin_calendar/assets/arcana.webp" in archive.namelist()
        index = json.loads(archive.read("astrbot_plugin_checkin_calendar/assets/fonts/index.json"))
        for face in [index['base'], *index['blocks'].values()]:
            assert f"astrbot_plugin_checkin_calendar/assets/fonts/{face['file']}" in archive.namelist()
    print(f"安装包验证通过：{metadata['name']} {metadata['version']}，{len(manifest)} 个文件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    verify(parser.parse_args().archive)
