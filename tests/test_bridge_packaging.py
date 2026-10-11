import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_controlplane_does_not_import_the_bridge():
    files = sorted((ROOT / "src" / "controlplane").glob("*.py"))
    assert files  # 目录为空时下面的循环一次都不跑,断言恒真
    for path in files:
        assert "inference_bridge" not in _imports(path), path.name


def test_the_bridge_only_depends_on_controlplane_and_the_stdlib():
    allowed_first_party = {"controlplane", "inference_bridge"}
    other_first_party = {p.name for p in (ROOT / "src").iterdir() if p.is_dir()} - allowed_first_party
    files = sorted((ROOT / "src" / "inference_bridge").glob("*.py"))
    assert files
    for path in files:
        assert not (_imports(path) & other_first_party), path.name
