import pytest
import yaml

from actio.errors import ActioDataError
from actio.signals import default_registry
from actio.skills import Skill, check_library, route, route_order
from simulator.datamodel import TurnRecord


def _skill(sid, *, trigger=None, priority=0, version=1, level="micro"):
    return Skill(
        id=sid, version=version, level=level,
        trigger=trigger if trigger is not None else {"signal": "emotion_high"},
        policy_instructions=(f"do {sid}",), priority=priority,
    )


def _angry():
    return [TurnRecord(0, "b", "我很生气")]


SKILL_DICT = {
    "id": "skill-calm", "version": 2, "level": "macro", "priority": 5,
    "trigger": {"all": [{"signal": "emotion_high"}, {"not": {"signal": "first_turn"}}]},
    "policy": {"instructions": ["先回应对方的情绪", "再谈事情本身"]},
    "description": "情绪优先",
}


def test_to_dict_from_dict_round_trip_and_exact_shape():
    s = Skill.from_dict(SKILL_DICT)
    assert s.policy_instructions == ("先回应对方的情绪", "再谈事情本身")
    assert s.to_dict() == SKILL_DICT
    assert list(s.to_dict()) == ["id", "version", "level", "priority", "trigger", "policy", "description"]
    assert Skill.from_dict(yaml.safe_load(yaml.safe_dump(s.to_dict()))) == s


def test_priority_and_description_default():
    d = {k: v for k, v in SKILL_DICT.items() if k not in ("priority", "description")}
    s = Skill.from_dict(d)
    assert (s.priority, s.description) == (0, "")


def test_to_dict_does_not_share_the_trigger():
    s = Skill.from_dict(SKILL_DICT)
    s.to_dict()["trigger"]["all"].clear()
    assert s.trigger == SKILL_DICT["trigger"]


@pytest.mark.parametrize("patch", [
    {"id": ""}, {"version": 0}, {"version": 1.0}, {"version": True}, {"level": "meso"},
    {"trigger": {"bad": 1}}, {"policy": {"instructions": []}}, {"policy": {"instructions": [""]}},
    {"policy": {}}, {"priority": "high"}, {"extra": 1},
])
def test_from_dict_rejects_bad_fields(patch):
    with pytest.raises(ActioDataError):
        Skill.from_dict({**SKILL_DICT, **patch})


GOOD_SKILL = dict(id="skill-ok", version=1, level="micro", trigger={"signal": "emotion_high"},
                  policy_instructions=("do it",))


@pytest.mark.parametrize("patch, field", [
    ({"version": "2"}, "version"), ({"version": 0}, "version"), ({"version": True}, "version"),
    ({"level": "meso"}, "level"), ({"trigger": {"bad": 1}}, "trigger"),
    ({"policy_instructions": "先共情"}, "policy.instructions"),   # 字符串会被逐字渲染,必须拒绝
    ({"policy_instructions": ["先共情"]}, "policy.instructions"),  # list 不行,要 tuple
    ({"policy_instructions": ()}, "policy.instructions"), ({"policy_instructions": ("",)}, "policy.instructions"),
    ({"priority": "high"}, "priority"), ({"priority": 1.5}, "priority"), ({"description": None}, "description"),
])
def test_direct_construction_validates_every_field(patch, field):
    with pytest.raises(ActioDataError, match=field.replace(".", r"\.")):
        Skill(**{**GOOD_SKILL, **patch})


def test_direct_construction_accepts_a_valid_skill():
    assert Skill(**GOOD_SKILL).to_dict()["policy"] == {"instructions": ["do it"]}


@pytest.mark.parametrize("bad_id", ["calm", "skill-", "Skill-calm", "mem-calm", "skills-calm"])
def test_skill_id_must_start_with_the_skill_prefix(bad_id):
    with pytest.raises(ActioDataError, match=r"skill\.id: must start with 'skill-'"):
        Skill.from_dict({**SKILL_DICT, "id": bad_id})
    with pytest.raises(ActioDataError, match=r"skill\.id: must start with 'skill-'"):
        _skill(bad_id)


def test_a_skill_prefixed_id_is_accepted():
    assert Skill.from_dict({**SKILL_DICT, "id": "skill-x"}).id == "skill-x"
    assert _skill("skill-x").id == "skill-x"


def test_from_dict_rejects_missing_keys_and_non_dicts():
    with pytest.raises(ActioDataError, match="missing keys"):
        Skill.from_dict({"id": "skill-x"})
    with pytest.raises(ActioDataError):
        Skill.from_dict(["not", "a", "dict"])


def test_route_order_priority_then_version_then_id():
    skills = [_skill("skill-b", priority=1, version=1), _skill("skill-a", priority=1, version=1),
              _skill("skill-c", priority=1, version=3), _skill("skill-d", priority=2, version=1)]
    assert [s.id for s in sorted(skills, key=route_order)] == ["skill-d", "skill-c", "skill-a", "skill-b"]


def test_route_picks_the_first_hit_and_records_the_conflict():
    skills = [_skill("skill-low", priority=0), _skill("skill-high", priority=9),
              _skill("skill-never", trigger={"signal": "no_new_information"})]
    result = route(skills, _angry(), "a", default_registry())
    assert result.candidates == ("skill-high", "skill-low")
    assert result.selected.id == "skill-high"
    assert result.conflict is True
    assert result.signals == ("emotion_high", "first_turn")


def test_route_with_one_hit_has_no_conflict_and_with_none_selects_nothing():
    reg = default_registry()
    one = route([_skill("skill-only")], _angry(), "a", reg)
    assert (one.candidates, one.selected.id, one.conflict) == (("skill-only",), "skill-only", False)
    none = route([_skill("skill-only")], [TurnRecord(0, "b", "好的")], "a", reg)
    assert (none.candidates, none.selected, none.conflict) == ((), None, False)


def test_route_does_not_depend_on_input_order():
    reg = default_registry()
    skills = [_skill("skill-x", version=2), _skill("skill-y", version=2), _skill("skill-z", version=1)]
    assert route(skills, _angry(), "a", reg) == route(list(reversed(skills)), _angry(), "a", reg)


def test_check_library_rejects_duplicates_and_unknown_signals():
    reg = default_registry()
    check_library([_skill("skill-a"), _skill("skill-b")], reg)
    with pytest.raises(ActioDataError, match="duplicate skill id 'skill-a'"):
        check_library([_skill("skill-a"), _skill("skill-a", version=2)], reg)
    with pytest.raises(ActioDataError, match="unknown signal"):
        check_library([_skill("skill-a", trigger={"signal": "custom"})], reg)
    with pytest.raises(ActioDataError):
        check_library([SKILL_DICT], reg)
