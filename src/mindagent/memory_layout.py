"""部署记忆根目录的磁盘布局(设计三 4.1 节,50 篇"接服务后的在途记录"一段):一个情境一个世界,世界锁、
起点快照与验证快照的在途记录。

    <根>/deploy/<scenario_id>.lock                              世界锁(两个后端共用)
    <根>/snapshots/<round_id>/deploy-<episode_id>.rec.json      起点快照的在途记录:一个 episode 一份
    <根>/snapshots/<round_id>/verify-<scenario_id>.rec.json     验证快照的在途记录:一个情境一份

快照本身是部署管理面的 held 快照,rise 只在记录里记它的 id(客户端没有列快照的接口)。记录内容是
`{"world_id", "snapshot_id", "digest"}`,自带世界 id,所以不再有 `.world` 旁注,也不解析文件名。
两个后端的记录格式相同;世界文件、held 快照与重放世界放在哪里由后端定(90 篇第 4 章布局表),
`legacy_entries` 按后端判旧布局。文件名以 `deploy-` 起头、以 `.rec.json` 结尾才是起点快照的记录;
替身旧快照 `deploy-*.json` 不带 `.rec`,分得开。旧布局(按人格建库、快照文件与旁注)不迁移,
`legacy_entries` 只负责把它们认出来。
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from simple_actio.memory_port import PERSONA_ID_PATTERN
from controlplane.lock import hold_file_lock
from mindagent.errors import MindAgentDataError

_DEPLOY_PREFIX = "deploy-"
_RECORD_SUFFIX = ".rec.json"
_RECORD_KEYS = {"world_id", "snapshot_id", "digest"}

# 判旧布局只看这四种数据后缀(90 篇第 18 条):`.tmp`、`-wal`、`-shm`、`.lock` 是写到一半或运行时的文件,
# 不是世界数据,拒绝它们会让一次崩溃变成无法启动。`.rec.json` 也以 `.json` 结尾,列出来是为了与文档逐字对应。
_DATA_SUFFIXES = (".json", ".db", ".created", _RECORD_SUFFIX)
# 每个后端在 deploy/ 下允许的 `<已知情境><后缀>`。"starling-inprocess" 是第 2、3 步的过渡后端,第 4 步删。
_DEPLOY_SUFFIX = {"stand-in": ".json", "starling-inprocess": ".db", "starling": ".created"}


def _check_segment(name: str, value: str) -> str:
    """round_id、episode_id 要当一段路径用:不许含 `/`,不许是 `.`、`..`。带轮次前缀的
    `<round_id>/deploy-...` 只进事件 key,绝不拼路径。"""
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        raise ValueError(f"{name}: {value!r}")
    return value


def _check_world(scenario_id: str) -> str:
    """scenario_id 就是世界 id:部署库按它命名,过的是部署管理面同一份白名单。布局不接受库会拒收的世界 id。"""
    if not isinstance(scenario_id, str) or not PERSONA_ID_PATTERN.fullmatch(scenario_id):
        raise ValueError(f"scenario_id: {scenario_id!r}")
    return scenario_id


def _record_problem(record: object) -> str | None:
    """记录三个字段的校验,写与读共用:返回不合格的原因(不含字段内容),合格返回 None。"""
    if not isinstance(record, SnapshotRecord):
        return "not a SnapshotRecord"
    if not isinstance(record.world_id, str) or not PERSONA_ID_PATTERN.fullmatch(record.world_id):
        return "world_id is not a valid scenario id"
    if not isinstance(record.snapshot_id, str) or not record.snapshot_id or "/" in record.snapshot_id:
        return "snapshot_id must be a non-empty string without '/'"
    if not isinstance(record.digest, str) or not record.digest:
        return "digest must be a non-empty string"
    return None


@dataclass(frozen=True)
class SnapshotRecord:
    """一份在途记录:快照属于哪个世界、管理面给的快照 id、快照时的摘要。"""

    world_id: str
    snapshot_id: str
    digest: str


@dataclass(frozen=True)
class MemoryLayout:
    root: Path

    def world_lock(self, scenario_id: str) -> Path:
        return self.root / "deploy" / f"{_check_world(scenario_id)}.lock"

    def hold_world(self, scenario_id: str) -> AbstractContextManager[None]:
        """取该情境世界的独占锁,非阻塞,被占用时抛 FileLockBusyError。"""
        return hold_file_lock(self.world_lock(scenario_id))

    def _round_dir(self, round_id: str) -> Path:
        return self.root / "snapshots" / _check_segment("round_id", round_id)

    def snapshot(self, round_id: str, episode_id: str) -> Path:
        """起点快照的在途记录路径。"""
        _check_segment("episode_id", episode_id)
        return self._round_dir(round_id) / f"{_DEPLOY_PREFIX}{episode_id}{_RECORD_SUFFIX}"

    def verify_snapshot(self, round_id: str, scenario_id: str) -> Path:
        """验证快照的在途记录路径。"""
        return self._round_dir(round_id) / f"verify-{_check_world(scenario_id)}{_RECORD_SUFFIX}"

    def write_record(self, path: Path, record: SnapshotRecord) -> None:
        """写一份在途记录。先校验再落盘;先写临时文件再改名:崩在写一半留下的半截记录会让 `read_record`
        一直报错,要人来删。`sort_keys`、`ensure_ascii=False`,人删快照前要能读懂它。"""
        problem = _record_problem(record)
        if problem is not None:
            raise ValueError(f"in-flight record: {problem}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        body = json.dumps({"world_id": record.world_id, "snapshot_id": record.snapshot_id, "digest": record.digest},
                          ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _rel(self, path: Path) -> str:
        """错误信息里的路径:相对记忆根,不泄露绝对路径。"""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.name

    def read_record(self, path: Path) -> SnapshotRecord:
        """读一份在途记录。读不出、不是 JSON 对象、键不是恰好那三个、字段过不了校验(world_id 过世界 id 白名单)
        都抛 MindAgentDataError,文字里只有相对记忆根的路径与原因,不回显记录内容:坏记录不能变成路径,
        也不能当成"不属于任何世界"悄悄放过。"""
        rel = self._rel(path)
        try:
            doc = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError) as exc:     # JSONDecodeError、UnicodeDecodeError 都是 ValueError
            raise MindAgentDataError(f"在途记录读不出: {rel}: {type(exc).__name__}") from exc
        if not isinstance(doc, dict) or set(doc) != _RECORD_KEYS:
            raise MindAgentDataError(f"在途记录不是带 world_id、snapshot_id、digest 三个键的对象: {rel}")
        record = SnapshotRecord(doc["world_id"], doc["snapshot_id"], doc["digest"])
        problem = _record_problem(record)
        if problem is not None:
            raise MindAgentDataError(f"在途记录内容不合法: {rel}: {problem}")
        return record

    def world_of(self, path: Path) -> str:
        """在途记录所属的世界,读法与失败同 `read_record`。"""
        return self.read_record(path).world_id

    @staticmethod
    def _is_record(p: Path) -> bool:
        return p.is_file() and p.name.startswith(_DEPLOY_PREFIX) and p.name.endswith(_RECORD_SUFFIX)

    def round_snapshots(self, round_id: str) -> list[Path]:
        """该轮次目录下起点快照的在途记录(`deploy-*.rec.json`),按名排序。"""
        d = self._round_dir(round_id)
        if not d.is_dir():
            return []
        return sorted((p for p in d.iterdir() if self._is_record(p)), key=lambda p: p.name)

    def any_uncommitted_snapshots(self) -> list[Path]:
        """`snapshots/*/` 下起点快照的在途记录(任何情境、任何轮次),按路径排序,读不读得出都算。
        验证记录(`verify-`)不算。门② 有它们在就延后:在途轮次重入会重读库。"""
        base = self.root / "snapshots"
        if not base.is_dir():
            return []
        return sorted(p for d in base.iterdir() if d.is_dir() for p in d.iterdir() if self._is_record(p))

    def uncommitted_snapshots(self, scenario_id: str) -> list[Path]:
        """任何轮次里 world_id 等于 scenario_id 的起点快照记录。任何一份记录读不出都抛 MindAgentDataError:
        宁可停下,也不漏判一份在途快照。"""
        _check_world(scenario_id)
        return [p for p in self.any_uncommitted_snapshots() if self.world_of(p) == scenario_id]

    def legacy_entries(self, known_scenarios: Iterable[str], *, backend: str) -> list[str]:
        """记忆根下按 backend 判属于旧布局的条目,相对记忆根的 POSIX 路径,排序(90 篇第 18 条与第 4 章布局表)。
        backend 取 "stand-in"、"starling-inprocess"(第 2、3 步的过渡)、"starling" 之一。

        - `deploy/` 直属文件只看四种数据后缀:不是 `<已知情境><本后端的后缀>` 的都报。按完整文件名比允许名单,
          所以 `s1.rec.json` 不会被当成情境 s1 的账本。其余文件(`.lock`、`.tmp`、`-wal`、`-shm` 等)不查。
        - `seeds/`、`runs/`:除了过渡后端(训练世界仍在进程内)都报目录本身,不展开里面的文件。
        - `snapshots/*/` 下的 `.db`、`.world` 与不带 `.rec` 的 `deploy-*.json`:三个后端都报。记录与 `verify/` 目录合法。
        - `held/`、`replay/` 下的 `.db`:服务后端逐个文件报(第 2、3 步过渡留下的);其余后端与其余文件不查。
        - 服务文件(`service*`、`.owner`)不查。

        读不出的在途记录不在这里报,由组合根逐份 `read_record` 报。"""
        if backend not in _DEPLOY_SUFFIX:
            raise ValueError(f"backend: {backend!r}")
        allowed = {f"{s}{_DEPLOY_SUFFIX[backend]}" for s in known_scenarios}
        found: list[Path] = []
        deploy = self.root / "deploy"
        if deploy.is_dir():
            found += (p for p in deploy.iterdir()
                      if p.is_file() and p.name.endswith(_DATA_SUFFIXES) and p.name not in allowed)
        if backend != "starling-inprocess":
            found += (p for p in (self.root / "seeds", self.root / "runs") if os.path.lexists(p))
        base = self.root / "snapshots"
        if base.is_dir():
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                found += (p for p in d.iterdir() if p.is_file() and (
                    p.name.endswith((".db", ".world"))
                    or (p.name.startswith(_DEPLOY_PREFIX) and p.name.endswith(".json")
                        and not p.name.endswith(_RECORD_SUFFIX))))
        if backend == "starling":
            for name in ("held", "replay"):
                if (self.root / name).is_dir():
                    found += (p for p in (self.root / name).rglob("*.db") if p.is_file())
        return sorted(p.relative_to(self.root).as_posix() for p in found)
