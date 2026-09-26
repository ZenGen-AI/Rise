from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from controlplane.envelope import Envelope
from controlplane.migration import load_product
from controlplane.paths import CONTROL_PLANE_ROOT, SUBDIRS


class GitStore:
    """git 树产物存储。所有 git 操作走子进程调用 git CLI（禁止语言 git 库）。"""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = Path(repo_root)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def init(self) -> None:
        self.repo_root.mkdir(parents=True, exist_ok=True)
        if not (self.repo_root / ".git").is_dir():
            self._git("init")
            # 保证提交可用（测试与 CI 环境可能无全局 git 身份）
            self._git("config", "user.email", "rismforge@example.com")
            self._git("config", "user.name", "rismforge")
        for sub in SUBDIRS:
            (self.repo_root / CONTROL_PLANE_ROOT / sub).mkdir(parents=True, exist_ok=True)

    def exists(self, rel_path: str) -> bool:
        return (self.repo_root / rel_path).is_file()

    def write_product(self, rel_path: str, env: Envelope, message: str) -> None:
        abs_path = self.repo_root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with abs_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(env.to_dict(), f, allow_unicode=True, sort_keys=False)
        self._git("add", rel_path)
        # 幂等性保护（§1.4）：若内容与已提交版本逐字节相同，git add 不会暂存任何
        # 改动，此时无条件 commit 会因 "nothing to commit" 以非零退出码失败并
        # 抛出 CalledProcessError（_git 使用 check=True）。先探测暂存区是否为空，
        # 为空则跳过提交（no-op），避免崩溃恢复重放半程 tick 时把守护进程整个杀死。
        staged_check = subprocess.run(
            ["git", "-C", str(self.repo_root), "diff", "--cached", "--quiet", rel_path],
            capture_output=True, text=True, check=False,
        )
        if staged_check.returncode == 0:
            return
        self._git("commit", "-m", message)

    def read_product(self, rel_path: str) -> Envelope:
        abs_path = self.repo_root / rel_path
        with abs_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return load_product(data)  # 走迁移器链（F.5 第四项税）

    def list_products(self, rel_dir: str) -> list[str]:
        abs_dir = self.repo_root / rel_dir
        if not abs_dir.is_dir():
            return []
        return [
            str(p.relative_to(self.repo_root)).replace("\\", "/")
            for p in sorted(abs_dir.glob("*.yaml"))
        ]
