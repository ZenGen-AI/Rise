"""RecipeStore 的两个处理器串起来:收录 → 复用两轮 → 第二轮退化 → 自动 deprecated。
另钉住依赖方向:recipestore 只导入 controlplane。"""
import ast
from pathlib import Path

import pytest

from controlplane.paths import recipe_candidate_file, recipe_lineage_file
from controlplane.reconcile import Action
from recipestore.admit import handle_admit_recipe
from recipestore.library import find_recipes, get_recipe
from recipestore.record import handle_record_track
from rs_fixtures import (
    l1,
    make_store,
    write_candidate_checkpoint,
    write_directive,
    write_model,
    write_recipe_candidate,
    write_report,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "recipestore"
R = "recipe-run-002"


def _use(store, run_id, score, *, flags=None, status="effective"):
    write_directive(store, run_id, recipe_ref=R)
    write_candidate_checkpoint(store, run_id, status)
    write_report(store, run_id, scores=[l1("tom", score)], baseline_ref="zengen-v1", flags=flags)
    handle_record_track(store, Action(kind="record_track", target_id=run_id, detail={}))


def test_admit_then_reuse_then_deprecate(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.5)])
    write_recipe_candidate(s, "run-002", "approved")
    handle_admit_recipe(s, Action(kind="admit_recipe", target_id=R, detail={}))
    assert s.read_product(recipe_candidate_file("run-002")).status == "effective"

    _use(s, "run-003", 0.6)
    _use(s, "run-004", 0.7, status="rejected")
    [view] = find_recipes(s)
    assert (view.id, view.n_reuse, view.mean_gain) == (R, 2, pytest.approx(0.15))

    _use(s, "run-005", 0.2, flags=[{"kind": "social_regression", "dimension": "tom"}])
    assert get_recipe(s, R).status == "deprecated"
    assert find_recipes(s) == []
    [view] = find_recipes(s, states=("deprecated",))
    assert view.n_reuse == 3 and view.mean_gain == pytest.approx((0.1 + 0.2 - 0.3) / 3)
    events = s.read_product(recipe_lineage_file("run-002")).payload["events"]
    assert [e["event"] for e in events] == ["admitted", "deprecated"]
    assert events[1]["run_id"] == "run-005"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_recipestore_only_imports_controlplane_among_project_packages():
    project = {p.name for p in SRC.parent.iterdir() if (p / "__init__.py").exists()}
    for path in sorted(SRC.glob("*.py")):
        bad = (_imports(path) & project) - {"controlplane", "recipestore"}
        assert not bad, f"{path.name} imports {sorted(bad)}"


def test_every_recipestore_module_starts_with_the_future_import():
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
