"""组合根的记忆后端:替身运行时身份与替身解析器(设计二 7.1 节)。

派发器在进入策略上下文之前调用注入的 `memory_resolver(seed, persona_ids)`,拿回每个人格的起点记忆摘要与
记忆运行时身份,两者一起进 config_hash 与训练记录。本模块有两路:

- 替身:每个人格都从空库起步,没有任何种子库可用。
- Starling(`StarlingLayout`、`starling_memory_runtime`、`starling_memory_resolver`、`TrainingCopies`,按
  RISMFORGE_MEMORY_CONFIG 装配):种子库由人放进 `seeds/<seed_id>/`,本仓库只读;解析器按种子库算每个人格的
  起点摘要;`TrainingCopies` 给训练路的每个对手位(按会话)与每个训练位(按 rollout)从种子复制一份私有库,
  终态即删。

本模块不导入 `starling`(全仓库只有 `actio/memory_starling.py` 导入它),没装 Starling 时 `import factory.memory_runtime`
照常成功,替身路径照常起得来。Starling 的版本与抽取提示摘要由调用方从 `actio.memory_starling` 取来传进
`starling_memory_runtime`;`starling_memory_resolver` 在被调用时才延迟导入 `actio.memory_starling`,所以缺依赖
在装配期响,不拖到派发时。`TrainingCopies` 同理:`StarlingMemory`、`copy_library` 在方法内才导入,没装 Starling
时类本身可以导入,只是造不出副本。

运行时身份里没有记忆根目录:同一份库搬了目录不是另一个实验,路径进了哈希会让换机即 T3。

组合根按记忆配置选后端(`memory_wiring`,设计二 5.1、6.2、6.4 节,第 10 节拒绝项):没设 RISMFORGE_MEMORY_CONFIG 走替身,
设了就装配 Starling,配置有任何问题在装配期抛 `FactoryConfigError`(命令行退出码 2),不退回替身静默跑。

部署记忆(设计二第 9 节):轮次处理器与门② 共用的一份 DeploymentMemory。替身的根目录在这棵工作树的
git 目录里(与仓库锁 rismforge.lock 同处):不进工作树、不进提交,跨进程重启保留,所以起点副本与门② 收录
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
from typing import TYPE_CHECKING

from actio.memory import FileTupleStore, TupleMemory
from actio.memory_port import PERSONA_ID_PATTERN, DeploymentMemoryStore, MemoryFailureError
from actio.trace import TRACE_SCHEMA_V2
from controlplane.daemon import DirectiveFailedError
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
from mindagent.memory_layout import MemoryLayout
from simulator.realtime.validation import validate_episode_id

if TYPE_CHECKING:       # 只给类型注解用:运行时不导入 actio.memory_starling(它导入 starling)
    from actio.memory_starling import StarlingEndpoints, StarlingMemory

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
    """替身部署记忆:FileTupleStore(<根>/deploy) 与 MemoryLayout(<根>) 共用一个根,运行时身份是替身那份的拷贝。
    启动日志不在这里记:选后端是 `memory_wiring` 的事,它只记一次。"""
    root = stand_in_memory_root(repo_root)
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


class TrainingCopies:
    """训练路的私有记忆副本(设计二 5.3、6.2、7.2 节):对手位按会话、训练位按 rollout,各拿一份从种子库复制出来的
    Starling 库,终态即删。

    - 绑定表:训练 run 进入策略上下文时 `bind(run_id, seed)`、退出时 `unbind`。表里的种子 id 决定这个 run 的
      副本从哪取起点,没有种子(None)就是新建空库。一把锁护这张表;副本本身各在各的目录里,互不共享状态。
    - 起点:`layout.seed(seed, persona_id)` 在就**用文件复制**取副本(`actio.memory_starling.copy_library`),种子里
      没有这个人格就新建空库。不用 `backup_to`:它要先把种子打开,`Memory.open` 会建 -wal、-shm 并跑迁移,
      种子目录与文件只读(5.3 节)时失败。种子没有写者,复制 `.db` 加 `-wal` 是一致的,所以副本的摘要等于
      解析器经 `library_digest` 算出的起点摘要。同一组 rollout 因此从同一个起点出发,又彼此看不见对方写的东西。
    - 删除:`release` 先把所有端口都关上,全部成功才删目录。关不上说明 Starling 还有在途调用,删目录会删掉一个
      仍在写的库;留下的目录由下次启动的 `sweep` 兜底。
    - 保留表:因端口关不上而保留的目录登记在 `_held`(同一把锁),`release` 后来成功删掉时撤销。两个 `open_*_copy`
      遇到登记过的目录抛非瞬态 `MemoryFailureError`、不删它:同一 episode id 的重试若在 close 超时之后到来,
      先删后建会删掉一个 Starling 工作线程还在写的库。今天的调用方走不到这里(轨迹源只重试瞬态失败),
      但防线要在这个类自己身上,不能靠三层调用方的约定。

    异常文字不含路径、端点与密钥:日志里只有目录名(会话 id 或 episode id)与异常类名。"""

    def __init__(self, layout: StarlingLayout, endpoints: StarlingEndpoints, *,
                 close_timeout_s: float = 60.0) -> None:
        self._layout = layout
        self._endpoints = endpoints
        self._close_timeout_s = close_timeout_s
        self._lock = threading.Lock()
        self._seeds: dict[str, str | None] = {}
        self._held: set[Path] = set()      # 因端口没关上而保留的目录;同一把锁护着

    # ---- 绑定表 ----

    def bind(self, run_id: str, seed: str | None) -> None:
        """记下这个 run 的种子 id(None 即从空库起)。同 run 再绑则覆盖。"""
        with self._lock:
            self._seeds[run_id] = seed

    def unbind(self, run_id: str) -> None:
        """没绑过也不报错。已经开出的副本不受影响,各自随会话或 rollout 的终态删除。"""
        with self._lock:
            self._seeds.pop(run_id, None)

    def seed_of(self, run_id: str) -> str | None:
        """没绑定抛 `KeyError`:策略上下文还没进入,或者已经退出。"""
        with self._lock:
            return self._seeds[run_id]

    # ---- 目录 ----

    def session_dir(self, run_id: str, session_id: str) -> Path:
        """`runs/<run_id>/<session_id>/`:一个会话所有对手位副本的目录。名字过布局的白名单(不合法抛 ValueError)。"""
        return self._layout.session_copy(run_id, session_id, "x").parent

    def client_dir(self, run_id: str, episode_id: str) -> Path:
        """`runs/<run_id>/client/<episode_id>/`:一条 rollout 的训练位副本的目录。"""
        return self._layout.client_copy(run_id, episode_id, "x").parent

    # ---- 开副本 ----

    def _refuse_if_held(self, directory: Path) -> None:
        """目录是 `release` 因端口没关上而保留的:那个库可能还有 Starling 工作线程在写,不能删也不能往里建。"""
        with self._lock:
            held = directory in self._held
        if held:
            raise MemoryFailureError(
                f"training copy {directory.name} is still held by a port that did not close", transient=False)

    def _open(self, run_id: str, path: Path, persona_id: str) -> StarlingMemory:
        from actio.memory_starling import StarlingMemory, copy_library, translate_failure

        seed = self.seed_of(run_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if seed is not None:
                source = self._layout.seed(seed, persona_id)
                try:
                    source.lstat()      # 与解析器同一判法:只有"确实没有这个文件"才从空库起,悬空的链接交给复制去报错
                except FileNotFoundError:
                    pass
                else:
                    copy_library(source, path)
        except OSError as exc:
            # 磁盘满、权限不够:译成非瞬态的记忆失败(文字只含类名)。裸 OSError 不会被训练位一侧当成记忆失败
            raise translate_failure(exc) from None
        return StarlingMemory.open(
            path, persona_id=persona_id, endpoints=self._endpoints, close_timeout_s=self._close_timeout_s)

    def open_session_copy(self, run_id: str, session_id: str, slot_id: str, persona_id: str) -> StarlingMemory:
        """一个对手位在一个会话里的副本。同一会话的各位共用 `session_dir`,由调用方在会话终态 `release` 一次。
        会话目录若是被保留的(见 `_held`)抛非瞬态 `MemoryFailureError`。"""
        path = self._layout.session_copy(run_id, session_id, slot_id)
        self._refuse_if_held(path.parent)
        return self._open(run_id, path, persona_id)

    def open_client_copy(self, run_id: str, episode_id: str, slot_id: str, persona_id: str) -> StarlingMemory:
        """训练位在一条 rollout 里的副本。同名目录先删后建:重试在同一线程里顺序发生,上一次尝试的上下文已经退出,
        它的目录若还留着(`release` 没删成),删了重来才能从起点出发。但目录是因端口没关上而保留的(见 `_held`)
        时不删,抛非瞬态 `MemoryFailureError`。打不开时把刚建的目录删掉:这个目录只属于这一个位,没有别的端口在里面。"""
        path = self._layout.client_copy(run_id, episode_id, slot_id)
        self._refuse_if_held(path.parent)
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            return self._open(run_id, path, persona_id)
        except BaseException:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise

    # ---- 收尾 ----

    def release(self, ports: Sequence[object], directory: Path) -> bool:
        """关掉所有端口,全部成功才删 `directory`,返回真。

        逐个 `close()` 且**一个关不上也继续关剩下的**:不关的话,那些端口的 SQLite 连接会一直开到进程退出。
        有端口关不上时(Starling 的在途调用没返回,`close(timeout)` 等不到)记一条 warning、不删目录,
        把目录登记进保留表(`open_*_copy` 据此拒绝往里重建),重抛第一个异常;调用返回后再 `release` 一次就能删,
        同时撤销登记。目录已经不在(会话没来得及开出副本)算删成功。
        `rmtree` 自己失败只记 warning、返回假、不抛:端口都已关上,不登记,留下的目录由 `sweep` 兜底。"""
        first: Exception | None = None
        for port in ports:
            try:
                port.close()
            except Exception as exc:
                if first is None:
                    first = exc
        if first is not None:
            with self._lock:
                self._held.add(directory)
            _log.warning("memory copy %s kept: a port did not close (%s)", directory.name, type(first).__name__)
            raise first
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            with self._lock:
                self._held.discard(directory)       # 端口都关上了,没有谁还在写
            _log.warning("memory copy %s kept: removal failed (%s)", directory.name, type(exc).__name__)
            return False
        with self._lock:
            self._held.discard(directory)
        return True

    def sweep(self) -> int:
        """删掉 `runs/` 下的每个子项,返回删了几项;`deploy/`、`seeds/`、`snapshots/` 不碰。启动时在仓库锁里调用:
        上一个进程崩溃时留下的副本在这里清掉。`runs/` 下的符号链接只删链接本身,不跟进去(跟着删会删到
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

    `deployment` 只在调用方要部署记忆(启用 start_agent_round 或 admit_proposal)时非空;`training_copies` 与 `sweep`
    只有 Starling 才有。`sweep` 只交出函数、不在装配时调:清扫要等持仓库锁(`run_daemon(on_locked=)`、
    `reconcile_one_tick` 的锁内),另一个守护进程可能正在用那些副本。"""

    backend: str
    deployment: DeploymentMemory | None
    memory_resolver: Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]
    training_copies: TrainingCopies | None
    sweep: Callable[[], int] | None


def memory_wiring(repo_root: Path, env: Mapping[str, str], *, deployment: bool) -> MemoryWiring:
    """读 RISMFORGE_MEMORY_CONFIG 选后端,只在这里记一次启动日志。日志与异常文字都不含记忆根目录与端点地址(第 10 节)。

    - 没设(或空串):替身。`deployment` 为真才造替身的部署记忆。
    - 设了,按序:Starling 的安装来源(未安装、editable 在这里拒绝,先于任何对 `actio.memory_starling` 的导入与对记忆
      根目录的写)、`OPENAI_API_KEY` 非空、认领记忆根目录(被另一个仓库占用就拒绝)、导入 `actio.memory_starling`。
      装了包但 C++ 扩展载不进来(`ImportError`)译成与「没装」同一条安装提示的 `FactoryConfigError`。"""
    config = read_memory_config(env=env, repo_root=repo_root)
    if config is None:
        _log.info("memory backend: stand-in")
        return MemoryWiring(
            "stand-in", stand_in_deployment_memory(repo_root) if deployment else None,
            stand_in_memory_resolver, None, None,
        )
    install = starling_install_source()
    check_api_key(env)
    claim_memory_root(config.root, repo_root)
    try:
        from actio.memory_starling import (
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
    copies = TrainingCopies(layout, endpoints)
    deployment_memory = None
    if deployment:
        deployment_memory = DeploymentMemory(
            StarlingDeploymentStore(layout.deploy_dir(), endpoints=endpoints),
            MemoryLayout(config.root, suffix=".db"),
            copy.deepcopy(runtime),
            lambda path: StarlingDeploymentStore(path, endpoints=endpoints),
        )
    _log.info("memory backend: starling")
    return MemoryWiring("starling", deployment_memory, starling_memory_resolver(layout, runtime), copies, copies.sweep)
