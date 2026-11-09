"""组合根的记忆后端:替身运行时身份与替身解析器(设计二 7.1 节)。

派发器在进入策略上下文之前调用注入的 `memory_resolver(seed, scenario_ids)`,拿回每个情境的起点世界摘要与
记忆运行时身份,两者一起进 config_hash 与训练记录。本模块有两路:

- 替身:每个情境都从空世界起步,没有任何种子世界可用。
- Starling(`StarlingLayout`、`starling_memory_runtime`、`starling_memory_resolver`、`StarlingTrainingWorlds`,按
  RISE_MEMORY_CONFIG 装配):种子世界由人放进 `seeds/<seed_id>/<scenario_id>.db`,本仓库只读;解析器按种子
  世界算每个情境的起点摘要。

训练路的记忆是 rollout 世界(设计 5.1、5.2、6.3 节,`TrainingWorlds`):一条 rollout 一个世界,训练位与对手位都是
世界里的主体。客户端(`factory.interact_policy` 的 `policy_for_rollout`)建世界并开训练位端口,服务端
(`factory.agent.AgentRuntime`)按 episode id 加入、开对手位端口,客户端等服务端端口关完才删世界。两个实现:
`StarlingTrainingWorlds` 的世界是 `runs/<run_id>/<episode_id>.db`,从 `seeds/<seed>/<scenario>.db` 复制起点;
`StandInTrainingWorlds` 的世界是内存里的一份 `TupleMemory`,从空世界起,不落盘。

本模块不导入 `starling`(全仓库只有 `simple_actio/memory_starling.py` 导入它),没装 Starling 时 `import factory.memory_runtime`
照常成功,替身路径照常起得来。Starling 的版本与抽取提示摘要由调用方从 `simple_actio.memory_starling` 取来传进
`starling_memory_runtime`;`starling_memory_resolver` 在被调用时才延迟导入 `simple_actio.memory_starling`,所以缺依赖
在装配期响,不拖到派发时。`StarlingTrainingWorlds` 同理:`StarlingMemory`、`copy_library` 在方法内才导入,没装 Starling
时类本身可以导入,只是造不出世界。

运行时身份里没有记忆根目录:同一份库搬了目录不是另一个实验,路径进了哈希会让换机即 T3。

组合根按记忆配置选后端(`memory_wiring`,设计二 5.1、6.2、6.4 节,第 10 节拒绝项):没设 RISE_MEMORY_CONFIG 走替身,
设了就装配 Starling,配置有任何问题在装配期抛 `FactoryConfigError`(命令行退出码 2),不退回替身静默跑。
记忆根是旧布局(按人格建库、没有 `.world` 旁注的起点快照)时同样在装配期拒绝启动,不迁移(设计三 4.8 节);
`.world` 旁注坏了也在这里报,而不是留到门② 才抛。

部署记忆(设计二第 9 节):轮次处理器与门② 共用的一份 DeploymentMemory。替身的根目录在这棵工作树的
git 目录里(与仓库锁 rise.lock 同处):不进工作树、不进提交,跨进程重启保留,所以起点副本与门② 收录
的记忆不随进程退出丢失;一棵树一个 git 目录,两个仓库混写同一个根的问题在替身上不存在。
"""
from __future__ import annotations

import copy
import logging
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from simple_actio.memory import FileTupleStore, TupleMemory
from simple_actio.memory_port import (
    PERSONA_ID_PATTERN,
    DeploymentMemoryStore,
    MemoryBackend,
    MemoryEvent,
    MemoryFailureError,
    MemoryHit,
    RememberOutcome,
)
from simple_actio.trace import TRACE_SCHEMA_V2
from controlplane.daemon import DirectiveFailedError
from controlplane.gitstore import GitStore
from controlplane.lock import git_dir
from factory.errors import FactoryConfigError
from factory.memory_config import (
    MEMORY_CONFIG_ENV,
    MemoryConfig,
    check_api_key,
    claim_memory_root,
    read_memory_config,
    starling_install_source,
)
from mindagent.deployment import SESSION_CLOSE_WAIT_S
from mindagent.errors import MindAgentDataError
from mindagent.memory_layout import MemoryLayout, _tag_path
from simulator.realtime.validation import validate_episode_id

if TYPE_CHECKING:       # 只给类型注解用:运行时不导入 simple_actio.memory_starling(它导入 starling)
    from simple_actio.memory_starling import StarlingEndpoints

_log = logging.getLogger(__name__)

STAND_IN_MEMORY_RUNTIME: dict = {
    "backend": "stand-in",
    "starling_version": None,
    "extraction": None,
    "embedding": None,
    "trace_schema": TRACE_SCHEMA_V2,
}


