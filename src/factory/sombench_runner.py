"""真实 SoMEval 运行器(设计 21 的 DX-16 到 DX-21、Ruling 1 到 6)。

一个候选一次评测:读候选导出 -> 起 vLLM 服务(经 compute.service) -> 在 SoMEval 检出目录里跑
两次子进程(predict、metric) -> 读 detailed_metrics.jsonl 转成 SampleOutcome -> 扫工作目录确认
凭证没落盘。服务与隧道由 compute 负责,本模块不认识作业控制细节。

SoMEval 的 log_dir 与 task_dir 都相对当前目录,而 tasks/ 在检出目录里,所以子进程的当前目录
必须是检出目录(Ruling 1)。所以结果根、实验配置都放在树外的工作目录里,并用绝对路径指定。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from compute.errors import ComputeError
from compute.service import ServiceHandle, serve as real_serve
from compute.tunnel import SshTunnel, http_probe
from compute.vllm_service import VllmServeConfig, build_serve_job
from controlplane.deferred import DeferredError
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file
from diagnostic.contamination import normalize_text
from diagnostic.datamodel import DiagnosticItem, SampleOutcome
from factory.errors import FactoryConfigError
from factory.eval_config import EvalConfig

ADAPTER = "forge"
MODEL = "forge"
PROTOCOL = "cot"
SOMBENCH_MAX_MODEL_LEN = 40960
EXPORT_FORMAT = "qwen4_exp_vllm_2d"
POLL_SECONDS = 5.0
SERVE_TIMEOUT_S = 900.0
PREDICT_TIMEOUT_S = 6 * 3600.0
METRIC_TIMEOUT_S = 2 * 3600.0
_TRACKED_DIRS = ("logs", "results", "figures")


class CandidateUnusableError(Exception):
    """候选存点无法用来评测:缺失、是 mock 导出、端点对不上。确定性失败,判 T2。"""


class EvalOutputError(Exception):
    """SoMEval 的结果不完整或不一致:缺题、指纹不符。确定性失败,判 T2。"""


class EvalSecretLeakError(Exception):
    """工作目录里出现了凭证字面值。不继承延后基类:删除目录后让守护进程带着原因退出。"""


class EvalTransientError(DeferredError):
    """可重试的失败:子进程非零退出、裁判端点出错、无答案占比超阈值。守护进程按退避重试,不写树。

    子进程的 stderr 可能带凭证,所以尾巴放在 `log_tail` 属性里,不进 `reason`;写进日志的
    那一份已经过 `_redact`,完整一份落在工作目录 0600 的日志文件里。"""

    def __init__(self, reason: str, *, log_tail: str = "") -> None:
        super().__init__(reason)
        self.log_tail = log_tail


@dataclass
class SomevalRunner:
    """实现 `diagnostic.someval_adapter.ScoredSoMEvalRunner`。"""

    store: GitStore
    client: object
    endpoint: object
    cfg: EvalConfig
    build_job: Callable[[VllmServeConfig], object] = build_serve_job
    tunnel_factory: Callable[..., object] | None = None
    probe: Callable[[str, str], bool] = http_probe
    serve: Callable[..., object] = real_serve
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    now: Callable[[], datetime] = datetime.now
    commit_of: Callable[[Path], str] | None = None
    _last_stats: dict | None = field(default=None, init=False, repr=False)

    def evaluate(
        self, items: list[DiagnosticItem], candidate_ref: str, k: int
    ) -> dict[str, list[str]]:
        raise NotImplementedError("SomevalRunner only scores items; use evaluate_scored()")

    def evaluate_scored(
        self, items: list[DiagnosticItem], candidate_ref: str, k: int
    ) -> dict[str, list[SampleOutcome]]:
        if k != 1:
            raise FactoryConfigError(
                f"the SoMEval runner evaluates every item once; the protocol fixes repeats at 1, got k={k}"
            )
        ref = self._export_ref(candidate_ref)
        run_root = self.cfg.work_root / candidate_ref / self.now().strftime("%Y%m%d-%H%M%S")
        run_root.mkdir(parents=True, exist_ok=False)
        try:
            with self._served(ref) as handle:
                result_dir = self._run_someval(handle, run_root)
            # 先扫凭证再解读结果:可疑文件不留在盘上,也不让它的内容进入任何异常信息。
            self._scan_for_secrets(run_root)
            outcomes, stats = self._read_outcomes(result_dir, items)
            self._last_stats = stats
            return outcomes
        except EvalSecretLeakError:
            shutil.rmtree(run_root, ignore_errors=True)
            raise

    def last_stats(self) -> dict | None:
        """最近一次评测的统计(协议、样本数、各类错误计数、数据集哈希与 SoMEval 提交)。

        只作记录,写进报告载荷的 someval 块,不参与判级。"""
        return None if self._last_stats is None else dict(self._last_stats)

    # ---------- 候选 ----------

    def _export_ref(self, candidate_ref: str) -> BigRef:
        rel = candidate_checkpoint_file(candidate_ref)
        if not self.store.exists(rel):
            raise CandidateUnusableError(f"there is no candidate checkpoint for {candidate_ref!r}")
        payload = self.store.read_product(rel).payload
        raw = payload.get("checkpoint_ref")
        if not isinstance(raw, dict):
            raise CandidateUnusableError("the candidate checkpoint carries no checkpoint_ref")
        ref = BigRef.from_dict(raw)
        hint = ref.fetch_hint or {}
        if hint.get("format") != EXPORT_FORMAT or hint.get("type") != "roll_ssh":
            raise CandidateUnusableError(
                "the candidate is not a real adapter export; real evaluation needs a roll_ssh export"
            )
        prefix = f"ssh://{self.endpoint.endpoint_id}/"
        if not ref.uri.startswith(prefix):
            raise CandidateUnusableError(
                "the candidate export lives on another endpoint than the one this evaluator is wired to"
            )
        return ref

    def _adapter_path(self, ref: BigRef) -> str:
        return "/" + ref.uri[len(f"ssh://{self.endpoint.endpoint_id}/"):]

    # ---------- 服务 ----------

    @contextmanager
    def _served(self, ref: BigRef) -> Iterator[ServiceHandle]:
        cfg = VllmServeConfig(
            model_path=self.endpoint.base_model_path,
            adapter_name=ADAPTER,
            adapter_path=self._adapter_path(ref),
            remote_port=self.cfg.serve_port,
            max_model_len=SOMBENCH_MAX_MODEL_LEN,
        )
        factory = self.tunnel_factory
        if factory is None:
            alias = getattr(self.client, "alias", None)
            if not alias:
                raise ComputeError("the compute client does not expose its ssh alias for the tunnel")
            factory = lambda: SshTunnel(alias, remote_port=cfg.remote_port)  # noqa: E731
        with self.serve(
            self.client, cfg, build_job=self.build_job, tunnel_factory=factory, probe=self.probe,
            startup_timeout_s=SERVE_TIMEOUT_S, poll_s=POLL_SECONDS,
        ) as handle:
            self._serving_key = handle.api_key
            try:
                yield handle
            finally:
                self._serving_key = None

    # ---------- 子进程 ----------

    def _experiment_config(self, handle: ServiceHandle, run_root: Path) -> dict:
        return {
            "llm": {
                "model_name": MODEL,
                "api_key": "${VLLM_API_KEY}",
                "api_url": f"{handle.base_url}/v1",
                "max_workers": 16,
            },
            "protocol": PROTOCOL,
            "stage": "predict",
            "datasets": ["SoMBench"],
            "max_samples": 0,
            "normalized_datasets_path": str(self.cfg.someval_root / "datasets"),
            "results_path": str(run_root / "results"),
            "figures_path": str(run_root / "figures"),
        }

    def _env(self, handle: ServiceHandle) -> dict[str, str]:
        """子进程只拿到它需要的四个变量。RUN_TIMESTAMP 不传:run_eval.py 会用自己的时间戳覆盖。"""
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "VLLM_API_KEY": handle.api_key,
            "OPENAI_API_KEY": self.cfg.judge_api_key,
            "OPENAI_BASE_URL": self.cfg.judge_base_url,
        }

    def _run_someval(self, handle: ServiceHandle, run_root: Path) -> Path:
        """跑 predict 与 metric,返回结果目录。

        目录名里的时间戳由 run_eval.py 自己生成(它会覆盖 RUN_TIMESTAMP),我们事先猜不到,
        所以 predict 前后各列一次目录,新增的那个就是本次的。"""
        before = self._existing_experiments(run_root)
        config_path = run_root / "experiment_config.yaml"
        config_path.write_text(
            yaml.safe_dump(self._experiment_config(handle, run_root), sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        result_dir: Path | None = None
        for stage, timeout in (("predict", PREDICT_TIMEOUT_S), ("metric", METRIC_TIMEOUT_S)):
            argv = [str(self.cfg.someval_python), "run_eval.py", "--experiment-config", str(config_path)]
            if stage == "metric":
                if result_dir is None:
                    raise EvalOutputError("cannot run metric: the predict stage left no experiment directory")
                argv.extend(["--exp-dir", result_dir.name])
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["stage"] = stage
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=True, allow_unicode=True), encoding="utf-8")
            completed = self.run(
                argv, cwd=str(self.cfg.someval_root), env=self._env(handle),
                capture_output=True, timeout=timeout, check=False,
            )
            if completed.returncode != 0:
                stderr = self._redact((completed.stderr or b"").decode("utf-8", "replace"))
                log_name = self._write_stderr(run_root, stage, stderr)
                raise EvalTransientError(
                    f"SoMEval {stage} exited with code {completed.returncode}; "
                    f"stderr in {log_name} (redacted)",
                    log_tail=stderr[-400:],
                )
            if stage == "predict":
                fresh = self._existing_experiments(run_root) - before
                if len(fresh) != 1:
                    raise EvalOutputError(
                        f"the predict stage left {len(fresh)} new experiment directories, expected exactly one"
                    )
                result_dir = (run_root / "results" / "SoMBench" / MODEL / fresh.pop())
        assert result_dir is not None
        return result_dir

    def _existing_experiments(self, run_root: Path) -> set[str]:
        base = run_root / "results" / "SoMBench" / MODEL
        return {p.name for p in base.glob("exp_*")} if base.is_dir() else set()

    def _redact(self, text: str) -> str:
        """把已知凭证从子进程输出里抹掉。宁可多抹,也不让密钥进日志与异常。"""
        for secret in (self.cfg.judge_api_key, getattr(self, "_serving_key", None)):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def _write_stderr(self, run_root: Path, stage: str, text: str) -> str:
        """完整 stderr 落到工作目录,0600。返回相对工作目录的文件名,不返回绝对路径。"""
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"someval-{stage}.stderr.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        return f"logs/{path.name}"

    def _read_outcomes(
        self, result_dir: Path, items: list[DiagnosticItem]
    ) -> tuple[dict[str, list[SampleOutcome]], dict]:
        metrics_path = result_dir / "detailed_metrics.jsonl"
        if not metrics_path.is_file():
            raise EvalOutputError(f"SoMEval wrote no detailed metrics under {result_dir}")
        stored = json.loads((result_dir / "config.json").read_text(encoding="utf-8"))
        experiment = stored.get("experiment_config") or {}
        if (experiment.get("protocol") != PROTOCOL
                or (experiment.get("llm_config") or {}).get("model_name") != MODEL):
            raise EvalOutputError("the SoMEval result directory was produced by a different experiment")

        wanted = {item.id for item in items}
        outcomes: dict[str, list[SampleOutcome]] = {}
        counts: dict[str, int] = {}
        samples = 0
        content_none = 0
        for line in metrics_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            samples += 1
            reason = record.get("error_reason")
            counts[reason] = counts.get(reason, 0) + 1
            if reason == "judge_error":
                raise EvalTransientError("the judge endpoint failed; the whole run is discarded")
            if reason == "content_none":
                content_none += 1
            sample_id = record.get("sample_id")
            if sample_id in wanted:
                outcomes.setdefault(sample_id, []).append(SampleOutcome(
                    score=1 if record.get("is_correct") else 0,
                    extracted=str(record.get("extracted") or ""),
                    error_reason=reason,
                ))
        missing = wanted - set(outcomes)
        if missing:
            raise EvalOutputError(
                f"{len(missing)} of {len(wanted)} items are absent from the SoMEval results"
            )
        if samples and content_none / samples > self.cfg.content_none_ratio_max:
            raise EvalTransientError(
                f"{content_none} of {samples} samples came back empty, above the configured ratio"
            )
        extra = {k: v for k, v in counts.items() if k not in (None, "wrong_answer", "below_threshold",
                                                             "extraction_failed", "content_none")}
        stats = {
            "protocol": PROTOCOL,
            "samples": samples,
            "content_none": content_none,
            "extraction_failed": counts.get("extraction_failed", 0),
            "unknown_error_reasons": extra,
            "dataset_sha256": self._dataset_sha(),
            "someval_commit": self._commit(),
        }
        return outcomes, stats

    def _dataset_sha(self) -> str:
        from factory.sombench import file_sha256, sombench_parquet

        return file_sha256(sombench_parquet(self.cfg.someval_root))

    def _commit(self) -> str:
        if self.commit_of is not None:
            return self.commit_of(self.cfg.someval_root)
        try:
            head = subprocess.run(
                ["git", "-C", str(self.cfg.someval_root), "rev-parse", "--short", "HEAD"],
                capture_output=True, check=False, text=True, timeout=10,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(self.cfg.someval_root), "status", "--porcelain"],
                capture_output=True, check=False, text=True, timeout=10,
            ).stdout.strip()
        except OSError:
            return "unknown"
        return f"{head or 'unknown'}{'+dirty' if dirty else ''}"

    # ---------- 凭证 ----------

    def _scan_for_secrets(self, run_root: Path) -> None:
        needles = {self.cfg.judge_api_key.encode("utf-8")}
        for path in sorted(run_root.rglob("*")):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            for needle in needles:
                if needle in blob:
                    raise EvalSecretLeakError(
                        f"the evaluation work directory contains a credential in "
                        f"{path.relative_to(run_root).as_posix()}; the directory was deleted"
                    )
