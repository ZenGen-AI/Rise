"""可复现性校验(设计 §6.1)。纯函数:不读 git 树、不改入参。

错误 = 未锚定的软引用或缺失的必填项;警告 = 已知的复现限制。结果写进候选的 repro,
不硬拒绝,由审核门③的人参考。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RECIPE_TYPES = ("batch", "interact", "agent_rsi")
# 与 trainplanner.context.RECIPE_TYPES(计划 4)是同两份字面量,必须同步;计划 9 任务 109
# 有一条跨计划一致性测试钉住两边相等(本包不导入 trainplanner,所以只能放在计划 9)。

# TrainingConfig(mindtrainer.training_config)里没有默认值的字段。本包不导入 mindtrainer,
# 所以这里只核对"键在场",类型与取值由 MindTrainer 在派发时校验。
TRAINING_CONFIG_REQUIRED = (
    "mode", "trained_slot", "base_checkpoint_ref", "teacher_model_ref", "reward",
    "sft_steps", "grpo_steps", "sft_batch_size", "groups_per_step", "group_size",
    "grpo_config", "opd_coefficient", "checkpoint_every_steps", "allow_resume",
)

# 相对引用:取值随时间漂移,锚不住。比较时忽略大小写。
RELATIVE_TOKENS = frozenset({
    "latest", "current", "current_best", "best", "newest", "head", "previous", "prev",
})
_RELATIVE_SUFFIXES = (":latest", "-latest", "@latest", "_latest")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")

INTERACT_WARNING = "interact recipes are nondeterministic: a rerun can only be highly similar, not identical"


@dataclass(frozen=True)
class ReproReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"errors": list(self.errors), "warnings": list(self.warnings)}


def is_relative_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    low = value.strip().lower()
    return low in RELATIVE_TOKENS or low.endswith(_RELATIVE_SUFFIXES)


def _walk_strings(obj: object, path: str):
    if isinstance(obj, dict):
        for k in sorted(obj, key=str):
            yield from _walk_strings(obj[k], f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def _concrete_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != "" and not is_relative_ref(value)


def _need_str(p: dict, key: str, errors: list[str], label: str | None = None) -> None:
    label = label or key
    value = p.get(key)
    if value is None:
        errors.append(f"{label}: missing")
    elif not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: must be a non-empty string")


def _need_str_list(p: dict, key: str, errors: list[str]) -> None:
    value = p.get(key)
    if value is None:
        errors.append(f"{key}: missing")
    elif not isinstance(value, list) or not value:
        errors.append(f"{key}: must be a non-empty list")
    else:
        for i, v in enumerate(value):
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{key}[{i}]: must be a non-empty string")


def _need_dict(p: dict, key: str, errors: list[str]) -> None:
    value = p.get(key)
    if value is None:
        errors.append(f"{key}: missing")
    elif not isinstance(value, dict) or not value:
        errors.append(f"{key}: must be a non-empty mapping")


def _check_bigref(cfg: dict, key: str, errors: list[str]) -> None:
    ref = cfg.get(key)
    if not isinstance(ref, dict):
        errors.append(f"training_config.{key}: must be a BigRef mapping")
        return
    if not isinstance(ref.get("sha256"), str) or not _SHA256.match(ref["sha256"]):
        errors.append(f"training_config.{key}.sha256: must be a 64-char lowercase hex digest")
    if not isinstance(ref.get("uri"), str) or not ref["uri"].strip():
        errors.append(f"training_config.{key}.uri: must be a non-empty string")


def _check_training_config(p: dict, recipe_type: str, errors: list[str]) -> dict:
    cfg = p.get("training_config")
    if cfg is None:
        errors.append("training_config: missing")
        return {}
    if not isinstance(cfg, dict):
        errors.append("training_config: must be a mapping")
        return {}
    for key in TRAINING_CONFIG_REQUIRED:
        if key not in cfg:
            errors.append(f"training_config.{key}: missing")
    if "mode" in cfg and cfg["mode"] != recipe_type:
        errors.append(f"training_config.mode: {cfg['mode']!r} does not match recipe_type {recipe_type!r}")
    for key in ("base_checkpoint_ref", "teacher_model_ref"):
        if key in cfg:
            _check_bigref(cfg, key, errors)
    return cfg


def _check_code_version(p: dict, errors: list[str]) -> None:
    cv = p.get("code_version")
    if not isinstance(cv, dict):
        errors.append("code_version: missing")
        return
    commit = cv.get("repo_commit")
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        errors.append("code_version.repo_commit: must be a 7-40 char lowercase hex commit id")


def _check_batch(p: dict, errors: list[str], warnings: list[str]) -> None:
    cfg = _check_training_config(p, "batch", errors)
    stages = p.get("stage_manifests")
    if not isinstance(stages, dict) or not stages:
        errors.append("stage_manifests: must be a non-empty mapping")
        stages = {}
    for stage in sorted(stages, key=str):  # key=str:非字符串键也不让 sorted 抛 TypeError
        entry = stages[stage]
        if not isinstance(entry, dict):
            errors.append(f"stage_manifests.{stage}: must be a mapping")
            continue
        if not _concrete_str(entry.get("id")):
            errors.append(f"stage_manifests.{stage}.id: missing")
        h = entry.get("manifest_hash")
        if not isinstance(h, str) or not _SHA256.match(h):
            errors.append(f"stage_manifests.{stage}.manifest_hash: must be a 64-char lowercase hex digest")
    refs = cfg.get("stage_manifest_refs", {}) if isinstance(cfg, dict) else {}
    if isinstance(refs, dict):
        for stage in sorted(refs, key=str):
            entry = stages.get(stage)
            if not isinstance(entry, dict):
                errors.append(f"stage_manifests.{stage}: training_config names it but it is not anchored")
            elif entry.get("id") != refs[stage]:
                errors.append(
                    f"stage_manifests.{stage}.id: {entry.get('id')!r} does not match "
                    f"training_config.stage_manifest_refs.{stage} {refs[stage]!r}"
                )
    _check_code_version(p, errors)
    anchor = p.get("eval_anchor")
    if not isinstance(anchor, dict):
        errors.append("eval_anchor: missing")
    else:
        _need_str(anchor, "taxonomy_version", errors, "eval_anchor.taxonomy_version")
        _need_str(anchor, "item_pool_version", errors, "eval_anchor.item_pool_version")


def _check_interact(p: dict, errors: list[str], warnings: list[str]) -> None:
    _check_training_config(p, "interact", errors)
    _need_str_list(p, "scenario_ids", errors)
    bindings = p.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append("bindings: must be a non-empty list")
        bindings = []
    for i, b in enumerate(bindings):
        if not isinstance(b, dict):
            errors.append(f"bindings[{i}]: must be a mapping")
            continue
        if b.get("model_backend") != "self_play" and not _concrete_str(b.get("model_ref")):
            errors.append(f"bindings[{i}].model_ref: opponent must name a concrete model id")
    _check_code_version(p, errors)
    if p.get("nondeterministic") is not True:
        errors.append("nondeterministic: interact recipes must declare nondeterministic: true")
    warnings.append(INTERACT_WARNING)


def _check_agent_rsi(p: dict, errors: list[str], warnings: list[str]) -> None:
    _need_str(p, "bound_model_id", errors)
    _need_str_list(p, "scenario_ids", errors)
    _need_str_list(p, "persona_ids", errors)
    _need_dict(p, "analyzer_config", errors)
    _need_dict(p, "verifier_config", errors)
    _need_str(p, "generator_version", errors)
    uses_bridge = p.get("uses_bridge", False)
    if not isinstance(uses_bridge, bool):
        errors.append("uses_bridge: must be a boolean")
    elif uses_bridge:
        _need_str(p, "prompt_template_version", errors)


_CHECKS = {"batch": _check_batch, "interact": _check_interact, "agent_rsi": _check_agent_rsi}


def _scan_view(payload: dict) -> dict:
    """参与相对引用扫描的那部分载荷(浅拷贝,不改入参)。两处例外:repro 是本函数自己的输出;
    training_config.reward.outcome_rules 是奖励的结局匹配关键词(自然语言词表,不是引用),
    用户把 best、current、previous 当关键词是正常用法。"""
    scanned = {k: v for k, v in payload.items() if k != "repro"}
    cfg = scanned.get("training_config")
    if isinstance(cfg, dict) and isinstance(cfg.get("reward"), dict) and "outcome_rules" in cfg["reward"]:
        reward = {k: v for k, v in cfg["reward"].items() if k != "outcome_rules"}
        scanned["training_config"] = dict(cfg, reward=reward)
    return scanned


def validate_reproducibility(recipe_payload: dict) -> ReproReport:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(recipe_payload, dict):
        return ReproReport(errors=("payload: must be a mapping",), warnings=())
    recipe_type = recipe_payload.get("recipe_type")
    check = _CHECKS.get(recipe_type) if isinstance(recipe_type, str) else None
    if check is None:
        errors.append(f"recipe_type: must be one of {list(RECIPE_TYPES)}, got {recipe_type!r}")
    else:
        check(recipe_payload, errors, warnings)
    for path, value in _walk_strings(_scan_view(recipe_payload), ""):
        if is_relative_ref(value):
            errors.append(f"{path}: relative reference {value!r} is not anchored")
    return ReproReport(errors=tuple(errors), warnings=tuple(warnings))
