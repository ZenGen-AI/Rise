"""决策后端:把一份 PackedContext 变成模型输出的文本。三个实现都返回文本,走同一条解析路径。"""
from __future__ import annotations

import json
from typing import Protocol

from inference_bridge import InferenceBridge
from trainplanner.context import RecipeSummary
from trainplanner.prompt import PackedContext

# HeuristicBackend 只 clone batch 配方:AgentRSI 配方构造不成训练指令,interact 默认被领域校验
# 拒绝(allow_interact 默认关),而确定性后端重试也只会给出同一个答案。
CLONABLE_RECIPE_TYPES = ("batch",)


class DecisionBackend(Protocol):
    name: str

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str: ...


class BridgeDecisionBackend:
    """经外部推理桥接取模型输出。发出去的是 packed.prompt(含前几次被拒的原因)。
    桥接抛出的 BridgeError 原样上抛,不在这里重试或改类别。"""

    def __init__(self, bridge: InferenceBridge, *, name: str = "bridge") -> None:
        if not name:
            raise ValueError("backend name must be non-empty")
        self._bridge = bridge
        self.name = name

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str:
        return self._bridge.complete(packed.prompt, run_id=run_id, task_id=task_id).text


def _recipe_rank(r: RecipeSummary) -> tuple:
    # 与 recipestore 的 order="track" 一致:mean_gain 降序(None 排最后),n_reuse 降序,id 升序
    return (r.mean_gain is None, -(r.mean_gain or 0.0), -r.n_reuse, r.id)


class HeuristicBackend:
    """纯确定性:不看提示文本,只看 packed.context。

    取一级分数最低的维度(同分按维度名升序);该维度有 effective 的 batch 配方,就 clone
    战绩最好的一条(不扰动);否则 new 一条 batch。
    """

    name = "heuristic"

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str:
        ctx = packed.context
        scores = ctx.diagnostic.l1_scores
        dimension = min(scores, key=lambda d: (scores[d], d))
        usable = sorted(
            (
                r
                for r in ctx.recipes
                if r.dimension == dimension
                and r.status == "effective"
                and r.recipe_type in CLONABLE_RECIPE_TYPES
            ),
            key=_recipe_rank,
        )
        score = scores[dimension]
        if usable:
            best = usable[0]
            decision = {
                "target_dimension": dimension,
                "action": "clone",
                "mode": None,
                "source_recipe_id": best.id,
                "perturbation": {},
                "data_synthesis_hint": "",
                "rationale": (
                    f"heuristic: {dimension} has the lowest l1 score ({score}); "
                    f"clone {best.id}, the best-tracked effective recipe for it"
                ),
            }
        else:
            decision = {
                "target_dimension": dimension,
                "action": "new",
                "mode": "batch",
                "source_recipe_id": None,
                "perturbation": {},
                "data_synthesis_hint": "",
                "rationale": (
                    f"heuristic: {dimension} has the lowest l1 score ({score}); "
                    "no effective recipe targets it, so start a new batch round"
                ),
            }
        return json.dumps(decision, ensure_ascii=False)


class ScriptedDecisionBackend:
    """测试用:按顺序吐罐头回答。元素是字符串则返回它,是异常实例则抛出它。
    calls 记下每次收到的 (prompt, run_id, task_id),便于断言重试提示的内容。"""

    def __init__(self, responses: list, *, name: str = "scripted") -> None:
        self._responses = list(responses)
        self.name = name
        self.calls: list[tuple[str, str, str]] = []

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str:
        self.calls.append((packed.prompt, run_id, task_id))
        if not self._responses:
            raise AssertionError("ScriptedDecisionBackend: script exhausted")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
