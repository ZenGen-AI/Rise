"""部署记忆根目录的磁盘布局(设计三 4.1 节):一个情境一个世界,世界锁、整世界起点快照、验证快照。

替身下 `MemoryLayout(mem_root)` 与 `FileTupleStore(mem_root / "deploy")` 共用同一个根:

    <根>/deploy/<scenario_id>.json                              部署世界
    <根>/deploy/<scenario_id>.lock                              世界锁
    <根>/snapshots/<round_id>/deploy-<episode_id>.json          起点快照:一个 episode 一份,整个世界
    <根>/snapshots/<round_id>/deploy-<episode_id>.world         快照旁注:内容是它所属的 scenario_id
    <根>/snapshots/<round_id>/verify-<scenario_id>.json         验证快照:一个情境一份
    <根>/snapshots/<round_id>/verify/                           验证临时世界

后缀由 `suffix` 给:替身是 `.json`,真实 Starling 后端是 `.db`;`.lock` 与 `.world` 的后缀固定。
快照文件名里没有情境也没有人格,起点快照属于哪个世界只读 `.world` 旁注,不解析文件名。
旁注先于快照写(崩在两步之间会留下没有快照的旁注):它不算在途快照,也不算旧布局遗留。
旁注内容就是 scenario_id,和世界 id 用同一份白名单(`PERSONA_ID_PATTERN`)校验;读旁注时只容忍末尾
恰好一个换行(人手 `echo s1 > x.world` 恢复丢失的旁注会带它),其余空白一律当坏旁注。
旧布局(按人格建库、没有旁注的快照)不迁移,`legacy_entries` 只负责把它们认出来。
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from actio.memory_port import PERSONA_ID_PATTERN
from controlplane.lock import hold_file_lock
from mindagent.errors import MindAgentDataError

_DEPLOY_PREFIX = "deploy-"
_WORLD_SUFFIX = ".world"


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


def _tag_path(snapshot: Path) -> Path:
    """快照的 `.world` 旁注:同目录、同主名。"""
    return snapshot.with_suffix(_WORLD_SUFFIX)


@dataclass(frozen=True)
class MemoryLayout:
    root: Path
    suffix: str = ".json"

    def world_lock(self, scenario_id: str) -> Path:
        return self.root / "deploy" / f"{_check_world(scenario_id)}.lock"

    def hold_world(self, scenario_id: str) -> AbstractContextManager[None]:
        """取该情境世界的独占锁,非阻塞,被占用时抛 FileLockBusyError。"""
        return hold_file_lock(self.world_lock(scenario_id))

    def _round_dir(self, round_id: str) -> Path:
        return self.root / "snapshots" / _check_segment("round_id", round_id)

    def snapshot(self, round_id: str, episode_id: str) -> Path:
        _check_segment("episode_id", episode_id)
        return self._round_dir(round_id) / f"{_DEPLOY_PREFIX}{episode_id}{self.suffix}"

    def verify_snapshot(self, round_id: str, scenario_id: str) -> Path:
        return self._round_dir(round_id) / f"verify-{_check_world(scenario_id)}{self.suffix}"

    def verify_scratch(self, round_id: str) -> Path:
        """基线与候选各自的快照副本放它下面,轮次结束连目录删。"""
        return self._round_dir(round_id) / "verify"

    def write_world_tag(self, snapshot: Path, scenario_id: str) -> None:
        """在快照旁写 `.world` 旁注(内容是 scenario_id)。快照可以还不存在:调用方先写旁注、后取备份。
        先写临时文件再改名:崩在写一半留下的空旁注会让 `world_of` 一直报错,要人来删。"""
        _check_world(scenario_id)
        tag = _tag_path(snapshot)
        tag.parent.mkdir(parents=True, exist_ok=True)
        tmp = tag.with_name(f".{tag.name}.tmp")
        try:
            tmp.write_text(scenario_id, encoding="utf-8")
            os.replace(tmp, tag)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _rel(self, path: Path) -> str:
        """错误信息里的路径:相对记忆根,不泄露绝对路径。"""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.name

    def world_of(self, snapshot: Path) -> str:
        """读快照的 `.world` 旁注。旁注缺失、读不出、内容(去掉末尾恰好一个 `\\n` 后)过不了世界 id 白名单
        都抛 MindAgentDataError:坏旁注不能变成路径,也不能当成"不属于任何世界"悄悄放过。
        按字节读再解码:不做换行翻译,`s1\\r\\n` 这类内容照实判坏。"""
        tag = _tag_path(snapshot)
        try:
            content = tag.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MindAgentDataError(f"快照旁注读不出: {self._rel(tag)}: {type(exc).__name__}") from exc
        scenario_id = content[:-1] if content.endswith("\n") else content
        if not PERSONA_ID_PATTERN.fullmatch(scenario_id):
            raise MindAgentDataError(f"快照旁注内容不是合法的情境 id: {self._rel(tag)}: {content!r}")
        return scenario_id

    def _is_snapshot(self, p: Path) -> bool:
        return p.is_file() and p.name.startswith(_DEPLOY_PREFIX) and p.name.endswith(self.suffix)

    def round_snapshots(self, round_id: str) -> list[Path]:
        """该轮次目录下以 `deploy-` 起头、以 suffix 结尾的文件(不含 `.world` 旁注),按名排序。"""
        d = self._round_dir(round_id)
        if not d.is_dir():
            return []
        return sorted((p for p in d.iterdir() if self._is_snapshot(p)), key=lambda p: p.name)

    def any_uncommitted_snapshots(self) -> list[Path]:
        """`snapshots/*/` 下名字以 `deploy-` 起头、以 suffix 结尾的文件(任何情境、任何轮次),按路径排序,
        有没有旁注都算。验证快照(`verify-`)、验证副本目录与 `.world` 旁注不算。门② 有它们在就延后:
        在途轮次重入会重读库。"""
        base = self.root / "snapshots"
        if not base.is_dir():
            return []
        return sorted(p for d in base.iterdir() if d.is_dir() for p in d.iterdir() if self._is_snapshot(p))

    def tagged_snapshots(self) -> list[Path]:
        """带 `.world` 旁注文件的起点快照(任何情境、任何轮次),按路径排序。装配期逐个 `world_of` 它们,
        坏旁注当场暴露,而不是留到门② 才抛。"""
        return [p for p in self.any_uncommitted_snapshots() if _tag_path(p).is_file()]

    def uncommitted_snapshots(self, scenario_id: str) -> list[Path]:
        """任何轮次里旁注内容等于 scenario_id 的起点快照。没有旁注的快照是旧布局遗留,不在其中
        (`legacy_entries` 报它们)。任何一份快照的旁注坏了都抛 MindAgentDataError:宁可停下,
        也不漏判一份在途快照。"""
        _check_world(scenario_id)
        return [p for p in self.tagged_snapshots() if self.world_of(p) == scenario_id]

    def legacy_entries(self, known_scenarios: Iterable[str]) -> list[str]:
        """记忆根下属于旧布局的文件,相对记忆根的 POSIX 路径,排序。两类:
        `deploy/` 直属的库文件(suffix 结尾)里主名不在 known_scenarios 的(`.lock` 不算);
        `snapshots/*/` 下没有 `.world` 旁注的起点快照。验证快照与验证副本目录、孤儿旁注都不算。"""
        known = set(known_scenarios)
        found: list[Path] = []
        deploy = self.root / "deploy"
        if deploy.is_dir():
            found += (p for p in deploy.iterdir()
                      if p.is_file() and p.name.endswith(self.suffix) and p.name[:-len(self.suffix)] not in known)
        found += (p for p in self.any_uncommitted_snapshots() if not _tag_path(p).is_file())
        return sorted(p.relative_to(self.root).as_posix() for p in found)
