"""子命令 `factory seed`:从部署世界分叉种子世界(90 篇 `factory seed` 的规则表、FC-44;00 篇世界表)。

    factory seed <repo> --seed-id <id> --scenario <id> [--scenario ...] [--snapshot <id>] [--new-version]

- 种子 id 过 `PERSONA_ID_PATTERN`,指令里的 `memory.seed` 填同一个值;每个情境都要在 `agent.yaml` 的 `scenario_ids` 里,
  按 id 排序逐个处理。`--snapshot` 是人在看板快照列表里看到的快照 id,只能配一个 `--scenario`。
- 每个情境:取世界锁(被占:部署轮次或门② 在用它)→ 锁内有在途 episode 拒绝(那条 episode 会被回滚)→ 部署世界不在拒绝
  → 同组标签下已有种子而没给 `--new-version` 拒绝并列出它 → 版本取同组最大版本加一(首份 1)→ 分叉。持着世界锁时
  没有 rise 的写者,所以不另拍快照。分叉超时先按确切世界名查,查到当作成功,查不到提示重跑。
- 只打开服务、算 rise 设置(`open_memory_service`),不造部署管理面与训练世界;不写控制面的树,不取仓库锁。用完关会话,
  拉起模式下停服务。拉起模式下守护进程在跑时归属锁被占,由 `factory.starling_service` 拒绝。

一切拒绝都是 `FactoryConfigError`(命令行退出码 2),文字只有码、类名、情境 id 与世界名,不含 token、端点与记忆根路径。
"""
from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from controlplane.gitstore import GitStore
from controlplane.lock import FileLockBusyError
from factory.errors import FactoryConfigError
from factory.memory_config import read_memory_config
from factory.memory_runtime import ServiceHandle, find_seeds, open_memory_service
from mindagent.errors import MindAgentDataError
from simple_actio.memory_port import PERSONA_ID_PATTERN, MemoryFailureError


@dataclass(frozen=True)
class SeedResult:
    """建出的一份种子:完整世界名与分叉返回的摘要(等于来源的摘要)。"""

    world: str
    digest: str


def _scenarios(repo_root: Path, scenario_ids: Sequence[str]) -> list[str]:
    from factory.agent import AGENT_CONFIG, read_agent_settings        # 惰性:拖进 grpc 与 mindagent

    known = set(read_agent_settings(GitStore(str(repo_root)))[0].scenario_ids)
    wanted = sorted(set(scenario_ids))
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise FactoryConfigError(f"scenario {unknown[0]!r:.60} is not in the scenario_ids of {AGENT_CONFIG}")
    return wanted


def _check_request(seed_id: str, scenario_ids: Sequence[str], snapshot_id: str | None) -> None:
    if not isinstance(seed_id, str) or not PERSONA_ID_PATTERN.fullmatch(seed_id):
        raise FactoryConfigError(
            "--seed-id must be 1 to 128 characters of [A-Za-z0-9._-] starting with a letter or digit")
    if not scenario_ids:
        raise FactoryConfigError("give at least one --scenario")
    if snapshot_id is not None and len(set(scenario_ids)) != 1:
        raise FactoryConfigError("--snapshot needs exactly one --scenario")


def _deploy_world_exists(service: ServiceHandle, name: str, scenario_id: str) -> bool:
    """部署世界在:按 `repo`、`root`、`kind=deploy`、`scenario_id` 标签能查到这个名字。另一个记忆根的同名世界不算。"""
    from simple_actio.memory_starling import list_all_worlds

    labels = {"repo": service.prefix, "root": service.root_label, "kind": "deploy", "scenario_id": scenario_id}
    return any(w.get("name") == name for w in list_all_worlds(service.session, labels))


