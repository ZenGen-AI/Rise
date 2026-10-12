import pytest

from controlplane.paths import recipe_lineage_file, recipe_track_file
from controlplane.states import InvalidTransitionError
from recipestore.library import RecipeNotFoundError, find_recipes, get_recipe, read_track_entries
from recipestore.track import (
    DEPRECATE,
    KEEP,
    append_track_entry,
    deprecate,
    deprecated_by,
    lineage_envelope,
    verdict,
)
from rs_fixtures import agent_rsi_payload, make_store
from rs_recipes import entry, put_recipe

R = "recipe-run-003"


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    put_recipe(s, "run-003", entries=[])
    return s


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD").strip())


def test_append_adds_an_entry(store):
    assert append_track_entry(store, R, entry("run-004", 0.1)) is None
    assert read_track_entries(store, R) == [entry("run-004", 0.1)]


def test_append_is_idempotent_by_run_id_and_the_first_write_wins(store):
    append_track_entry(store, R, entry("run-004", 0.1))
    before = _commits(store)
    append_track_entry(store, R, entry("run-004", 0.9, regressed=True))
    assert read_track_entries(store, R) == [entry("run-004", 0.1)]
    assert _commits(store) == before


def test_append_keeps_order(store):
    for i, g in enumerate([0.1, None, -0.2]):
        append_track_entry(store, R, entry(f"run-01{i}", g))
    assert [e["run_id"] for e in read_track_entries(store, R)] == ["run-010", "run-011", "run-012"]


def test_append_creates_the_track_file_when_it_is_missing(tmp_path):
    s = make_store(tmp_path)
    put_recipe(s, "run-003")  # 没有 track_record
    append_track_entry(s, R, entry("run-004", None))
    assert s.exists(recipe_track_file("run-003"))
    assert read_track_entries(s, R) == [entry("run-004", None)]


def test_append_to_a_missing_recipe_raises(store):
    with pytest.raises(RecipeNotFoundError):
        append_track_entry(store, "recipe-run-404", entry("run-004", 0.1))


@pytest.mark.parametrize("bad", [
    {"run_id": "run-004"},
    dict(entry("run-004", 0.1), extra=1),
    entry("", 0.1),
    entry("run-004", "0.1"),
    entry("run-004", True),
    dict(entry("run-004", 0.1), promoted="yes"),
    dict(entry("run-004", 0.1), regressed=None),
    entry("run-004", 0.1, used_as="new"),
    "run-004",
])
def test_a_malformed_entry_is_refused(store, bad):
    with pytest.raises(ValueError):
        append_track_entry(store, R, bad)
    assert read_track_entries(store, R) == []


def test_verdict_keeps_when_the_latest_entry_did_not_regress():
    assert verdict([]) == KEEP
    assert verdict([entry("a", 0.1)]) == KEEP
    assert verdict([entry("a", 0.1, regressed=True), entry("b", 0.0)]) == KEEP


def test_verdict_deprecates_when_the_latest_entry_regressed():
    assert verdict([entry("a", 0.1), entry("b", -0.1, regressed=True)]) == DEPRECATE
    assert verdict([entry("a", None, regressed=True)]) == DEPRECATE


def test_deprecate_moves_the_recipe_and_records_why_in_the_lineage(store):
    assert deprecate(store, R, reason="regressed on run-004", run_id="run-004") is True
    assert get_recipe(store, R).status == "deprecated"
    lineage = store.read_product(recipe_lineage_file("run-003"))
    assert (lineage.type, lineage.status) == ("recipe_lineage", "executed")
    assert lineage.payload["events"] == [{"event": "deprecated", "run_id": "run-004", "reason": "regressed on run-004"}]
    assert find_recipes(store) == []
    assert [v.id for v in find_recipes(store, states=("deprecated",))] == [R]


def test_deprecate_appends_to_an_existing_lineage(store):
    admitted = {"recipe_id": R, "source_run_id": "run-003", "admitted_from": R,
                "events": [{"event": "admitted", "run_id": "run-003"}]}
    store.write_product(recipe_lineage_file("run-003"), lineage_envelope(R, admitted), "t")
    deprecate(store, R, reason="r", run_id="run-004")
    payload = store.read_product(recipe_lineage_file("run-003")).payload
    assert payload["admitted_from"] == R
    assert [e["event"] for e in payload["events"]] == ["admitted", "deprecated"]


def test_deprecating_twice_changes_nothing(store):
    deprecate(store, R, reason="r", run_id="run-004")
    before = _commits(store)
    assert deprecate(store, R, reason="other", run_id="run-005") is False
    assert _commits(store) == before
    events = store.read_product(recipe_lineage_file("run-003")).payload["events"]
    assert events == [{"event": "deprecated", "run_id": "run-004", "reason": "r"}]


def test_a_crash_between_lineage_and_status_converges_on_rerun(store, monkeypatch):
    real = store.write_product
    calls = []

    def flaky(rel, env, msg):
        calls.append(rel)
        if len(calls) == 2:
            raise OSError("disk full")
        real(rel, env, msg)

    monkeypatch.setattr(store, "write_product", flaky)
    with pytest.raises(OSError):
        deprecate(store, R, reason="r", run_id="run-004")
    monkeypatch.setattr(store, "write_product", real)
    assert get_recipe(store, R).status == "effective"  # 只写了 lineage
    assert deprecate(store, R, reason="r", run_id="run-004") is True
    assert get_recipe(store, R).status == "deprecated"
    assert len(store.read_product(recipe_lineage_file("run-003")).payload["events"]) == 1


def test_only_an_effective_recipe_can_be_deprecated(tmp_path):
    s = make_store(tmp_path)
    put_recipe(s, "run-003", status="superseded")
    with pytest.raises(InvalidTransitionError):
        deprecate(s, R, reason="r", run_id="run-004")


def test_deprecate_needs_a_reason(store):
    with pytest.raises(ValueError):
        deprecate(store, R, reason="", run_id="run-004")


def test_deprecated_by_reads_the_triggering_run_from_the_lineage(store):
    assert deprecated_by(store, R) is None
    deprecate(store, R, reason="r", run_id="run-004")
    assert deprecated_by(store, R) == "run-004"


def test_an_agent_rsi_recipe_takes_entries_and_a_verdict_like_any_other(tmp_path):
    # 已知限制 12:append_track_entry、verdict、deprecate 不看 recipe_type
    s = make_store(tmp_path)
    rid = put_recipe(s, "run-007", payload=agent_rsi_payload(), entries=[])
    append_track_entry(s, rid, entry("run-008", 0.2))
    append_track_entry(s, rid, entry("run-009", None, regressed=True))
    entries = read_track_entries(s, rid)
    assert [e["run_id"] for e in entries] == ["run-008", "run-009"]
    assert verdict(entries) == DEPRECATE
    assert deprecate(s, rid, reason="regressed", run_id="run-009") is True
    assert get_recipe(s, rid).payload["recipe_type"] == "agent_rsi"
    assert [v.id for v in find_recipes(s, states=("deprecated",), recipe_type="agent_rsi")] == [rid]
