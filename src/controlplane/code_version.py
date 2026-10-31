"""代码版本:rise 自己的 git HEAD,不是产物树(GitStore)的 HEAD。

两处装配都要它:factory 写配方候选的 code_version,以及 MindTrainer 写训练记录的 code_versions。
后者所在的分层规则不许 mindtrainer 导入 factory,所以实现放在 controlplane;factory.wire 仍以
原有名字 re-export,调用方不必改。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]  # src/controlplane/code_version.py -> 代码仓库根


def resolve_code_commit(override: str | None, *, code_root: Path = CODE_ROOT) -> str | None:
    """override 优先;否则 git -C <代码仓库根> rev-parse HEAD;不是 git 仓库或没有 git 时返回 None。

    None 是合法的:取不到代码版本不该拦住任何事,记录里只是少一条来历。
    """
    if override is not None:
        return override
    try:
        out = subprocess.run(["git", "-C", str(code_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=False)
    except OSError:
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None
