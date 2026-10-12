import pytest

from recipestore.library import (
    RecipeNotFoundError,
    RecipeView,
    compare,
    find_recipes,
    get_recipe,
    read_track_entries,
    track_summary,
)
from rs_fixtures import agent_rsi_payload, interact_payload, make_store
from rs_recipes import entry, put_recipe


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def test_get_recipe_reads_the_envelope(store):
    put_recipe(store, "run-003")
    env = get_recipe(store, "recipe-run-003")
    assert (env.id, env.type, env.status) == ("recipe-run-003", "recipe", "effective")


@pytest.mark.parametrize("bad", ["recipe-run-404", "run-003", ""])
def test_get_recipe_raises_for_a_missing_or_malformed_id(store, bad):
    put_recipe(store, "run-003")
    with pytest.raises(RecipeNotFoundError):
        get_recipe(store, bad)


def test_mean_gain_counts_only_entries_with_a_gain():
    entries = [entry("r1", 0.2), entry("r2", None), entry("r3", 0.4)]
    assert track_summary(entries) == (pytest.approx(0.3), 3)


def test_mean_gain_is_none_without_any_gain():
    assert track_summary([entry("r1", None)]) == (None, 1)
    assert track_summary([]) == (None, 0)


def test_a_boolean_gain_is_not_a_number():
    assert track_summary([entry("r1", True)]) == (None, 1)


def test_find_recipes_returns_views_with_the_track_summary(store):
    put_recipe(store, "run-003", entries=[entry("run-004", 0.1), entry("run-005", None)])
    [view] = find_recipes(store)
    assert isinstance(view, RecipeView)
    assert (view.id, view.recipe_type, view.dimension, view.status) == ("recipe-run-003", "batch", "tom", "effective")
    assert (view.mean_gain, view.n_reuse) == (pytest.approx(0.1), 2)
    assert view.payload["source_run_id"] == "run-003"


def test_a_recipe_without_a_track_file_has_no_reuse(store):
    put_recipe(store, "run-003")
    [view] = find_recipes(store)
    assert (view.mean_gain, view.n_reuse) == (None, 0)
    assert read_track_entries(store, "recipe-run-003") == []


def test_track_order_is_mean_gain_then_reuse_then_id(store):
    put_recipe(store, "run-001", entries=[entry("x1", 0.1)])
    put_recipe(store, "run-002", entries=[entry("x2", 0.3)])
    put_recipe(store, "run-003", entries=[entry("x3", 0.1), entry("x4", None)])  # 同 0.1,复用更多
    put_recipe(store, "run-004", entries=[])  # 没有增益数据,排最后
    put_recipe(store, "run-000", entries=[entry("x5", 0.1)])  # 同 0.1 同次数,按 id
    assert [v.id for v in find_recipes(store)] == [
        "recipe-run-002", "recipe-run-003", "recipe-run-000", "recipe-run-001", "recipe-run-004",
    ]


def test_a_negative_mean_gain_still_ranks_above_no_data(store):
    put_recipe(store, "run-001", entries=[entry("x1", -0.2)])
    put_recipe(store, "run-002", entries=[entry("x2", None)])
    assert [v.id for v in find_recipes(store)] == ["recipe-run-001", "recipe-run-002"]


def test_id_order(store):
    put_recipe(store, "run-002", entries=[entry("x", 0.9)])
    put_recipe(store, "run-001")
    assert [v.id for v in find_recipes(store, order="id")] == ["recipe-run-001", "recipe-run-002"]


def test_an_unknown_order_is_refused(store):
    with pytest.raises(ValueError, match="order must be one of"):
        find_recipes(store, order="gain")


def test_filters(store):
    put_recipe(store, "run-001", dimension="tom")
    put_recipe(store, "run-002", dimension="emotion")
    put_recipe(store, "run-003", dimension="tom", payload=interact_payload())
    put_recipe(store, "run-004", dimension="tom", status="deprecated")
    assert [v.id for v in find_recipes(store, dimension="tom", order="id")] == ["recipe-run-001", "recipe-run-003"]
    assert [v.id for v in find_recipes(store, recipe_type="interact")] == ["recipe-run-003"]
    assert [v.id for v in find_recipes(store, states=("deprecated",))] == ["recipe-run-004"]
    assert len(find_recipes(store, states=("effective", "deprecated"))) == 4


def test_an_empty_store_has_no_recipes(store):
    assert find_recipes(store) == []


def test_stray_directories_are_ignored(store):
    put_recipe(store, "run-001")
    (store.repo_root / "control-plane" / "recipes" / "notes").mkdir()
    (store.repo_root / "control-plane" / "recipes" / "recipe-run-009").mkdir()  # 没有 recipe.yaml
    assert [v.id for v in find_recipes(store)] == ["recipe-run-001"]


def test_compare_lists_config_differences_and_the_track(store):
    put_recipe(store, "run-001", entries=[entry("x1", 0.2)])
    other = interact_payload()
    put_recipe(store, "run-002", payload=other, entries=[entry("x2", None), entry("x3", None)])
    out = compare(store, "recipe-run-001", "recipe-run-002")
    assert (out["a"], out["b"]) == ("recipe-run-001", "recipe-run-002")
    diff = out["config_diff"]
    assert diff["recipe_type"] == {"a": "batch", "b": "interact"}
    assert diff["training_config.mode"] == {"a": "batch", "b": "interact"}
    assert diff["scenario_ids"] == {"a": "<missing>", "b": ["scen-1"]}
    assert diff["stage_manifests.sft.id"] == {"a": "dsm-run-003-sft", "b": "<missing>"}
    assert "source_run_id" not in diff and not any(k.startswith(("result", "repro")) for k in diff)
    assert out["track"] == {
        "recipe-run-001": {"status": "effective", "mean_gain": pytest.approx(0.2), "n_reuse": 1},
        "recipe-run-002": {"status": "effective", "mean_gain": None, "n_reuse": 2},
    }


def test_two_identical_configs_have_no_difference(store):
    put_recipe(store, "run-001")
    put_recipe(store, "run-002")
    assert compare(store, "recipe-run-001", "recipe-run-002")["config_diff"] == {}


def test_compare_raises_for_a_missing_recipe(store):
    put_recipe(store, "run-001")
    with pytest.raises(RecipeNotFoundError):
        compare(store, "recipe-run-001", "recipe-run-404")


def test_an_agent_rsi_recipe_is_found_and_ranked_like_any_other(store):
    # 已知限制 12:agent_rsi 配方不自动提名,但检索照常工作
    put_recipe(store, "run-001", entries=[entry("x1", 0.1)])
    put_recipe(store, "run-002", payload=agent_rsi_payload(), entries=[entry("x2", 0.3), entry("x3", None)])
    [view] = find_recipes(store, recipe_type="agent_rsi")
    assert (view.id, view.recipe_type, view.mean_gain, view.n_reuse) == (
        "recipe-run-002", "agent_rsi", pytest.approx(0.3), 2)
    assert view.payload["bound_model_id"] == "zengen-v2"
    assert [v.id for v in find_recipes(store)] == ["recipe-run-002", "recipe-run-001"]