def stand_in_memory_resolver(seed: str | None, scenario_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]:
    """替身解析:seed 为 null 时每个情境得到空世界摘要,runtime 是身份的一份拷贝(调用方改它不伤常量)。

    seed 非 null 抛 DirectiveFailedError 而不是普通异常:派发器对普通异常只报类名,而种子需要记忆配置
    这件事要原样告诉人。原因里只有种子 id 与变量名,不含路径。"""
    if seed is not None:
        raise DirectiveFailedError(
            f"memory.seed {seed!r} needs a memory config: set RISE_MEMORY_CONFIG "
            "(the stand-in memory has no seed libraries)"
        )
    empty = TupleMemory().digest()
    return {s: empty for s in scenario_ids}, copy.deepcopy(STAND_IN_MEMORY_RUNTIME)


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
    """替身部署记忆的根目录:<git 目录>/rise-memory。"""
    return git_dir(Path(repo_root)) / "rise-memory"


_SHOWN_ENTRIES = 5     # 旧布局拒绝启动时文字里列出的条目数,多出来的只报个数


def check_memory_layout(layout: MemoryLayout, repo_root: Path) -> None:
    """记忆根是旧布局(设计三 4.8 节)或有坏的 `.world` 旁注时拒绝启动,抛 `FactoryConfigError`(命令行退出码 2)。
    只读磁盘,先于任何对记忆根的写。不迁移、不静默忽略:旧库里的记忆不会被任何世界读到,留着会让人以为还在用。

    - 已知情境取自 `agent.yaml` 的 `scenario_ids`;文件不在时是空集,`deploy/` 下任何库都算旧布局。
      `agent.yaml` 读不了的 `FactoryConfigError` 原样上抛。
    - 旧布局条目由 `MemoryLayout.legacy_entries` 给,文字列出至多 5 条。它只按文件名判:从 `agent.yaml` 删掉的
      情境的世界也会被报出,所以文字指向把情境加回配置或把文件移出记忆根,不指向清空目录。没有旁注的 `deploy-`
      快照也算在里面,但新格式快照的旁注丢了会被同样报出,所以文字里点明这一种恢复办法。
    - 有旁注的快照逐个 `world_of`:旁注坏了在装配期报,而不是留到门② 才抛。文字里的路径相对记忆根。"""
    from factory.agent import AGENT_CONFIG, read_agent_settings        # 惰性:拖进 grpc 与 mindagent

    store = GitStore(str(repo_root))
    known = read_agent_settings(store)[0].scenario_ids if store.exists(AGENT_CONFIG) else ()
    entries = layout.legacy_entries(known)
    if entries:
        shown = ", ".join(entries[:_SHOWN_ENTRIES])
        if len(entries) > _SHOWN_ENTRIES:
            shown += f" and {len(entries) - _SHOWN_ENTRIES} more"
        # 只按文件名判,分不出"已从 agent.yaml 移除的情境的世界"和"旧的按人格库":指向移回或移走,不指向清空
        raise FactoryConfigError(
            f"memory root has the old per-persona layout ({shown}); "
            "if any of these are worlds of scenarios removed from agent.yaml, add the scenarios back to agent.yaml "
            "or move the files out of the memory root; delete them only once they are confirmed to be old "
            "per-persona libraries; "
            "a deploy-deploy-* snapshot listed here may instead be a new snapshot that lost its .world tag: "
            "write the scenario id into <snapshot>.world or delete the snapshot"
        )
    for snapshot in layout.tagged_snapshots():
        try:
            layout.world_of(snapshot)
        except MindAgentDataError:
            tag = _tag_path(snapshot).relative_to(layout.root).as_posix()       # 不回显旁注内容和绝对路径
            raise FactoryConfigError(
                f"memory root has a snapshot world tag that cannot be used ({tag}); "
                "write the scenario id into that .world file or delete the snapshot and its tag"
            ) from None


def stand_in_deployment_memory(repo_root: Path) -> DeploymentMemory:
    """替身部署记忆:FileTupleStore(<根>/deploy,held 快照在 <根>/held,重放世界在 <根>/replay) 与 MemoryLayout(<根>)
    共用一个根,运行时身份是替身那份的拷贝。
    造之前先过 `check_memory_layout`(后缀 `.json`),旧布局拒绝启动。
    启动日志不在这里记:选后端是 `memory_wiring` 的事,它只记一次。"""
    root = stand_in_memory_root(repo_root)
    layout = MemoryLayout(root)
    check_memory_layout(layout, repo_root)
    return DeploymentMemory(
        FileTupleStore(root / "deploy", held_root=root / "held", replay_root=root / "replay"),
        layout, copy.deepcopy(STAND_IN_MEMORY_RUNTIME), FileTupleStore)   # 第四个字段在 Task 7 删


# ---------------- Starling 那一路 ----------------

