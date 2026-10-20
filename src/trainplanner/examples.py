"""对外契约的样例:一份完整、合法的 PlanningContext 字典。

测试与计划 5(TrainScheduler)共用它:assemble_context 的产出必须能被
PlanningContext.from_dict 读回,形状与这里逐键一致。每次调用返回新的深拷贝。
"""
from __future__ import annotations

import copy

_EXAMPLE_CONTEXT = {
    "run_id": "run-003",
    "candidate": {"id": "cand-run-003", "status": "effective", "reject_reason": None},
    "diagnostic": {
        "l1_scores": {"empathy": 0.71, "pragmatics": 0.64, "tom": 0.58},
        "regression_flags": [
            {"kind": "social_regression", "subject": "pragmatics", "magnitude": 0.06}
        ],
        "error_profiles": [
            {
                "l2_dimension": "tom.false_belief",
                "error_type_distribution": {"literal_reading": 0.5, "perspective_swap": 0.5},
            }
        ],
        "benchmark_version": "bench-v1",
        "grade": "real",
    },
    "recipes": [
        {
            "id": "recipe-run-001",
            "recipe_type": "batch",
            "dimension": "tom",
            "status": "effective",
            "mean_gain": 0.04,
            "n_reuse": 2,
        },
        {
            "id": "recipe-run-002",
            "recipe_type": "batch",
            "dimension": "empathy",
            "status": "effective",
            "mean_gain": None,
            "n_reuse": 0,
        },
    ],
    "recent_decisions": [
        {
            "decision_id": "planner-run-002",
            "next_run_id": "run-003",
            "target_dimension": "tom",
            "action": "clone",
            "source_recipe_id": "recipe-run-001",
            "candidate_status": "effective",
            "gain": 0.03,
        }
    ],
    "cost_gain": [
        {"run_id": "run-002", "target_dimension": "empathy", "gpu_hours": None, "tokens": None, "gain": None},
        {"run_id": "run-003", "target_dimension": "tom", "gpu_hours": 2.0, "tokens": 12000, "gain": 0.03},
    ],
    "strategy_experience": [],
}


def example_context_dict() -> dict:
    return copy.deepcopy(_EXAMPLE_CONTEXT)
