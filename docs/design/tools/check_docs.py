#!/usr/bin/env python3
"""设计文档提交前检查。有错误退出码 1,告警不影响退出码。

  python3 tools/check_docs.py              检查全部文档
  python3 tools/check_docs.py 10-x.md      只检查指定文档

检查项:共享区块与图已同步;没有跳转到其他文档/计划/旧补充章节的措辞与链接;
图都是已存在的 SVG 且通过卫生检查,没有 Mermaid、位图、ASCII 框图;图的节点数;
子系统文档的章节齐全、顺序固定、含头部元信息;正文里反引号包着的仓库路径真实存在
(同一行带 [计划中] 或 [推迟] 标记的除外);反引号里的类名、函数名在代码或施工计划里存在,
只在计划里的符号所在行必须带 [计划中](只告警,规则见 symbol_index.py)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "diagrams"))
import svgkit  # noqa: E402
import sync_docs  # noqa: E402
try:
    import symbol_index  # noqa: E402
except Exception:  # 符号核对是增强项,索引模块出问题不该挡住其他检查
    symbol_index = None

REQUIRED = [
    ("0", "系统速览"), ("1", "定位"), ("2", "目标、非目标与约束"), ("3", "架构"),
    ("4", "数据模型与状态机"), ("5", "接口契约"), ("6", "关键流程"), ("7", "关键决策"),
    ("8", "失败处理与可观测"), ("9", "测试策略"), ("10", "现状、局限与未核实项"),
]
FORBIDDEN = [
    (r"补充 ?[A-G]\b", "引用旧设计的补充章节"),
    (r"计划 ?[0-9]+", "引用实施计划编号"),
    (r"docs/superpowers", "指向旧文档或计划"),
    (r"上游设计|前置文档|旧设计", "指向其他文档"),
    (r"详见|参见", "跳转措辞"),
    (r"\]\((?!#|diagrams/|https?://)[^)]*\.md", "指向其他 markdown 文档的链接"),
]
BOX_CORNERS = "┌┐┘╔╗╚╝┏┓┗┛"
PATH_RE = re.compile(r"`((?:src|tests|scripts|cli|schemas)/[^`\s]*)`")
NODE_WARN, NODE_ERR = 12, 16


def docs() -> list[Path]:
    found = sorted(ROOT.glob("[0-9][0-9]-*.md"))
    readme = ROOT / "README.md"
    return found + ([readme] if readme.exists() else [])


def check_one(doc: Path, errors: list[str], warns: list[str]) -> None:
    name = doc.name
    text = doc.read_text(encoding="utf-8")
    lines = text.split("\n")

    try:
        if sync_docs.sync_text(text) != text:
            errors.append(f"{name}: 共享区块或图与源不同步,运行 tools/sync_docs.py")
    except SystemExit as exc:
        errors.append(f"{name}: {exc}")

    if "```mermaid" in text:
        errors.append(f"{name}: 含 Mermaid 图,改用 svgkit 生成的 SVG")
    if any(c in text for c in BOX_CORNERS):
        errors.append(f"{name}: 含 ASCII 框图字符,改用 svgkit 生成的 SVG")

    in_fence = False
    for no, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        for pattern, why in ([] if name == "README.md" else FORBIDDEN):
            if re.search(pattern, line):
                errors.append(f"{name}:{no}: {why}: {line.strip()[:70]}")
        for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", line):
            target = m.group(1)
            if not target.endswith(".svg"):
                errors.append(f"{name}:{no}: 图必须是 SVG: {target}")
            elif not (ROOT / target).is_file():
                errors.append(f"{name}:{no}: 图文件不存在: {target}")
        if in_fence:
            continue
        planned = "[计划中]" in line or "[推迟]" in line
        for m in PATH_RE.finditer(line):
            path = re.sub(r":\d+(-\d+)?$", "", m.group(1)).rstrip(".,;:)")
            if any(ch in path for ch in "*<{…"):
                continue
            if not planned and not (REPO / path).exists():
                errors.append(f"{name}:{no}: 路径不存在且未标 [计划中]: {path}")
        if symbol_index is not None:
            for msg in symbol_index.check_line(line):
                warns.append(f"{name}:{no}: 符号告警: {msg}")

    for m in re.finditer(r"<!-- figure:begin ([\w.-]+) -->", text):
        fig = m.group(1)
        svg = ROOT / "diagrams" / f"{fig}.svg"
        meta = ROOT / "diagrams" / f"{fig}.meta.json"
        if svg.is_file():
            for problem in svgkit.check(svg):
                errors.append(f"{name}: 图 {fig}: {problem}")
        if meta.is_file():
            body = json.loads(meta.read_text(encoding="utf-8"))["describe"]
            nodes = [l for l in body.split("**连线**")[0].split("\n") if l.startswith("- ")]
            if len(nodes) > NODE_ERR:
                errors.append(f"{name}: 图 {fig} 有 {len(nodes)} 个节点,超过 {NODE_ERR}")
            elif len(nodes) > (NODE_ERR if name.startswith("00-") else NODE_WARN):
                warns.append(f"{name}: 图 {fig} 有 {len(nodes)} 个节点,超过 {NODE_WARN},考虑拆图")

    if name in ("README.md",) or name.startswith("00-"):
        return
    head = "\n".join(lines[:40])
    for key in ("代码位置", "基线提交", "依赖", "对应实施计划"):
        if key not in head:
            errors.append(f"{name}: 头部元信息缺「{key}」")
    if "<!-- shared:begin context-block -->" not in text:
        errors.append(f"{name}: 缺系统速览共享区块")
    last = -1
    for num, title in REQUIRED:
        m = re.search(rf"^## {num}\. {re.escape(title)}\s*$", text, re.M)
        if not m:
            errors.append(f"{name}: 缺章节「## {num}. {title}」")
        elif m.start() < last:
            errors.append(f"{name}: 章节「{num}. {title}」顺序不对")
        else:
            last = m.start()
    if not re.search(r"^## 附 本文术语\s*$", text, re.M):
        errors.append(f"{name}: 缺「## 附 本文术语」")


def main() -> int:
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    if names:
        files = []
        for n in names:
            p = ROOT / n
            if p.is_file():
                files.append(p)
            else:
                print(f"跳过(还不存在): {n}")
    else:
        files = docs()
    if not files:
        print("没有设计文档可检查")
        return 0
    errors: list[str] = []
    warns: list[str] = []
    for doc in files:
        check_one(doc, errors, warns)
    for w in warns:
        print("告警", w)
    for e in errors:
        print("错误", e)
    print(f"检查 {len(files)} 篇文档:{len(errors)} 个错误,{len(warns)} 条告警")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
