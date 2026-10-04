import math

import pytest

from mindtrainer.datamodel import RewardedTrajectory
from mindtrainer.grouping import (
    DegenerateGroupError, count_degenerate_groups, group_advantages, group_by_id,
    trajectory_advantages, validate_groups,
)
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord


def _rt(group_id, reward, ep_id):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=2,
    )
    episode = Episode(
        ep_id, "s1", assignment, [TurnRecord(0, "a", "u"), TurnRecord(1, "b", "v")], "max_turns",
    )
    return RewardedTrajectory(episode, group_id, [reward, None], reward)


# ---------------- group_by_id ----------------

def test_group_by_id_keeps_first_seen_group_order_and_in_group_order():
    items = [_rt("g2", 0.1, "e1"), _rt("g1", 0.2, "e2"), _rt("g2", 0.3, "e3"), _rt("g1", 0.4, "e4")]
    groups = group_by_id(items)
    assert list(groups) == ["g2", "g1"]
    assert [r.episode.id for r in groups["g2"]] == ["e1", "e3"]
    assert [r.episode.id for r in groups["g1"]] == ["e2", "e4"]


def test_group_by_id_of_nothing_is_empty():
    assert group_by_id([]) == {}


# ---------------- group_advantages ----------------

def test_advantage_is_reward_minus_the_group_mean():
    assert group_advantages({"g": [1.0, 0.0, 0.5]}) == {"g": [0.5, -0.5, 0.0]}


def test_each_group_has_its_own_independent_baseline():
    out = group_advantages({"g1": [1.0, 0.0], "g2": [0.2, 0.4]})
    assert out["g1"] == [0.5, -0.5]
    assert out["g2"] == pytest.approx([-0.1, 0.1])


@pytest.mark.parametrize("rewards", [[0.3, 0.9, 0.1, 0.5], [2.0, 2.0], [1, 0, 0]])
def test_advantages_within_a_group_sum_to_zero(rewards):
    assert sum(group_advantages({"g": rewards})["g"]) == pytest.approx(0.0, abs=1e-12)


def test_identical_rewards_give_all_zero_advantages():
    assert group_advantages({"g": [0.7, 0.7, 0.7]}) == {"g": [0.0, 0.0, 0.0]}


def test_zero_variance_advantages_are_exactly_zero_for_equivalent_float_spellings():
    assert group_advantages({"g": [0.1 + 0.2, 0.3]}) == {"g": [0.0, 0.0]}


@pytest.mark.parametrize(
    "bad,needle",
    [([], "group 'g' is empty"), ([0.5, math.nan], "non-finite"), ([math.inf, 0.5], "non-finite")],
    ids=["empty", "nan", "inf"],
)
def test_group_advantages_rejects_unusable_groups(bad, needle):
    with pytest.raises(ValueError, match=needle):
        group_advantages({"g": bad})


# ---------------- trajectory_advantages ----------------

def test_trajectory_advantages_line_up_with_input_order_when_groups_interleave():
    items = [
        _rt("g1", 1.0, "e1"), _rt("g2", 0.0, "e2"),
        _rt("g1", 0.0, "e3"), _rt("g2", 0.5, "e4"),
    ]
    assert trajectory_advantages(items) == [0.5, -0.25, -0.5, 0.25]


def test_trajectory_advantages_of_nothing_is_empty():
    assert trajectory_advantages([]) == []


# ---------------- count_degenerate_groups ----------------

def test_zero_variance_groups_are_counted():
    groups = {"flat": [1.0, 1.0, 1.0], "varied": [0.0, 1.0]}
    assert count_degenerate_groups(groups) == 1


def test_equivalent_floating_point_spellings_count_as_zero_variance():
    assert count_degenerate_groups({"g": [0.1 + 0.2, 0.3]}) == 1


def test_a_single_member_group_is_trivially_zero_variance():
    assert count_degenerate_groups({"g": [0.4]}) == 1


def test_no_degenerate_groups_counts_zero():
    assert count_degenerate_groups({"g1": [0.0, 1.0], "g2": [0.2, 0.9]}) == 0


def test_count_degenerate_groups_rejects_an_empty_group():
    with pytest.raises(ValueError, match="group 'g' is empty"):
        count_degenerate_groups({"g": []})


# ---------------- validate_groups ----------------

def test_groups_of_exactly_min_size_pass():
    validate_groups({"g1": [1, 2], "g2": [1, 2, 3]}, min_group_size=2)


def test_too_small_groups_are_listed_by_id_in_sorted_order():
    with pytest.raises(DegenerateGroupError) as exc:
        validate_groups({"gb": [1], "ga": [1], "gc": [1, 2]}, min_group_size=2)
    assert exc.value.group_ids == ["ga", "gb"]
    assert exc.value.min_group_size == 2
    assert "ga" in str(exc.value) and "gb" in str(exc.value) and "gc" not in str(exc.value)


def test_a_larger_min_group_size_flags_groups_that_were_fine_at_two():
    groups = {"g1": [1, 2], "g2": [1, 2, 3]}
    validate_groups(groups, min_group_size=2)
    with pytest.raises(DegenerateGroupError) as exc:
        validate_groups(groups, min_group_size=3)
    assert exc.value.group_ids == ["g1"]


def test_zero_variance_does_not_trigger_the_size_check():
    validate_groups({"g": [0.5, 0.5]}, min_group_size=2)


@pytest.mark.parametrize("bad", [1, 0, -1])
def test_min_group_size_below_two_is_itself_rejected(bad):
    with pytest.raises(ValueError, match="min_group_size must be >= 2"):
        validate_groups({"g": [1, 2]}, min_group_size=bad)
