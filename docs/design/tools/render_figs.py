#!/usr/bin/env python3
"""把 diagrams/*.svg 用无头浏览器渲染成 PNG,供目检。PNG 写在 /private/tmp/rise-preview,不入库。

  python3 docs/design/tools/render_figs.py              渲染全部
  python3 docs/design/tools/render_figs.py 名字 ...      只渲染指定的图(不带 .svg)

多个写作者共用同一个浏览器,所以整次渲染持有文件锁,串行执行,避免互相改视口。
渲染完用 Read 工具打开打印出的 PNG 路径逐张看。
"""
import fcntl
import re
import subprocess
import sys
from pathlib import Path

BROWSE = Path.home() / ".claude/skills/gstack/browse/dist/browse"
DIAG = Path(__file__).resolve().parent.parent / "diagrams"
OUT = Path("/private/tmp/rise-preview")
OUT.mkdir(parents=True, exist_ok=True)
names = sys.argv[1:] or [p.stem for p in sorted(DIAG.glob("*.svg"))]

with open(OUT / ".lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    for name in names:
        svg_path = DIAG / f"{name}.svg"
        if not svg_path.is_file():
            print(f"FAIL {name}: 没有 {svg_path}(先运行 build_all.py)")
            continue
        svg = svg_path.read_text(encoding="utf-8")
        m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
        w, h = int(float(m[1])), int(float(m[2]))
        html = OUT / f"{name}.html"
        html.write_text('<!doctype html><meta charset="utf-8"><body style="margin:0;background:#fff">' + svg + "</body>",
                        encoding="utf-8")
        png = OUT / f"{name}.png"
        png.unlink(missing_ok=True)
        for args in (["viewport", f"{w}x{h}"], ["goto", f"file://{html}"], ["screenshot", "--viewport", str(png)]):
            subprocess.run([str(BROWSE), *args], capture_output=True, text=True, timeout=120)
        print(png if png.exists() else f"FAIL {name}: 没有生成截图")
