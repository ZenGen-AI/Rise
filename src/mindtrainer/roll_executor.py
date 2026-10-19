"""RollExecutor:把 ROLL 的整段 SFT 作业接到编排器的阶段协议上(真实后端设计 §5)。

它住在 `mindtrainer` 而不是 `compute`:设计 §5 把它放这里,而且它要认识 Episode、
StepResult 与 CheckpointCorruptError,这些都是训练侧的概念。依赖方向是
`mindtrainer -> compute`,`compute` 自己仍然不认识训练(设计 §3 开头)。

只支持 SFT(`supported_phases`)。逐步方法一律响亮失败:真实执行器只能整段跑,
被逐步调用说明编排器被改坏了。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from compute.client import ComputeClient, JobSpec
from compute.endpoint import ComputeEndpoint
from compute.errors import HelperError, TransportError
from controlplane.envelope import BigRef
from mindtrainer.datamodel import StepResult
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.training_config import InvalidTrainingConfigError
from simulator.datamodel import Episode

LAUNCHER_VERSION = "v2"  # 进 job_id 的哈希;改了启动命令的拼法就要改它(v2:固定存点根目录并给作业独立的 Ray 临时目录;v1:入口改到 scripts/qwen38/、解释器改 python3)
TRAIN_SCRIPT = "scripts/qwen38/run_sft_validation.py"  # 相对 roll_root;ROLL4ZenGen 仓库里的真实位置
METRIC_PREFIX = "metrics_tag:"  # V2 未核实:ROLL 的逐步指标行前缀,主机验证后改这里
MAX_ATTEMPTS = 8  # 同一 job_id 下的尝试序号上限,与 max_resume_attempts 分开记账(设计 §5.2)


class TrainingDataRejectedError(RuntimeError):
    """上传前准入检查命中:训练数据里出现诊断题的内容。确定性失败,整条指令判 failed。

    原因里只有行数与窗口数,不带任何文本:命中的内容就是评测题面。"""

    def __init__(self, bad_lines: int, hits: int) -> None:
        super().__init__(
            f"{bad_lines} training row(s) overlap the diagnostic benchmark "
            f"({hits} matching 20-character windows); refusing to upload"
        )
        self.bad_lines = bad_lines
        self.hits = hits


class MetricsMissingError(RuntimeError):
    """远端作业成功了,但拿不到对账得上的逐步指标。

    瞬态:重试只重新解析同一批产物,不重训,所以**不**进两个 _DETERMINISTIC
    (设计 §7.3、§7.4)。计入 resume_attempts。
    """


def _render_rows(episode: Episode, trained_slot: str) -> list[dict]:
    """一个剧集 -> 多行 {"instruction", "output"}(设计 §5.2 第 1 步,多轮监督)。

    被训练角色位说了 T 句就出 T 行:第 t 行的 output 是它的第 t 句,instruction 是
    这句之前的全部发言按固定模板拼成的文本。每句都被监督,上下文只作输入。

    用行展开而不是逐轮 loss mask,是因为 sft_lora.yaml 只有 prompt_key/response_key
    这一对键,表达不了逐轮掩码。代价:同一段上下文会被重复前向,token 量随 T 平方增长。
    语义上等价于逐轮掩码,计算上不等价(设计 §5.2)。
    """
    rows: list[dict] = []
    history: list[str] = []
    for turn in episode.turns:
        line = f"{turn.speaker_slot}: {turn.utterance}"
        if turn.speaker_slot == trained_slot:
            rows.append({"instruction": "\n".join(history), "output": turn.utterance})
        history.append(line)
    return rows


@dataclass
class RollExecutor:
    """ROLL SSH 作业的分阶段执行器(设计 §5)。

    client:已装配好的 ComputeClient(由 factory.compute_config.build_compute_client 造)。
    endpoint:同一个端点的冻结配置,路径与阈值都从它取。
    run_id:进 job_id 的哈希,避免不同 run 的同样数据撞同一作业目录。
    """

    client: ComputeClient
    endpoint: ComputeEndpoint
    run_id: str

    supported_phases = frozenset({"sft"})  # 设计 §5.1:真实后端本轮只做 SFT
    checkpoint_type = "roll_ssh"  # 跨模式续训检查比对的就是它(设计 §6.1)

    # 上传前的准入检查(设计 21 的 DX-19、设计 30 的 MT-15):收整份 train.jsonl 字节,
    # 返回 (有命中的行数, 命中窗口数)。由组合根注入,mindtrainer 不认识 diagnostic。
    # 缺省不检查:直接构造执行器的既有测试与 mock 路径不受影响;真训练经组合根装配时恒有它。
    row_gate: Callable[[bytes], tuple[int, int]] | None = None

    _base_uri: str | None = field(default=None, init=False, repr=False)
    _stage_ref: BigRef | None = field(default=None, init=False, repr=False)

    # ---------- 协议:资源与存点 ----------

    def preflight(self) -> None:
        """GPU、内存、磁盘检查。不取租约、不终止任何进程(设计 §3.3、D8)。"""
        self.client.preflight()

    def load(self, checkpoint_ref: BigRef) -> None:
        """只做校验与记录,不占 GPU(设计 §5.1)。

        接受两类引用:指向 base_model_path 的起点基座,和本设计产出的训练存点。
        mock:// 引用抛 CheckpointCorruptError:重试不会好,但它不在任何 _DETERMINISTIC 里、`load` 在 `try` 之外,
        所以和 MockExecutor 的损坏存点一样原样上抛、记录保持 pending(本任务不改这条,见 Task 7 Step 2)。
        适配器导出引用抛 InvalidTrainingConfigError:真实转换产出的是
        qwen4_exp_vllm_2d 布局,不是 HF 训练起点,所以本轮只支持单轮(设计 §5.1)。
        """
        hint = checkpoint_ref.fetch_hint or {}
        if hint.get("format") == "qwen4_exp_vllm_2d":
            raise InvalidTrainingConfigError(
                "this checkpoint is an adapter export, not a training start point; "
                "real backends support one round only, so point base_checkpoint_ref at base_model_path"
            )
        if not checkpoint_ref.uri.startswith("ssh://"):
            raise CheckpointCorruptError(
                f"RollExecutor cannot load {checkpoint_ref.uri!r}: it expects an ssh:// reference"
            )
        path = self._remote_path(checkpoint_ref.uri)
        if path != self.endpoint.base_model_path and not self.checkpoint_intact(checkpoint_ref):
            raise CheckpointCorruptError(f"the checkpoint at {checkpoint_ref.uri!r} is not intact")
        self._base_uri = checkpoint_ref.uri
        self._stage_ref = None

    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool:
        """sampled 口径核对 manifest(设计 §6.1)。

        传输错误一律上抛 TransportError,**不**返回 False:False 会让 decide_resume
        升级成 NeedsHumanError(T3,永久),一次网络抖动不该把 run 判死(设计 §5.1)。
        只有核对完成且确实不符才返回 False。
        """
        if not checkpoint_ref.uri.startswith("ssh://"):
            return False
        try:
            digest = self.client.hash_tree(self._remote_path(checkpoint_ref.uri), "sampled")
        except HelperError as exc:
            if exc.code == "not_found":
                return False  # 目录真的不在:核对完成,结论是不完整
            raise
        # V15:还要检查 ROLL 的结构文件齐全(tracker、迭代目录、各 worker 的 RNG、
        # scheduler、适配器配置)。只能证明结构完整,不证明张量没坏。
        return digest == checkpoint_ref.sha256

    def save_checkpoint(self, tag: str) -> BigRef:
        """ROLL 的存点在阶段内部就写出了,这里只把它封成 BigRef,不另外触发保存。

        循环为空时(续训时 start == total,崩在最终存点之后、置 executed 之前)本次
        没有阶段产出,`orchestrator` 仍会无条件调用一次。此时回认 load 记下的那份,
        并校验它仍然完整;认不出就抛 CheckpointCorruptError,绝不返回一个没有对应
        目录的引用(设计 §5.1)。
        """
        if self._stage_ref is not None:
            return self._stage_ref
        if self._base_uri is None:
            raise CheckpointCorruptError("save_checkpoint was called before load")
        path = self._remote_path(self._base_uri)
        try:
            digest = self.client.hash_tree(path, "sampled")
        except HelperError as exc:
            if exc.code == "not_found":
                raise CheckpointCorruptError(
                    f"no checkpoint was produced in this stage and {self._base_uri!r} is gone"
                ) from None
            raise
        return BigRef(
            uri=self._base_uri,
            sha256=digest,
            size_bytes=0,
            fetch_hint={"type": self.checkpoint_type, "format": "roll_lora_legacy"},
        )

    # ---------- 协议:逐步方法一律拒绝 ----------

    def sft_step(self, episodes, *, trained_slot):  # noqa: ARG002
        raise RuntimeError("RollExecutor runs whole stages; call run_sft_stage instead of sft_step")

    def grpo_opd_step(self, rewarded, *, opd_coefficient, teacher_model_ref):  # noqa: ARG002
        raise RuntimeError("RollExecutor does not support GRPO; supported_phases is ('sft',)")

    # ---------- 协议:阶段 ----------

    def stage_end(self, batches, *, first_step, limit):  # noqa: ARG002
        """恒返回 limit。一个作业只有一份 gradient_accumulation 配置,切细不能让不同
        行数的步共存(设计 §5.2 第 2 步),所以本轮不切细。接口保留以备磁盘约束。"""
        return limit

    def run_sft_stage(
        self,
        batches: list[list[Episode]],
        *,
        first_step: int,
        trained_slot: str,
        prior_batches: list[list[Episode]],
    ) -> list[StepResult]:
        """渲染数据、推 job_id、跑整段作业、回收逐步指标(设计 §5.2)。"""
        if self._base_uri is None:
            raise RuntimeError("run_sft_stage was called before load")
        last_step = first_step + len(batches)

        # 1. 数据按步序排列:先前缀([0, first_step)),再本段。ROLL 的 DataLoader 是
        #    shuffle=False、drop_last=True,按文件顺序取整批,所以行序必须就是步序。
        #    V14:这里还要加"每步行数恒等于 G"与 tokenizer 实测截断两道预检,
        #    不过不补齐也不丢行——drop_last 会静默丢,所以预检要替 ROLL 先挡住。
        rows: list[dict] = []
        for batch in list(prior_batches) + list(batches):
            for episode in batch:
                rows.extend(_render_rows(episode, trained_slot))
        payload = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
        data = payload.encode("utf-8")
        if self.row_gate is not None:
            bad_lines, hits = self.row_gate(data)
            if hits:
                raise TrainingDataRejectedError(bad_lines, hits)

        # 2. 作业身份。run_id 与起点存点必须进哈希,否则起点不同而数据相同的两次续训
        #    会撞同一目录(设计 §5.2 第 4 步)。last_step 是开区间终点。
        job_id = hashlib.sha256(
            "\0".join([
                self.run_id, self._base_uri, str(first_step), str(last_step),
                hashlib.sha256(data).hexdigest(), self.endpoint.sft_template, LAUNCHER_VERSION,
            ]).encode("utf-8")
        ).hexdigest()[:16]

        # 3. 尝试序号。start 对「同 spec 的已存在目录」直接返回当前状态,这让守护进程
        #    崩溃后能接上正在跑的作业;但同一条规则会让已失败的作业永远重跑不了。
        #    所以目录名带尝试序号,job_id 仍只由确定输入算出(设计 §5.2 第 4 步)。
        attempt_id = self._pick_attempt(job_id)
        work = self.endpoint.work_root.rstrip("/")
        train_path = f"{work}/data/{attempt_id}/train.jsonl"
        out_dir = f"{work}/runs/{self.run_id}/{attempt_id}"
        self.client.put(train_path, data)

        # 4. 启动命令(V9 未核实:--max-steps 是否真能截到段尾、--resume 的路径层级)
        #    入口在 ROLL4ZenGen 的 scripts/qwen38/ 下;解释器用 python3,镜像里没有 `python`。
        #    --checkpoint-output-dir 不传的话,存点落在 <out_dir>/checkpoints/<时间戳>/,时间戳是
        #    ROLL 框架无条件追加的(base_config.py),调用方事先猜不到;固定根目录之后,时间戳那一层
        #    仍在,但位置落在我们给的目录之下,而最终位置以 completion.json 为准(见 _read_outcome)。
        ckpt_root = f"{out_dir}/checkpoints"
        argv = [
            "python3", f"{self.endpoint.roll_root.rstrip('/')}/{TRAIN_SCRIPT}",
            "--config", self.endpoint.sft_template,
            "--model-path", self.endpoint.base_model_path,
            "--output-dir", out_dir,
            "--checkpoint-output-dir", ckpt_root,
            "--train-data", train_path,
            "--heldout-data", self.endpoint.heldout_jsonl,
            "--max-steps", str(last_step),
            "--save-steps", str(last_step),
            "--eval-steps", str(last_step),
        ]
        if first_step > 0:
            argv += ["--resume", self._remote_path(self._base_uri)]
        # Ray 用 Unix 域套接字,路径上限 107 字节,ROLL 因此要求 ROLL_RAY_TEMP_DIR 至多 37 个字符
        # (ray_temp_dir.py)。work_root 可以很长,所以不能放在它下面;用 job_id 的前 8 位区分作业,
        # 否则两个作业会共用一个 Ray 会话。B1 的第一次尝试就死在这里。
        spec = JobSpec(
            argv=tuple(argv), cwd=self.endpoint.roll_root,
            env={"ROLL_RAY_TEMP_DIR": f"/tmp/ray-{job_id[:8]}"},
        )

        # 5. 整段跑。run_job 自己取租约、轮询、释放;非零退出抛 RemoteJobFailedError
        #    (日志尾巴在 log_tail 里,不进 reason),超时抛 RemoteJobTimeoutError,
        #    租约被占抛 ComputeBusyError——三者都是 DeferredError(Ruling 9、10)。
        self.client.run_job(attempt_id, spec)

        # 6. 回收存点位置与逐步指标。指标行在混杂日志里离末尾很远(B1 实测第 0 步距末尾 40 行,
        #    而按步数取的尾巴只有 48 行,刚好踩线),步数一多会系统性丢掉早期步;所以不从日志取,
        #    而是读训练器写在存点里的 log_history。step 必须从 ROLL 自己的记录里读,不能按位置合成:
        #    否则 orchestrator 的 result.step != step 检查会恒为真,那道校验就被废掉了(设计 §5.2 第 7 步)。
        ckpt_dir = self._read_outcome(out_dir)
        results = self._parse_metrics(ckpt_dir, first_step, last_step)

        # 7. 段尾存点。目录是 <ckpt_root>/<时间戳>/checkpoint-<global_step>,global_step 是零基索引,
        #    所以段尾那份叫 checkpoint-{last_step - 1};完整路径取自 completion.json,不去猜时间戳。
        self._stage_ref = BigRef(
            uri=f"ssh://{self.endpoint.endpoint_id}{ckpt_dir}",
            sha256=self.client.hash_tree(ckpt_dir, "sampled"),
            size_bytes=0,  # Ruling 15:与 MockExecutor 一致,不填
            fetch_hint={
                "type": self.checkpoint_type,
                "format": "roll_lora_legacy",
                "job_id": attempt_id,
                "step": last_step,
            },
        )
        return results

    # ---------- 内部 ----------

    def _pick_attempt(self, job_id: str) -> str:
        """挑这次要用的尝试目录:仍在跑或已成功的直接接上,失败过的另起一个。"""
        for n in range(1, MAX_ATTEMPTS + 1):
            attempt_id = f"{job_id}-a{n}"
            status = self.client.status(attempt_id)
            if status.state == "missing":
                return attempt_id
            if status.state == "running":
                return attempt_id  # 幂等:接上正在跑的那次
            if status.state == "exited" and status.exit_code == 0:
                return attempt_id  # 已成功:run_job 会直接返回它的结果
        raise MetricsMissingError(
            f"job {job_id} has already used {MAX_ATTEMPTS} attempts; a human should look at the logs"
        )

    def _read_outcome(self, out_dir: str) -> str:
        """读训练脚本的 completion.json,返回段尾存点目录。

        存点路径里有一层 ROLL 框架追加的时间戳,事先猜不到,所以以脚本自己报的为准。
        读不到、不是 JSON、没有 checkpoint 键、报的位置在 out_dir 之外,都抛 MetricsMissingError:
        绝不退化成去猜目录名。"""
        path = f"{out_dir}/completion.json"
        try:
            record = json.loads(self.client.get(path))
            ckpt = str(record["checkpoint"])
        except HelperError as exc:
            if exc.code == "not_found":
                raise MetricsMissingError(
                    f"the training job left no completion.json at {path}; refusing to guess the checkpoint location"
                ) from None
            raise
        except (ValueError, TypeError, KeyError):
            raise MetricsMissingError(f"{path} is not a completion record with a checkpoint") from None
        if not (ckpt == out_dir or ckpt.startswith(out_dir + "/")) or ".." in ckpt.split("/"):
            raise MetricsMissingError(f"{path} reports a checkpoint outside the job directory")
        return ckpt

    def _parse_metrics(self, ckpt_dir: str, first_step: int, last_step: int) -> list[StepResult]:
        """从存点里的 log_history 取每步的 sft 损失,与 [first_step, last_step) 对账。

        log_history 的 system/step 是本次作业内的步号(从 0 起);续训时作业起点是 first_step,
        所以全局步号 = first_step + system/step。按 system/step 对账、不按下标:乱序、重复、
        多出来的步都不能被按位置收下。读不到、缺步、缺 loss 键都抛 MetricsMissingError:
        绝不退化成按位置编号。_update_summary 直接读 metrics["sft_loss"],缺键会是 KeyError,
        所以这里一并覆盖(设计 §5.2 第 7 步)。
        """
        path = f"{ckpt_dir}/pipeline/worker_state_pipeline.json"
        try:
            history = json.loads(self.client.get(path))["log_history"]
        except HelperError as exc:
            if exc.code == "not_found":
                raise MetricsMissingError(f"no training history at {path}") from None
            raise
        except (ValueError, TypeError, KeyError):
            raise MetricsMissingError(f"{path} has no readable log_history") from None
        seen: dict[int, float] = {}
        for entry in history if isinstance(history, list) else []:
            try:
                local = int(entry["system/step"])
                seen[first_step + local] = float(entry["sft_train/loss@sum"])
            except (ValueError, TypeError, KeyError):
                continue
        wanted = list(range(first_step, last_step))
        missing = [s for s in wanted if s not in seen]
        if missing:
            raise MetricsMissingError(
                f"the remote job reported no sft loss for step(s) {missing} "
                f"in [{first_step}, {last_step}); refusing to synthesise step numbers"
            )
        return [StepResult(step, "sft", {"sft_loss": seen[step]}) for step in wanted]

    def _remote_path(self, uri: str) -> str:
        """ssh://<endpoint_id>/<绝对路径> -> /<绝对路径>。"""
        rest = uri[len("ssh://"):]
        _, slash, path = rest.partition("/")
        if not slash:
            raise CheckpointCorruptError(f"{uri!r} carries no remote path")
        return "/" + path
