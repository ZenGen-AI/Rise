import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import planner_prompt_file
from trainplanner.context import PlanningContext
from trainplanner.decision import DECISION_KEYS
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, check_template, render_prompt
from trainplanner.templates import (
    PLANNER_V1,
    PLANNER_V1_VERSION,
    TemplateError,
    install_default_template,
    install_template,
    load_template,
)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _commits(store):
    # 空仓库安全:刚 init、还没有提交时 rev-list HEAD 会抛 CalledProcessError;--all 返回 0
    return store._git("rev-list", "--all", "--count").strip()


def test_planner_v1_uses_every_section_of_the_context():
    assert set(check_template(PLANNER_V1)) == set(PLACEHOLDERS) - {"context"}


def test_planner_v1_names_every_decision_key_and_demands_one_json_object():
    for key in DECISION_KEYS:
        assert f'"{key}"' in PLANNER_V1
    assert "exactly one JSON object" in PLANNER_V1
    assert '"not recorded"' in PLANNER_V1 and "not zero and not free" in PLANNER_V1
    for value in ('"new"', '"clone"', '"replay"', '"batch"', '"interact"'):
        assert value in PLANNER_V1


def test_planner_v1_states_the_rules_the_scheduler_enforces():
    assert "null is not a scalar value" in PLANNER_V1
    assert "dotted path" in PLANNER_V1 and "perturbable whitelist" in PLANNER_V1
    # 示例键必须真在计划 5 的默认 perturbable 白名单里
    # (opd_coefficient、sft_steps、grpo_steps、grpo_config.learning_rate)
    assert '"grpo_config.learning_rate"' in PLANNER_V1 and '"sft_steps"' in PLANNER_V1
    assert "interact is off by default" in PLANNER_V1
    assert "larger is worse" in PLANNER_V1 and "smaller is worse" in PLANNER_V1
    assert "reward_hacking_suspected" in PLANNER_V1


def test_planner_v1_renders_against_the_example_context():
    ctx = PlanningContext.from_dict(example_context_dict())
    text = render_prompt(ctx, PLANNER_V1, PLANNER_V1_VERSION).text
    assert "{{" not in text
    assert "recipe-run-001" in text


def test_install_default_writes_a_prompt_template_envelope(store):
    rel = install_default_template(store)
    assert rel == planner_prompt_file(PLANNER_V1_VERSION)
    assert rel == "control-plane/config/prompts/planner-v1.yaml"
    env = store.read_product(rel)
    assert env.type == "prompt_template"
    assert env.id == "prompt-planner-v1"
    assert env.status == "executed"
    assert env.payload == {"version": "v1", "text": PLANNER_V1}


def test_load_template_reads_it_back(store):
    install_default_template(store)
    tpl = load_template(store, PLANNER_V1_VERSION)
    assert (tpl.version, tpl.text) == ("v1", PLANNER_V1)


def test_installing_twice_is_a_no_op(store):
    install_default_template(store)
    before = _commits(store)
    install_default_template(store)
    assert _commits(store) == before


def test_a_different_text_for_the_same_version_is_refused(store):
    install_template(store, "v2", "first {{run_id}}")
    with pytest.raises(TemplateError, match="bump the version"):
        install_template(store, "v2", "second {{run_id}}")
    assert load_template(store, "v2").text == "first {{run_id}}"


def test_an_invalid_template_is_never_written(store):
    with pytest.raises(TemplateError, match="unknown placeholders"):
        install_template(store, "v3", "{{budget}}")
    assert not store.exists(planner_prompt_file("v3"))


def test_loading_a_missing_version_fails_clearly(store):
    with pytest.raises(TemplateError, match="planner prompt template v9 not found"):
        load_template(store, "v9")


@pytest.mark.parametrize("version", ["", "a/b", "..", "v 1"], ids=["empty", "slash", "dotdot", "space"])
@pytest.mark.parametrize("op", ["install", "load"])
def test_a_malformed_version_is_refused(store, op, version):
    with pytest.raises(TemplateError, match="template version must match"):
        if op == "install":
            install_template(store, version, "{{run_id}}")
        else:
            load_template(store, version)
    # 什么都没写:prompts/ 目录都没被建出来,仓库里也没有任何提交
    assert not (store.repo_root / "control-plane/config/prompts").exists()
    assert store._git("rev-list", "--all").strip() == ""


def _put(store, version, *, type_="prompt_template", payload):
    from controlplane.envelope import Envelope, OwnerRef

    env = Envelope(
        id=f"prompt-planner-{version}", type=type_, version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1, payload=payload,
    )
    store.write_product(planner_prompt_file(version), env, "test: hand-written template")


@pytest.mark.parametrize(
    "type_, payload, match",
    [
        ("config", {"version": "v4", "text": "{{run_id}}"}, "expected type prompt_template"),
        ("prompt_template", {"version": "v4"}, "exactly the keys version and text"),
        ("prompt_template", {"version": "v4", "text": "{{run_id}}", "x": 1}, "exactly the keys"),
        ("prompt_template", {"version": "v5", "text": "{{run_id}}"}, "payload version"),
        ("prompt_template", {"version": "v4", "text": "{{oops}}"}, "unknown placeholders"),
    ],
)
def test_a_malformed_template_in_the_tree_is_refused(store, type_, payload, match):
    _put(store, "v4", type_=type_, payload=payload)
    with pytest.raises(TemplateError, match=match):
        load_template(store, "v4")
