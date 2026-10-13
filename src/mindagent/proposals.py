"""技能提案生成器(闭环集成设计 §8.3):每条行为发现产出一份草案。

默认走规则模板(信号 -> 一条"先处理该信号"的技能);传入桥接时,让外部模型只写策略文本,
触发条件仍由发现的信号决定。外部模型必须只输出一个 JSON 对象;解析失败按 T2 带着错误信息
重试,上限 max_parse_retries,耗尽抛 ProposalParseError。桥接自己的 BridgeError 原样上抛。
记忆增量生成器 V1 不做:草案里只有 kind 为 skill 的增量。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from actio.errors import ActioDataError
from actio.skills import Skill
from mindagent.analysis import BehaviorFinding
from mindagent.errors import MindAgentDataError, ProposalParseError

RULE_GENERATOR = "rule_template.v1"
BRIDGE_GENERATOR = "bridge.v1"
PROMPT_TEMPLATE_VERSION = "mindagent-skill-v1"
CANDIDATE_PRIORITY = 100

_RULE_TEXT = {
    "emotion_high": ("先回应对方的情绪,确认对方的感受,再谈具体问题",),
    "no_new_information": ("对方没有给出新信息时,先澄清追问,不要重复自己的立场",),
}

PROMPT_TEMPLATE = """你在为一个对话智能体写一条技能的响应策略。
跨多个对话观察到的行为模式:{pattern}
信号:{signal};受影响的对话 {support} 个,占比 {ratio:.2f}。
这条技能会在信号 {signal} 为真时被选中。请只输出一个 JSON 对象,不要输出任何其他文字:
{{"instructions": ["给模型的指令", ...], "description": "一句话说明"}}
instructions 是非空的字符串列表;description 是字符串。不允许其他键。"""


@dataclass(frozen=True)
class ProposalDraft:
    finding_id: str
    increments: tuple[dict, ...]
    generator: str

    def skill_definitions(self) -> list[dict]:
        return [inc["definition"] for inc in self.increments if inc["kind"] == "skill"]


def _skill_definition(finding: BehaviorFinding, instructions: list[str], description: str) -> dict:
    definition = {
        "id": f"skill-candidate-{finding.signal}",
        "version": 1,
        "level": "micro",
        "priority": CANDIDATE_PRIORITY,
        "trigger": {"signal": finding.signal},
        "policy": {"instructions": list(instructions)},
        "description": description,
    }
    Skill.from_dict(definition)  # 必须能被 Actio 读回,否则是生成器的 bug
    return definition


def rule_template(finding: BehaviorFinding) -> list[str]:
    return list(_RULE_TEXT.get(finding.signal, (f"信号 {finding.signal} 为真时,先处理它指向的状况",)))


def parse_strategy(text: str) -> tuple[list[str], str]:
    """严格解析外部模型的输出。失败抛 ValueError(原因可读)。"""
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"output is not a single JSON object: {exc.msg}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"output must be a JSON object, got {type(obj).__name__}")
    extra = sorted(set(obj) - {"instructions", "description"})
    if extra:
        raise ValueError(f"unknown keys {extra}")
    instructions = obj.get("instructions")
    if (
        not isinstance(instructions, list) or not instructions
        or any(not isinstance(s, str) or not s.strip() for s in instructions)
    ):
        raise ValueError("instructions must be a non-empty list of non-empty strings")
    description = obj.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    return [s.strip() for s in instructions], description


def attempt_task_id(task_id: str, attempt: int) -> str:
    """第 attempt 次(从 1 起)桥接调用的 task_id:<task_id>-attempt<n>。"""
    return f"{task_id}-attempt{attempt}"


def _from_bridge(finding: BehaviorFinding, bridge, *, run_id: str, task_id: str, max_parse_retries: int) -> tuple[list[str], str]:
    prompt = PROMPT_TEMPLATE.format(
        pattern=finding.pattern, signal=finding.signal, support=finding.support, ratio=finding.ratio,
    )
    attempts = 0
    last = ""
    while attempts <= max_parse_retries:
        attempts += 1
        asked = prompt if not last else f"{prompt}\n\n上一次的输出不合格:{last}。请重新输出。"
        # 每次尝试各用一个 task_id:账本对同一 (task_id, cost_type) 只记一次,复用会丢掉重试的成本
        result = bridge.complete(asked, run_id=run_id, task_id=attempt_task_id(task_id, attempts))
        try:
            return parse_strategy(result.text)
        except ValueError as exc:
            last = str(exc)
    raise ProposalParseError(f"finding {finding.id}: {last}", attempts=attempts)


def propose(
    findings: list[BehaviorFinding] | tuple[BehaviorFinding, ...],
    *,
    bridge=None,
    run_id: str = "",
    task_id: str = "mindagent-propose",
    max_parse_retries: int = 2,
) -> list[ProposalDraft]:
    """每条发现一份草案,顺序与 findings 相同。传入桥接时,第 i 条发现的第 n 次尝试用
    task_id-<i>-attempt<n> 调用,这样记账每次尝试各记一条。传入桥接时 run_id 必须非空
    (桥接会拿它记账、拼日志路径),否则在调用桥接之前抛 MindAgentDataError。"""
    if isinstance(max_parse_retries, bool) or not isinstance(max_parse_retries, int) or max_parse_retries < 0:
        raise ValueError(f"max_parse_retries must be an int >= 0, got {max_parse_retries!r}")
    if bridge is not None and not run_id:
        raise MindAgentDataError("propose with a bridge needs a non-empty run_id")
    drafts: list[ProposalDraft] = []
    for i, finding in enumerate(findings, start=1):
        if bridge is None:
            instructions = rule_template(finding)
            description = f"rule template for {finding.signal}"
            generator = RULE_GENERATOR
        else:
            instructions, description = _from_bridge(
                finding, bridge, run_id=run_id, task_id=f"{task_id}-{i}",
                max_parse_retries=max_parse_retries,
            )
            generator = BRIDGE_GENERATOR
        try:
            definition = _skill_definition(finding, instructions, description)
        except ActioDataError as exc:
            raise ProposalParseError(f"finding {finding.id}: {exc}", attempts=1) from None
        drafts.append(ProposalDraft(
            finding_id=finding.id,
            increments=({"kind": "skill", "definition": definition},),
            generator=generator,
        ))
    return drafts
