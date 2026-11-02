"""ROLL 训练与导出的运行环境规格:镜像、ROLL 版本、两份依赖源码、容器参数。

这些是代码常量,不是树内配置:它们和 ROLL 的提交、Megatron 的补丁状态是一起被验证过的,
改任何一项都该改代码并经评审(与 `endpoint.check_launch_prefix` 的白名单同理)。

部署目录按 ROLL 提交命名(`deps/roll-<commit12>`);容器名由 docker run 的完整参数算出(见 compute.roll_deploy),
规格或挂载任何一项变了就是一个新容器,旧容器原样留着(只碰 `CONTAINER_PREFIX` 开头的容器,且从不停、不删)。
`digest` 是 ROLL 版本与依赖的身份,进 job_id 与记账。

依赖源码用主机上已经验证过的副本(181 上跑通 LoRA SFT 的那一份),只读挂进容器:
- Megatron-LM:core_dev_r0.16.0 的 bfa1d31 加 ROLL 补丁清单里的前 11 个补丁,缺最后一步
  checkpoint_padding(`distrib_optimizer.py` 停在那个补丁的 SOURCE 哈希)。只影响优化器存点里的填充值。
- fla:flash-linear-attention 0.6.0,已打 ROLL 的 Hopper dv 补丁。
指纹是 `hash-tree full` 加 `skip_pycache` 的值,2026-11-02 在 161、171、181、211 上实测一致。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

IMAGE_ID = "sha256:18036f10b00d9825b867c0f40485609de79e726ec9ec6dd52d3a1c09b13a1527"  # roll-lab:snap1
CONTAINER_PREFIX = "forge-roll-"
SFT_TEMPLATE = "scripts/qwen38/configs/sft_lora.yaml"   # 相对 ROLL 部署目录
SHM_SIZE = "32g"


@dataclass(frozen=True)
class Dependency:
    name: str              # host_deps_root 下的目录名
    mount: str             # 容器里的挂载点(只读)
    fingerprint: str       # hash-tree full + skip_pycache


DEPENDENCIES = (
    Dependency("Megatron-LM", "/r2n/megatron",
               "16819e70677d6b74fe39d1820cd01145d8feb5cdc8db2e07f50cf9c06efbac2f"),
    Dependency("fla", "/r2n/fla",
               "2ef8d23a54d1fb45c27211a78340d45fc3d02472bd3fc5f36ebce540a55fb6ce"),
)

# 进容器 Config.Env:作业进程继承它(助手的作业环境 = 助手进程的环境加 spec.env)。
# NCCL_NVLS_ENABLE=0:这批 H800 上 NVLS 多播绑定失败,ROLL 与同机其他任务都关掉它。
BASE_ENV = (("NCCL_NVLS_ENABLE", "0"), ("PYTHONDONTWRITEBYTECODE", "1"))


@dataclass(frozen=True)
class RollEnv:
    roll_commit: str
    roll_tree: str                   # 部署目录的 hash-tree full + skip_pycache(由归档在本机算出)
    image_id: str = IMAGE_ID
    dependencies: tuple[Dependency, ...] = DEPENDENCIES
    env: tuple[tuple[str, str], ...] = BASE_ENV
    shm_size: str = SHM_SIZE

    @property
    def digest(self) -> str:
        """规格的内容摘要(12 位):进容器名、job_id 与记账。"""
        body = json.dumps({
            "roll_commit": self.roll_commit, "roll_tree": self.roll_tree, "image_id": self.image_id,
            "dependencies": [[d.name, d.mount, d.fingerprint] for d in self.dependencies],
            "env": [list(kv) for kv in self.env], "shm_size": self.shm_size,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]

    def __post_init__(self) -> None:
        # 提交号进远端路径(roll-<commit12>),所以只认 40 位小写十六进制
        if not (isinstance(self.roll_commit, str) and len(self.roll_commit) == 40
                and all(c in "0123456789abcdef" for c in self.roll_commit)):
            raise ValueError("roll_commit must be a 40-character lowercase hex commit id")

    def roll_dir(self, work_root: str) -> str:
        return f"{work_root.rstrip('/')}/deps/roll-{self.roll_commit[:12]}"

    def record(self) -> dict:
        """写进训练记录的那一份(纯 JSON)。存点与导出的 fetch_hint 只取其中两项,见 version_hint。"""
        return {
            "roll_commit": self.roll_commit, "roll_env": self.digest, "image_id": self.image_id,
            "dependencies": {d.name: d.fingerprint[:12] for d in self.dependencies},
        }


def version_hint(record: dict | None) -> dict:
    """存点与导出 fetch_hint 里的版本两项。没有记账时为空,既有引用的形状不变。"""
    if not record:
        return {}
    return {"roll_commit": record["roll_commit"], "roll_env": record["roll_env"]}
