#!/usr/bin/env python3
"""检查脚本的自测:用一篇最小合规文档加一组反例,验证 check_docs.py 的每条规则。
改动 tools/ 下任何脚本之后运行,全部 PASS 才算数。  python3 docs/design/tools/selftest.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_docs  # noqa: E402
import symbol_index  # noqa: E402

# "只在计划里"的样例符号每次现取:写死一个名字,它一旦落地进代码,用例就会无声地过期。
PLAN_ONLY = next(n for n in sorted(symbol_index._plans() - symbol_index._code())
                 if symbol_index.CAMEL.match(n) and symbol_index._candidate(n) == n
                 and n not in symbol_index.ALLOW)

HEAD = ("# 测试文档\n\n| 项 | 内容 |\n|---|---|\n| 一句话 | 测试 |\n| 状态 | [已落地] |\n| 所在层 | 控制层 |\n"
        "| 代码位置 | `src/controlplane` |\n| 基线提交 | `8dd295b` |\n| 依赖 | 无 |\n| 被依赖 | 无 |\n"
        "| 对应实施计划 | 无 |\n\n")
BODY = "## 0. 系统速览\n\n<!-- shared:begin context-block -->\n<!-- shared:end context-block -->\n\n"
for _num, _title in check_docs.REQUIRED[1:]:
    BODY += f"## {_num}. {_title}\n\n无。\n\n"
GOOD = HEAD + BODY + "## 附 本文术语\n\n无。\n"


def run(text):
    try:
        text = check_docs.sync_docs.sync_text(text)
    except SystemExit:
        pass
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "10-scratch.md"
        f.write_text(text, encoding="utf-8")
        errors, warns = [], []
        check_docs.check_one(f, errors, warns)
    return errors, warns


def has(items, needle):
    return any(needle in x for x in items)


CASES = [
    ("最小合规文档通过", GOOD, lambda e, w: not e and not w),
    ("缺章节", GOOD.replace("## 5. 接口契约", "## 5. 接口"), lambda e, w: has(e, "缺章节")),
    ("跳转措辞", GOOD + "\n详见某处。\n", lambda e, w: has(e, "跳转")),
    ("引用计划编号", GOOD + "\n见计划 5。\n", lambda e, w: has(e, "实施计划编号")),
    ("Mermaid", GOOD + "\n```mermaid\ngraph TD\n```\n", lambda e, w: has(e, "Mermaid")),
    ("ASCII 框图", GOOD + "\n┌──┐\n", lambda e, w: has(e, "ASCII")),
    ("不存在的路径", GOOD + "\n见 `src/nonexistent/foo.py`。\n", lambda e, w: has(e, "路径不存在")),
    ("计划中的路径", GOOD + "\n`src/actio/skills.py` [计划中]\n", lambda e, w: not e),
    ("存在的路径", GOOD + "\n`src/controlplane/daemon.py` 是守护进程\n", lambda e, w: not e),
    ("图占位", GOOD + "\n<!-- figure:begin 00-states -->\n<!-- figure:end 00-states -->\n", lambda e, w: not e),
    ("缺图文件", GOOD + "\n<!-- figure:begin 99-nope -->\n<!-- figure:end 99-nope -->\n", lambda e, w: has(e, "缺图")),
    ("符号在代码里", GOOD + "\n`reconcile_once()` 与 `TreeState`\n", lambda e, w: not e and not has(w, "符号告警")),
    ("符号只在计划里且未标", GOOD + f"\n`{PLAN_ONLY}` 是视图\n", lambda e, w: has(w, "缺 [计划中]")),
    ("符号只在计划里且已标", GOOD + f"\n`{PLAN_ONLY}` [计划中]\n", lambda e, w: not has(w, "符号告警")),
    ("符号哪里都没有", GOOD + "\n`FooBarBaz()`\n", lambda e, w: has(w, "都找不到")),
    ("外部点号调用不查", GOOD + "\n`json.dumps()`\n", lambda e, w: not has(w, "符号告警")),
    ("协议消息名", GOOD + "\n`SubmitTurn` 与 `CreateSession`\n", lambda e, w: not has(w, "符号告警")),
]

if __name__ == "__main__":
    bad = 0
    for name, text, ok in CASES:
        e, w = run(text)
        passed = ok(e, w)
        bad += not passed
        print(("PASS " if passed else "FAIL ") + name)
        if not passed:
            print("     errors:", e[:3], "\n     warns:", w[:3])
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "diagrams"))
    import svgkit  # noqa: E402
    FIG_CASES = [("图里的进展字样被拒绝", "计划中的模块", True), ("图里的目标描述通过", "调和守护进程", False)]
    for name, title, want_fail in FIG_CASES:
        c = svgkit.Canvas(400, 200, title="t", desc="d")
        c.box(20, 20, 200, title)
        try:
            c.to_svg()
            failed = False
        except ValueError:
            failed = True
        passed = failed == want_fail
        bad += not passed
        print(("PASS " if passed else "FAIL ") + name)
    print(f"\n{len(CASES) + len(FIG_CASES) - bad}/{len(CASES) + len(FIG_CASES)} 通过")
    sys.exit(1 if bad else 0)
