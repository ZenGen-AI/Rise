import pytest

from controlplane import paths


def test_run_dir():
    assert paths.run_dir("run-001") == "control-plane/runs/run-001"


def test_model_file():
    assert paths.model_file(3) == "control-plane/models/zengen-v3.yaml"


def test_backends_config_file():
    assert paths.backends_config_file() == "control-plane/config/backends.yaml"


def test_subdirs_match_spec():
    assert paths.SUBDIRS == [
        "runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios",
        "personas", "diagnostics",
        "recipe_candidates", "skill_library", "memory_library", "agent_rounds",
    ]


@pytest.mark.parametrize(
    ("fn", "args", "expected"),
    [
        ("recipe_candidate_file", ("run-003",), "control-plane/recipe_candidates/recipe-run-003.yaml"),
        ("recipe_dir", ("run-003",), "control-plane/recipes/recipe-run-003"),
        ("recipe_file", ("run-003",), "control-plane/recipes/recipe-run-003/recipe.yaml"),
        ("recipe_lineage_file", ("run-003",), "control-plane/recipes/recipe-run-003/lineage.yaml"),
        ("recipe_track_file", ("run-003",), "control-plane/recipes/recipe-run-003/track_record.yaml"),
        (
            "proposal_file", ("proposal-agent-round-zengen-v3-1",),
            "control-plane/skill_memory_proposals/proposal-agent-round-zengen-v3-1/proposal.yaml",
        ),
        ("skill_file", ("skill-ack-emotion",), "control-plane/skill_library/skill-ack-emotion.yaml"),
        ("memory_file", ("mem-0001",), "control-plane/memory_library/mem-0001.yaml"),
        ("library_state_file", (), "control-plane/library_state.yaml"),
        ("agent_round_file", ("zengen-v3",), "control-plane/agent_rounds/agent-round-zengen-v3.yaml"),
        ("planner_decision_file", ("run-003",), "control-plane/runs/run-003/planner_decision.yaml"),
        ("nomination_file", ("run-003",), "control-plane/runs/run-003/nomination.yaml"),
        ("recipe_track_note_file", ("run-003",), "control-plane/runs/run-003/recipe_track.yaml"),
        ("run_trace_file", ("run-003",), "control-plane/runs/run-003/run_trace.yaml"),
        ("failure_record_file", ("run-003", "plan-1"), "control-plane/runs/run-003/failure-plan-1.yaml"),
        ("scheduler_config_file", (), "control-plane/config/scheduler.yaml"),
        ("training_defaults_file", (), "control-plane/config/training_defaults.yaml"),
        ("planner_prompt_file", ("v1",), "control-plane/config/prompts/planner-v1.yaml"),
    ],
)
def test_closed_loop_paths(fn, args, expected):
    assert getattr(paths, fn)(*args) == expected


def test_run_trace_file_is_where_observe_cost_reads(tmp_path):
    from controlplane.envelope import Envelope, OwnerRef
    from controlplane.gitstore import GitStore
    from controlplane.observe import cost

    store = GitStore(str(tmp_path))
    store.init()
    env = Envelope(
        id="trace-run-1", type="run_trace", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1, links=["run-1"],
        payload={"cost_records": [{"task_id": "t1", "cost_type": "gpu", "gpu_hours": 2.0}]},
    )
    store.write_product(paths.run_trace_file("run-1"), env, "seed")
    assert "total: gpu_hours=2.0 tokens=0" in cost(store, "run-1")


def test_init_creates_the_new_subdirs_and_not_prompts(tmp_path):
    from controlplane.gitstore import GitStore

    GitStore(str(tmp_path)).init()
    root = tmp_path / paths.CONTROL_PLANE_ROOT
    for sub in ("recipe_candidates", "skill_library", "memory_library", "agent_rounds"):
        assert (root / sub).is_dir()
    assert not (root / "config" / "prompts").exists()
