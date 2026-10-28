"""部署记忆根目录的磁盘布局(设计二 5.3 节):人格库锁、每轮每集的起点副本、验证快照。

替身下 `MemoryLayout(mem_root)` 与 `FileTupleStore(mem_root / "deploy")` 共用同一个根:部署库
`deploy/<persona_id>.json`、锁 `deploy/<persona_id>.lock`、起点副本
`snapshots/<round_id>/<episode_id>-<persona_id>.json`、验证快照 `snapshots/<round_id>/verify-<persona_id>.json`。
后缀由 `suffix` 给:替身是 `.json`,真实 Starling 后端是 `.db`。
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from actio.errors import ActioDataError
from actio.memory_port import PERSONA_ID_PATTERN
from controlplane.lock import hold_file_lock

_DEPLOY_PREFIX = "deploy-"


def _check_persona(persona_id: str) -> str:
    if not isinstance(persona_id, str) or not PERSONA_ID_PATTERN.fullmatch(persona_id):
        raise ActioDataError(f"persona_id: {persona_id!r}")
    return persona_id


def _check_segment(name: str, value: str) -> str:
    """round_id、episode_id 要当一段路径用:不许含 `/`,不许是 `.`、`..`。带轮次前缀的
    `<round_id>/deploy-...` 只进事件 key,绝不拼路径。"""
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        raise ValueError(f"{name}: {value!r}")
    return value


@dataclass(frozen=True)
class MemoryLayout:
    root: Path
    suffix: str = ".json"

    def deploy_lock(self, persona_id: str) -> Path:
        return self.root / "deploy" / f"{_check_persona(persona_id)}.lock"

    def _round_dir(self, round_id: str) -> Path:
        return self.root / "snapshots" / _check_segment("round_id", round_id)

    def snapshot(self, round_id: str, episode_id: str, persona_id: str) -> Path:
        _check_persona(persona_id)
        _check_segment("episode_id", episode_id)
        return self._round_dir(round_id) / f"{episode_id}-{persona_id}{self.suffix}"

    def verify_snapshot(self, round_id: str, persona_id: str) -> Path:
        return self._round_dir(round_id) / f"verify-{_check_persona(persona_id)}{self.suffix}"

    def verify_scratch(self, round_id: str) -> Path:
        """基线与候选各自的快照副本放它下面,轮次结束连目录删。"""
        return self._round_dir(round_id) / "verify"

    def round_snapshots(self, round_id: str) -> list[Path]:
        """该轮次目录下以 `deploy-` 起头、以 suffix 结尾的文件,按名排序。"""
        d = self._round_dir(round_id)
        if not d.is_dir():
            return []
        return sorted(
            (p for p in d.iterdir()
             if p.is_file() and p.name.startswith(_DEPLOY_PREFIX) and p.name.endswith(self.suffix)),
            key=lambda p: p.name,
        )

    def any_uncommitted_snapshots(self) -> list[Path]:
        """`snapshots/*/` 下名字以 `deploy-` 起头、以 suffix 结尾的文件(任何人格、任何轮次),按路径排序。
        验证快照(`verify-`)与验证副本目录不算。门② 有它们在就延后:在途轮次重入会重读库。"""
        base = self.root / "snapshots"
        if not base.is_dir():
            return []
        return sorted(
            p
            for d in base.iterdir() if d.is_dir()
            for p in d.iterdir()
            if p.is_file() and p.name.startswith(_DEPLOY_PREFIX) and p.name.endswith(self.suffix)
        )

    def uncommitted_snapshots(self, persona_id: str) -> list[Path]:
        """any_uncommitted_snapshots 里以 `-<persona_id><suffix>` 结尾的那些。只按文件名匹配:
        人格 id 里允许 `-`,`deploy-m-s-1-x-pa.json` 对人格 `pa` 与 `x-pa` 都算命中。宁可多判:多判只让
        门② 多延后一次,不会丢记忆。"""
        tail = f"-{_check_persona(persona_id)}{self.suffix}"
        return [p for p in self.any_uncommitted_snapshots() if p.name.endswith(tail)]

    def persona_lock(self, persona_id: str) -> AbstractContextManager[None]:
        return hold_file_lock(self.deploy_lock(persona_id))
