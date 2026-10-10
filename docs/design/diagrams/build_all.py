#!/usr/bin/env python3
"""批量生成图。约定:diagrams/fig_<名字>.py 暴露 build() -> svgkit.Canvas,
产出 <名字>.svg 与 <名字>.meta.json,文档里用同名的 figure 标记引用。

  python3 diagrams/build_all.py              生成全部并做卫生检查
  python3 diagrams/build_all.py 名字 ...      只生成指定的图
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import svgkit  # noqa: E402


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("--")]
    built, failed = [], 0
    for src in sorted(HERE.glob("fig_*.py")):
        name = src.stem[len("fig_"):]
        if wanted and name not in wanted:
            continue
        out = HERE / f"{name}.svg"
        try:
            _load(src).build().save(out)
        except Exception as exc:  # 布局错误(溢出、重叠、越界)在这里暴露
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            continue
        problems = svgkit.check(out)
        if problems:
            failed += 1
            print(f"FAIL {name}: {'; '.join(problems)}")
            continue
        built.append(name)
        print(f"ok   {name}")
    if wanted:
        for name in wanted:
            if name not in built and not (HERE / f"fig_{name}.py").exists():
                failed += 1
                print(f"FAIL {name}: 没有 fig_{name}.py")
    print(f"共 {len(built)} 张成功,{failed} 张失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