def starling_memory_runtime(config: MemoryConfig, *, version: str, install_source: str, prompts_digest: str) -> dict:
    """Starling 的记忆运行时身份(设计二 5.2 节):五个键,值里只有决定记忆行为的东西。

    `version` 是 `starling.__version__`,它区分不了同版本号的两个提交,所以后面拼上安装来源
    (`factory.memory_config.starling_install_source()` 的 `git:` 或 `record:` 串)。`prompts_digest` 是
    `simple_actio.memory_starling.extraction_prompts_digest()`。两个端点地址用配置里已规范化的值(不带 userinfo)。
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


def _segment(name: str, value: object) -> str:
    """拼进路径的一段名字:与 CreateSession.episode_id 同一份白名单(`[A-Za-z0-9._-]{1,128}`,不是 `.`、`..`)。"""
    reason = "must be a str" if not isinstance(value, str) else validate_episode_id(value)
    if reason is not None:
        raise ValueError(f"{name} {value!r:.60}: {reason}")
    return value


@dataclass(frozen=True)
class StarlingLayout:
    """记忆根目录下的路径布局(设计二 5.3 节,设计 5.1 节)。每一段名字拼路径之前都校验,不合法抛 `ValueError`
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

    def seed(self, seed_id: str, scenario_id: str) -> Path:
        """`seeds/<seed_id>/<scenario_id>.db`:一个情境的种子世界,解析器算起点摘要、rollout 世界复制起点都读它,只读。"""
        seed_dir = self.seed_dir(seed_id)
        return seed_dir / f"{self._named('scenario_id', scenario_id)}.db"

    def rollout_world(self, run_id: str, episode_id: str) -> Path:
        """`runs/<run_id>/<episode_id>.db`:一条 rollout 的世界,训练位与对手位都在里面。"""
        return self.runs_dir() / _segment("run_id", run_id) / f"{_segment('episode_id', episode_id)}.db"

    @staticmethod
    def _named(what: str, value: object) -> str:
        """情境 id 过 `PERSONA_ID_PATTERN`(部署库的世界 id 白名单)。用 fullmatch:模式以 `$` 结尾,match 会放过 "t1\n"。"""
        if not isinstance(value, str) or not PERSONA_ID_PATTERN.fullmatch(value):
            raise ValueError(f"{what} {value!r:.60}: expected 1 to 128 characters of [A-Za-z0-9._-] "
                             "starting with a letter or digit")
        return value


def starling_memory_resolver(
    layout: StarlingLayout, runtime: dict,
) -> Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]:
    """Starling 的 `memory_resolver`:派发器进策略上下文之前调用,按种子世界算每个情境的起点摘要。

    - `seed` 为 None:每个情境都是空世界的摘要。
    - 否则 `seeds/<seed>/` 必须是目录。其中有 `<scenario_id>.db` 的情境,摘要取这个世界(只读,在临时副本上算);
      没有的情境从空世界起,与 `seed: null` 对单个情境的含义相同。种子只覆盖部分情境是合法的;一个配置情境都
      对不上、目录里却有 `*.db`(多半是以人格命名的旧种子库)时抛 `DirectiveFailedError`,原因写种子 id 与至多
      5 个文件主名。目录里一个库都没有时仍是全部从空世界起。

    种子 id 不合法或目录不在,抛 `DirectiveFailedError`(原因里只有种子 id,不含根目录路径)。库文件打不开、
    不是库、摘要算不出,抛 `MemoryFailureError` 且不接:派发器对普通异常只报类名、判 T2。**库文件在但坏,
    绝不能当成"没有这个库"而悄悄从空世界起**,所以只有 lstat 报 `FileNotFoundError`(文件根本不存在)才走空世界,
    悬空的符号链接与读不了的目录都不算。

    `runtime` 在这里拷一份,每次返回再拷一份:调用方改返回值、装配方事后改原字典,都不影响解出的身份。
    `simple_actio.memory_starling` 在本函数里才导入(它导入 `starling`),所以没装 Starling 时装配期就抛 ImportError。"""
    from simple_actio.memory_starling import empty_digest, library_digest

    identity = copy.deepcopy(runtime)

    def resolve(seed: str | None, scenario_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]:
        if seed is None:
            empty = empty_digest()
            return {s: empty for s in scenario_ids}, copy.deepcopy(identity)
        seed_dir = layout.seed_dir(seed)
        if not seed_dir.is_dir():
            raise DirectiveFailedError(f"memory.seed {seed!r} has no directory under the memory root's seeds/")
        digests: dict[str, str] = {}
        missing = 0
        for scenario_id in scenario_ids:
            path = layout.seed(seed, scenario_id)
            try:
                path.lstat()        # 不跟符号链接:悬空的链接算"有这个库",交给 library_digest 抛错
            except FileNotFoundError:
                digests[scenario_id] = empty_digest()         # 只有"确实没有这个文件"才从空世界起
                missing += 1
                continue
            digests[scenario_id] = library_digest(path)      # 其余 OSError(如目录无权限)原样上抛,不当空世界
        if scenario_ids and missing == len(scenario_ids):
            # 一个情境都对不上、目录里却有库:多半是按人格命名的旧种子,拒绝,不让整批悄悄从空世界起
            stems = sorted(p.stem for p in seed_dir.glob("*.db"))
            if stems:
                shown = ", ".join(stems[:_SHOWN_ENTRIES])
                if len(stems) > _SHOWN_ENTRIES:
                    shown += f" and {len(stems) - _SHOWN_ENTRIES} more"
                raise DirectiveFailedError(
                    f"memory.seed {seed!r} matches none of the configured scenarios but holds libraries ({shown}); "
                    "seed worlds are now named by scenario id (seeds/<seed>/<scenario_id>.db)")
        return digests, copy.deepcopy(identity)

    return resolve


