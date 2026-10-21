"""规划提示模板:包内默认模板 PLANNER_V1,以及它在树里的信封形态。

模板产物:type 为 prompt_template,status 为 executed,载荷恰为 {version, text},
路径 planner_prompt_file(version)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import planner_prompt_file
from trainplanner.prompt import PromptRenderError, check_template

_NOW = "2026-10-09T00:00:00Z"
TEMPLATE_TYPE = "prompt_template"
TEMPLATE_STATUS = "executed"

PLANNER_V1_VERSION = "v1"
# 版本号拼进文件名 planner-<version>.yaml:只允许单个路径段里安全的字符,"/" 会建出子目录
_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")

PLANNER_V1 = """You are the training planner of a model factory. A training round has just
finished. Read the evidence below and choose what the next round should do.

## Source run
{{run_id}}

## Candidate checkpoint of the source run
{{candidate}}

## Diagnostic summary of the source run
l1_scores maps each first-level dimension to its score (higher is better).
regression_flags lists statistically significant problems; error_profiles shows
where the errors concentrate. The meaning of a flag's magnitude depends on its kind:
for "social_regression" it is the drop in the subject dimension (larger is worse);
for "g_probe_failure" it is the probe's score (smaller is worse);
for "reward_hacking_suspected" it is reward gain minus diagnostic gain
(larger is worse).
{{diagnostic}}

## Recipes on record
Only recipes whose status is "effective" may be cloned or replayed.
mean_gain is the average gain of past reuses (null means no comparable result yet).
{{recipes}}

## Recent planner decisions and how their rounds turned out
{{recent_decisions}}

## Cost and gain of recent rounds
gpu_hours and tokens are "not recorded" when nothing was logged for that round;
that means unknown, not zero and not free.
{{cost_gain}}

## Your answer
Output exactly one JSON object and nothing else: no prose before or after it.
The object must have exactly these seven keys:

- "target_dimension": string. The first-level dimension the next round should improve,
  normally one of the keys of l1_scores.
- "action": one of "new", "clone", "replay".
  "new" trains from the default training config; "clone" reuses a recipe's training
  config with a small perturbation; "replay" reruns a recipe exactly as recorded.
- "mode": "batch" or "interact" when action is "new"; null for "clone" and "replay"
  (the recipe already fixes the mode). Use "batch": interact is off by default and a
  decision that uses it is normally rejected.
- "source_recipe_id": the id of an effective recipe when action is "clone" or "replay";
  null when action is "new".
- "perturbation": object mapping training-config keys to new scalar values. Allowed only
  for "clone"; must be {} for "new" and "replay". Each key is a dotted path into the
  recipe's training config, for example "grpo_config.learning_rate" or "sft_steps".
  Only keys on the scheduler's perturbable whitelist are accepted; any other key is
  rejected and costs you one of your limited attempts. Each value must be a string,
  a number or true/false; null is not a scalar value and is rejected.
- "data_synthesis_hint": string, may be empty. What the training data should emphasise.
- "rationale": non-empty string. Why this choice, citing the evidence above.
"""

PLANNER_V2_VERSION = "v2"
# 能渲染第七段的模板版本。新增一个渲染该段的模板时加进来,配置校验据此判断。
STRATEGY_TEMPLATE_VERSIONS = (PLANNER_V2_VERSION,)

_STRATEGY_SECTION = """
## Strategy experience from earlier rounds
Each line summarises a past strategy family: the action and mode, the exact
perturbation values that were tried, how many comparable rounds support it, the
mean gain, and how many of those rounds contradicted it. These lines come only
from rounds whose evidence grade was real and whose baseline model and benchmark
version match the current ones. Treat them as prior evidence, not as rules:
a strategy with many contradictions is weak, and a strategy that is absent may
simply be untested. Reuse a perturbation value that worked rather than inventing
a new one when the evidence points that way.
{{strategy_experience}}
"""

PLANNER_V2 = PLANNER_V1.replace(
    "\n## Your answer\n", _STRATEGY_SECTION + "\n## Your answer\n"
)


class TemplateError(ValueError):
    """树里的模板缺失、形状不对,或与要写入的模板冲突。确定性错误,重试无益。"""


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    text: str


def _check_version(version: object) -> None:
    if not isinstance(version, str) or not _VERSION.match(version) or version in (".", ".."):
        raise TemplateError(
            f"template version must match [A-Za-z0-9._-]+ and not be '.' or '..', got {version!r}"
        )


def _envelope(version: str, text: str) -> Envelope:
    return Envelope(
        id=f"prompt-planner-{version}",
        type=TEMPLATE_TYPE,
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status=TEMPLATE_STATUS,
        schema_version=CURRENT_SCHEMA_VERSION,
        payload={"version": version, "text": text},
    )


def install_template(store: GitStore, version: str, text: str) -> str:
    """把一份模板写进树。已有同版本同文本则不动;同版本不同文本拒绝覆盖。返回相对路径。"""
    _check_version(version)
    try:
        check_template(text)
    except PromptRenderError as exc:
        raise TemplateError(f"template {version}: {exc}") from exc
    rel = planner_prompt_file(version)
    if store.exists(rel):
        existing = load_template(store, version)
        if existing.text != text:
            raise TemplateError(
                f"{rel} already holds a different template for version {version}; "
                "bump the version instead of overwriting"
            )
        return rel
    store.write_product(rel, _envelope(version, text), f"trainplanner: install planner prompt {version}")
    return rel


def install_default_template(store: GitStore) -> str:
    """装好 v1 与 v2,返回 v1 的路径:调用方与既有测试都按 v1 的契约走。"""
    rel = install_template(store, PLANNER_V1_VERSION, PLANNER_V1)
    install_template(store, PLANNER_V2_VERSION, PLANNER_V2)
    return rel


def load_template(store: GitStore, version: str) -> PromptTemplate:
    _check_version(version)
    rel = planner_prompt_file(version)
    if not store.exists(rel):
        raise TemplateError(f"planner prompt template {version} not found at {rel}")
    env = store.read_product(rel)
    if env.type != TEMPLATE_TYPE:
        raise TemplateError(f"{rel}: expected type {TEMPLATE_TYPE}, got {env.type}")
    payload = env.payload
    if set(payload) != {"version", "text"}:
        raise TemplateError(f"{rel}: payload must have exactly the keys version and text")
    if payload["version"] != version:
        raise TemplateError(f"{rel}: payload version {payload['version']!r} != {version!r}")
    text = payload["text"]
    try:
        check_template(text)
    except PromptRenderError as exc:
        raise TemplateError(f"{rel}: {exc}") from exc
    return PromptTemplate(version=version, text=text)
