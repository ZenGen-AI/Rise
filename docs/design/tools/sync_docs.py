#!/usr/bin/env python3
"""同步设计文档里由工具维护的区块,让"每篇文档自包含"与"共享内容只有一份源"同时成立。

两类区块,标记行必须独占一行,区块内容由工具整段重写,不要手改:

  <!-- shared:begin NAME -->      内容 = _shared/NAME.md 的全文
  <!-- shared:end NAME -->

  <!-- figure:begin NAME -->      内容 = 图 diagrams/NAME.svg、图号与说明、文字版读图
  <!-- figure:end NAME -->        (说明与文字版来自 diagrams/NAME.meta.json,由 svgkit 生成)

用法:
  python3 tools/sync_docs.py            重写所有文档
  python3 tools/sync_docs.py 10-x.md    只处理指定文档(多人并行写作时用)
  python3 tools/sync_docs.py --check    只检查,有文档不同步则退出码 1
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(
    r"<!-- (?P<kind>shared|figure):begin (?P<name>[\w.-]+) -->\n(?:.*?\n)?<!-- (?P=kind):end (?P=name) -->",
    re.S,
)


def shared_body(name: str) -> str:
    path = ROOT / "_shared" / f"{name}.md"
    if not path.is_file():
        raise SystemExit(f"缺共享区块源文件: {path}")
    return path.read_text(encoding="utf-8").strip()


def figure_body(name: str, number: int) -> str:
    svg = ROOT / "diagrams" / f"{name}.svg"
    meta = ROOT / "diagrams" / f"{name}.meta.json"
    if not svg.is_file() or not meta.is_file():
        raise SystemExit(f"缺图: {svg} 或 {meta}(先运行 diagrams/build_all.py)")
    m = json.loads(meta.read_text(encoding="utf-8"))
    return (
        f"![{m['title']}](diagrams/{name}.svg)\n\n"
        f"**图 {number}　{m['title']}**　{m['desc']}\n\n"
        f"<details>\n<summary>文字版:图中的节点与连线</summary>\n\n{m['describe']}\n\n</details>"
    )


def sync_text(text: str) -> str:
    counter = {"n": 0}

    def repl(match: re.Match) -> str:
        kind, name = match["kind"], match["name"]
        if kind == "figure":
            counter["n"] += 1
            body = figure_body(name, counter["n"])
        else:
            body = shared_body(name)
        return f"<!-- {kind}:begin {name} -->\n{body}\n<!-- {kind}:end {name} -->"

    return BLOCK.sub(repl, text)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale: list[str] = []
    names = [a for a in argv if not a.startswith("--")]
    targets = [ROOT / n for n in names] if names else sorted(ROOT.glob("[0-9][0-9]-*.md"))
    for doc in targets:
        if not doc.is_file():
            print(f"跳过(还不存在): {doc.name}")
            continue
        old = doc.read_text(encoding="utf-8")
        new = sync_text(old)
        if new != old:
            stale.append(doc.name)
            if not check:
                doc.write_text(new, encoding="utf-8")
    if check:
        if stale:
            print("不同步:", ", ".join(stale))
            return 1
        print("所有文档的共享区块与图都已同步")
        return 0
    print("已重写:", ", ".join(stale) if stale else "(没有变化)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