class TrainingWorlds(Protocol):
    """训练路的 rollout 世界(设计 5.1、5.2、6.3 节):一条 rollout 一个世界,训练位与对手位都是世界里的主体。

    - `bind(run_id, seed)`、`unbind(run_id)`、`seed_of(run_id)`:训练 run 进入策略上下文时绑定种子 id,退出时解绑。
    - `open_rollout(run_id, scenario_id, episode_id, holder)`:客户端建世界(先删后建,从这个 run 的种子里该情境的
      种子世界复制起点,没有就是空世界),登记进 `episode_id → 世界` 表,开训练位主体的端口。同名世界在保留表里
      (服务端端口没关上)时先有界等它的端口关上(默认 70 秒),仍没关上才抛瞬态 `MemoryFailureError`。
    - `join_rollout(episode_id, holder)`:服务端在已登记的世界里开对手位主体的端口;表里没有抛非瞬态 `MemoryFailureError`。
    - `release_session(episode_id, ports)`:服务端在会话终态关自己的端口。
    - `close_rollout(episode_id, ports, *, wait_s)`:客户端关训练位端口,等服务端端口关完,删世界、摘表项,返回真;
      等不到就把世界移进保留表、记 warning、返回假,不抛。
    - `sweep()`:启动清扫,返回删了几项。"""

    def bind(self, run_id: str, seed: str | None) -> None: ...

    def unbind(self, run_id: str) -> None: ...

    def seed_of(self, run_id: str) -> str | None: ...

    def open_rollout(self, run_id: str, scenario_id: str, episode_id: str, holder: str) -> MemoryBackend: ...

    def join_rollout(self, episode_id: str, holder: str) -> MemoryBackend: ...

    def close_rollout(self, episode_id: str, ports: Sequence[object], *,
                      wait_s: float = SESSION_CLOSE_WAIT_S) -> bool: ...

    def release_session(self, episode_id: str, ports: Sequence[object]) -> None: ...

    def sweep(self) -> int: ...


class _World:
    """表里的一项:世界本身(替身是 TupleMemory,Starling 是库文件路径)、开着的端口数、等端口全关用的 Condition。
    `ready` 在世界建好、训练位端口开出来之后才置真,之前服务端加入不进来。三个可变字段只在持表锁时改。"""

    def __init__(self, lock: threading.Lock) -> None:
        self.world: object = None
        self.open = 1                        # 训练位的端口:建世界之前先占上,同名的并发重建就看得见它
        self.ready = False
        self.closed = threading.Condition(lock)


def _close_all(ports: Sequence[object]) -> Exception | None:
    """逐个 close,一个关不上也继续关剩下的(不关的话它们的连接会一直开到进程退出),返回第一个异常。"""
    first: Exception | None = None
    for port in ports:
        try:
            port.close()
        except Exception as exc:
            if first is None:
                first = exc
    return first


