#!/usr/bin/env python3
"""svgkit:设计文档配图用的最小矢量绘图库。只依赖标准库,输出纯 SVG,不含任何位图。

    from svgkit import Canvas
    c = Canvas(960, 400, title="标题", desc="一句话说明这张图讲什么")
    a = c.box(40, 60, 200, "ControlPlane", "状态机 · 审核门", kind="control")
    b = c.box(360, 60, 200, "MindTrainer", kind="compute")
    c.edge(a, b, label="directive")
    c.save("out.svg")
    print(c.describe())          # 由同一份源生成的文字版读图说明,贴进文档图下方

布局由调用方给坐标。库负责三道检查,出错即抛 ValueError:文字溢出方框、方框互相重叠、图元超出画布。
卫生检查:  python3 svgkit.py --check a.svg
"""
from __future__ import annotations

import html
import json
import re
import math
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

FONT = ("-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Noto Sans CJK SC',"
        "'Microsoft YaHei','Segoe UI',Helvetica,Arial,sans-serif")
MONO = "'SF Mono',Menlo,Consolas,'Noto Sans Mono CJK SC',monospace"
INK, MUTED, LINE = "#0f172a", "#475569", "#475569"

# 种类 -> (填充, 描边)。颜色只表达"属于哪一层/哪一类",全套文档统一。
PALETTE = {
    "control": ("#eff6ff", "#2563eb"),   # 控制层
    "compute": ("#f0fdf4", "#16a34a"),   # 计算层
    "infra": ("#f5f3ff", "#7c3aed"),     # 共享基础设施
    "human": ("#fffbeb", "#d97706"),     # 人工审核门、人工动作
    "store": ("#ecfeff", "#0891b2"),     # 数据与存储
    "neutral": ("#f8fafc", "#64748b"),   # 中性
    "danger": ("#fef2f2", "#dc2626"),    # 失败、风险
}


def _cw(ch: str) -> float:
    o = ord(ch)
    if 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F:
        return 1.0
    if ch == " ":
        return 0.32
    if ch in "iljtfI.,:;'|!()[]/\\-":
        return 0.36
    if ch in "mwMW@":
        return 0.9
    if ch.isupper():
        return 0.68
    if ch.isdigit():
        return 0.58
    if o < 128:
        return 0.57
    return 0.95


def text_width(s: str, size: float, bold: bool = False) -> float:
    return sum(_cw(c) for c in s) * size * (1.06 if bold else 1.0)


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _rounded_path(pts, r: float = 7) -> str:
    if len(pts) < 3:
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
        l1, l2 = math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x1, y2 - y1)
        rr = min(r, l1 / 2, l2 / 2)
        if rr <= 0:
            d.append(f"L{x1:.1f},{y1:.1f}")
            continue
        ax, ay = x1 + (x0 - x1) / l1 * rr, y1 + (y0 - y1) / l1 * rr
        bx, by = x1 + (x2 - x1) / l2 * rr, y1 + (y2 - y1) / l2 * rr
        d.append(f"L{ax:.1f},{ay:.1f} Q{x1:.1f},{y1:.1f} {bx:.1f},{by:.1f}")
    d.append(f"L{pts[-1][0]:.1f},{pts[-1][1]:.1f}")
    return " ".join(d)


