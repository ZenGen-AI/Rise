"""跨计划一致性核对(任务 109 Step 4):同一事实在两个计划里各有一处实现时,在真实产物上比对。"""
import pytest

from controlplane.daemon import apply_action, read_tree_state
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, model_file, planner_decision_file
from controlplane.reconcile import Action
from diagnostic.evaluator import resolve_baseline
from factory.evaluation import build_mock_evaluator
from factory_fixtures import closed_loop_repo
from mindagent.rounds import _read_model
from recipestore.admit import has_repro_evidence
from recipestore.gain import baseline_run_of
from trainplanner.backends import HeuristicBackend
from trainscheduler.plan_handler import make_plan_handler

# 审核门③"证据在场"的共享用例表。TypeScript 一侧的同一张表在计划 8 任务 96 的
# cli/test/gates.test.ts("approves a recipe candidate ..." 三条与 "refuses a recipe candidate whose repro is ..."
# 的循环);两侧任何一侧改了,另一侧照改(本计划「跨计划核对」一节列出了这张表)。
REPRO_CASES = [
    ({"errors": [], "warnings": ["interact runs are only highly similar"]}, True),
    ({"errors": [], "warnings": []}, True),
    ({"errors": ["unanchored ref"], "warnings": []}, True),
    (None, False), ("<missing>", False), ({}, False), ([], False), ("ok", False), ({"x": 1}, False),
    ({"errors": []}, False), ({"errors": {}, "warnings": []}, False),
    ({"errors": [], "warnings": None}, False), ({"errors": [], "warnings": "w"}, False),
]


@pytest.mark.parametrize("repro,accepted", REPRO_CASES)
def test_has_repro_evidence_matches_the_shared_table(repro, accepted):
    payload = {"recipe_type": "batch"} if repro == "<missing>" else {"recipe_type": "batch", "repro": repro}
    assert has_repro_evidence(payload) is accepted


def _promoted(tmp_path, run_id, n):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    store.write_product(candidate_checkpoint_file(run_id), Envelope(
        id=f"cand-{run_id}", type="candidate_checkpoint", version=1, created_at="t", created_by=OwnerRef(),
        status="approved", schema_version=1, payload={}), "seed approved candidate")
    apply_action(store, Action("promote_candidate", f"cand-{run_id}", {"run_id": run_id, "model_version": n}))
    return store


@pytest.mark.parametrize("run_id,n", [("run-001", 1), ("run-010", 2), ("run-xyz", 7)])
def test_plan_3_and_plan_8_agree_on_which_run_produced_a_model(tmp_path, run_id, n):
    """计划 3 的 baseline_run_of 取"cand-<x> 也在 links 里"的那一项;计划 8 的 read_tree_state
    (与计划 7 的 _read_model)取 links[1]。在 promote_candidate 写出的真实模型产物上两者相同。"""
    store = _promoted(tmp_path, run_id, n)
    links = store.read_product(model_file(n)).links
    assert baseline_run_of(store, f"zengen-v{n}") == (run_id, "ok")
    (view,) = read_tree_state(store, enabled=frozenset({"start_agent_round"})).models
    assert view.run_id == links[1] == run_id
    assert _read_model(store, f"zengen-v{n}")[1] == run_id