class _RolloutWorlds:
    """两个实现共用的表逻辑:绑定表、`episode_id → _World` 表、保留表,一把锁护三张表与每项的计数。

    端口计数:每个端口开出来之前先计一个(`open_rollout` 在建世界前、`join_rollout` 在开端口前),端口关上时由
    `_release_once` 造的回调减一,归零时唤醒 `close_rollout`。回调只取这把表锁,不取任何世界文件锁,也不碰
    `AgentRuntime._lock`:它跑在服务端处理器线程的收尾里。开端口失败时把这一次计数退掉。

    保留表:`close_rollout` 等不到计数归零(服务端的端口没在期限内关上),或客户端自己的端口关不上时,世界移进保留表。
    同名的 `open_rollout` 在保留世界还有端口开着时先有界等待(`held_wait_s`,默认 70 秒):服务端 join 超时时回收线程
    晚于客户端的等待期限才关端口,轨迹源的重试又没有间隔,不等的话重试预算一下子就用完。等到端口全关就先删后建,
    等不到才拒绝(瞬态);服务端不能再加入保留的世界。

    子类实现 `_create`(先删后建一个世界)、`_port`(在世界里开一个主体的端口)、`_discard`(删世界)与 `sweep`。
    异常与日志文字不含路径、端点与密钥:只有 episode id 与异常类名。"""

    def __init__(self, *, held_wait_s: float = SESSION_CLOSE_WAIT_S) -> None:
        self._lock = threading.Lock()
        self._seeds: dict[str, str | None] = {}
        self._worlds: dict[str, _World] = {}
        self._held: dict[str, _World] = {}
        self.held_wait_s = held_wait_s          # open_rollout 等保留世界端口关上的期限;测试传小值

    # ---- 绑定表 ----

    def bind(self, run_id: str, seed: str | None) -> None:
        """记下这个 run 的种子 id(None 即从空世界起)。同 run 再绑则覆盖。"""
        with self._lock:
            self._seeds[run_id] = seed

    def unbind(self, run_id: str) -> None:
        """没绑过也不报错。已经开出的世界不受影响,各自随 rollout 的终态删除。"""
        with self._lock:
            self._seeds.pop(run_id, None)

    def seed_of(self, run_id: str) -> str | None:
        """没绑定抛 `KeyError`:策略上下文还没进入,或者已经退出。"""
        with self._lock:
            return self._seeds[run_id]

    # ---- 子类实现 ----

    def _create(self, run_id: str, scenario_id: str, episode_id: str, seed: str | None) -> object:
        raise NotImplementedError

    def _port(self, world: object, holder: str, release: Callable[[], None]) -> MemoryBackend:
        raise NotImplementedError

    def _discard(self, world: object) -> None:
        raise NotImplementedError

    # ---- 计数 ----

    def _release_once(self, rec: _World) -> Callable[[], None]:
        """给一个端口造它的关闭回调:计数减一,归零时唤醒等待者。**同一个端口只减一次**:两条线程同时关同一个端口时
        回调会被调两次,多减的那次会扣掉同世界别的端口,客户端就在服务端端口开着时删世界。只做减一与通知,不抛。"""
        done = False

        def release() -> None:
            nonlocal done
            with self._lock:
                if done:
                    return
                done = True
                rec.open -= 1
                if rec.open == 0:
                    rec.closed.notify_all()

        return release

    def _busy(self, episode_id: str) -> _World | None:
        """两张表里同名、还有端口开着的第一项(先保留表、后世界表),没有返回 None。调用方持表锁。"""
        for table in (self._held, self._worlds):
            rec = table.get(episode_id)
            if rec is not None and rec.open > 0:
                return rec
        return None

    # ---- 客户端 ----

    def open_rollout(self, run_id: str, scenario_id: str, episode_id: str, holder: str) -> MemoryBackend:
        """建世界、登记、开 holder 的端口。重试在同一线程里顺序发生,上一次尝试的上下文已经退出,所以同名世界
        先删后建,从起点出发。它在保留表里还有端口开着(等服务端收尾)时,持表锁有界等待 `held_wait_s`(等待时放锁),
        醒来后复核两张表。仍有端口开着就不删,抛瞬态 `MemoryFailureError`:轨迹源把它记成一次
        role_memory_unavailable 再重跑。世界表里的同名项还有端口开着(上一次尝试的 `close_rollout` 还没跑完)时
        不等、直接抛:等它就是和 `close_rollout` 等同一项的 Condition,本方先醒的话,新建的世界会被随后醒来的
        `close_rollout` 摘掉并删掉。建不成时摘掉表项、删掉半建的世界。run 没绑定抛 `KeyError`。"""
        seed = self.seed_of(run_id)
        with self._lock:
            held, live = self._held.get(episode_id), self._worlds.get(episode_id)
            if held is not None and held.open > 0 and (live is None or live.open == 0):
                held.closed.wait_for(lambda: held.open == 0, self.held_wait_s)
            if self._busy(episode_id) is not None:          # 醒来后重新从两张表取表项复核
                raise MemoryFailureError(
                    f"rollout world {episode_id} is still held by a port that did not close", transient=True)
            self._held.pop(episode_id, None)
            self._worlds.pop(episode_id, None)
            rec = _World(self._lock)
            self._worlds[episode_id] = rec

        def forget() -> None:
            with self._lock:
                if self._worlds.get(episode_id) is rec:
                    del self._worlds[episode_id]

        try:
            world = self._create(run_id, scenario_id, episode_id, seed)
        except BaseException:
            forget()
            raise
        try:
            port = self._port(world, holder, self._release_once(rec))
        except BaseException:
            forget()
            try:
                self._discard(world)
            except OSError:
                pass            # 留给启动清扫;正在传播的才是根因
            raise
        with self._lock:
            rec.world = world
            rec.ready = True
        return port

    def close_rollout(self, episode_id: str, ports: Sequence[object], *,
                      wait_s: float = SESSION_CLOSE_WAIT_S) -> bool:
        """关客户端的端口,等这个世界的端口计数归零(服务端在会话终态关自己的端口),摘表项、删世界,返回真。

        - 计数在 `wait_s` 内没归零:世界移进保留表,记 warning(只有 episode id),返回假,不抛。这条 rollout 的轨迹
          已经拿到了,照常结束;同名重试在端口全关之前判瞬态,世界文件留给重试的先删后建或启动清扫。
        - 客户端自己有端口关不上(Starling 的在途调用没返回):剩下的照样关,不等、不删,世界移进保留表,
          记 warning,重抛第一个异常(调用方据此判训练位记忆失败)。
        - 世界删不掉(`OSError`):端口都已关上,不进保留表,记 warning,返回假,不抛。
        - 表里没有这个世界(从没开成,或已经收过尾):关完端口返回真。"""
        first = _close_all(ports)
        with self._lock:
            rec = self._worlds.get(episode_id)
            if rec is None:
                done = True
            elif first is not None:
                done = False
            else:
                done = rec.closed.wait_for(lambda: rec.open == 0, wait_s)
            if rec is not None:
                del self._worlds[episode_id]
                if not done:
                    self._held[episode_id] = rec
        if first is not None:
            if rec is not None:
                _log.warning("rollout world %s kept: a client port did not close (%s)",
                             episode_id, type(first).__name__)
            raise first
        if rec is None:
            return True
        if not done:
            _log.warning("rollout world %s kept: server ports were still open after %gs", episode_id, wait_s)
            return False
        try:
            self._discard(rec.world)
        except OSError as exc:
            _log.warning("rollout world %s kept: removal failed (%s)", episode_id, type(exc).__name__)
            return False
        return True

    # ---- 服务端 ----

    def join_rollout(self, episode_id: str, holder: str) -> MemoryBackend:
        """在客户端已建好的世界里开 holder 的端口。表里没有(客户端没开、已收尾,或世界在保留表里)抛非瞬态
        `MemoryFailureError`:服务端按造后端失败拒绝会话。"""
        with self._lock:
            rec = self._worlds.get(episode_id)
            if rec is None or not rec.ready:
                raise MemoryFailureError(f"no rollout world is open for episode {episode_id}", transient=False)
            rec.open += 1           # 先计数再开端口:开的这段时间里,客户端的 close_rollout 也会等它
            world = rec.world
        release = self._release_once(rec)
        try:
            return self._port(world, holder, release)
        except BaseException:
            release()
            raise

    def release_session(self, episode_id: str, ports: Sequence[object]) -> None:
        """服务端在会话终态关自己的端口:每个端口关上时计数减一、唤醒等着删世界的客户端。一个关不上也继续关
        剩下的,最后记 warning(只有 episode id 与类名)并重抛第一个异常;关不上的端口不减计数,客户端等到期限后
        把世界留进保留表。不取任何世界文件锁,也不取运行时的锁。"""
        first = _close_all(ports)
        if first is not None:
            _log.warning("rollout world %s: a server port did not close (%s)", episode_id, type(first).__name__)
            raise first

    def sweep(self) -> int:
        raise NotImplementedError


