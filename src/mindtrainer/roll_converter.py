"""RollLoraConverter:ROLL 训练存点 -> vLLM LoRA 适配器导出(设计 §6.2)。

纯 CPU:`CUDA_VISIBLE_DEVICES` 置空、`run_job(lease=False)`,不占 GPU 租约,但转换峰值
约 8 GiB 主机内存,所以靠端点的 `min_free_host_mem_gib` 预检(V5 待主机核实)。

产物目录是内容寻址的:`exports/<训练存点 sha8>/`。写的时候先落到
`exports/<sha8>.tmp-<token>/`,算完 full 哈希、写完成标记,再原子改名。所以"目录存在
且带标记"就意味着"上一次确实写完了"。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from compute.client import ComputeClient, JobSpec
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import SHA256_HEX, BigRef
from mindtrainer.executor import CheckpointCorruptError

EXPORT_MARKER = ".exported"  # 内容是产物目录的 full 哈希
EXPORT_FORMAT = "qwen4_exp_vllm_2d"  # ROLL 的 LoRAHFConverter 布局(设计 §1 已核实)

# ROLL4ZenGen 的导出是 convert_checkpoint_to_hf 的 Python 调用,没有命令行入口(README 与 B2 实测)。
# 此前假定的 scripts/export_lora_for_vllm.py 不存在。路径只经环境变量传进来,程序文本是固定常量:
# argv 里没有任何来自引用的字符串。
#
# 用 convert_checkpoint_to_hf 而不是 README 里的 LoRAHFConverter(...):后者的 __init__ 现在要求 verbose,
# 照 README 抄会抛 TypeError(B2 实测)。low_mem=False 是适配器导出的唯一合法取值(源码:适配器不支持流式)。
EXPORT_PROGRAM = (
    "import os, torch\n"
    "from mcore_adapter.models.converter.post_converter import convert_checkpoint_to_hf\n"
    "convert_checkpoint_to_hf(\n"
    "    model_name_or_path=os.environ['ROLL_EXPORT_BASE'],\n"
    "    adapter_name_or_path=os.environ['ROLL_EXPORT_ADAPTER'],\n"
    "    save_directory=os.environ['ROLL_EXPORT_OUT'],\n"
    "    torch_dtype=torch.bfloat16,\n"
    "    low_mem=False,\n"
    "    verbose=False,\n"
    ")\n"
)
PROGRAM_SHA8 = hashlib.sha256(EXPORT_PROGRAM.encode("utf-8")).hexdigest()[:8]


def _job_identity(source_sha256: str, tmp_dir: str) -> str:
    """导出作业的 id。程序文本的哈希进来:改了程序就是另一个作业,不会撞上旧程序留下的成功记录。"""
    return "export-" + hashlib.sha256(
        f"{source_sha256}\0{tmp_dir}\0{EXPORT_FORMAT}\0{PROGRAM_SHA8}".encode("utf-8")
    ).hexdigest()[:12]


@dataclass
class RollLoraConverter:
    """训练存点 -> 适配器导出(设计 §6.2)。

    client / endpoint:与执行器同一套装配(factory.compute_config.build_compute_client)。
    """

    client: ComputeClient
    endpoint: ComputeEndpoint

    def convert(self, ref: BigRef) -> BigRef:
        if not ref.uri.startswith("ssh://"):
            raise CheckpointCorruptError(
                f"RollLoraConverter cannot convert {ref.uri!r}: it expects an ssh:// reference"
            )
        if not SHA256_HEX.match(ref.sha256):
            # BigRef 构造时已保证这一条(I8);sha256[:8] 直接进远端路径,所以这里再判一次作纵深防御:
            # 非十六进制的 64 字符串(例如 "../../..")会让产物目录逃出 work_root。
            raise CheckpointCorruptError(
                f"the checkpoint reference carries no usable sha256: {ref.sha256!r}"
            )
        src = self._remote_path(ref.uri)
        work = self.endpoint.work_root.rstrip("/")
        export_dir = f"{work}/exports/{ref.sha256[:8]}"

        # 1. 已经转过就直接返回(幂等)。标记在而哈希不符,说明产物被动过:
        #    重做只会再次覆盖同一份产物、结果同样不可信,所以抛 CheckpointCorruptError 而不是重做;
        #    它的分级沿用现状(不在 _DETERMINISTIC、原样上抛),不是 T2。
        recorded = self._read_marker(export_dir)
        if recorded is not None:
            # 标记的内容就是这个哈希,所以标记自己不进哈希;否则改名之后重算必然对不上。
            actual = self.client.hash_tree(export_dir, "full", exclude=(EXPORT_MARKER,))
            if actual != recorded:
                raise CheckpointCorruptError(
                    f"the export at {export_dir} records {recorded[:12]}... "
                    f"but its contents hash to {actual[:12]}..."
                )
            return self._build_ref(export_dir, actual, ref)

        # 2. 内存预检。转换不占 GPU,但峰值约 8 GiB,挤爆主机会把别人的训练一起拖垮。
        #    不够就按忙处理:ComputeBusyError 是 DeferredError,延后重试,不耗续训预算。
        self._require_host_memory()

        # 3. 写进自己的 tmp 目录。token 让并发的两次转换互不覆盖。
        tmp_dir = f"{export_dir}.tmp-{uuid.uuid4().hex[:8]}"
        job_id = _job_identity(ref.sha256, tmp_dir)
        spec = JobSpec(
            argv=("python3", "-c", EXPORT_PROGRAM),
            cwd=self.endpoint.roll_root,
            env={
                "CUDA_VISIBLE_DEVICES": "",  # 设计 §6.2 第 1 步:纯 CPU
                "ROLL_EXPORT_BASE": self.endpoint.base_model_path,
                "ROLL_EXPORT_ADAPTER": src,
                "ROLL_EXPORT_OUT": tmp_dir,
            },
        )
        try:
            # lease=False:纯 CPU 作业不跟训练抢 GPU 租约(设计 §6.2 第 1 步)
            self.client.run_job(job_id, spec, tail_lines=40, lease=False)
            # 4. full 口径:LoRA 适配器的形状只由 rank 与层数决定,两次不同训练的导出
            #    目录文件名与字节数完全相同,只记大小会让它们撞同一个哈希,而候选归属
            #    靠 checkpoint_ref.sha256 判断(设计 §6.1、D14)。
            digest = self.client.hash_tree(tmp_dir, "full")
            # 5. 标记写在 tmp 里,随改名一起变成正式产物:不会出现"目录已就位但没标记"。
            self.client.put(f"{tmp_dir}/{EXPORT_MARKER}", digest.encode("ascii"))
            self.client.rename(tmp_dir, export_dir)
        except HelperError as exc:
            if exc.code == "exists":
                # 别人刚好把同一个 sha8 转完了:它的产物与我们要产的是同一份,
                # 认它,并清掉自己这份未完成的 tmp。
                self._discard(tmp_dir)
                return self.convert(ref)
            self._discard(tmp_dir)
            raise
        except Exception:
            # 清理范围严格限制在自己创建的 tmp 目录,绝不碰已完成的 exports/<sha8>/
            self._discard(tmp_dir)
            raise
        return self._build_ref(export_dir, digest, ref)

    # ---------- 内部 ----------

    def _read_marker(self, export_dir: str) -> str | None:
        """产物目录的完成标记;没有标记(含目录不存在)返回 None。"""
        try:
            data = self.client.get(f"{export_dir}/{EXPORT_MARKER}")
        except HelperError as exc:
            if exc.code == "not_found":
                return None
            raise
        recorded = data.decode("ascii", "replace").strip()
        if len(recorded) != 64:
            raise CheckpointCorruptError(
                f"the export marker at {export_dir} does not hold a sha256"
            )
        return recorded

    def _require_host_memory(self) -> None:
        """空闲主机内存不足按忙处理(设计 §6.2 第 1 步)。

        V15:`probe` 的应答字段名待主机核实;拿不到读数时放行而不是硬拦——
        预检本身不该成为新的失败源,真不够的话转换会在远端 OOM,那是 T1 可重试。
        """
        probe = getattr(self.client, "probe", None)
        if probe is None:
            return
        free = probe().get("mem_available_gib")
        if free is None:
            return
        if float(free) < self.endpoint.min_free_host_mem_gib:
            raise ComputeBusyError(
                f"only {float(free):.1f} GiB of host memory is free; "
                f"the export needs {self.endpoint.min_free_host_mem_gib:.1f} GiB"
            )

    def _discard(self, tmp_dir: str) -> None:
        """清掉自己造的 tmp。清理失败不掩盖原始异常:留个孤儿目录比丢掉错因好。"""
        try:
            self.client.rm_tmp(tmp_dir)
        except Exception:  # noqa: BLE001
            pass

    def _build_ref(self, export_dir: str, digest: str, source: BigRef) -> BigRef:
        """uri 不带 .hf 后缀:只有 MockConverter 看后缀(设计 §6.2 第 4 步)。"""
        return BigRef(
            uri=f"ssh://{self.endpoint.endpoint_id}{export_dir}",
            sha256=digest,
            size_bytes=0,  # Ruling 15:与 MockExecutor 一致,不填
            fetch_hint={
                "type": "roll_ssh",
                "format": EXPORT_FORMAT,
                "base_model_path": self.endpoint.base_model_path,
                "requires": "native-vllm-lora",  # 需要 ROLL 的 vLLM 补丁(设计 V3)
                "source_checkpoint_sha256": source.sha256,
            },
        )

    def _remote_path(self, uri: str) -> str:
        """ssh://<endpoint_id>/<绝对路径> -> /<绝对路径>,且必须落在 work_root 之内。

        这个路径会作为 `--checkpoint` 进导出作业的 argv,助手的 `_abs_path_inside` 看不到它,
        所以容器边界只能在这里把住:训练存点一定在 `work_root/runs/` 下(见 roll_executor
        的 out_dir),指向别处的引用是装配错了端点或引用被改过,不是能转换的东西。
        """
        rest = uri[len("ssh://"):]
        endpoint_id, slash, path = rest.partition("/")
        if not slash:
            raise CheckpointCorruptError(f"{uri!r} carries no remote path")
        if endpoint_id != self.endpoint.endpoint_id:
            raise CheckpointCorruptError(
                f"{uri!r} names endpoint {endpoint_id!r} but this converter is wired to "
                f"{self.endpoint.endpoint_id!r}"
            )
        path = "/" + path
        work = self.endpoint.work_root.rstrip("/")
        if ".." in path.split("/") or not (path == work or path.startswith(work + "/")):
            raise CheckpointCorruptError(f"{uri!r} points outside the endpoint work root")
        return path
