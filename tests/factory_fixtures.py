"""factory 测试共用的仓库搭建与假处理器(不是测试文件,pytest 不收集)。"""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, run_dir
from factory.defaults import install_defaults
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.data_spec import parse_data_spec
from mindtrainer.training_config import RewardConfig, load_config_from_directive

BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})
DATA_SPEC = {"scenario_count": 2, "seed": 0, "purpose": "training",
             "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}


def training_config(run_id: str) -> dict:
    return {
        "mode": "batch", "trained_slot": "a",
        "base_checkpoint_ref": BASE.to_dict(), "teacher_model_ref": TEACHER.to_dict(),
        "reward": REWARD.to_dict(), "sft_steps": 1, "grpo_steps": 1, "sft_batch_size": 1,
        "groups_per_step": 1, "group_size": 2, "grpo_config": {}, "opd_coefficient": 0.1,
        "checkpoint_every_steps": 1, "allow_resume": True,
        "stage_manifest_refs": {"sft": f"dsm-{run_id}-sft", "grpo_opd": f"dsm-{run_id}-grpo_opd"},
    }


def write_directive(store: GitStore, run_id: str, *, data_spec: dict | None = None,
                    status: str = "pending", dimension: str = "tom") -> Envelope:
    payload = {"target_dimension": dimension, "recipe_ref": "", "training_config": training_config(run_id)}
    if data_spec is not None:
        payload["data_spec"] = data_spec
    env = Envelope(id=run_id, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status=status, schema_version=1, payload=payload)
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")
    return env


def produce_datasets(store: GitStore, directive: Envelope) -> None:
    """用默认生产器把指令点名的两份清单先写进树(“带数据集清单的仓库”)。"""
    TemplateDataProducer().produce(
        store, directive.id, load_config_from_directive(directive), parse_data_spec(DATA_SPEC)
    )


def closed_loop_repo(root: str, *, run_id: str | None = "run-001", with_datasets: bool = True) -> GitStore:
    """一棵装好默认资产与配置的仓库;run_id 非空时再放一条 pending 指令(及其数据集清单)。"""
    store = GitStore(root)
    store.init()
    install_defaults(store)
    if run_id is not None:
        env = write_directive(store, run_id)
        if with_datasets:
            produce_datasets(store, env)
    return store


def approve(store: GitStore, run_id: str) -> None:
    rel = candidate_checkpoint_file(run_id)
    cand = store.read_product(rel)
    cand.status = "approved"
    store.write_product(rel, cand, f"human: approve {cand.id}")


def git_head(store: GitStore) -> str:
    return subprocess.run(["git", "-C", str(store.repo_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def git_log_messages(store: GitStore) -> list[str]:
    out = subprocess.run(["git", "-C", str(store.repo_root), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def tree_files(store: GitStore) -> dict[str, bytes]:
    root = store.repo_root / "control-plane"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@contextmanager
def held_lock(repo_root):
    """另起一个进程持有仓库排他锁(跨进程才真的互斥,与锁用 flock 还是 lockf 无关);退出时放锁。"""
    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from controlplane.lock import acquire_repo_lock\n"
        f"with acquire_repo_lock({str(repo_root)!r}):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    # with Popen:退出时关闭并回收两条管道;裸 Popen 会留下未关闭的文件对象,在 -W error 下变成 ResourceWarning
    with subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
        try:
            assert proc.stdout.readline().strip() == "locked"
            yield
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)


class Recorder:
    """假处理器:记下 (动作名, target_id),不写任何东西。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handler(self, store, action) -> None:
        self.calls.append((action.kind, action.target_id))


def recording_builders(recorder: Recorder, keys=None, seen_contexts: list | None = None) -> dict:
    from controlplane.reconcile import CLOSED_LOOP_ACTIONS

    def build(ctx):
        if seen_contexts is not None:
            seen_contexts.append(ctx)
        return recorder.handler

    return {k: build for k in (CLOSED_LOOP_ACTIONS if keys is None else keys)}
