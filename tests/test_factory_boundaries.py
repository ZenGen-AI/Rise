"""factory 的边界:没有任何包导入 factory;factory 每个模块以 __future__ 开头。"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _imports(path):
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_no_other_package_imports_factory():
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).parts[0] == "factory":
            continue
        bad = [n for n in _imports(path) if n == "factory" or n.startswith("factory.")]
        assert not bad, f"{path.relative_to(SRC)} imports {bad}"


def test_every_factory_module_starts_with_future_annotations():
    for path in sorted((SRC / "factory").glob("*.py")):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
