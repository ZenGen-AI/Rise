"""策略经验的边界:依赖方向、文件头、无外部 agent 写入路径。"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FILES = [REPO / "src/trainscheduler/experience.py", REPO / "src/trainscheduler/experience_port.py"]
FORBIDDEN = {"actio", "mindagent", "diagnostic", "simulator", "factory", "inference_bridge",
             "trainplanner"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def test_no_forbidden_imports():
    for path in FILES:
        assert not (_imports(path) & FORBIDDEN), path.name


def test_future_import_comes_before_any_other_statement():
    """仓库的写法是模块 docstring 在前、__future__ 紧随其后,所以断言的是"文档字符串之后的第一个语句"。"""
    for path in FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = list(tree.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]          # 跳过模块文档字符串
        assert body, path.name
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name


def test_recipe_imports_go_through_the_port():
    source = (REPO / "src/trainscheduler/experience_port.py").read_text(encoding="utf-8")
    assert "from trainscheduler.recipes_port import" in source
    assert "from recipestore" not in source
