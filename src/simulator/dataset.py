"""数据集清单与分组数据集生产方。

spec §5.1 要求配方用 sha256 锚定数据集。清单是**生产方契约**——SocialSimulator
产出数据集的同时产出锚定它的清单，MindTrainer 只消费并逐条校验——所以放在
simulator，而不是 mindtrainer。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from simulator.backends import ModelBackend
from simulator.datamodel import Episode, Persona, RoleAssignment, Scenario
from simulator.runner import run_episode
from simulator.store_adapter import read_episode, write_episode

_NOW = "2026-10-03T00:00:00Z"
_NAME = re.compile(r"^[a-z0-9_]+$")


class DatasetIntegrityError(Exception):
    """数据集与清单不一致：清单被改、episode 被改/丢失、清单为空。"""


@dataclass
class DatasetManifest:
    id: str
    run_id: str
    name: str  # 阶段名，如 "sft" / "grpo_opd"
    episode_refs: list[dict]  # [{"id": ..., "sha256": ...}]，按 id 排序
    manifest_hash: str  # 对排序后的 episode_refs 的规范化 JSON 取 sha256

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "name": self.name,
            "episode_refs": [dict(r) for r in self.episode_refs],
            "manifest_hash": self.manifest_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DatasetManifest":
        return cls(
            id=d["id"],
            run_id=d["run_id"],
            name=d["name"],
            episode_refs=[dict(r) for r in d["episode_refs"]],
            manifest_hash=d["manifest_hash"],
        )


def _canonical_json(obj) -> str:
    # 不依赖 YAML 排版：键排序、无空白、不转义非 ASCII
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def episode_digest(episode: Episode) -> str:
    return _sha256(_canonical_json(episode.to_dict()))


def _manifest_hash(episode_refs: list[dict]) -> str:
    return _sha256(_canonical_json(sorted(episode_refs, key=lambda r: r["id"])))


def episode_group_id(episode: Episode) -> str:
    """GRPO 的"同一上下文"分组键，完全由 episode 自带的 role_assignment 推出：
    scenario_id + 每个 binding 的 (slot, persona, backend, model_ref)，顺序有意义
    （发言轮转顺序）。model_ref 必须在内——换了对手模型就不是同一上下文，组内
    基线会混入不可比的样本。max_turns 不入键：它是预算，不是上下文。"""
    key = {
        "scenario_id": episode.scenario_id,
        "bindings": [
            {
                "slot": b.slot_id,
                "persona": b.persona_id,
                "backend": b.model_backend,
                "ref": b.model_ref,
            }
            for b in episode.role_assignment.bindings
        ],
    }
    return _sha256(_canonical_json(key))[:16]


def build_manifest(
    store: GitStore, run_id: str, name: str, episode_ids: list[str]
) -> DatasetManifest:
    if not _NAME.match(name):
        raise ValueError(f"dataset name must match [a-z0-9_]+, got {name!r}")
    if not episode_ids:
        raise ValueError("cannot build a manifest for zero episodes")
    if len(set(episode_ids)) != len(episode_ids):
        raise ValueError("duplicate episode ids in manifest input")
    refs = [
        {"id": eid, "sha256": episode_digest(read_episode(store, run_id, eid))}
        for eid in sorted(episode_ids)
    ]
    return DatasetManifest(
        id=f"dsm-{run_id}-{name}",
        run_id=run_id,
        name=name,
        episode_refs=refs,
        manifest_hash=_manifest_hash(refs),
    )


def verify_manifest(store: GitStore, manifest: DatasetManifest) -> list[Episode]:
    """逐条校验并按清单顺序返回 episodes。任何不一致都抛 DatasetIntegrityError，
    绝不静默跳过——配方靠这份哈希精确复现数据集（spec §5.1）。"""
    if not manifest.episode_refs:
        raise DatasetIntegrityError(f"manifest {manifest.id} is empty")
    if _manifest_hash(manifest.episode_refs) != manifest.manifest_hash:
        raise DatasetIntegrityError(f"manifest {manifest.id}: manifest_hash mismatch")
    episodes: list[Episode] = []
    for ref in manifest.episode_refs:
        try:
            episode = read_episode(store, manifest.run_id, ref["id"])
        except FileNotFoundError as exc:
            raise DatasetIntegrityError(
                f"manifest {manifest.id}: episode {ref['id']} is missing"
            ) from exc
        if episode_digest(episode) != ref["sha256"]:
            raise DatasetIntegrityError(
                f"manifest {manifest.id}: episode {ref['id']} sha256 mismatch"
            )
        episodes.append(episode)
    return episodes


def write_manifest(store: GitStore, manifest: DatasetManifest) -> str:
    env = Envelope(
        id=manifest.id,
        type="dataset_manifest",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=CURRENT_SCHEMA_VERSION,
        links=[manifest.run_id],
        payload=manifest.to_dict(),
    )
    path = dataset_manifest_file(manifest.run_id, manifest.name)
    store.write_product(path, env, f"dataset_manifest: {manifest.id}")
    return path


def read_manifest(store: GitStore, run_id: str, name: str) -> DatasetManifest:
    env = store.read_product(dataset_manifest_file(run_id, name))
    return DatasetManifest.from_dict(env.payload)


def produce_grouped_dataset(
    store: GitStore,
    run_id: str,
    name: str,
    *,
    scenarios: list[tuple[Scenario, RoleAssignment]],
    personas: dict[str, Persona],
    backends_for_rollout: Callable[[Scenario, int], dict[str, ModelBackend]],
    group_size: int,
) -> DatasetManifest:
    """批量生产方：每个 scenario 连续 rollout group_size 条，落盘并写清单。

    - SFT 数据集用 group_size=1；离线分组 GRPO 的数据集需要 ≥2，且各 rollout
      要真的有差异（backends_for_rollout 按 rollout 序号给出不同的后端），否则
      组内零方差、优势恒为 0。本函数不替消费方判定，只产出；退化组由
      MindTrainer 在训练前校验并显式报错。
    - episode id 带阶段名，同一 run 下 sft 与 grpo_opd 两份数据集互不冲突。
    """
    if group_size < 1:
        raise ValueError(f"group_size must be >= 1, got {group_size}")
    if not scenarios:
        raise ValueError("scenarios must not be empty")
    episode_ids: list[str] = []
    for scenario, assignment in scenarios:
        for k in range(group_size):
            episode = run_episode(
                scenario, assignment, personas,
                backends_for_rollout(scenario, k),
                f"{run_id}-{name}-{scenario.id}-r{k}",
            )
            write_episode(store, run_id, episode)
            episode_ids.append(episode.id)
    manifest = build_manifest(store, run_id, name, episode_ids)
    write_manifest(store, manifest)
    return manifest
