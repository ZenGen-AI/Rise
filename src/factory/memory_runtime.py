"""组合根的记忆后端:替身与 Starling 记忆服务两路(设计二 7.1 节;设计四 90 篇)。

派发器在进入策略上下文之前调用注入的 `memory_resolver(seed, scenario_ids)`,拿回每个情境的起点世界摘要与
记忆运行时身份,两者一起进 config_hash 与训练记录。

- 替身:每个情境都从空世界起步,没有任何种子世界可用。
- Starling(`starling_memory_resolver`、`StarlingTrainingWorlds`,按 RISE_MEMORY_CONFIG 装配):经服务会话连记忆服务,
  世界、快照、分叉都按名字交给服务,rise 不打开库文件。`seed` 为空或某个情境没有种子时,起点是空种子
  `<前缀>.seed-empty`;否则按标签找种子(`find_seeds`),`bind` 按解出的起点摘要钉住那一版。

训练路的记忆是 rollout 世界(设计 5.1、5.2、6.3 节,`TrainingWorlds`):一条 rollout 一个世界,训练位与对手位都是
世界里的主体。客户端(`factory.interact_policy` 的 `policy_for_rollout`)建世界并开训练位端口,服务端
(`factory.agent.AgentRuntime`)按 episode id 加入、开对手位端口,客户端等服务端端口关完才收尾世界。两个实现:
`StarlingTrainingWorlds` 从起点分叉可回收世界、收尾只关不删;`StandInTrainingWorlds` 的世界是内存里的一份
`TupleMemory`,从空世界起,不落盘。

本模块不导入 `starling_client`(只有 `simple_actio/memory_starling.py` 与 `factory/starling_session.py` 导入它),
没装 extra memory 时 `import factory.memory_runtime` 照常成功,替身路径照常起得来。用到客户端的东西都在函数里才导入。

运行时身份里没有记忆根目录:同一份记忆搬了目录不是另一个实验,路径进了哈希会让换机即 T3。

组合根按记忆配置选后端(`memory_wiring`,设计二 5.1、6.2、6.4 节,第 10 节拒绝项):没设 RISE_MEMORY_CONFIG 走替身,
设了就装配 Starling,配置有任何问题在装配期抛 `FactoryConfigError`(命令行退出码 2),不退回替身静默跑。
记忆根是旧布局(按人格建库、快照文件与旁注、进程内留下的库文件,判法按后端)时同样在装配期拒绝启动,不迁移
(设计三 4.8 节,90 篇第 18 条);在途记录读不出也在这里报,而不是留到门② 才抛。

部署记忆(设计二第 9 节):轮次处理器与门② 共用的一份 DeploymentMemory。替身的根目录在这棵工作树的
git 目录里(与仓库锁 rise.lock 同处):不进工作树、不进提交,跨进程重启保留,所以起点副本与门② 收录
的记忆不随进程退出丢失;一棵树一个 git 目录,两个仓库混写同一个根的问题在替身上不存在。
"""
from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from simple_actio.memory import FileTupleStore, TupleMemory
from simple_actio.memory_port import (
    DeploymentMemoryStore,
    MemoryBackend,
    MemoryEvent,
    MemoryFailureError,
    MemoryHit,
    RememberOutcome,
)
from simple_actio.trace import TRACE_SCHEMA_V2
from controlplane.daemon import DirectiveFailedError
from controlplane.deferred import DeferredError
from controlplane.gitstore import GitStore
from controlplane.lock import git_dir
from factory.errors import FactoryConfigError
from factory.memory_budget import SPAWN_LAYERS, MemoryTimeBudget, memory_time_budget, spawn_time_budget
from factory.memory_config import (
    MEMORY_CONFIG_ENV,
    MemoryConfig,
    check_api_key,
    claim_memory_root,
    read_memory_config,
)
from mindagent.deployment import SESSION_CLOSE_WAIT_S
from mindagent.errors import MindAgentDataError
from mindagent.memory_layout import MemoryLayout

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
    """部署记忆的三件套:库(管理面)、目录布局、运行时身份。验证重放也只经这个管理面分叉重放世界。
    组合根只造一份,轮次处理器与门② 拿同一个 store:它的"打开着的端口数"是实例内状态,共用才能让
    restore 的守卫看见轮次处理器开着的端口。"""

    store: DeploymentMemoryStore
    layout: MemoryLayout
    runtime: dict
    budget: MemoryTimeBudget = SPAWN_LAYERS      # 各层时限;外部模式按服务端点推出


def stand_in_memory_root(repo_root: Path) -> Path:
    """替身部署记忆的根目录:<git 目录>/rise-memory。"""
    return git_dir(Path(repo_root)) / "rise-memory"


_SHOWN_ENTRIES = 5     # 旧布局拒绝启动时文字里列出的条目数,多出来的只报个数


