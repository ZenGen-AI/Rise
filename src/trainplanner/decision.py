"""规划决策与严格解析。

模型输出必须是单个 JSON 对象;允许整段被一个 markdown 代码围栏包住(``` 或 ```json),
围栏外只能是空白。七个键全部必填,未知键拒绝,任何不合规则抛 DecisionParseError,
不做宽容猜测。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from trainplanner.context import DECISION_ACTIONS

MODES = ("batch", "interact")
DECISION_KEYS = (
    "target_dimension",
    "action",
    "mode",
    "source_recipe_id",
    "perturbation",
    "data_synthesis_hint",
    "rationale",
)

_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


class DecisionParseError(ValueError):
    """模型输出不是合规的规划决策。reason 是给人和给重试提示看的一句话。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PlannerDecision:
    """直接构造不做校验;从模型输出或字典得到决策一律走 parse_decision / from_dict。"""

    target_dimension: str
    action: str  # "new" | "clone" | "replay"
    mode: str | None  # "batch" | "interact",仅 new 需要
    source_recipe_id: str | None  # clone / replay 必填
    perturbation: dict  # 仅 clone 可非空
    data_synthesis_hint: str
    rationale: str

    def to_dict(self) -> dict:
        """七个键全量,与模型输出的形状相同。from_dict(to_dict(d)) == d。"""
        return {
            "target_dimension": self.target_dimension,
            "action": self.action,
            "mode": self.mode,
            "source_recipe_id": self.source_recipe_id,
            "perturbation": dict(self.perturbation),
            "data_synthesis_hint": self.data_synthesis_hint,
            "rationale": self.rationale,
        }

    def decision_payload(self) -> dict:
        """planner_decision 产物 payload.decision 的形状(设计 §4.2):去掉 rationale 的六个键。"""
        d = self.to_dict()
        del d["rationale"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> PlannerDecision:
        """与 parse_decision 走同一套规则,只是输入已经是字典。"""
        return _decision_from_obj(d)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        m = _FENCE.match(stripped)
        if m is None:
            raise DecisionParseError(
                "output starts a markdown code fence but is not exactly one fenced block"
            )
        return m.group("body")
    return stripped


def _reject_constant(name: str) -> object:
    raise DecisionParseError(f"non-finite number {name} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise DecisionParseError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _is_scalar(v: object) -> bool:
    if isinstance(v, (bool, str, int)):
        # 整数一定有限,不进 isfinite:超大整数(如 10**400)转 float 会抛 OverflowError
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def _clip(value: object, n: int = 80) -> str:
    """模型给的值回显进错误信息(进而进重试提示与失败记录)前先截断,repr 超过 n 个字符就截掉并加 …。"""
    try:
        text = repr(value)
    except (ValueError, RecursionError):  # 超过 4300 位的整数、深层嵌套的列表
        return f"<unprintable {type(value).__name__}>"
    return text if len(text) <= n else text[:n] + "…"


def _decision_from_obj(obj: object) -> PlannerDecision:
    if not isinstance(obj, dict):
        raise DecisionParseError(f"output must be a JSON object, got {type(obj).__name__}")
    missing = [k for k in DECISION_KEYS if k not in obj]
    if missing:
        raise DecisionParseError(f"missing keys: {missing}")
    unknown = sorted(str(k) for k in obj if k not in DECISION_KEYS)
    if unknown:
        raise DecisionParseError(f"unknown keys: {unknown}")

    target = obj["target_dimension"]
    if not isinstance(target, str) or not target.strip():
        raise DecisionParseError("target_dimension must be a non-empty string")
    action = obj["action"]
    if action not in DECISION_ACTIONS:
        raise DecisionParseError(f"action must be one of {list(DECISION_ACTIONS)}, got {_clip(action)}")
    mode = obj["mode"]
    if mode is not None and mode not in MODES:
        raise DecisionParseError(f"mode must be one of {list(MODES)} or null, got {_clip(mode)}")
    source = obj["source_recipe_id"]
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise DecisionParseError("source_recipe_id must be a non-empty string or null")
    perturbation = obj["perturbation"]
    if not isinstance(perturbation, dict):
        raise DecisionParseError("perturbation must be a JSON object")
    for k, v in perturbation.items():
        if not isinstance(k, str) or not k.strip():
            raise DecisionParseError("perturbation keys must be non-empty strings")
        if not _is_scalar(v):
            raise DecisionParseError(
                f"perturbation[{_clip(k)}] must be a string, number or boolean, got {_clip(v)}"
            )
    hint = obj["data_synthesis_hint"]
    if not isinstance(hint, str):
        raise DecisionParseError("data_synthesis_hint must be a string")
    rationale = obj["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise DecisionParseError("rationale must be a non-empty string")

    if action == "new":
        if mode is None:
            raise DecisionParseError("action 'new' requires mode ('batch' or 'interact')")
        if source is not None:
            raise DecisionParseError("action 'new' must not name a source_recipe_id")
        if perturbation:
            raise DecisionParseError("action 'new' must not carry a perturbation")
    else:
        if source is None:
            raise DecisionParseError(f"action {action!r} requires source_recipe_id")
        if mode is not None:
            raise DecisionParseError(
                f"action {action!r} must leave mode null (the recipe fixes it)"
            )
        if action == "replay" and perturbation:
            raise DecisionParseError("action 'replay' must not carry a perturbation")

    return PlannerDecision(
        target_dimension=target,
        action=action,
        mode=mode,
        source_recipe_id=source,
        perturbation=dict(perturbation),
        data_synthesis_hint=hint,
        rationale=rationale,
    )


def parse_decision(text: str) -> PlannerDecision:
    """把模型输出解析成 PlannerDecision。任何不合规都抛 DecisionParseError。"""
    if not isinstance(text, str):
        raise DecisionParseError(f"output must be text, got {type(text).__name__}")
    body = _strip_fence(text)
    if not body.strip():
        raise DecisionParseError("output is empty")
    try:
        obj = json.loads(
            body, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except DecisionParseError:
        raise  # 重复键、NaN:钩子自己给了原因
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"output is not a single JSON value: {exc.msg} at char {exc.pos}") from exc
    except (ValueError, RecursionError) as exc:
        # 超过 4300 位的整数让 json.loads 抛普通 ValueError;深层嵌套抛 RecursionError(3.11–3.13)。
        # 都是模型输出的问题,转成解析失败进重试,不让它漏出 plan()
        raise DecisionParseError(
            f"output is not a single JSON value: {type(exc).__name__}: {_clip(str(exc))}"
        ) from exc
    return _decision_from_obj(obj)