class _CountedPort:
    """替身 rollout 世界里一个主体的端口:TupleMemory 视图,关闭时计数减一。"""

    def __init__(self, view: TupleMemory, release: Callable[[], None]) -> None:
        self._view = view
        self._release = release

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        return self._view.recall(query, limit=limit)

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        return self._view.remember(event)

    def embed_pending(self) -> int:
        return self._view.embed_pending()

    def digest(self) -> str:
        return self._view.digest()

    def close(self) -> None:
        self._view.close()
        self._release()


class StandInTrainingWorlds(_RolloutWorlds):
    """替身的 rollout 世界(设计 5.1 节):每条 rollout 一份内存里的 `TupleMemory`,各主体经 `world(holder)` 在同一份
    账本上开视图,不落盘,删世界就是摘表项。起点是空世界:替身不支持种子,绑了种子的 run 开世界时抛非瞬态
    `MemoryFailureError`(派发器的替身解析器早已拒绝过种子,走不到这里)。"""

    def _create(self, run_id: str, scenario_id: str, episode_id: str, seed: str | None) -> TupleMemory:
        if seed is not None:
            raise MemoryFailureError("the stand-in memory has no seed libraries", transient=False)
        return TupleMemory()

    def _port(self, world: object, holder: str, release: Callable[[], None]) -> MemoryBackend:
        return _CountedPort(world.world(holder), release)

    def _discard(self, world: object) -> None:
        pass

    def sweep(self) -> int:
        """没有落盘的东西要清。"""
        return 0


_LIBRARY_SIDECARS = ("-wal", "-shm")


def _remove_library(path: Path) -> None:
    """删库文件与它的 -wal、-shm;不存在的不算错。"""
    for victim in (path, *(path.with_name(path.name + suffix) for suffix in _LIBRARY_SIDECARS)):
        victim.unlink(missing_ok=True)


