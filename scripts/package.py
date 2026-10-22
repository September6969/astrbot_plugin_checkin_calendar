"""只打包插件源码和必要资源，排除运行记录、缓存与预览图。"""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = {"__init__.py", "main.py", "store.py", "presentation.py", "rendering.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "README.md", "LICENSE", "VALIDATION.md", ".gitignore"}


def build(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in ROOT.rglob("*") if p.is_file() and (
        (p.parent == ROOT and p.name in TOP_LEVEL)
        or (p.relative_to(ROOT).parts[0] in {"assets", "templates", "scripts", "tests"} and "__pycache__" not in p.parts and p.suffix in {".py", ".html", ".woff2", ".png", ".md", ".txt"})
    )]
    manifest = {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for p in sorted(files):
            name = str(Path(ROOT.name) / p.relative_to(ROOT)).replace("\\", "/")
            archive.write(p, name)
            manifest[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已打包 {len(files)} 个文件：{output}（{output.stat().st_size:,} bytes）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    build(parser.parse_args().output)
