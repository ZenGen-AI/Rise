"""组合根的记忆后端:替身运行时身份与替身解析器(设计二 7.1 节)。

派发器在进入策略上下文之前调用注入的 `memory_resolver(seed, persona_ids)`,拿回每个人格的起点记忆摘要与
记忆运行时身份,两者一起进 config_hash 与训练记录。本模块有两路:

- 替身:每个人格都从空库起步,没有任何种子库可用。
- Starling(`StarlingLayout`、`starling_memory_runtime`、`starling_memory_resolver`,按 RISMFORGE_MEMORY_CONFIG
  装配):种子库由人放进 `seeds/<seed_id>/`,本仓库只读;解析器按种子库算每个人格的起点摘要。

本模块不导入 `starling`(全仓库只有 `actio/memory_starling.py` 导入它),没装 Starling 时 `import factory.memory_runtime`
照常成功,替身路径照常起得来。Starling 的版本与抽取提示摘要由调用方从 `actio.memory_starling` 取来传进
`starling_memory_runtime`;`starling_memory_resolver` 在被调用时才延迟导入 `actio.memory_starling`,所以缺依赖
在装配期响,不拖到派发时。

运行时身份里没有记忆根目录:同一份库搬了目录不是另一个实验,路径进了哈希会让换机即 T3。

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
from actio.memory_port import PERSONA_ID_PATTERN, DeploymentMemoryStore
from actio.trace import TRACE_SCHEMA_V2
from controlplane.daemon import DirectiveFailedError
from controlplane.lock import git_dir
from factory.memory_config import MemoryConfig
from mindagent.memory_layout import MemoryLayout
from simulator.realtime.validation import validate_episode_id

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


# ---------------- Starling 那一路 ----------------

def starling_memory_runtime(config: MemoryConfig, *, version: str, install_source: str, prompts_digest: str) -> dict:
    """Starling 的记忆运行时身份(设计二 5.2 节):五个键,值里只有决定记忆行为的东西。

    `version` 是 `starling.__version__`,它区分不了同版本号的两个提交,所以后面拼上安装来源
    (`factory.memory_config.starling_install_source()` 的 `git:` 或 `record:` 串)。`prompts_digest` 是
    `actio.memory_starling.extraction_prompts_digest()`。两个端点地址用配置里已规范化的值(不带 userinfo)。
    `config.root` 不进身份:位置不是实验的一部分。密钥更不在这里,它只在环境变量里。
    每次返回一份新字典。"""
    return {
        "backend": "starling",
        "starling_version": f"{version}+{install_source}",
        "extraction": {
            "base_url": config.extraction_base_url, "model": config.extraction_model,
            "timeout_ms": config.timeout_ms, "max_retries": config.max_retries, "prompts": prompts_digest,
        },
        "embedding": {
            "base_url": config.embedding_base_url, "model": config.embedding_model, "dim": config.embedding_dim,
            "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
        },
        "trace_schema": TRACE_SCHEMA_V2,
    }


_CLIENT_DIR = "client"      # runs/<run_id>/client/ 放客户端训练位的副本,会话 id 不能占这个名字


def _segment(name: str, value: object) -> str:
    """拼进路径的一段名字:与 CreateSession.episode_id 同一份白名单(`[A-Za-z0-9._-]{1,128}`,不是 `.`、`..`)。"""
    reason = "must be a str" if not isinstance(value, str) else validate_episode_id(value)
    if reason is not None:
        raise ValueError(f"{name} {value!r:.60}: {reason}")
    return value


@dataclass(frozen=True)
class StarlingLayout:
    """记忆根目录下的路径布局(设计二 5.3 节)。每一段名字拼路径之前都校验,不合法抛 `ValueError`
    (种子 id 例外,见 `seed_dir`)。只算路径,不碰磁盘。"""

    root: Path

    def deploy_dir(self) -> Path:
        return self.root / "deploy"

    def runs_dir(self) -> Path:
        return self.root / "runs"

    def seed_dir(self, seed_id: str) -> Path:
        """`seeds/<seed_id>`。种子 id 来自指令配置,不合法是人写错了配置,所以抛 `DirectiveFailedError`
        (派发器原样保留原因、判 T2),而不是只报类名的 `ValueError`。原因里只有种子 id,不含根目录。"""
        if not isinstance(seed_id, str) or validate_episode_id(seed_id) is not None:
            raise DirectiveFailedError(f"memory.seed {seed_id!r:.60} is not a safe directory name")
        return self.root / "seeds" / seed_id

    def seed(self, seed_id: str, persona_id: str) -> Path:
        """`seeds/<seed_id>/<persona_id>.db`:训练起点种子库,只读。"""
        seed_dir = self.seed_dir(seed_id)
        return seed_dir / f"{self._persona(persona_id)}.db"

    def session_copy(self, run_id: str, session_id: str, slot_id: str) -> Path:
        """`runs/<run_id>/<session_id>/<slot_id>.db`:服务端对手位的私有副本。会话 id 不能是 `client`:
        它与下面客户端副本的 `runs/<run_id>/client/` 同级,撞名会让两类副本混进同一个目录。服务端今天生成的
        会话 id 都是 `sess-<hex>`,碰巧避开;这里把碰巧变成强制。"""
        if _segment("session_id", session_id) == _CLIENT_DIR:
            raise ValueError(f"session_id {session_id!r}: collides with the runs/<run_id>/{_CLIENT_DIR}/ directory")
        return self.runs_dir() / _segment("run_id", run_id) / session_id / f"{_segment('slot_id', slot_id)}.db"

    def client_copy(self, run_id: str, episode_id: str, slot_id: str) -> Path:
        """`runs/<run_id>/client/<episode_id>/<slot_id>.db`:客户端训练位的私有副本。"""
        return (self.runs_dir() / _segment("run_id", run_id) / _CLIENT_DIR / _segment("episode_id", episode_id)
                / f"{_segment('slot_id', slot_id)}.db")

    @staticmethod
    def _persona(persona_id: object) -> str:
        """人格 id 过 `PERSONA_ID_PATTERN`。用 fullmatch:模式以 `$` 结尾,match 会放过 "pa\n"。"""
        if not isinstance(persona_id, str) or not PERSONA_ID_PATTERN.fullmatch(persona_id):
            raise ValueError(f"persona_id {persona_id!r:.60}: expected 1 to 128 characters of [A-Za-z0-9._-] "
                             "starting with a letter or digit")
        return persona_id


def starling_memory_resolver(
    layout: StarlingLayout, runtime: dict,
) -> Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]:
    """Starling 的 `memory_resolver`:派发器进策略上下文之前调用,按种子库算每个人格的起点摘要。

    - `seed` 为 None:每个人格都是空库的摘要。
    - 否则 `seeds/<seed>/` 必须是目录。其中有 `<persona_id>.db` 的人格,摘要取这个库(只读,在临时副本上算);
      没有的人格从空库起,与 `seed: null` 对单个人格的含义相同。

    种子 id 不合法或目录不在,抛 `DirectiveFailedError`(原因里只有种子 id,不含根目录路径)。库文件打不开、
    不是库、摘要算不出,抛 `MemoryFailureError` 且不接:派发器对普通异常只报类名、判 T2。**库文件在但坏,
    绝不能当成"没有这个库"而悄悄从空库起**,所以只有 lstat 报 `FileNotFoundError`(文件根本不存在)才走空库,
    悬空的符号链接与读不了的目录都不算。

    `runtime` 在这里拷一份,每次返回再拷一份:调用方改返回值、装配方事后改原字典,都不影响解出的身份。
    `actio.memory_starling` 在本函数里才导入(它导入 `starling`),所以没装 Starling 时装配期就抛 ImportError。"""
    from actio.memory_starling import empty_digest, library_digest

    identity = copy.deepcopy(runtime)

    def resolve(seed: str | None, persona_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]:
        if seed is None:
            empty = empty_digest()
            return {p: empty for p in persona_ids}, copy.deepcopy(identity)
        if not layout.seed_dir(seed).is_dir():
            raise DirectiveFailedError(f"memory.seed {seed!r} has no directory under the memory root's seeds/")
        digests: dict[str, str] = {}
        for persona_id in persona_ids:
            path = layout.seed(seed, persona_id)
            try:
                path.lstat()        # 不跟符号链接:悬空的链接算"有这个库",交给 library_digest 抛错
            except FileNotFoundError:
                digests[persona_id] = empty_digest()          # 只有"确实没有这个文件"才从空库起
                continue
            digests[persona_id] = library_digest(path)       # 其余 OSError(如目录无权限)原样上抛,不当空库
        return digests, copy.deepcopy(identity)

    return resolve
