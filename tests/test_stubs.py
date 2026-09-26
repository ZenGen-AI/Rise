from controlplane.gitstore import GitStore
from controlplane.paths import run_dir
from simulator.stubs import social_simulator_stub, mind_trainer_stub, execute_dispatch


def test_social_simulator_stub_produces_scenario():
    env = social_simulator_stub("run-001", "tom")
    assert env.type == "scenario_pool"
    assert env.status == "executed"
    assert "run-001" in env.links


def test_mind_trainer_stub_produces_pending_candidate():
    env = mind_trainer_stub("run-001", "scen-001")
    assert env.type == "candidate_checkpoint"
    assert env.status == "pending_review"
    assert env.links == ["run-001", "scen-001"]
    # checkpoint 作为大体量引用，不入产物本体
    assert env.payload["checkpoint_ref"]["sha256"]
    assert env.payload["checkpoint_ref"]["fetch_hint"] is None


def test_execute_dispatch_writes_scenario_and_candidate(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    execute_dispatch(store, "run-001", "tom")
    files = store.list_products(run_dir("run-001"))
    kinds = {store.read_product(f).type for f in files}
    assert "scenario_pool" in kinds
    assert "candidate_checkpoint" in kinds
    # 候选处于待审，等待人工审核门①
    cand = next(store.read_product(f) for f in files if store.read_product(f).type == "candidate_checkpoint")
    assert cand.status == "pending_review"