class StarlingTrainingWorlds(_RolloutWorlds):
    """Starling 的 rollout 世界(设计 5.1 节):世界是 `runs/<run_id>/<episode_id>.db`,各主体用 `StarlingMemory.open`
    在同一个库上开端口(holder 作 Starling 的 agent 名),`on_close` 减计数。

    起点:绑了种子且 `seeds/<seed>/<scenario>.db` 在就**用文件复制**取世界(`simple_actio.memory_starling.copy_library`),
    没有就新建空库。不用 `backup_to`:它要先把种子打开,`Memory.open` 会建 -wal、-shm 并跑迁移,种子目录与文件只读时
    失败。种子没有写者,复制 `.db` 加 `-wal` 是一致的。同一组 rollout 因此从同一个起点出发,又彼此看不见对方写的东西。
    只有 lstat 报 `FileNotFoundError`(确实没有这个文件)才从空库起,悬空的符号链接交给复制去报错。

    复制、建目录、删旧世界抛的 `OSError` 译成非瞬态的 `MemoryFailureError`(文字只含类名);裸 `OSError` 不会被训练位
    一侧当成记忆失败。"""

    def __init__(self, layout: StarlingLayout, endpoints: StarlingEndpoints, *,
                 close_timeout_s: float = 60.0, held_wait_s: float = SESSION_CLOSE_WAIT_S) -> None:
        super().__init__(held_wait_s=held_wait_s)
        self._layout = layout
        self._endpoints = endpoints
        self._close_timeout_s = close_timeout_s

    def _create(self, run_id: str, scenario_id: str, episode_id: str, seed: str | None) -> Path:
        from simple_actio.memory_starling import copy_library, translate_failure

        path = self._layout.rollout_world(run_id, episode_id)      # 名字不合法在这里抛 ValueError
        source = None if seed is None else self._layout.seed(seed, scenario_id)
        try:
            _remove_library(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if source is not None:
                try:
                    source.lstat()      # 与解析器同一判法:只有"确实没有这个文件"才从空库起
                except FileNotFoundError:
                    pass
                else:
                    copy_library(source, path)
        except OSError as exc:
            try:
                _remove_library(path)       # 复制到一半的文件不留
            except OSError:
                pass
            raise translate_failure(exc) from None
        return path

    def _port(self, world: object, holder: str, release: Callable[[], None]) -> MemoryBackend:
        from simple_actio.memory_starling import StarlingMemory

        return StarlingMemory.open(world, holder=holder, endpoints=self._endpoints,
                                   close_timeout_s=self._close_timeout_s, on_close=release)

    def _discard(self, world: object) -> None:
        _remove_library(world)

    def sweep(self) -> int:
        """删掉 `runs/` 下的每个子项,返回删了几项;`deploy/`、`seeds/`、`snapshots/` 不碰。启动时在仓库锁里调用:
        上一个进程崩溃时留下的世界在这里清掉。`runs/` 下的符号链接只删链接本身,不跟进去(跟着删会删到
        `runs/` 之外,而且 `shutil.rmtree` 对符号链接直接抛 OSError)。`runs/` 自己是符号链接时抛非瞬态
        `MemoryFailureError`、什么都不删:遍历会跟进链接,清空它指向的目录。某一项删不掉记 warning 并继续。"""
        runs = self._layout.runs_dir()
        if runs.is_symlink():
            raise MemoryFailureError("memory root runs/ is a symlink; refusing to sweep", transient=False)
        try:
            entries = list(runs.iterdir())
        except FileNotFoundError:
            return 0
        removed = 0
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
            except OSError as exc:
                _log.warning("memory sweep kept %s (%s)", entry.name, type(exc).__name__)
                continue
            removed += 1
        return removed


# ---------------- 组合根选后端 ----------------

# 设计二 6.4 节、FC-31:接上 Starling 后部署的一轮最坏多 20 秒(recall 嵌入 1 次,remember 抽取 3 次,embed_pending 嵌入 1 批,
# 每次至多 4 秒),所以服务端等一次提交放宽到 40 秒,整条流的 deadline 按 max_turns × 每轮 40 秒折算。替身没有网络调用,
# 保持 AgentRuntime 默认的 5 秒与 15 秒。
DEPLOY_MEMORY_TURN_TIMEOUT_S = 40.0
DEPLOY_MEMORY_PER_TURN_S = 40.0

_INSTALL_HINT = "run pip install '.[memory]' (or uv sync --extra memory)"


@dataclass(frozen=True)
class MemoryWiring:
    """组合根按记忆配置选出的后端:装配 `assemble` 要的全部东西。

    `deployment` 只在调用方要部署记忆(启用 start_agent_round 或 admit_proposal)时非空;`training_worlds` 两个后端都有
    (替身是 `StandInTrainingWorlds`),策略工厂与运行时共用这一个。`sweep` 只有 Starling 才有(替身没有落盘的世界),
    只交出函数、不在装配时调:清扫要等持仓库锁(`run_daemon(on_locked=)`、`reconcile_one_tick` 的锁内),
    另一个守护进程可能正在用那些世界。"""

    backend: str
    deployment: DeploymentMemory | None
    memory_resolver: Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]
    training_worlds: TrainingWorlds
    sweep: Callable[[], int] | None