def test_plan_8_evaluator_baseline_names_the_same_run(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    build_mock_evaluator(store)(store, "run-001", "cand-run-001")
    store.write_product(candidate_checkpoint_file("run-001"), Envelope(
        id="cand-run-001", type="candidate_checkpoint", version=1, created_at="t", created_by=OwnerRef(),
        status="approved", schema_version=1, payload={}), "seed approved candidate")
    apply_action(store, Action("promote_candidate", "cand-run-001", {"run_id": "run-001", "model_version": 1}))
    ref, scores = resolve_baseline(store, "run-002", "factory-bench-v1", backends="mock")
    assert ref == "zengen-v1" and scores
    assert baseline_run_of(store, ref)[0] == "run-001"


def test_a_full_pass_report_can_be_planned(tmp_path):
    """设计 R21 / 计划 5 Ruling 23 的回归:现有 diagnostic.error_classifier 对"基题与变体都答对"的变体族给
    空标签,真实报告里出现 error_type_distribution == {"": 1.0};计划 5 的 assemble_context 丢掉空标签再交给
    计划 4 的 PlanningContext,所以全答对的报告也能被规划,计划 4 收到的分布是空映射。"""
    from diagnostic.evaluator import make_diagnostic_evaluator
    from diagnostic.scorer import MockJudgeBackend
    from diagnostic.someval_adapter import MockSoMEvalRunner
    from factory.defaults import DEFAULT_EVALUATOR_CONFIG as C, DEFAULT_ITEM_POOL
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    right = MockSoMEvalRunner(fixed={i.id: [i.reference_answer] * C["sampling_k"] for i in DEFAULT_ITEM_POOL.items})
    make_diagnostic_evaluator(taxonomy_version=C["taxonomy_version"], item_pool_version=C["item_pool_version"],
                              someval_runner=right, judge_backend=MockJudgeBackend(model_id="j", fixed={}),
                              benchmark_version=C["benchmark_version"])(store, "run-001", "cand-run-001")
    report = store.read_product("control-plane/runs/run-001/diagnostic_report.yaml").payload
    assert [p["error_type_distribution"] for p in report["error_profiles"]] == [{"": 1.0}]  # 真实形状
    store.write_product(candidate_checkpoint_file("run-001"), Envelope(
        id="cand-run-001", type="candidate_checkpoint", version=1, created_at="t", created_by=OwnerRef(),
        status="effective", schema_version=1, payload={}), "seed effective candidate")

    seen = []

    class Recording(HeuristicBackend):
        def decide(self, packed, *, run_id, task_id):
            seen.append([p.error_type_distribution for p in packed.context.diagnostic.error_profiles])
            return super().decide(packed, run_id=run_id, task_id=task_id)

    make_plan_handler(backend=Recording())(store, Action("plan_next_round", "run-001", {}))
    d = store.read_product(planner_decision_file("run-001"))
    assert d.status == "executed", d.payload.get("failure")
    assert seen and seen[0] == [{}]


def test_recipe_types_agree_between_recipestore_and_trainplanner():
    """计划 3 的 recipestore.validate.RECIPE_TYPES 与计划 4 的 trainplanner.context.RECIPE_TYPES 是两份字面量
    (两个包互不导入,计划 4 已知限制第 9 条)。这里是唯一同时导入两边的地方。"""
    from recipestore.validate import RECIPE_TYPES as recipestore_types
    from trainplanner.context import RECIPE_TYPES as trainplanner_types
    assert set(recipestore_types) == set(trainplanner_types), (
        f"recipestore.validate.RECIPE_TYPES {sorted(recipestore_types)} != "
        f"trainplanner.context.RECIPE_TYPES {sorted(trainplanner_types)}: 两边都要改,保持同一组配方类型"
    )
    assert len(set(recipestore_types)) == len(recipestore_types)
    assert len(set(trainplanner_types)) == len(trainplanner_types)


NEW_PACKAGES = ("inference_bridge", "recipestore", "trainplanner", "trainscheduler", "actio", "mindagent", "factory")


def test_controlplane_imports_none_of_the_new_packages():
    """设计 §11 的依赖方向:controlplane 不导入闭环集成新增的任何包。各计划的边界测试只查自己包往外的导入,
    本计划是唯一同时看得见所有新包的地方。只看绝对导入(相对导入留在 controlplane 内部)。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "controlplane"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in NEW_PACKAGES:
                    offenders.append(f"{path.relative_to(root.parent)}:{node.lineno} imports {top}")
    assert sorted(root.rglob("*.py")), "src/controlplane not found"
    assert not offenders, "controlplane must not import the new packages: " + "; ".join(offenders)


RUN_NAMES = ["run-1", "run-001", "run-999", "run-1000", "run-٥٠", "run-1b", "run-", "RUN-001", "run-+1",
             "scratch"]


@pytest.mark.parametrize("name", RUN_NAMES)
def test_run_number_parsers_agree(name):
    """计划 5 的 trainscheduler.run_ids.run_number 与计划 8 的 controlplane.reconcile._RUN_ID 各写了一份 run 编号
    正则(设计 §3.2:^run-([0-9]+)$)。两边对同一个名字必须同时认或同时不认,认出的编号相等;
    "run-٥٠"(阿拉伯-印度数字)用来钉住 [0-9] 与 \\d 的分叉。"""
    from controlplane.reconcile import _RUN_ID
    from trainscheduler.run_ids import run_number

    number = run_number(name)
    match = _RUN_ID.match(name)
    assert (number is not None) == (match is not None), (
        f"{name!r}: trainscheduler.run_ids.run_number -> {number!r}, controlplane.reconcile._RUN_ID matches: "
        f"{match is not None}; 两边都要改,保持同一个 run 编号口径(设计 §3.2)"
    )
    if match is not None:
        assert number == int(match.group(1)), f"{name!r}: {number} != {int(match.group(1))}; 两边都要改"


# 设计 §11 的分层规则:{源包: 不得导入的包}
LAYER_RULES = {
    "controlplane": ("diagnostic", "mindtrainer"),
    "mindtrainer": ("diagnostic",),
    "diagnostic": ("mindtrainer",),
}


def _top_level_imports(path):
    """一个 .py 文件里全部绝对导入的 (行号, 顶层包名)。相对导入留在包内,不看。"""
    import ast
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module.split(".")[0]


def test_layer_rules_hold():
    """设计 §11 的三条分层规则,在真实源码上常驻检查:controlplane 不导入 diagnostic 与 mindtrainer,
    mindtrainer 不导入 diagnostic,diagnostic 不导入 mindtrainer。各计划里原先只有一次性 grep。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for package, forbidden in LAYER_RULES.items():
        files = sorted((src / package).rglob("*.py"))
        assert files, f"src/{package} not found"
        for path in files:
            for lineno, top in _top_level_imports(path):
                if top in forbidden:
                    offenders.append(f"{path.relative_to(src)}:{lineno} imports {top}")
    assert not offenders, "layer rules (design §11) violated: " + "; ".join(offenders)