def _shown(entries: list[str]) -> str:
    shown = ", ".join(entries[:_SHOWN_ENTRIES])
    if len(entries) > _SHOWN_ENTRIES:
        shown += f" and {len(entries) - _SHOWN_ENTRIES} more"
    return shown


def check_memory_layout(layout: MemoryLayout, repo_root: Path, *, backend: str) -> None:
    """记忆根是旧布局(设计三 4.8 节,90 篇第 18 条)或有读不出的在途记录时拒绝启动,抛 `FactoryConfigError`
    (命令行退出码 2)。只读磁盘,先于任何对记忆根的写。不迁移、不静默忽略:旧库里的记忆不会被任何世界读到,
    留着会让人以为还在用。

    - 已知情境取自 `agent.yaml` 的 `scenario_ids`;文件不在时是空集,`deploy/` 下任何世界文件都算旧布局。
      `agent.yaml` 读不了的 `FactoryConfigError` 原样上抛。
    - 旧布局条目由 `MemoryLayout.legacy_entries(..., backend=)` 按后端给,文字列出至多 5 条。它只按文件名判:
      从 `agent.yaml` 删掉的情境的世界也会被报出,所以文字指向把情境加回配置或把文件移出记忆根,不指向清空目录。
    - 在途记录逐份 `read_record`:读不出在装配期报,而不是留到门② 才抛。文字里的路径相对记忆根,不回显记录内容。"""
    from factory.agent import AGENT_CONFIG, read_agent_settings        # 惰性:拖进 grpc 与 mindagent

    store = GitStore(str(repo_root))
    known = read_agent_settings(store)[0].scenario_ids if store.exists(AGENT_CONFIG) else ()
    entries = layout.legacy_entries(known, backend=backend)
    if entries:
        # 只按文件名判,分不出"已从 agent.yaml 移除的情境的世界"和"旧的按人格库":指向移回或移走,不指向清空
        # 提示照 FC-37 不变;旧的"快照丢了 .world 旁注"一句删掉:接服务后没有旁注,补写旁注救不回任何东西
        raise FactoryConfigError(
            f"memory root has the old per-persona layout ({_shown(entries)}); "
            "if any of these are worlds of scenarios removed from agent.yaml, add the scenarios back to agent.yaml "
            "or move the files out of the memory root; delete them only once they are confirmed to be old "
            "per-persona libraries"
        )
    for record in layout.any_uncommitted_snapshots():
        try:
            layout.read_record(record)
        except MindAgentDataError:
            rel = record.relative_to(layout.root).as_posix()       # 不回显记录内容和绝对路径
            raise FactoryConfigError(
                f"memory root has an in-flight record that cannot be read ({rel}); "
                "fix its world_id, snapshot_id and digest, or delete it once its held snapshot is dealt with"
            ) from None


def stand_in_deployment_memory(repo_root: Path) -> DeploymentMemory:
    """替身部署记忆:FileTupleStore(<根>/deploy,held 快照在 <根>/held,重放世界在 <根>/replay) 与 MemoryLayout(<根>)
    共用一个根,运行时身份是替身那份的拷贝。
    造之前先过 `check_memory_layout`(后端 `stand-in`),旧布局拒绝启动。
    启动日志不在这里记:选后端是 `memory_wiring` 的事,它只记一次。"""
    root = stand_in_memory_root(repo_root)
    layout = MemoryLayout(root)
    check_memory_layout(layout, repo_root, backend="stand-in")
    return DeploymentMemory(
        FileTupleStore(root / "deploy", held_root=root / "held", replay_root=root / "replay"),
        layout, copy.deepcopy(STAND_IN_MEMORY_RUNTIME))


# ---------------- Starling 那一路 ----------------

def starling_memory_resolver(
    session, *, prefix: str, root_label: str, empty_seed_world: str, runtime: dict,
) -> Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]:
    """Starling 的 `memory_resolver`:派发器进策略上下文之前调用,给每个情境的起点世界摘要。

    - `seed` 为 None:每个情境都从空种子 `<前缀>.seed-empty` 起,摘要每次向服务现取(rollout 世界从它分叉,分叉出的摘要
      与它相等)。
    - `seed` 非空:每个情境按标签找种子(`find_seeds`),取版本最大那份的摘要;该情境没有种子就用空种子的摘要。
      这个 `seed_id` 一个情境都没有种子:`DirectiveFailedError`(30 篇失败表,原因写种子 id)。
    - 服务的瞬态失败(连不上、超时、会话致命)抛 `DeferredError`,派发器原样上抛、指令留 pending(30 篇 MT-32);
      永久失败抛 `MemoryFailureError` 不接,派发器只报类名、判 T2。

    `runtime` 在这里拷一份,每次返回再拷一份:调用方改返回值、装配方事后改原字典,都不影响解出的身份。
    `simple_actio.memory_starling` 在被调用时才导入(它导入 `starling_client`)。"""
    identity = copy.deepcopy(runtime)

    def resolve(seed: str | None, scenario_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]:
        from simple_actio.memory_starling import world_digest

        try:
            empty = world_digest(session, name=empty_seed_world, labels={"repo": prefix, "kind": "seed-empty"})
            found = {} if seed is None else {
                s: find_seeds(session, prefix=prefix, root_label=root_label, seed_id=seed, scenario_id=s)
                for s in scenario_ids}
        except MemoryFailureError as exc:
            if exc.transient:
                raise DeferredError(f"memory service: {exc}") from None
            raise
        if seed is not None and not any(found.values()):
            raise DirectiveFailedError(f"memory.seed {seed!r:.60}: no seed world under this id")
        return ({s: found[s][0]["digest"] if found.get(s) else empty for s in scenario_ids},
                copy.deepcopy(identity))

    return resolve


