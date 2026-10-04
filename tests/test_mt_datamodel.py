import json

import pytest
import yaml

from mindtrainer.datamodel import RewardedTrajectory, TrajectoryBatch
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord


def _episode(ep_id="ep-1", n_turns=4):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=n_turns,
    )
    turns = [
        TurnRecord(
            i, "a" if i % 2 == 0 else "b", f"u{i}",
            inner_thought="thought" if i % 2 == 0 else None,
        )
        for i in range(n_turns)
    ]
    return Episode(ep_id, "s1", assignment, turns, "max_turns")


# ---------------- TrajectoryBatch ----------------

def test_batch_round_trip():
    batch = TrajectoryBatch(
        [_episode("ep-1"), _episode("ep-2")],
        {"manifest_id": "dsm-run-1-sft", "episode_count": 2},
    )
    assert TrajectoryBatch.from_dict(batch.to_dict()) == batch


def test_batch_metadata_defaults_to_empty_and_is_not_shared_between_instances():
    a, b = TrajectoryBatch([_episode()]), TrajectoryBatch([_episode()])
    a.metadata["x"] = 1
    assert b.metadata == {}


def test_batch_to_dict_does_not_alias_metadata():
    batch = TrajectoryBatch([_episode()], {"failed_attempts": [{"rollout": 0}]})
    batch.to_dict()["metadata"]["failed_attempts"].append({"rollout": 99})
    assert batch.metadata["failed_attempts"] == [{"rollout": 0}]


def test_batch_to_dict_is_plain_data_that_survives_json_and_yaml():
    d = TrajectoryBatch([_episode()], {"manifest_hash": "ab" * 32}).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d


# ---------------- RewardedTrajectory ----------------

def test_rewarded_round_trip_keeps_none_for_non_trained_turns():
    rewarded = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75)
    back = RewardedTrajectory.from_dict(rewarded.to_dict())
    assert back == rewarded
    assert back.per_turn_rewards == [0.5, None, 1.0, None]


def test_rewarded_to_dict_survives_json_with_none_as_null():
    d = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d


@pytest.mark.parametrize("rewards", [[0.5, None, 1.0], [0.5, None, 1.0, None, 0.1]], ids=["too_few", "too_many"])
def test_rewarded_rejects_rewards_not_aligned_with_turns(rewards):
    with pytest.raises(ValueError, match="ep-1"):
        RewardedTrajectory(_episode(), "g1", rewards, 0.5)


def test_from_dict_enforces_the_same_alignment():
    d = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75).to_dict()
    d["per_turn_rewards"].pop()
    with pytest.raises(ValueError, match="per_turn_rewards"):
        RewardedTrajectory.from_dict(d)


def test_rewarded_to_dict_does_not_alias_the_reward_list():
    rewarded = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75)
    rewarded.to_dict()["per_turn_rewards"][0] = 99.0
    assert rewarded.per_turn_rewards[0] == 0.5
