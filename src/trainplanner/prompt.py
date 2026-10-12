"""提示渲染。

占位符语法:`{{name}}`,名字两侧允许空白(`{{ name }}`)。可用名字固定为 PLACEHOLDERS。
- 名字不在 PLACEHOLDERS 里 → PromptRenderError(不留空、不原样保留)。
- 模板里出现不构成占位符的 `{{` → PromptRenderError。单独的 `}}` 是普通文本。
- 模板一个占位符都没有 → PromptRenderError(提示里不会带任何上下文)。
- 替换只扫描一遍模板,替换进去的值里即使含 `{{` 也不会再被解释。
结构化的值渲染成 JSON(键排序、缩进 2、不转义非 ASCII),同一上下文渲染结果逐字节稳定。
cost_gain 里为 None 的成本(gpu_hours、tokens)渲染成字符串 "not recorded",不写 null 也不写 0:
没记录的成本不能被模型读成免费。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from trainplanner.context import PlanningContext

PLACEHOLDERS = (
    "run_id",
    "candidate",
    "diagnostic",
    "recipes",
    "recent_decisions",
    "cost_gain",
    "context",
)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_OPEN = re.compile(r"\{\{")

NOT_RECORDED = "not recorded"
_COST_KEYS = ("gpu_hours", "tokens")


class PromptRenderError(ValueError):
    """模板与上下文渲染不出一份完整的提示。这是模板的缺陷,不是模型的,不进重试。"""


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _render_costs(entries: list[dict]) -> list[dict]:
    return [
        {k: (NOT_RECORDED if k in _COST_KEYS and v is None else v) for k, v in e.items()}
        for e in entries
    ]


def placeholder_values(ctx: PlanningContext) -> dict[str, str]:
    d = ctx.to_dict()
    d["cost_gain"] = _render_costs(d["cost_gain"])
    return {
        "run_id": ctx.run_id,
        "candidate": _json(d["candidate"]),
        "diagnostic": _json(d["diagnostic"]),
        "recipes": _json(d["recipes"]),
        "recent_decisions": _json(d["recent_decisions"]),
        "cost_gain": _json(d["cost_gain"]),
        "context": _json(d),
    }


@dataclass(frozen=True)
class PackedContext:
    """一次规划要发给后端的全部东西。

    text 是模板渲染结果;feedback 是前几次被拒的原因,prompt 把两者拼成真正发出的提示。
    context 原样带着,确定性后端(HeuristicBackend)直接读它,不去解析提示文本。
    """

    text: str
    template_version: str
    context: PlanningContext
    placeholders: tuple[str, ...]
    feedback: tuple[str, ...] = field(default_factory=tuple)

    @property
    def prompt(self) -> str:
        if not self.feedback:
            return self.text
        lines = [
            self.text.rstrip("\n"),
            "",
            "## Rejected attempts",
            "Your previous output was rejected for the reasons below. "
            "Fix them and output only one JSON object.",
        ]
        lines += [f"- {reason}" for reason in self.feedback]
        return "\n".join(lines) + "\n"

    def with_feedback(self, feedback: tuple[str, ...]) -> PackedContext:
        return replace(self, feedback=tuple(feedback))


def check_template(template_text: str) -> tuple[str, ...]:
    """只查模板本身:语法、名字是否已知、至少一个占位符。返回用到的名字(排序去重)。"""
    if not isinstance(template_text, str):
        raise PromptRenderError("template_text must be a string")
    matches = list(_PLACEHOLDER.finditer(template_text))
    starts = {m.start() for m in matches}
    for m in _OPEN.finditer(template_text):
        if m.start() not in starts:
            line = template_text.count("\n", 0, m.start()) + 1
            raise PromptRenderError(f"line {line}: '{{{{' does not open a valid placeholder")
    if not matches:
        raise PromptRenderError(
            "template references no placeholder; the prompt would carry no context"
        )
    used = sorted({m.group(1) for m in matches})
    unknown = [name for name in used if name not in PLACEHOLDERS]
    if unknown:
        raise PromptRenderError(
            f"template uses unknown placeholders {unknown}; known: {list(PLACEHOLDERS)}"
        )
    return tuple(used)


def render_prompt(ctx: PlanningContext, template_text: str, template_version: str) -> PackedContext:
    if not isinstance(ctx, PlanningContext):
        raise TypeError(f"ctx must be a PlanningContext, got {type(ctx).__name__}")
    if not isinstance(template_version, str) or not template_version.strip():
        raise PromptRenderError("template_version must be a non-empty string")
    used = check_template(template_text)
    values = placeholder_values(ctx)
    text = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template_text)
    return PackedContext(
        text=text, template_version=template_version, context=ctx, placeholders=used
    )