def _seed_version(world: dict) -> int | None:
    """种子世界 `version` 标签的整数值;不是正整数(不是 rise 建的)返回 None。"""
    value = (world.get("labels") or {}).get("version")
    if not isinstance(value, str) or not value.isdigit() or int(value) < 1:
        return None
    return int(value)


def find_seeds(session, *, prefix: str, root_label: str, seed_id: str, scenario_id: str) -> list[dict]:
    """按标签找种子(00 篇"按标签找种子"):`list_worlds` 按 `repo`、`root`、`kind=seed`、`seed_id`、`scenario_id` 查,
    带摘要,翻完所有页,只留 `seed` 为真、`version` 是正整数的项,按 `(version, created_at, name)` 降序。
    同版本有几份(人在看板升级出来的种子照抄旧种子的标签)时 `created_at` 大的在前,再相同名字大的在前。
    `factory seed` 与 Starling 的解析器、训练世界共用。`simple_actio.memory_starling` 在调用时才导入。"""
    from simple_actio.memory_starling import list_all_worlds

    labels = {"repo": prefix, "root": root_label, "kind": "seed", "seed_id": seed_id, "scenario_id": scenario_id}
    rows = [w for w in list_all_worlds(session, labels, with_digest=True)
            if w.get("seed") and _seed_version(w) is not None]
    return sorted(rows, key=lambda w: (_seed_version(w), str(w.get("created_at") or ""), str(w.get("name") or "")),
                  reverse=True)


class TrainingWorlds(Protocol):
    """训练路的 rollout 世界(设计 5.1、5.2、6.3 节):一条 rollout 一个世界,训练位与对手位都是世界里的主体。

    - `bind(run_id, seed, digests)`、`unbind(run_id)`、`seed_of(run_id)`:训练 run 进入策略上下文时绑定种子 id 与派发器
      解出的起点摘要 `memory.digests`(按情境),退出时解绑。Starling 按摘要为每个情境钉住一份种子;替身只存不用。
    - `open_rollout(run_id, scenario_id, episode_id, holder)`:客户端建世界(替身是空世界;Starling 从钉住的种子分叉),
      登记进 `episode_id → 世界` 表,开训练位主体的端口。同名世界在保留表里
      (服务端端口没关上)时先有界等它的端口关上(默认 70 秒),仍没关上才抛瞬态 `MemoryFailureError`。
    - `join_rollout(episode_id, holder)`:服务端在已登记的世界里开对手位主体的端口;表里没有抛非瞬态 `MemoryFailureError`。
    - `release_session(episode_id, ports)`:服务端在会话终态关自己的端口。
    - `close_rollout(episode_id, ports, *, wait_s)`:客户端关训练位端口,等服务端端口关完,删(替身)或关(Starling)世界、
      摘表项,返回真;等不到就把世界移进保留表、记 warning、返回假,不抛。"""

    def bind(self, run_id: str, seed: str | None, digests: Mapping[str, str]) -> None: ...

    def unbind(self, run_id: str) -> None: ...

    def seed_of(self, run_id: str) -> str | None: ...

    def open_rollout(self, run_id: str, scenario_id: str, episode_id: str, holder: str) -> MemoryBackend: ...

    def join_rollout(self, episode_id: str, holder: str) -> MemoryBackend: ...

    def close_rollout(self, episode_id: str, ports: Sequence[object], *,
                      wait_s: float | None = None) -> bool: ...

    def release_session(self, episode_id: str, ports: Sequence[object]) -> None: ...


