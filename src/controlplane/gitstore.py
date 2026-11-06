from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from controlplane.envelope import Envelope
from controlplane.migration import UnknownSchemaVersionError, load_product
from controlplane.validate import EnvelopeValidationError
from controlplane.paths import CONTROL_PLANE_ROOT, SUBDIRS

# read_product 在「文件在、内容用不了」时抛的异常:YAML 解析失败(含合并冲突标记)、缺键、类型不对
# (BigRef 不合规抛的 InvalidBigRefError 是 TypeError 子类)、信封校验失败、schema_version 无法迁移。
# 重读也不会变好,是确定性的。OSError 不在其中:磁盘与权限问题是瞬态,调用方应原样上抛。
# 各处判「读不出」都引用这一份,不另写元组(此前有四份,其中一份漏了信封校验失败)。
UNREADABLE_PRODUCT_ERRORS = (KeyError, TypeError, AttributeError, yaml.YAMLError,
                             EnvelopeValidationError, UnknownSchemaVersionError)


class GitStore:
    """git 树产物存储。所有 git 操作走子进程调用 git CLI（禁止语言 git 库）。"""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = Path(repo_root)

    def _git(self, *args: str) -> str:
        try:
            return self._run(*args, check=True).stdout
        except subprocess.CalledProcessError as exc:
            # 默认的 CalledProcessError 只把 git 的 stderr 放在属性里,pytest 摘要看不到原话,
            # 而 git 的报错正是这类失败唯一的线索(见计划 9 期间七次瞬态 exit 128)。
            detail = (exc.stderr or "").strip()
            raise subprocess.CalledProcessError(
                exc.returncode, exc.cmd, output=exc.stdout,
                stderr=f"{detail}\ngit {' '.join(args)} exited {exc.returncode}",
            ) from exc

    def _run(self, *args: str, check: bool) -> subprocess.CompletedProcess:
        # LC_ALL=C:下面要靠 git 的报错文本分支(未跟踪 vs 无改动),本地化的消息会让它瞎掉。
        env = {**os.environ, "LC_ALL": "C"}
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True, text=True, check=check, env=env,
        )

    def init(self) -> None:
        self.repo_root.mkdir(parents=True, exist_ok=True)
        if not (self.repo_root / ".git").is_dir():
            self._git("init")
            # 保证提交可用（测试与 CI 环境可能无全局 git 身份）
            self._git("config", "user.email", "rise@example.com")
            self._git("config", "user.name", "rise")
        for sub in SUBDIRS:
            (self.repo_root / CONTROL_PLANE_ROOT / sub).mkdir(parents=True, exist_ok=True)

    def exists(self, rel_path: str) -> bool:
        return (self.repo_root / rel_path).is_file()

    def write_product(self, rel_path: str, env: Envelope, message: str) -> None:
        abs_path = self.repo_root / rel_path
        existed = abs_path.is_file()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with abs_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(env.to_dict(), f, allow_unicode=True, sort_keys=False)
        # 只提交本文件(控制面 CP-12):别的文件若已在暂存区,不被卷进这次提交,也不丢——
        # commit -- <path> 的行为就是如此(已实测)。
        #
        # 文件原先不在磁盘上,就一定没进过索引,`commit -- <path>` 认不出它,直接走 add:
        # 先试 commit 再回退等于白跑一个子进程,而全量测试里八成以上的写入都是新文件。
        if not existed:
            self._git("add", rel_path)
            self._git("commit", "-m", message, "--", rel_path)
            return
        # 文件已在磁盘上:绝大多数情况它也已被跟踪且内容有变,此时一个子进程就够
        # (commit -- <path> 对已跟踪文件会隐式暂存它)。全量里 write_product 被调七千多次。
        if self._run("commit", "-m", message, "--", rel_path, check=False).returncode == 0:
            return
        # 走到这里有三种可能:内容逐字节相同(幂等 no-op)、已落盘但没进过索引(崩溃恢复
        # 留下的半程状态)、真故障。**不要靠 git 的提示文本分辨**:`commit -- <path>` 做的是
        # 部分提交,它按 HEAD 加 pathspec 造临时索引,所以暂存区里有别的文件时,提示会从
        # "nothing to commit" 变成 "nothing added to commit but untracked files present"。
        # 改用结构信号:add 之后看该路径在暂存区里到底有没有差异。
        #
        # 幂等性保护(§1.4):内容相同就是 no-op,不是故障——崩溃恢复重放半程 tick 不能把
        # 守护进程杀掉。真故障由 _git 的 check=True 抛出来。
        self._git("add", rel_path)
        if self._run("diff", "--cached", "--quiet", "--", rel_path, check=False).returncode == 0:
            return
        self._git("commit", "-m", message, "--", rel_path)

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
