"""配方提名(设计 §6.3、R7、§15):nominate_recipe 动作的 Handler 与它的两个纯读函数。

只对 batch 与 interact 训练 run 提名;agent_rsi 配方 V1 不自动提名(设计 §15)。
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade, evidence_acceptable
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    dataset_manifest_file,
    nomination_file,
    recipe_candidate_file,
)
from controlplane.reconcile import Action
from mindtrainer.data_spec import manifest_data_run
from trainscheduler.assemble import directive_file
from trainscheduler.config import SchedulerConfig, load_scheduler_config
from trainscheduler.recipes_port import (
    compute_gain,
    read_report_payload,
    recipe_id_for,
    regression_flags,
    validate_reproducibility,
)

Handler = Callable[[GitStore, Action], None]
ConfigLoader = Callable[[GitStore], SchedulerConfig]

NOW = "2026-10-09T00:00:00Z"
NOMINATED = "nominated"
SKIPPED = "skipped"
NOMINABLE_TYPES = ("batch", "interact")
EVAL_ANCHOR_KEYS = (
    "taxonomy_version", "item_pool_version", "benchmark_version",
    "judge_prompt_version", "scoring_config_version",
)


@dataclass(frozen=True)
class NominationVerdict:
    nominate: bool
    reason: str
    gain: float | None
    baseline_run_id: str | None
    # True:前置条件还没到(候选未到终态、缺指令或缺报告),以后会自己变好。处理器不写标记,直接返回。
    not_yet: bool = False


def _skip(reason: str, gain: float | None = None, baseline: str | None = None) -> NominationVerdict:
    return NominationVerdict(False, reason, gain, baseline)


def _not_yet(reason: str) -> NominationVerdict:
    return NominationVerdict(False, reason, None, None, not_yet=True)


def _training_mode(directive: Envelope) -> str | None:
    cfg = directive.payload.get("training_config")
    mode = cfg.get("mode") if isinstance(cfg, dict) else None
    return mode if isinstance(mode, str) else None


def nomination_check(
    store: GitStore, run_id: str, *, config: SchedulerConfig, accept_mock_evidence: bool | None = None
) -> NominationVerdict:
    """只读。accept_mock_evidence 为 None 时取 config.accept_mock_evidence。"""
    accept_mock = config.accept_mock_evidence if accept_mock_evidence is None else accept_mock_evidence
    cpath = candidate_checkpoint_file(run_id)
    if not store.exists(cpath):
        return _not_yet(f"{run_id} has no candidate checkpoint")
    status = store.read_product(cpath).status
    if status == "rejected":
        return _skip(f"candidate of {run_id} is rejected, only effective candidates are nominated")
    if status != "effective":
        return _not_yet(f"candidate of {run_id} is {status}, only effective candidates are nominated")
    dpath = directive_file(run_id)
    if not store.exists(dpath):
        return _not_yet(f"{run_id} has no directive")
    directive = store.read_product(dpath)
    mode = _training_mode(directive)
    if mode not in NOMINABLE_TYPES:
        return _skip(f"directive {run_id} is not a batch or interact training directive (mode {mode!r})")
    dimension = directive.payload.get("target_dimension")
    if not isinstance(dimension, str) or not dimension:
        return _skip(f"directive {run_id} has no target_dimension")
    report = read_report_payload(store, run_id)
    if report is None:
        return _not_yet(f"{run_id} has no diagnostic report")
    grade = diagnostic_grade(report)
    if not evidence_acceptable(grade, accept_mock):
        return _skip(f"diagnostic evidence for {run_id} is {grade}, which is not acceptable")
    gain = compute_gain(store, run_id, dimension)
    if gain.gain is None:
        return _skip(f"no comparable gain: {gain.reason}", None, gain.baseline_run_id)
    if gain.gain < config.min_gain:
        return _skip(
            f"gain {gain.gain:.4f} on {dimension} is below min_gain {config.min_gain}",
            gain.gain, gain.baseline_run_id,
        )
    flags = regression_flags(report)
    if flags:
        kinds = sorted({str(f.get("kind")) for f in flags})
        return _skip(f"diagnostic report carries regression flags {kinds}", gain.gain, gain.baseline_run_id)
    return NominationVerdict(True, f"gain {gain.gain:.4f} on {dimension} >= min_gain {config.min_gain}",
                             gain.gain, gain.baseline_run_id)


def _stage_manifests(store: GitStore, training_config: dict) -> dict:
    out: dict = {}
    refs = training_config.get("stage_manifest_refs") or {}
    for stage in sorted(refs):
        ref = refs[stage]
        data_run = manifest_data_run(ref, stage) if isinstance(ref, str) else None
        manifest_hash = None
        if data_run is not None and store.exists(dataset_manifest_file(data_run, stage)):
            manifest_hash = store.read_product(dataset_manifest_file(data_run, stage)).payload.get("manifest_hash")
        out[stage] = {"id": ref, "manifest_hash": manifest_hash}
    return out


def build_recipe_candidate(
    store: GitStore,
    run_id: str,
    *,
    config: SchedulerConfig,
    repo_commit: str | None = None,
    accept_mock_evidence: bool | None = None,
) -> Envelope:
    """按设计 §4.2 的配方载荷拼出候选信封(pending_review),不写树。run 不可提名时抛 ValueError。
    repo_commit 是产出这一轮的代码版本;不知道就是 None,可复现性校验会把它记成错误。"""
    verdict = nomination_check(store, run_id, config=config, accept_mock_evidence=accept_mock_evidence)
    if not verdict.nominate:
        raise ValueError(f"{run_id} is not nominable: {verdict.reason}")
    directive = store.read_product(directive_file(run_id))
    candidate = store.read_product(candidate_checkpoint_file(run_id))
    report = read_report_payload(store, run_id)
    training_config = copy.deepcopy(directive.payload["training_config"])
    recipe_type = training_config["mode"]
    anchor = {key: None for key in EVAL_ANCHOR_KEYS}
    anchor["benchmark_version"] = report.get("benchmark_version")
    payload: dict = {
        "recipe_type": recipe_type,
        "dimension": directive.payload["target_dimension"],
        "source_run_id": run_id,
        "eval_anchor": anchor,
        "result": {
            "gain": verdict.gain,
            "baseline_ref": report.get("baseline_ref"),
            "candidate_ref": candidate.id,
            "promoted": True,
        },
        "nondeterministic": recipe_type == "interact",
        "training_config": training_config,
        "code_version": {"repo_commit": repo_commit},
    }
    if recipe_type == "batch":
        payload["stage_manifests"] = _stage_manifests(store, training_config)
    else:
        payload["scenario_ids"] = list(training_config.get("scenario_ids", []))
        payload["bindings"] = copy.deepcopy(training_config.get("bindings", []))
    payload["repro"] = validate_reproducibility(payload).to_dict()
    return Envelope(
        id=recipe_id_for(run_id), type="recipe_candidate", version=1, created_at=NOW,
        created_by=OwnerRef(), status="pending_review", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id, candidate.id], payload=payload,
    )


def _note(store: GitStore, run_id: str, verdict: NominationVerdict, result: str) -> None:
    env = Envelope(
        id=f"nomination-{run_id}", type="nomination_note", version=1, created_at=NOW,
        created_by=OwnerRef(), status="executed", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id] + ([recipe_id_for(run_id)] if result == NOMINATED else []),
        payload={
            "result": result,
            "reason": verdict.reason,
            "gain": verdict.gain,
            "baseline_run_id": verdict.baseline_run_id,
            "recipe_candidate_id": recipe_id_for(run_id) if result == NOMINATED else None,
        },
    )
    store.write_product(nomination_file(run_id), env, f"trainscheduler: nomination {run_id} ({result})")


def make_nominate_handler(
    *,
    config_loader: ConfigLoader = load_scheduler_config,
    accept_mock_evidence: bool | None = None,
    repo_commit: str | None = None,
) -> Handler:
    """accept_mock_evidence 不为 None 时覆盖 scheduler.yaml 里的同名键(组合根的 --allow-mock-evidence)。"""

    def handle(store: GitStore, action: Action) -> None:
        run_id = action.target_id
        if store.exists(nomination_file(run_id)):
            return  # 标记就是幂等键
        config = config_loader(store)
        cpath = recipe_candidate_file(run_id)
        if store.exists(cpath):
            # 崩在"候选已写、标记未写"之间,或候选已被人审核过:不重写候选,只补标记
            verdict = nomination_check(store, run_id, config=config, accept_mock_evidence=accept_mock_evidence)
            existing = store.read_product(cpath)
            _note(store, run_id, NominationVerdict(
                True, f"recipe candidate {existing.id} already exists ({existing.status}); not rewritten",
                verdict.gain, verdict.baseline_run_id,
            ), NOMINATED)
            return
        verdict = nomination_check(store, run_id, config=config, accept_mock_evidence=accept_mock_evidence)
        if verdict.not_yet:
            return  # 过早的动作:不写标记,留给以后(标记是幂等键,写了就永不再提名)
        if not verdict.nominate:
            _note(store, run_id, verdict, SKIPPED)
            return
        env = build_recipe_candidate(
            store, run_id, config=config, repo_commit=repo_commit, accept_mock_evidence=accept_mock_evidence
        )
        store.write_product(cpath, env, f"trainscheduler: nominate {env.id}")
        _note(store, run_id, verdict, NOMINATED)

    return handle