class _World:
    """表里的一项:世界本身(替身是 TupleMemory,Starling 是完整世界名)、开着的端口数、等端口全关用的 Condition。
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

    子类实现 `_create`(建一个世界)、`_port`(在世界里开一个主体的端口)、`_discard`(删或关世界),可选 `_describe`
    (日志里的物理世界名)。异常与日志文字不含路径、端点与密钥:只有 episode id、世界名与异常类名或码。"""

    def __init__(self, *, held_wait_s: float = SESSION_CLOSE_WAIT_S,
                 close_wait_s: float = SESSION_CLOSE_WAIT_S) -> None:
        self._lock = threading.Lock()
        self._seeds: dict[str, str | None] = {}
        self._pins: dict[str, dict[str, tuple[str, str]]] = {}     # run_id → {scenario_id: (起点世界名, 摘要)}
        self._worlds: dict[str, _World] = {}
        self._held: dict[str, _World] = {}
        self.held_wait_s = held_wait_s          # open_rollout 等保留世界端口关上的期限;测试传小值
        self.close_wait_s = close_wait_s        # close_rollout 不给 wait_s 时等服务端端口关完的期限(外部模式按预算推出)

    # ---- 绑定表 ----

    def bind(self, run_id: str, seed: str | None, digests: Mapping[str, str]) -> None:
        """记下这个 run 的种子 id(None 即从空世界起)与起点摘要。同 run 再绑则覆盖。子类的 `_pin` 先算出每个情境
        钉住的起点,失败时整个不绑(表里没有这个 run)。"""
        pins = self._pin(run_id, seed, dict(digests))
        with self._lock:
            self._seeds[run_id] = seed
            self._pins[run_id] = pins

    def unbind(self, run_id: str) -> None:
        """没绑过也不报错。已经开出的世界不受影响,各自随 rollout 的终态删除。"""
        with self._lock:
            self._seeds.pop(run_id, None)
            self._pins.pop(run_id, None)

    def seed_of(self, run_id: str) -> str | None:
        """没绑定抛 `KeyError`:策略上下文还没进入,或者已经退出。"""
        with self._lock:
            return self._seeds[run_id]

    def _pinned(self, run_id: str, scenario_id: str) -> tuple[str, str] | None:
        """这个 run 为该情境钉住的 `(起点世界名, 摘要)`;没钉(替身,或 bind 时没给这个情境)返回 None。"""
        with self._lock:
            return self._pins.get(run_id, {}).get(scenario_id)

    # ---- 子类实现 ----

    def _pin(self, run_id: str, seed: str | None, digests: dict[str, str]) -> dict[str, tuple[str, str]]:
        """bind 时为每个情境定下起点;缺省不钉。"""
        return {}

    def _create(self, run_id: str, scenario_id: str, episode_id: str, seed: str | None) -> object:
        raise NotImplementedError

    def _port(self, world: object, holder: str, release: Callable[[], None]) -> MemoryBackend:
        raise NotImplementedError

    def _discard(self, world: object) -> None:
        raise NotImplementedError

    def _describe(self, world: object) -> str:
        """warning 里跟在 episode id 后面的物理世界名;替身没有。"""
        return ""

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
                      wait_s: float | None = None) -> bool:
        """`wait_s` 不给取 `close_wait_s`。关客户端的端口,等这个世界的端口计数归零(服务端在会话终态关自己的端口),摘表项、删世界,返回真。

        - 计数在 `wait_s` 内没归零:世界移进保留表,记 warning(只有 episode id),返回假,不抛。这条 rollout 的轨迹
          已经拿到了,照常结束;同名重试在端口全关之前判瞬态,世界文件留给重试的先删后建或启动清扫。
        - 客户端自己有端口关不上(Starling 的在途调用没返回):剩下的照样关,不等、不删,世界移进保留表,
          记 warning,重抛第一个异常(调用方据此判训练位记忆失败)。
        - 世界删不掉(`OSError`):端口都已关上,不进保留表,记 warning,返回假,不抛。
        - 表里没有这个世界(从没开成,或已经收过尾):关完端口返回真。"""
        wait_s = self.close_wait_s if wait_s is None else wait_s
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
                _log.warning("rollout world %s%s kept: a client port did not close (%s)",
                             episode_id, self._describe(rec.world), type(first).__name__)
            raise first
        if rec is None:
            return True
        if not done:
            _log.warning("rollout world %s%s kept: server ports were still open after %gs",
                         episode_id, self._describe(rec.world), wait_s)
            return False
        try:
            self._discard(rec.world)
        except OSError as exc:
            _log.warning("rollout world %s%s kept: removal failed (%s)",
                         episode_id, self._describe(rec.world), type(exc).__name__)
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
    `MemoryFailureError`(派发器的替身解析器早已拒绝过种子,走不到这里)。`bind` 收到的起点摘要不用:起点恒是空世界。"""

    def _create(self, run_id: str, scenario_id: str, episode_id: str, seed: str | None) -> TupleMemory:
        if seed is not None:
            raise MemoryFailureError("the stand-in memory has no seed libraries", transient=False)
        return TupleMemory()

    def _port(self, world: object, holder: str, release: Callable[[], None]) -> MemoryBackend:
        return _CountedPort(world.world(holder), release)

    def _discard(self, world: object) -> None:
        pass


# 分叉回 `world_exists` 时换后缀重试的次数上限(90 篇分叉的命名与重试表)
FORK_ATTEMPTS = 3


class StarlingTrainingWorlds(_RolloutWorlds):
    """Starling 的 rollout 世界(90 篇"接服务后 StarlingTrainingWorlds 的变化"表):每条 rollout 从起点分叉一个可回收世界
    `<前缀>.ro.<episode_id>.<后缀>`,`retention_group` 是 run id,传完整的 rise 设置;各主体在当前服务会话里开句柄。
    rollout 收尾只关世界不删(客户端没有删世界的接口),可回收世界留给 Starling 清理,所以没有启动清扫。

    起点(`bind` 时钉住,`_pin`):`seed` 为 None 时每个情境是空种子 `empty_seed = (世界名, 摘要)`;否则每个情境在
    `find_seeds` 的结果里取摘要等于派发器解出的 `digests[sid]` 的版本最大的那份,钉住它的世界名与摘要。解析之后有人
    `factory seed --new-version` 建了新版本,在跑的 run 也不换起点。该情境没有对得上的种子而 `digests[sid]` 等于空种子摘要
    时钉空种子;一份都对不上判永久失败,不绑。

    分叉(`_create`):从钉住的起点分叉。调用之前定下 8 位后缀与确切世界名,`attempt` 标签在进程内按 episode id 从 1 递增,只给人看。
    - 成功:返回摘要不等于钉住的摘要判永久(第二道);否则用它。
    - `world_schema_behind`:种子落后于当前 schema,判永久,原因写"在看板里升级种子"。
    - `world_exists`:换后缀重试,至多 `FORK_ATTEMPTS` 次。
    - `service_timeout`:服务端可能已经建好了。按确切世界名查 `list_worlds`(`repo`、`root`、`kind=rollout`、`episode_id`
      标签,带摘要),查到且摘要等于起点摘要才用,否则换后缀重建。不按 `(episode_id, attempt)` 认:续训重跑同一 step 时
      逻辑 episode id 不变,会认领上一个进程留下、已有写入的世界。
    - 其余失败照客户端码表映射后抛。

    `admin` 缺省是 `session.admin`;测试包一层注入超时。日志文字只有 episode id、世界名与码。"""

    def __init__(self, session, *, prefix: str, root_label: str, settings: dict, empty_seed: tuple[str, str],
                 close_timeout_s: float = 60.0, held_wait_s: float = SESSION_CLOSE_WAIT_S,
                 close_wait_s: float = SESSION_CLOSE_WAIT_S,
                 admin: Callable[[str, dict], dict] | None = None) -> None:
        super().__init__(held_wait_s=held_wait_s, close_wait_s=close_wait_s)
        self._session = session
        self.prefix = prefix
        self._root_label = root_label
        self._settings = settings
        self.empty_seed = empty_seed
        self._close_timeout_s = close_timeout_s
        self._admin = admin if admin is not None else session.admin
        self._attempts: dict[str, int] = {}
        self._attempts_lock = threading.Lock()

    def _next_attempt(self, episode_id: str) -> int:
        with self._attempts_lock:
            n = self._attempts.get(episode_id, 0) + 1
            self._attempts[episode_id] = n
            return n

    def _call(self, op: str, body: dict) -> dict:
        """管理调用;注入的 `admin` 可能抛未译的客户端异常,在这里一并译成带 `code` 的 MemoryFailureError。"""
        from simple_actio.memory_starling import translate_client_failure

        try:
            return self._admin(op, body)
        except Exception as exc:
            raise translate_client_failure(exc, port_closed_by_rise=False, in_drain_window=False) from None

    def _found(self, name: str, episode_id: str, digest: str) -> bool:
        """分叉超时之后:名字等于 `name` 的 rollout 世界在,且摘要等于起点摘要。"""
        from simple_actio.memory_starling import list_all_worlds

        labels = {"repo": self.prefix, "root": self._root_label, "kind": "rollout", "episode_id": episode_id}
        rows = list_all_worlds(_Admin(self._call), labels, with_digest=True)
        return any(w.get("name") == name and w.get("digest") == digest for w in rows)

    def _pin(self, run_id: str, seed: str | None, digests: dict[str, str]) -> dict[str, tuple[str, str]]:
        if seed is None:
            return {sid: self.empty_seed for sid in digests}
        pins: dict[str, tuple[str, str]] = {}
        for sid in sorted(digests):
            rows = find_seeds(_Admin(self._call), prefix=self.prefix, root_label=self._root_label, seed_id=seed,
                              scenario_id=sid)
            match = next((w for w in rows if w.get("digest") == digests[sid]), None)      # 已按版本降序
            if match is not None:
                pins[sid] = (match["name"], match["digest"])
            elif digests[sid] == self.empty_seed[1]:
                pins[sid] = self.empty_seed
            else:
                raise MemoryFailureError(
                    f"no seed world of {seed!r:.60} for scenario {sid} has the resolved start digest", transient=False)
        return pins

    def _create(self, run_id: str, scenario_id: str, episode_id: str, seed: str | None) -> str:
        from factory import starling_names

        pin = self._pinned(run_id, scenario_id)
        if pin is None and seed is not None:
            raise MemoryFailureError(f"rollout world for {episode_id}: scenario {scenario_id} has no pinned seed",
                                     transient=False)
        source, start = self.empty_seed if pin is None else pin
        for _ in range(FORK_ATTEMPTS):
            name = starling_names.rollout_world(self.prefix, episode_id, starling_names.new_suffix())
            attempt = self._next_attempt(episode_id)
            body = {"source": source, "name": name, "snapshot_id": None,
                    "labels": starling_names.labels(self.prefix, self._root_label, "rollout", run_id=run_id,
                                                    scenario_id=scenario_id, episode_id=episode_id,
                                                    seed_id=seed or "", attempt=str(attempt)),
                    "retention_group": run_id, "reclaimable": True, "seed": False, "settings": self._settings}
            try:
                out = self._call("fork", body)
            except MemoryFailureError as exc:
                code = getattr(exc, "code", None)
                if code == "world_exists":
                    continue
                if code == "service_timeout" and self._found(name, episode_id, start):
                    return name
                if code == "service_timeout":
                    continue
                if code == "world_schema_behind":
                    behind = MemoryFailureError(
                        f"seed world {source} is behind the current schema; upgrade the seed in the dashboard",
                        transient=False)
                    behind.code = code
                    raise behind from None
                raise
            if out.get("digest") != start:
                self._discard(name)
                raise MemoryFailureError(f"rollout world {name}: fork digest does not match the pinned start",
                                         transient=False)
            return name
        raise MemoryFailureError(f"rollout world for {episode_id}: fork failed {FORK_ATTEMPTS} times", transient=False)

    def _port(self, world: object, holder: str, release: Callable[[], None]) -> MemoryBackend:
        from simple_actio.memory_starling import StarlingMemory

        session, timeout = self._session, self._close_timeout_s

        def wrap(remote, session_release, generation):
            def both() -> None:
                try:
                    session_release()
                finally:
                    release()

            return StarlingMemory(remote, generation=generation, session=session, release=both,
                                  close_timeout_s=timeout)

        return session.open_port(world, holder, read_only=False, settings=self._settings, wrap=wrap)

    def _discard(self, world: object) -> None:
        """关世界,只关不删。失败只记 warning(码与世界名),不抛:`close_rollout` 只接 `OSError`,世界关不上不影响这条 rollout。"""
        try:
            self._call("close_world", {"world": world})
        except MemoryFailureError as exc:
            _log.warning("rollout world %s: close_world failed (%s)", world, getattr(exc, "code", None) or exc)

    def _describe(self, world: object) -> str:
        return f" (world {world})" if isinstance(world, str) else ""


class _Admin:
    """把一个 `admin(op, body)` 函数包成 `list_all_worlds` 要的会话形状。"""

    def __init__(self, admin: Callable[[str, dict], dict]) -> None:
        self.admin = admin


# ---------------- 组合根选后端 ----------------

# 部署路的等提交与整条流的每轮(设计二 6.4 节、FC-31、FC-51)在 `MemoryWiring.budget`:拉起模式 40 秒与 40 秒
# (`factory.memory_budget.SPAWN_LAYERS`),外部模式按服务的端点推出。替身没有网络调用,保持 AgentRuntime 默认的 5 秒与 15 秒。

_INSTALL_HINT = "run pip install '.[memory]' (or uv sync --extra memory)"


@dataclass(frozen=True)
class MemoryWiring:
    """组合根按记忆配置选出的后端:装配 `assemble` 要的全部东西。

    `deployment` 只在调用方要部署记忆(启用 start_agent_round 或 admit_proposal)时非空;`training_worlds` 两个后端都有
    (替身是 `StandInTrainingWorlds`),策略工厂与运行时共用这一个。替身的后三项都是空的:
    - `on_locked`:持仓库锁之后才跑的检查(90 篇"世界设置"一段、FC-48、第 23 条),只交出函数、不在装配时调
      (`run_daemon(on_locked=)`、`reconcile_one_tick` 的锁内)。第二个进程撞锁之前不能开过任何句柄。
    - `closers`:按登记顺序是关会话、拉起模式下停服务;组合根最先登记,逆序执行时最后跑。
    - `session`:服务会话,组合根据它查致命状态。
    - `budget`:各层时限(`factory.memory_budget`)。替身与拉起模式是固定的 `SPAWN_LAYERS`,外部模式按服务的端点推出。"""

    backend: str
    deployment: DeploymentMemory | None
    memory_resolver: Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]
    training_worlds: TrainingWorlds
    on_locked: Callable[[], None] | None = None
    closers: tuple[Callable[[], None], ...] = ()
    session: object | None = None
    budget: MemoryTimeBudget = SPAWN_LAYERS


def _known_scenarios(repo_root: Path) -> tuple[str, ...]:
    from factory.agent import AGENT_CONFIG, read_agent_settings        # 惰性:拖进 grpc 与 mindagent

    store = GitStore(str(repo_root))
    return read_agent_settings(store)[0].scenario_ids if store.exists(AGENT_CONFIG) else ()


def deploy_world_check(session, store, repo_root: Path) -> Callable[[], None]:
    """持仓库锁之后的部署世界核对(`MemoryWiring.on_locked`,90 篇"世界设置"一段、FC-48、第 23 条):对 `agent.yaml` 里
    每个情境,部署世界不在而创建标记在,或核对不过(不是 rise 建的、属于另一个记忆根),或带 rise 设置开不了只读句柄
    (`world_settings_mismatch`),都抛 `FactoryConfigError`(退出码 2),文字写世界名与码。世界不在也没有标记:
    没东西可查,第一次用时再建。

    只读句柄直接经会话开(管理面的 `open` 只开可写句柄,`open_replay` 只开重放世界),开完就关。不开可写句柄:
    它会让正在跑的另一个守护进程的恢复收到 `world_held`。"""
    def check() -> None:
        for scenario_id in _known_scenarios(repo_root):
            name = None
            try:
                name = store.verify_world(scenario_id)
                if name is None:
                    continue
                remote, release = session.open_port(name, "rise-check", read_only=True, settings=store.settings,
                                                    wrap=lambda remote, release, generation: (remote, release))
                try:
                    remote.close(timeout=HANDLE_CHECK_CLOSE_S)
                finally:
                    release()
            except MemoryFailureError as exc:
                code = getattr(exc, "code", None)
                what = f"world {name}: {code}" if name is not None and code else str(exc)
                raise FactoryConfigError(f"memory service: {what}") from None
            except Exception as exc:          # 关只读句柄的超时之类:客户端异常的类名
                raise FactoryConfigError(f"memory service: world {name}: {type(exc).__name__}") from None

    return check


HANDLE_CHECK_CLOSE_S = 30.0     # 关持锁核对用的只读句柄:没有在途调用,用句柄调用的超时


def _service_endpoint(config: MemoryConfig, env: Mapping[str, str], closers: list):
    """拉起或连接服务,返回 `(url_of, token_of, restart)`。拉起模式下 `closers` 里登记停服务;外部模式下 token 从
    `token_file` 读(去掉首尾空白),rise 不碰服务进程。"""
    if config.mode == "external":
        try:
            token = config.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FactoryConfigError(f"memory config: token_file cannot be read ({type(exc).__name__})") from None
        if not token:
            raise FactoryConfigError("memory config: token_file is empty")
        url = config.url
        return (lambda: url), (lambda: token), None
    from factory.starling_service import spawn_service

    service = spawn_service(config.root, env)
    closers.append(service.close)
    return (lambda: service.url), (lambda: service.token), service.restart


@dataclass(frozen=True)
class ServiceHandle:
    """打开了的记忆服务(`open_memory_service` 的结果),`memory_wiring` 与 `factory seed` 共用。
    `closers` 按登记顺序是关会话、拉起模式下停服务;`close()` 照这个顺序全跑一遍,一个失败也跑完剩下的。"""

    session: object
    config: MemoryConfig
    settings: dict
    prefix: str
    root_label: str
    layout: MemoryLayout
    runtime: dict
    empty_seed: tuple[str, str]
    closers: tuple[Callable[[], None], ...]
    budget: MemoryTimeBudget = SPAWN_LAYERS

    def close(self) -> None:
        first: Exception | None = None
        for close in self.closers:
            try:
                close()
            except Exception as exc:
                if first is None:
                    first = exc
        if first is not None:
            raise first


def _close_quietly(closers: Sequence[Callable[[], None]]) -> None:
    for close in closers:
        try:
            close()
        except Exception:
            pass          # 正在传播的才是根因


def open_memory_service(repo_root: Path, env: Mapping[str, str]) -> ServiceHandle | None:
    """装配顺序的 ① 至 ⑦(90 篇 4c 前一行、第 12 至 23 条),`memory_wiring` 与 `factory seed` 共用。
    ① 读 RISE_MEMORY_CONFIG:没设(或空串)返回 None(替身),不导入 `starling_client`、不拉起服务;② 安装来源:
    `starling-client` 非 editable、有 git 提交,拉起模式下 `starling-memory` 也要且两者同一提交;③ 拉起模式下
    `OPENAI_API_KEY` 非空;④ 认领记忆根、查旧布局;⑤ 拉起或连接服务,造服务会话(协议、token、提交不一致在这里退出码 2);
    ⑥ 算 rise 设置、运行时身份与各层时限(外部模式按服务端点推出,并据此换上会话的句柄超时与排空窗口);
    ⑦ `ensure_world` 空种子。**不**开任何部署世界的句柄。⑤ 之后任何一步失败,先关会话、
    停服务再上抛。配置有任何问题都是 `FactoryConfigError`(退出码 2)。"""
    config = read_memory_config(env=env, repo_root=repo_root)
    if config is None:
        return None
    from factory.memory_config import CLIENT_DIST, STARLING_DIST, client_install_commit

    pinned = client_install_commit(CLIENT_DIST)
    if config.mode == "spawn":
        server = client_install_commit(STARLING_DIST)
        if server != pinned:
            raise FactoryConfigError(
                f"{CLIENT_DIST} is at {pinned} but {STARLING_DIST} is at {server}; install both from one commit")
        check_api_key(env)
    claim_memory_root(config.root, repo_root)
    memory_layout = MemoryLayout(config.root)
    check_memory_layout(memory_layout, repo_root, backend="starling")
    try:
        from factory import starling_names
        from factory.starling_session import ServiceSession, service_runtime
        from simple_actio.memory_starling import ensure_empty_seed
    except ImportError as exc:
        raise FactoryConfigError(
            f"{MEMORY_CONFIG_ENV} is set but the Starling client cannot be loaded ({type(exc).__name__}); "
            f"{_INSTALL_HINT}"
        ) from None
    closers: list[Callable[[], None]] = []
    try:
        url_of, token_of, restart = _service_endpoint(config, env, closers)
        session = ServiceSession(url_of=url_of, token_of=token_of, pinned_commit=pinned, restart=restart)
        closers.insert(0, session.close)                 # 先关会话,再停服务
        settings = starling_names.rise_settings(config, session.info)     # 外部模式的端点取自服务默认世界
        runtime = service_runtime(session.info, settings, config)
        budget = _time_budget(config, settings)
        session.retune(handle_timeout_s=budget.handle_s, drain_window_s=budget.drain_window_s)
        _log.info("%s", budget.describe())
        prefix, label = config.world_prefix, starling_names.root_label(config.root)
        try:
            empty_seed = ensure_empty_seed(session, names=starling_names, prefix=prefix, root_label=label,
                                           settings=settings)
        except MemoryFailureError as exc:
            raise FactoryConfigError(f"memory service: {exc}") from None
    except BaseException:
        _close_quietly(closers)
        raise
    return ServiceHandle(session, config, settings, prefix, label, memory_layout, runtime, empty_seed, tuple(closers),
                         budget)


def _time_budget(config: MemoryConfig, settings: dict) -> MemoryTimeBudget:
    """⑥ 之后:外部模式按 rise 设置里的两个端点推出各层(FC-51),拉起模式是固定的 `SPAWN_LAYERS`(算出的最坏只进日志)。"""
    if config.mode == "external":
        return memory_time_budget(settings["extraction"], settings["embedding"])
    return spawn_time_budget(settings["extraction"], settings["embedding"])


def memory_wiring(repo_root: Path, env: Mapping[str, str], *, deployment: bool) -> MemoryWiring:
    """读 RISE_MEMORY_CONFIG 选后端,只在这里记一次启动日志。日志与异常文字都不含记忆根目录、端点地址与 token(第 10 节)。

    - 没设(或空串):替身。`deployment` 为真才造替身的部署记忆。不导入 `starling_client`,不拉起服务。
    - 设了:① 读配置,② 至 ⑦ 由 `open_memory_service` 做;再造部署管理面、训练世界与解析器。**不**开任何部署世界的
      句柄:已有部署世界的核对在持锁之后(`on_locked`)。服务打开之后任何一步失败,先关会话、停服务再上抛。"""
    service = open_memory_service(repo_root, env)
    if service is None:
        _log.info("memory backend: stand-in")
        return MemoryWiring(
            "stand-in", stand_in_deployment_memory(repo_root) if deployment else None,
            stand_in_memory_resolver, StandInTrainingWorlds(),
        )
    config = service.config
    try:
        from factory import starling_names
        from simple_actio.memory_starling import StarlingDeploymentStore

        session, prefix, label, settings = service.session, service.prefix, service.root_label, service.settings
        budget = service.budget
        store = StarlingDeploymentStore(session, names=starling_names, prefix=prefix, root_label=label,
                                        settings=settings, marker_dir=config.root / "deploy",
                                        empty_seed_digest=service.empty_seed[1], close_timeout_s=budget.port_close_s)
        worlds = StarlingTrainingWorlds(session, prefix=prefix, root_label=label, settings=settings,
                                        empty_seed=service.empty_seed, close_timeout_s=budget.port_close_s,
                                        held_wait_s=budget.close_wait_s, close_wait_s=budget.close_wait_s)
        resolver = starling_memory_resolver(session, prefix=prefix, root_label=label,
                                            empty_seed_world=service.empty_seed[0], runtime=service.runtime)
    except BaseException:
        _close_quietly(service.closers)
        raise
    deployment_memory = None
    if deployment:
        deployment_memory = DeploymentMemory(store, service.layout, copy.deepcopy(service.runtime), budget)
    _log.info("memory backend: starling")
    return MemoryWiring("starling", deployment_memory, resolver, worlds,
                        on_locked=deploy_world_check(session, store, repo_root) if deployment else None,
                        closers=service.closers, session=session, budget=budget)