def _seed_one(service: ServiceHandle, *, seed_id: str, scenario_id: str, snapshot_id: str | None,
              new_version: bool) -> SeedResult:
    """一个情境:世界锁内核对、分叉。调用方已过请求核对。"""
    from factory import starling_names

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(service.layout.hold_world(scenario_id))
        except FileLockBusyError:
            raise FactoryConfigError(f"a deployment round or gate 2 is using world {scenario_id}") from None
        try:
            in_flight = service.layout.uncommitted_snapshots(scenario_id)
        except MindAgentDataError:
            raise FactoryConfigError(
                f"world {scenario_id}: an in-flight record cannot be read; fix or remove it first") from None
        if in_flight:
            raise FactoryConfigError(
                f"world {scenario_id} has an in-flight episode (its round would roll it back); "
                "let the round finish first")
        source = starling_names.deploy_world(service.prefix, scenario_id)
        if not _deploy_world_exists(service, source, scenario_id):
            raise FactoryConfigError(f"deploy world {source} does not exist")
        group = find_seeds(service.session, prefix=service.prefix, root_label=service.root_label,
                           seed_id=seed_id, scenario_id=scenario_id)
        latest = int(group[0]["labels"]["version"]) if group else 0
        if group and not new_version:
            raise FactoryConfigError(
                f"seed {seed_id} of scenario {scenario_id} already exists: {group[0]['name']} (version {latest}); "
                f"pass --new-version to build version {latest + 1}")
        version = latest + 1
        name = starling_names.seed_world(service.prefix, seed_id, scenario_id, version)
        body = {"source": source, "name": name, "snapshot_id": snapshot_id,
                "labels": starling_names.labels(service.prefix, service.root_label, "seed", seed_id=seed_id,
                                                scenario_id=scenario_id, version=str(version)),
                "retention_group": None, "reclaimable": False, "seed": True, "settings": service.settings}
        try:
            out = service.session.admin("fork", body)
        except MemoryFailureError as exc:
            if getattr(exc, "code", None) != "service_timeout":
                raise
            # 服务端可能已经建好了:按确切世界名查,分叉要么完整成功要么不留东西
            for world in find_seeds(service.session, prefix=service.prefix, root_label=service.root_label,
                                    seed_id=seed_id, scenario_id=scenario_id):
                if world.get("name") == name and world.get("digest"):
                    return SeedResult(name, world["digest"])
            raise FactoryConfigError(f"fork of {name} timed out and the world is not there; run factory seed again") \
                from None
        return SeedResult(name, out["digest"])


def build_seeds(repo_root: Path, env: Mapping[str, str], *, seed_id: str, scenario_ids: Sequence[str],
                snapshot_id: str | None, new_version: bool) -> list[SeedResult]:
    """`factory seed` 的全部工作,按情境 id 排序返回建出的种子。没设 RISE_MEMORY_CONFIG(替身)直接拒绝,
    先于读 `agent.yaml` 与打开服务;请求核对在打开服务之前。某个情境失败时,文字里带上它之前已建成的种子。"""
    repo_root = Path(repo_root)
    if read_memory_config(env=env, repo_root=repo_root) is None:
        raise FactoryConfigError("the stand-in memory has no seeds; set RISE_MEMORY_CONFIG")
    _check_request(seed_id, scenario_ids, snapshot_id)
    wanted = _scenarios(repo_root, scenario_ids)
    service = open_memory_service(repo_root, env)
    if service is None:                                   # 两次读之间变量被清空:与上面同一条拒绝
        raise FactoryConfigError("the stand-in memory has no seeds; set RISE_MEMORY_CONFIG")
    results: list[SeedResult] = []
    try:
        for scenario_id in wanted:
            try:
                results.append(_seed_one(service, seed_id=seed_id, scenario_id=scenario_id,
                                         snapshot_id=snapshot_id, new_version=new_version))
            except MemoryFailureError as exc:
                raise _with_built(FactoryConfigError(f"memory service: world {scenario_id}: {exc}"), results) \
                    from None
            except FactoryConfigError as exc:
                raise _with_built(exc, results) from None
    finally:
        service.close()
    return results


def _with_built(exc: FactoryConfigError, results: list[SeedResult]) -> FactoryConfigError:
    if not results:
        return exc
    return FactoryConfigError(f"{exc}; already built: {', '.join(r.world for r in results)}")