def _check_runs_dir(root: Path) -> None:
    """`<根>/runs` 是符号链接或存在但不是目录,装配期拒绝(`FactoryConfigError`,命令行退出码 2)。

    启动清扫会遍历并删掉 `runs/` 下的东西,符号链接会让它删到记忆根之外。`StarlingTrainingWorlds.sweep` 自己也拒绝符号链接,
    但它在持仓库锁之后才跑,抛出的 `MemoryFailureError` 不在 `factory.cli.main` 兜的异常里,操作员会看到 traceback 与
    退出码 1;这是记忆根的配置错,应与其他配置错走同一个出口。`sweep` 里的检查保留,作第二道(装配之后有人换了目录)。
    先判链接再判目录:`is_dir()` 会跟随链接。文字只写相对名,不含记忆根的绝对路径(第 10 节)。"""
    runs = root / "runs"
    if runs.is_symlink() or (runs.exists() and not runs.is_dir()):
        raise FactoryConfigError("memory root: runs/ is a symlink or not a directory; replace it with a plain directory")


def memory_wiring(repo_root: Path, env: Mapping[str, str], *, deployment: bool) -> MemoryWiring:
    """读 RISE_MEMORY_CONFIG 选后端,只在这里记一次启动日志。日志与异常文字都不含记忆根目录与端点地址(第 10 节)。

    - 没设(或空串):替身。`deployment` 为真才造替身的部署记忆。
    - 设了,按序:Starling 的安装来源(未安装、editable 在这里拒绝,先于任何对 `simple_actio.memory_starling` 的导入与对记忆
      根目录的写)、`OPENAI_API_KEY` 非空、`runs/` 不是符号链接也不是文件、认领记忆根目录(被另一个仓库占用就拒绝)、
      记忆根不是旧布局且 `.world` 旁注都完好(`check_memory_layout`,后缀 `.db`)、导入 `simple_actio.memory_starling`。
      替身的同一项检查在 `stand_in_deployment_memory` 里,只在造部署记忆时做。
      装了包但 C++ 扩展载不进来(`ImportError`)译成与「没装」同一条安装提示的 `FactoryConfigError`。"""
    config = read_memory_config(env=env, repo_root=repo_root)
    if config is None:
        _log.info("memory backend: stand-in")
        return MemoryWiring(
            "stand-in", stand_in_deployment_memory(repo_root) if deployment else None,
            stand_in_memory_resolver, StandInTrainingWorlds(), None,
        )
    install = starling_install_source()
    check_api_key(env)
    _check_runs_dir(config.root)
    claim_memory_root(config.root, repo_root)
    memory_layout = MemoryLayout(config.root, suffix=".db")
    check_memory_layout(memory_layout, repo_root)
    try:
        from simple_actio.memory_starling import (
            StarlingDeploymentStore,
            StarlingEndpoints,
            extraction_prompts_digest,
            starling_version,
        )
    except ImportError as exc:
        raise FactoryConfigError(
            f"{MEMORY_CONFIG_ENV} is set but Starling cannot be loaded ({type(exc).__name__}); {_INSTALL_HINT}"
        ) from None
    endpoints = StarlingEndpoints(
        extraction_base_url=config.extraction_base_url, extraction_model=config.extraction_model,
        embedding_base_url=config.embedding_base_url, embedding_model=config.embedding_model,
        embedding_dim=config.embedding_dim, timeout_ms=config.timeout_ms, max_retries=config.max_retries,
    )
    runtime = starling_memory_runtime(
        config, version=starling_version(), install_source=install, prompts_digest=extraction_prompts_digest(),
    )
    layout = StarlingLayout(config.root)
    worlds = StarlingTrainingWorlds(layout, endpoints)
    deployment_memory = None
    if deployment:
        deployment_memory = DeploymentMemory(
            # 过渡布局(第 2、3 步):held 快照在 <记忆根>/held,重放世界在 <记忆根>/replay,都是进程内的库文件
            StarlingDeploymentStore(layout.deploy_dir(), held_root=config.root / "held",
                                    replay_root=config.root / "replay", endpoints=endpoints),
            memory_layout,
            copy.deepcopy(runtime),
            lambda path: StarlingDeploymentStore(path, held_root=path / "held", replay_root=path / "replay",
                                                 endpoints=endpoints),     # Task 7 删
        )
    _log.info("memory backend: starling")
    return MemoryWiring("starling", deployment_memory, starling_memory_resolver(layout, runtime), worlds, worlds.sweep)