def _point_at(pts, f: float):
    segs = [(pts[i], pts[i + 1], math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
            for i in range(len(pts) - 1)]
    target = sum(s[2] for s in segs) * f
    for (x0, y0), (x1, y1), ln in segs:
        if target <= ln or ln == 0:
            t = 0 if ln == 0 else target / ln
            return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        target -= ln
    return pts[-1]


@dataclass
class Node:
    name: str
    x: float
    y: float
    w: float
    h: float

    def left(self, f=0.5): return (self.x, self.y + self.h * f)
    def right(self, f=0.5): return (self.x + self.w, self.y + self.h * f)
    def top(self, f=0.5): return (self.x + self.w * f, self.y)
    def bottom(self, f=0.5): return (self.x + self.w * f, self.y + self.h)

    @property
    def cx(self): return self.x + self.w / 2

    @property
    def cy(self): return self.y + self.h / 2


PROGRESS = re.compile(r"计划中|已落地|已实现|推迟|未接入|尚未|部分落地|无写方|今天|目前|(?<![A-Za-z0-9_])V1(?![A-Za-z0-9_])|工作包")


def _forbid_progress(svg: str) -> None:
    """图只画目标系统。可见文字、标题与说明里出现进展字样就报错,进展写在正文。"""
    m = PROGRESS.search(re.sub(r"<[^>]+>", " ", svg))
    if m:
        raise ValueError(f"图里出现进展字样 {m.group(0)!r}:图只画目标系统,进展写在正文的状态标记里")


class Canvas:
    def __init__(self, w: float, h: float, *, title: str, desc: str = ""):
        self.w, self.h, self.title, self.desc = w, h, title, desc
        self._lanes: list[str] = []
        self._lines: list[str] = []
        self._shapes: list[str] = []
        self._labels: list[str] = []
        self._markers: dict[str, str] = {}
        self._boxes: list[Node] = []
        self._extent: list[tuple] = []
        self.nodes: list[tuple[str, str]] = []
        self.edges: list[tuple] = []

    # ---------- 内部 ----------
    def _touch(self, x0, y0, x1, y1):
        self._extent.append((x0, y0, x1, y1))

    def _fit(self, s, size, bold, room, what):
        if text_width(s, size, bold) > room:
            raise ValueError(f"文字溢出 {what}: {s!r} 需要 {text_width(s, size, bold):.0f}px,只有 {room:.0f}px")

    def _marker(self, color):
        if color not in self._markers:
            self._markers[color] = f"ah{len(self._markers)}"
        return self._markers[color]

    def _register(self, node: Node):
        for o in self._boxes:
            if node.x < o.x + o.w - 1 and o.x < node.x + node.w - 1 and node.y < o.y + o.h - 1 and o.y < node.y + node.h - 1:
                raise ValueError(f"方框重叠: {node.name!r} 与 {o.name!r}")
        self._boxes.append(node)
        self._touch(node.x, node.y, node.x + node.w, node.y + node.h)

    # ---------- 图元 ----------
    def lane(self, x, y, w, h, label, *, kind="neutral", dashed=False, label_align="start"):
        _, stroke = PALETTE[kind]
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        self._lanes.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{stroke}" fill-opacity="0.05" '
            f'stroke="{stroke}" stroke-width="1.2"{dash}/>'
            f'<text x="{x + 14 if label_align == "start" else x + w - 14}" y="{y + 22}" text-anchor="{"start" if label_align == "start" else "end"}" font-size="13" font-weight="700" fill="{stroke}">{_esc(label)}</text>')
        self._touch(x, y, x + w, y + h)

    def box(self, x, y, w, title, sub=None, *, h=None, kind="neutral", dashed=False,
            title_size=15, sub_size=12, align="middle", name=None, mono_sub=False):
        subs = sub.split("\n") if sub else []
        lines_h = title_size * 1.3 + len(subs) * sub_size * 1.45
        h = h if h is not None else lines_h + 24
        pad = 14
        what = f"方框 {title!r}"
        self._fit(title, title_size, True, w - 2 * pad, what)
        for s in subs:
            self._fit(s, sub_size, False, w - 2 * pad, what)
        if lines_h + 8 > h:
            raise ValueError(f"{what} 高度不够: 需要 {lines_h + 8:.0f}px,给了 {h}px")
        fill, stroke = PALETTE[kind]
        node = Node(name or title, x, y, w, h)
        self._register(node)
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        tx = x + w / 2 if align == "middle" else x + pad
        anchor = "middle" if align == "middle" else "start"
        top = y + (h - lines_h) / 2
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" '
               f'stroke-width="1.5"{dash}/>',
               f'<text x="{tx}" y="{top + title_size * 0.98:.1f}" text-anchor="{anchor}" font-size="{title_size}" '
               f'font-weight="700" fill="{INK}">{_esc(title)}</text>']
        for i, s in enumerate(subs):
            fam = f' font-family="{MONO}"' if mono_sub else ""
            out.append(f'<text x="{tx}" y="{top + title_size * 1.3 + sub_size * 1.45 * i + sub_size * 1.0:.1f}" '
                       f'text-anchor="{anchor}" font-size="{sub_size}" fill="{MUTED}"{fam}>{_esc(s)}</text>')
        self._shapes.append("".join(out))
        self.nodes.append((node.name, " / ".join(subs)))
        return node

    def gate(self, x, y, w, title, sub=None, *, h=None, kind="human", name=None):
        subs = sub.split("\n") if sub else []
        lines_h = 15 * 1.3 + len(subs) * 12 * 1.45
        h = h if h is not None else lines_h + 26
        inset = 16
        what = f"审核门 {title!r}"
        self._fit(title, 15, True, w - 2 * inset - 8, what)
        for s in subs:
            self._fit(s, 12, False, w - 2 * inset - 8, what)
        fill, stroke = PALETTE[kind]
        node = Node(name or title, x, y, w, h)
        self._register(node)
        pts = [(x + inset, y), (x + w - inset, y), (x + w, y + h / 2), (x + w - inset, y + h),
               (x + inset, y + h), (x, y + h / 2)]
        poly = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        top = y + (h - lines_h) / 2
        out = [f'<polygon points="{poly}" fill="{fill}" stroke="{stroke}" stroke-width="1.8"/>',
               f'<text x="{x + w / 2}" y="{top + 15 * 0.98:.1f}" text-anchor="middle" font-size="15" '
               f'font-weight="700" fill="{INK}">{_esc(title)}</text>']
        for i, s in enumerate(subs):
            out.append(f'<text x="{x + w / 2}" y="{top + 15 * 1.3 + 12 * 1.45 * i + 12:.1f}" text-anchor="middle" '
                       f'font-size="12" fill="{MUTED}">{_esc(s)}</text>')
        self._shapes.append("".join(out))
        self.nodes.append((node.name, " / ".join(subs)))
        return node

    def store(self, x, y, w, title, sub=None, *, h=None, kind="store", name=None):
        subs = sub.split("\n") if sub else []
        ry = 9
        lines_h = 15 * 1.3 + len(subs) * 12 * 1.45
        h = h if h is not None else lines_h + 2 * ry + 14
        what = f"存储 {title!r}"
        self._fit(title, 15, True, w - 28, what)
        for s in subs:
            self._fit(s, 12, False, w - 28, what)
        fill, stroke = PALETTE[kind]
        node = Node(name or title, x, y, w, h)
        self._register(node)
        rx = w / 2
        body = (f'<path d="M{x},{y + ry} a{rx},{ry} 0 0 1 {w},0 v{h - 2 * ry} a{rx},{ry} 0 0 1 {-w},0 z" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
                f'<ellipse cx="{x + rx}" cy="{y + ry}" rx="{rx}" ry="{ry}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        top = y + ry * 2 + (h - 2 * ry - lines_h) / 2
        out = [body, f'<text x="{x + rx}" y="{top + 15 * 0.98:.1f}" text-anchor="middle" font-size="15" '
                     f'font-weight="700" fill="{INK}">{_esc(title)}</text>']
        for i, s in enumerate(subs):
            out.append(f'<text x="{x + rx}" y="{top + 15 * 1.3 + 12 * 1.45 * i + 12:.1f}" text-anchor="middle" '
                       f'font-size="12" fill="{MUTED}">{_esc(s)}</text>')
        self._shapes.append("".join(out))
        self.nodes.append((node.name, " / ".join(subs)))
        return node

    def badge(self, x, y, n, *, kind="neutral"):
        _, stroke = PALETTE[kind]
        self._shapes.append(
            f'<circle cx="{x}" cy="{y}" r="10" fill="{stroke}"/><text x="{x}" y="{y + 4}" text-anchor="middle" '
            f'font-size="11" font-weight="700" fill="#ffffff">{_esc(str(n))}</text>')
        self._touch(x - 10, y - 10, x + 10, y + 10)

    def text(self, x, y, s, *, size=12, anchor="start", color=MUTED, bold=False, mono=False):
        tw = text_width(s, size, bold)
        x0 = x if anchor == "start" else x - tw / 2 if anchor == "middle" else x - tw
        self._touch(x0, y - size, x0 + tw, y + size * 0.3)
        fam = f' font-family="{MONO}"' if mono else ""
        wt = ' font-weight="700"' if bold else ""
        self._labels.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" fill="{color}"{wt}{fam}>{_esc(s)}</text>')

    def label(self, x, y, s, *, size=12, color=MUTED, bold=False):
        tw = text_width(s, size, bold)
        self._touch(x - tw / 2 - 4, y - size * 0.7, x + tw / 2 + 4, y + size * 0.7)
        wt = ' font-weight="700"' if bold else ""
        self._labels.append(
            f'<rect x="{x - tw / 2 - 4:.1f}" y="{y - size * 0.62 - 2:.1f}" width="{tw + 8:.1f}" height="{size * 1.24 + 4:.1f}" '
            f'rx="4" fill="#ffffff" fill-opacity="0.94"/><text x="{x}" y="{y + size * 0.36:.1f}" text-anchor="middle" '
            f'font-size="{size}" fill="{color}"{wt}>{_esc(s)}</text>')

    def note(self, x, y, w, text, *, kind="human"):
        lines = text.split("\n")
        fill, stroke = PALETTE[kind]
        for s in lines:
            self._fit(s, 12, False, w - 24, "便签")
        h = len(lines) * 12 * 1.5 + 16
        self._shapes.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h:.1f}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
                            f'<rect x="{x}" y="{y}" width="4" height="{h:.1f}" rx="2" fill="{stroke}"/>')
        for i, s in enumerate(lines):
            self._shapes.append(f'<text x="{x + 14}" y="{y + 20 + i * 18:.1f}" font-size="12" fill="{INK}">{_esc(s)}</text>')
        self._touch(x, y, x + w, y + h)
        return h

    def arrow(self, pts, *, label=None, label_at=0.5, dashed=False, color=LINE, width=1.6, head=True, tail=False):
        mk = self._marker(color)
        attrs = f'fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"'
        if dashed:
            attrs += ' stroke-dasharray="6 4"'
        if head:
            attrs += f' marker-end="url(#{mk})"'
        if tail:
            attrs += f' marker-start="url(#{mk})"'
        self._lines.append(f'<path d="{_rounded_path(pts)}" {attrs}/>')
        for px, py in pts:
            self._touch(px, py, px, py)
        if label:
            lx, ly = _point_at(pts, label_at)
            self.label(lx, ly, label)

    def edge(self, a: Node, b: Node, a_side="right", b_side="left", *, a_f=0.5, b_f=0.5, via=None, **kw):
        p0, p1 = getattr(a, a_side)(a_f), getattr(b, b_side)(b_f)
        horiz = ("left", "right")
        if via is None:
            if a_side in horiz and b_side in horiz and abs(p0[1] - p1[1]) > 0.5:
                mx = (p0[0] + p1[0]) / 2
                via = [(mx, p0[1]), (mx, p1[1])]
            elif a_side not in horiz and b_side not in horiz and abs(p0[0] - p1[0]) > 0.5:
                my = (p0[1] + p1[1]) / 2
                via = [(p0[0], my), (p1[0], my)]
            elif a_side in horiz and b_side not in horiz and abs(p0[1] - p1[1]) > 0.5:
                via = [(p1[0], p0[1])]
            elif a_side not in horiz and b_side in horiz and abs(p0[0] - p1[0]) > 0.5:
                via = [(p0[0], p1[1])]
            else:
                via = []
        self.arrow([p0, *via, p1], **kw)
        self.edges.append((a.name, b.name, kw.get("label") or "", bool(kw.get("dashed")), bool(kw.get("tail"))))

    def legend(self, x, y, items, gap=22):
        """items: [(kind, 文字, 是否虚线)]"""
        cx = x
        for kind, text, dashed in items:
            fill, stroke = PALETTE[kind]
            dash = ' stroke-dasharray="4 3"' if dashed else ""
            self._labels.append(f'<rect x="{cx}" y="{y - 10}" width="18" height="13" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dash}/>'
                                f'<text x="{cx + 25}" y="{y + 1}" font-size="12" fill="{MUTED}">{_esc(text)}</text>')
            cx += 25 + text_width(text, 12) + gap
        self._touch(x, y - 10, cx, y + 4)

    # ---------- 输出 ----------
    def describe(self) -> str:
        lines = ["**节点**", ""]
        lines += [f"- {n}" + (f":{s}" if s else "") for n, s in self.nodes]
        if self.edges:
            lines += ["", "**连线**", ""]
            for a, b, lab, dashed, both in self.edges:
                arrow = "↔" if both else "→"
                style = "(虚线)" if dashed else ""
                lines.append(f"- {a} {arrow} {b}" + (f":{lab}" if lab else "") + style)
        return "\n".join(lines)

    def to_svg(self) -> str:
        for x0, y0, x1, y1 in self._extent:
            if x0 < -0.5 or y0 < -0.5 or x1 > self.w + 0.5 or y1 > self.h + 0.5:
                raise ValueError(f"图元超出画布 {self.w}x{self.h}: ({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f})")
        flat = self.describe().replace("**", "").replace("\n\n", "\n").replace("\n", ";")
        defs = "".join(
            f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" '
            f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{col}"/></marker>'
            for col, mid in self._markers.items())
        body = "".join(self._lanes + self._lines + self._shapes + self._labels)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
                f'height="{self.h}" role="img" aria-labelledby="t d" font-family="{FONT}">'
                f'<title id="t">{_esc(self.title)}</title><desc id="d">{_esc(self.desc + " " + flat)}</desc>'
                f'<defs>{defs}</defs><rect width="{self.w}" height="{self.h}" fill="#ffffff"/>{body}</svg>\n')
        _forbid_progress(svg)
        return svg

    def save(self, path):
        """写出 SVG,旁边再写一份 .meta.json(标题、说明、文字版读图),供 tools/sync_docs.py 嵌入文档。"""
        svg = self.to_svg()
        ET.fromstring(svg)
        path = Path(path)
        path.write_text(svg, encoding="utf-8")
        meta = {"title": self.title, "desc": self.desc, "describe": self.describe()}
        path.with_suffix(".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def check(path) -> list[str]:
    """卫生检查:格式良好、有 viewBox 与标题、没有位图/内嵌脚本。返回问题列表,空表示通过。"""
    issues = []
    raw = Path(path).read_text(encoding="utf-8")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return [f"XML 不合法: {exc}"]
    if not root.get("viewBox"):
        issues.append("缺 viewBox")
    ns = "{http://www.w3.org/2000/svg}"
    if root.find(f"{ns}title") is None:
        issues.append("缺 <title>")
    for bad in ("<image", "data:image", "base64", "<script", "<foreignObject"):
        if bad in raw:
            issues.append(f"含 {bad}")
    return issues


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--check":
        raise SystemExit("用法: python3 svgkit.py --check a.svg [b.svg ...]")
    for f in sys.argv[2:]:
        problems = check(f)
        print(f"{'ok  ' if not problems else 'FAIL'} {f} {'; '.join(problems)}")
