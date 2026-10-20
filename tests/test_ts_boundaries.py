"""依赖方向与 __future__ 头。

trainscheduler 允许导入的项目内模块(设计 §11):controlplane、recipestore(只经 recipes_port)、trainplanner、
inference_bridge(只取 BridgeError)、mindtrainer.training_config 与 mindtrainer.data_spec。
不导入 mindtrainer 的其他模块,不导入 diagnostic、simulator、actio、mindagent、factory。
反方向:controlplane、recipestore、trainplanner、mindtrainer、inference_bridge 都不导入 trainscheduler。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
PROJECT_ALLOWED = {"trainscheduler", "controlplane", "recipestore", "trainplanner", "inference_bridge", "mindtrainer"}
STDLIB_ALLOWED = {"__future__", "collections", "copy", "dataclasses", "hashlib", "json",
                  "math", "re", "typing"}
# mindtrainer 只允许这两个模块:不能把编排器、执行器、派发器拉进调度器
ALLOWED_MINDTRAINER = {"mindtrainer.training_config", "mindtrainer.data_spec"}
FORBIDDEN = {"diagnostic", "simulator", "actio", "mindagent", "factory"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _modules(pkg: str) -> list[Path]:
    return sorted((ROOT / pkg).glob("*.py"))


def test_trainscheduler_imports_only_the_allowed_packages():
    for path in _modules("trainscheduler"):
        for name in _imports(path):
            top = name.split(".")[0]
            assert top in PROJECT_ALLOWED | STDLIB_ALLOWED, f"{path.name} imports {name}"


def test_trainscheduler_imports_only_two_mindtrainer_modules():
    seen = set()
    for path in _modules("trainscheduler"):
        for name in _imports(path):
            if name.split(".")[0] == "mindtrainer":
                assert name in ALLOWED_MINDTRAINER, f"{path.name} imports {name}"
                seen.add(name)
    assert seen == ALLOWED_MINDTRAINER  # 声明与实际一致:两条都真的在用


def test_trainscheduler_never_imports_the_forbidden_packages():
    for path in _modules("trainscheduler"):
        hit = {n for n in _imports(path) if n.split(".")[0] in FORBIDDEN}
        assert not hit, f"{path.name} imports {sorted(hit)}"


def test_nobody_upstream_imports_trainscheduler():
    for pkg in ("controlplane", "recipestore", "trainplanner", "mindtrainer", "inference_bridge"):
        for path in _modules(pkg):
            assert not any(n.split(".")[0] == "trainscheduler" for n in _imports(path)), path


def test_every_module_starts_with_future_annotations():
    for path in _modules("trainscheduler"):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name


def test_recipestore_is_only_imported_through_the_port():
    for path in _modules("trainscheduler"):
        if path.name == "recipes_port.py":
            continue
        assert not any(n.startswith("recipestore") for n in _imports(path)), path.name
