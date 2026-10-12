import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ALLOWED = {"trainplanner", "controlplane", "inference_bridge"}
STDLIB_OK = {"__future__", "collections", "copy", "dataclasses", "json", "math", "re", "typing"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_trainplanner_imports_only_controlplane_inference_bridge_and_stdlib():
    for path in sorted((SRC / "trainplanner").glob("*.py")):
        extra = _imports(path) - ALLOWED - STDLIB_OK
        assert not extra, f"{path.name} imports {sorted(extra)}"


def test_controlplane_does_not_import_trainplanner():
    for path in sorted((SRC / "controlplane").glob("*.py")):
        assert "trainplanner" not in _imports(path), path.name


def test_every_trainplanner_module_starts_with_future_annotations():
    for path in sorted((SRC / "trainplanner").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
