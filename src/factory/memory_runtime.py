"""组合根的记忆后端:替身运行时身份与替身解析器(设计二 7.1 节)。

派发器在进入策略上下文之前调用注入的 `memory_resolver(seed, persona_ids)`,拿回每个人格的起点记忆摘要与
记忆运行时身份,两者一起进 config_hash 与训练记录。本模块给的是替身那一路:每个人格都从空库起步,
没有任何种子库可用。计划 B 第 7 步在同一模块按 RISMFORGE_MEMORY_CONFIG 加 Starling 那一路。

部署记忆(设计二第 9 节):轮次处理器与门② 共用的一份 DeploymentMemory。替身的根目录在这棵工作树的
git 目录里(与仓库锁 rismforge.lock 同处):不进工作树、不进提交,跨进程重启保留,所以起点副本与门② 收录
的记忆不随进程退出丢失;一棵树一个 git 目录,两个仓库混写同一个根的问题在替身上不存在。
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from actio.memory import FileTupleStore, TupleMemory
from actio.memory_port import DeploymentMemoryStore
from actio.trace import TRACE_SCHEMA_V2
from controlplane.daemon import DirectiveFailedError
from controlplane.lock import git_dir
from mindagent.memory_layout import MemoryLayout

_log = logging.getLogger(__name__)

STAND_IN_MEMORY_RUNTIME: dict = {
    "backend": "stand-in",
    "starling_version": None,
    "extraction": None,
    "embedding": None,
    "trace_schema": TRACE_SCHEMA_V2,
}


def stand_in_memory_resolver(seed: str | None, persona_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]:
    """替身解析:seed 为 null 时每个人格得到空库摘要,runtime 是身份的一份拷贝(调用方改它不伤常量)。

    seed 非 null 抛 DirectiveFailedError 而不是普通异常:派发器对普通异常只报类名,而种子需要记忆配置
    这件事要原样告诉人。原因里只有种子 id 与变量名,不含路径。"""
    if seed is not None:
        raise DirectiveFailedError(
            f"memory.seed {seed!r} needs a memory config: set RISMFORGE_MEMORY_CONFIG "
            "(the stand-in memory has no seed libraries)"
        )
    empty = TupleMemory().digest()
    return {p: empty for p in persona_ids}, copy.deepcopy(STAND_IN_MEMORY_RUNTIME)


@dataclass(frozen=True)
class DeploymentMemory:
    """部署记忆的四件套:库(管理面)、目录布局、运行时身份、验证重放用的临时副本库工厂。
    组合根只造一份,轮次处理器与门② 拿同一个 store:它的"打开着的端口数"是实例内状态,共用才能让
    restore_from 的守卫看见轮次处理器开着的端口。"""

    store: DeploymentMemoryStore
    layout: MemoryLayout
    runtime: dict
    scratch_store_factory: Callable[[Path], DeploymentMemoryStore]


def stand_in_memory_root(repo_root: Path) -> Path:
    """替身部署记忆的根目录:<git 目录>/rismforge-memory。"""
    return git_dir(Path(repo_root)) / "rismforge-memory"


def stand_in_deployment_memory(repo_root: Path) -> DeploymentMemory:
    """替身部署记忆:FileTupleStore(<根>/deploy) 与 MemoryLayout(<根>) 共用一个根,运行时身份是替身那份的拷贝。"""
    root = stand_in_memory_root(repo_root)
    _log.info("memory backend: stand-in")
    return DeploymentMemory(FileTupleStore(root / "deploy"), MemoryLayout(root),
                            copy.deepcopy(STAND_IN_MEMORY_RUNTIME), FileTupleStore)
