#!/usr/bin/env python3
"""跨文档一致性检查。check_docs.py 管单篇文档,本脚本管整套文档之间是否一致。

  python3 docs/design/tools/check_consistency.py            检查已写成的文档,缺的文档只提示
  python3 docs/design/tools/check_consistency.py --final    终检:12 篇必须齐全,README 必须与目录一一对应,不允许孤立的图

检查项:头部元信息齐全且基线提交一致;状态行的标记与总览一致;依赖行没有越出总览的依赖方向表;
图名以文档编号开头、每篇 3 到 5 张;决策编号前缀与文档对应、不重复、引用的编号都有定义;
状态标记只用三种写法;统一口径里禁用的说法;测试数是否出自实测;计划中的接口是否声明了"施工时可能微调"。
有错误退出码 1,告警不影响退出码。末尾打印汇总表供人工复核。
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
BASELINE = "8dd295b"
EXPECTED = ["00-overview.md", "10-controlplane.md", "20-socialsimulator.md", "21-diagnostic-evaluation.md",
            "30-mindtrainer.md", "40-actio.md", "50-mindagent.md", "60-recipestore.md",
            "70-trainscheduler.md", "71-trainplanner.md", "80-inference-bridge.md", "90-factory-and-cli.md"]
PREFIX = {"00": "SYS", "10": "CP", "20": "SS", "21": "DX", "30": "MT", "40": "AC", "50": "MA",
          "60": "RS", "70": "TS", "71": "TP", "80": "BR", "90": "FC"}
OWN_PKG = {"10": "controlplane", "20": "simulator", "21": "diagnostic", "30": "mindtrainer", "40": "actio",
           "50": "mindagent", "60": "recipestore", "70": "trainscheduler", "71": "trainplanner",
           "80": "inference_bridge", "90": "factory"}
STATUS_REQUIRED = {"10": {"已落地", "计划中"}, "20": {"已落地"}, "21": {"已落地"}, "30": {"已落地"},
                   "40": {"计划中"}, "50": {"计划中"}, "60": {"计划中"}, "70": {"计划中"}, "71": {"计划中"},
                   "80": {"已落地", "计划中"}, "90": {"已落地", "计划中"}}
# 总览 3.3 的依赖方向表。controlplane 对 simulator 的回指是骨架桩,总览已说明。
ALLOWED_DEPS = {
    "controlplane": {"simulator"}, "simulator": {"controlplane"}, "diagnostic": {"controlplane", "simulator"},
    "mindtrainer": {"controlplane", "simulator"}, "actio": {"simulator"}, "recipestore": {"controlplane"},
    "trainplanner": {"controlplane", "inference_bridge"},
    "trainscheduler": {"controlplane", "recipestore", "trainplanner", "mindtrainer", "inference_bridge"},
    "mindagent": {"controlplane", "actio", "inference_bridge", "simulator"},
    "inference_bridge": {"controlplane"}, "factory": set(),
}
NAME_TO_PKG = {"ControlPlane": "controlplane", "SocialSimulator": "simulator", "MindTrainer": "mindtrainer",
               "MindAgent": "mindagent", "RecipeStore": "recipestore", "TrainScheduler": "trainscheduler",
               "TrainPlanner": "trainplanner", "Actio": "actio"}
ALL_PKGS = sorted(set(OWN_PKG.values()))
HEADER_KEYS = ("一句话", "状态", "所在层", "代码位置", "基线提交", "依赖", "被依赖", "对应实施计划")
DECISION_HEADING = {"00": r"^## 11\. 决策总表\s*$"}
MARK_OK = {"[已落地]", "[计划中]", "[推迟]"}
PLANNED_NUMBERS = {2275, 2417, 1455, 142, 192, 145, 197, 189, 235, 185, 152, 160}
ID_ROW = re.compile(r"^\|\s*(R\d{1,2}|[A-Z]{2,3}-\d+)\s*\|")
ID_REF = re.compile(r"\b(?:SYS|CP|SS|DX|MT|AC|MA|RS|TS|TP|BR|FC)-\d+\b")
NUM_PAT = re.compile(r"(?<![\d.])(\d{3,4})(?![\d.])\s*(?:个|条)?\s*(?:Python |pytest )?(?:测试|用例)")

TERMS_ERR = [(r"Plan ?\d", "写「工作包 N」"), (r"控制平面", "统一写 ControlPlane")]
TERMS_WARN = [
    (r"协调器", "协调层写 ControlPlane"), (r"状态库", "写「产物树」"), (r"事件循环", "写「调和循环」"),
    (r"审批", "写「审核门」"), (r"闸门", "写「审核门」"), (r"旁路", "写「受限直连」"),
    (r"候选模型", "写「候选 checkpoint」"), (r"真实度", "写「证据等级」"),
    (r"(?<!开源)(?<!模型)底座", "底座只指开源基础模型,下一轮起点写「基座」"),
    (r"(?<![\w.\-<])v[12](?![\w.\-])", "版本写 V1、V2"),
    (r"§", "§ 通常引用旧设计的章节号,改成就地写出内容"),
]

BLOCKS = re.compile(r"<!-- (shared|figure):begin [\w.-]+ -->.*?<!-- \1:end [\w.-]+ -->", re.S)
FENCE = re.compile(r"```.*?```", re.S)
INLINE = re.compile(r"`[^`\n]*`")


def _blank(m: re.Match) -> str:
    return "\n" * m.group(0).count("\n")


def prose(text: str) -> str:
    """去掉工具维护的区块、代码块与行内代码,保留行号,供术语与标记检查使用。"""
    for rx in (BLOCKS, FENCE):
        text = rx.sub(_blank, text)
    return INLINE.sub("", text)


def header(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in text.split("\n")[:45]:
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m and m.group(1) != "项" and not set(m.group(1)) <= set("-: "):
            rows[m.group(1)] = m.group(2)
    return rows


def decision_ids(text: str, num: str) -> list[tuple[str, int]]:
    m = re.search(DECISION_HEADING.get(num, r"^## 7\. 关键决策\s*$"), text, re.M)
    if not m:
        return []
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = rest[: nxt.start()] if nxt else rest
    base = text[: m.end()].count("\n")
    return [(mm.group(1), base + i + 1) for i, line in enumerate(body.split("\n"))
            if (mm := ID_ROW.match(line))]


def known_test_numbers() -> set[int] | None:
    try:
        out = subprocess.run(["uv", "run", "--quiet", "pytest", "--collect-only", "-q"], cwd=REPO,
                             capture_output=True, text=True, timeout=180).stdout
    except Exception:
        return None
    per: Counter = Counter()
    for line in out.splitlines():
        if line.startswith("tests/") and "::" in line:
            per[line.split("::")[0][len("tests/"):]] += 1
    if not per:
        return None
    groups: Counter = Counter()
    for f, c in per.items():
        g = ("mt" if f.startswith("test_mt_") else "diag" if f.startswith("test_diag_")
             else "realtime" if f.startswith("test_realtime_") else "sim" if f.startswith("test_sim_") or f == "test_runner_stop_predicate.py"
             else "cross" if f.startswith("test_model_ref_and_isolation") else "cp")
        groups[g] += c
    return (set(per.values()) | set(groups.values()) | {sum(per.values()), groups["sim"] + groups["realtime"]}
            | PLANNED_NUMBERS)


def main(argv: list[str]) -> int:
    final = "--final" in argv
    errors: list[str] = []
    warns: list[str] = []

    def at(doc: str, line: int | None) -> str:
        return f"{doc}:{line}" if line else doc

    def err(doc, line, msg):
        errors.append(f"{at(doc, line)}: {msg}")

    def warn(doc, line, msg):
        warns.append(f"{at(doc, line)}: {msg}")

    on_disk = sorted(p.name for p in ROOT.glob("[0-9][0-9]-*.md"))
    for name in on_disk:
        if name not in EXPECTED:
            err(name, None, "不在 EXPECTED 清单里,需要同步更新 README、STYLE.md 与本脚本")
    present = [n for n in EXPECTED if n in on_disk]
    missing = [n for n in EXPECTED if n not in on_disk]
    if missing:
        (errors if final else warns).append(f"尚未写成: {', '.join(missing)}")

    known = known_test_numbers()
    defined: dict[str, list[tuple[str, int]]] = {}
    refs: list[tuple[str, int, str]] = []
    figs_used: set[str] = set()
    summary: list[str] = []

    for n in present:
        num = n[:2]
        text = (ROOT / n).read_text(encoding="utf-8")
        pr = prose(text)
        lines = text.split("\n")
        rows = header(text)

        if num == "00":
            if BASELINE not in rows.get("代码基线", ""):
                err(n, None, f"总览的「代码基线」行没有 {BASELINE}")
        else:
            for key in HEADER_KEYS:
                if key not in rows:
                    err(n, None, f"头部元信息缺「{key}」行")
            if BASELINE not in rows.get("基线提交", ""):
                err(n, None, f"「基线提交」行没有 {BASELINE}")
            marks = set(re.findall(r"\[(已落地|计划中|推迟)\]", rows.get("状态", "")))
            lack = STATUS_REQUIRED[num] - marks
            if lack:
                err(n, None, f"头部「状态」行缺标记 {sorted(lack)},总览把它归为 {sorted(STATUS_REQUIRED[num])}")
            dep = rows.get("依赖", "")
            mentioned = {p for p in ALL_PKGS if re.search(rf"(?<!\w){p}(?!\w)", dep)}
            mentioned |= {pkg for name, pkg in NAME_TO_PKG.items() if name in dep}
            if "诊断评测" in dep:
                mentioned.add("diagnostic")
            own = OWN_PKG[num]
            extra = mentioned - {own} - ALLOWED_DEPS[own]
            if own != "factory" and extra:
                warn(n, None, f"「依赖」行提到 {sorted(extra)},总览的依赖方向表里没有")
            if pr.count("[计划中]") >= 3 and "施工时可能微调" not in text:
                warn(n, None, "含 3 处以上 [计划中],却没有声明「施工时可能微调」")

        if not 250 <= len(lines) <= 900:
            warn(n, None, f"共 {len(lines)} 行,超出 250 到 900 行的常规范围")

        figs = re.findall(r"<!-- figure:begin ([\w.-]+) -->", text)
        for f in figs:
            figs_used.add(f)
            if not f.startswith(num + "-"):
                err(n, None, f"图 {f} 应以文档编号 {num}- 开头")
        if num != "00" and not 3 <= len(figs) <= 5:
            warn(n, None, f"有 {len(figs)} 张图,常规是 3 到 5 张")

        ids = decision_ids(text, num)
        seen_here: set[str] = set()
        for did, line in ids:
            defined.setdefault(did, []).append((n, line))
            if did in seen_here:
                err(n, line, f"决策编号 {did} 在本文重复定义")
            seen_here.add(did)
            if did.startswith("R") and did[1:].isdigit():
                if not 1 <= int(did[1:]) <= 21:
                    err(n, line, f"{did} 超出 R1 到 R21")
            elif did.split("-")[0] != PREFIX[num]:
                err(n, line, f"决策 {did} 的前缀应为 {PREFIX[num]}-")

        for no, line in enumerate(pr.split("\n"), 1):
            for m in ID_REF.finditer(line):
                refs.append((n, no, m.group(0)))
            for m in re.finditer(r"\bR(\d{1,2})\b", line):
                if not 1 <= int(m.group(1)) <= 21:
                    err(n, no, f"引用了不存在的决策 {m.group(0)}")
            for pat, hint in TERMS_ERR:
                if re.search(pat, line):
                    err(n, no, f"禁用说法「{re.search(pat, line).group(0)}」:{hint}")
            for pat, hint in TERMS_WARN:
                if re.search(pat, line):
                    warn(n, no, f"统一口径「{re.search(pat, line).group(0)}」:{hint}")
            for m in re.finditer(r"\[([^\]\n]{1,10})\]", line):
                inner = m.group(1)
                if re.search(r"落地|计划|推迟|规划|实现|完成|待", inner) and f"[{inner}]" not in MARK_OK:
                    err(n, no, f"状态标记写法不规范: [{inner}],只允许 [已落地] [计划中] [推迟]")
            if re.search(r"[(（](?:已落地|计划中|推迟)[)）]", line):
                warn(n, no, "状态标记要用方括号 [计划中],不要用圆括号")
            if known is not None:
                for m in NUM_PAT.finditer(line):
                    if int(m.group(1)) not in known:
                        warn(n, no, f"测试数 {m.group(1)} 不在实测或计划数里,请核对来源")

        summary.append(f"| {n} | {len(lines)} | {len(figs)} | "
                       f"{'、'.join(sorted(set(re.findall(r'\[(已落地|计划中|推迟)\]', rows.get('状态', ''))))) or '-'} | {len(ids)} |")

    for did, locs in defined.items():
        if not did.startswith("R") and len(locs) > 1:
            for doc, line in locs:
                err(doc, line, f"决策编号 {did} 在多篇文档里定义")
    for doc, line, did in refs:
        if did not in defined:
            warn(doc, line, f"引用的决策 {did} 没有在任何文档的决策表里定义")

    readme = ROOT / "README.md"
    if readme.is_file():
        listed = set(re.findall(r"`(\d\d-[\w-]+\.md)`", readme.read_text(encoding="utf-8")))
        for name in sorted(listed - set(EXPECTED)):
            err("README.md", None, f"列出了不在 EXPECTED 清单里的 {name}")
        for name in EXPECTED:
            if name not in listed:
                err("README.md", None, f"没有列出 {name}")
        for name in sorted(listed & set(missing)):
            (errors if final else warns).append(f"README.md: 列出的 {name} 尚未写成")
    else:
        err("README.md", None, "缺 README.md")

    for svg in sorted((ROOT / "diagrams").glob("*.svg")):
        if svg.stem not in figs_used:
            (errors if final else warns).append(f"diagrams/{svg.name}: 没有任何文档引用这张图")

    if known is None:
        warns.append("没能运行 pytest 收集,跳过了测试数核对")
    for w in warns:
        print("告警", w)
    for e in errors:
        print("错误", e)
    print("\n| 文档 | 行数 | 图 | 状态行标记 | 决策数 |\n|---|---|---|---|---|")
    print("\n".join(summary))
    print(f"\n一致性检查 {len(present)}/{len(EXPECTED)} 篇:{len(errors)} 个错误,{len(warns)} 条告警")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
