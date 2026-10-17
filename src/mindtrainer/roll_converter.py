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
from controlplane.envelope import BigRef
from mindtrainer.executor import CheckpointCorruptError

EXPORT_MARKER = ".exported"  # 内容是产物目录的 full 哈希
EXPORT_FORMAT = "qwen4_exp_vllm_2d"  # ROLL 的 LoRAHFConverter 布局(设计 §1 已核实)

# V15:真实导出命令待主机核实(V3、V5)。ROLL 文档记录的是 LoRAHFConverter,
# 入口脚本名与参数都还没在机器上跑过,所以这里只固定"纯 CPU、写进 tmp 目录"这两点。
EXPORT_SCRIPT = "scripts/export_lora_for_vllm.py"


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
        if len(ref.sha256) != 64:
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
        job_id = "export-" + hashlib.sha256(
            f"{ref.sha256}\0{tmp_dir}\0{EXPORT_FORMAT}".encode("utf-8")
        ).hexdigest()[:12]
        spec = JobSpec(
            argv=(
                "python", f"{self.endpoint.roll_root.rstrip('/')}/{EXPORT_SCRIPT}",
                "--checkpoint", src,
                "--base-model", self.endpoint.base_model_path,
                "--output", tmp_dir,
            ),
            cwd=self.endpoint.roll_root,
            env={"CUDA_VISIBLE_DEVICES": ""},  # 设计 §6.2 第 1 步:纯 CPU
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
        rest = uri[len("ssh://"):]
        _, slash, path = rest.partition("/")
        if not slash:
            raise CheckpointCorruptError(f"{uri!r} carries no remote path")
        return "/" + path
