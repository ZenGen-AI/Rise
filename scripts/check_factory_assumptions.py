#!/usr/bin/env python3
"""计划 9 对计划 3、5、7、8 的假设核对(任务 107、108、109 第 1 步都跑一次)。

一律从子模块导入(trainscheduler、mindagent 的 __init__.py 不导出名字)。每条假设一行:OK / MISSING(导入失败)/ MISMATCH(签名不符)。全部 OK 退出 0,否则退出 1。
只检查名字、参数名与参数种类,不调用任何处理器。
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

KW = inspect.Parameter.KEYWORD_ONLY
POS = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# (编号, 模块, 名字, 期望的位置参数名, 期望的 keyword-only 参数名;None 表示只查存在)
ASSUMPTIONS: list[tuple[str, str, str, tuple[str, ...] | None, tuple[str, ...] | None]] = [
    ("A3-1", "recipestore.library", "find_recipes", ("store",), ("dimension", "recipe_type", "states", "order")),
    ("A3-2", "recipestore.library", "get_recipe", ("store", "recipe_id"), ()),
    ("A3-3", "recipestore.admit", "handle_admit_recipe", ("store", "action"), ()),
    ("A3-4", "recipestore.record", "handle_record_track", ("store", "action"), ()),
    ("A3-5", "recipestore.gain", "compute_gain", ("store", "run_id", "dimension"), ()),
    ("A3-6", "recipestore.gain", "GainResult", None, None),
    ("A3-7", "recipestore.admit", "has_repro_evidence", ("payload",), ()),
    ("A3-8", "recipestore.gain", "baseline_run_of", ("store", "baseline_ref"), ()),
    ("A5-1", "trainscheduler.plan_handler", "make_plan_handler", (),
     ("backend", "template_loader", "config_loader", "defaults_loader")),
    ("A5-2", "trainscheduler.nominate", "make_nominate_handler", (),
     ("config_loader", "accept_mock_evidence", "repo_commit")),
    ("A5-3", "trainscheduler.config", "install_default_configs", ("store",), ()),
    ("A5-4", "trainscheduler.config", "SchedulerConfig", None, None),
    ("A5-5", "trainscheduler.config", "load_scheduler_config", ("store",), ()),
    ("A5-6", "trainscheduler.assemble", "assemble_context", ("store", "run_id"), ("config",)),
    ("A7-1", "mindagent.rounds", "make_start_round_handler", (),
     ("config", "client_factory", "backend_factory", "personas_loader", "bridge")),
    ("A7-2", "mindagent.admit", "handle_admit_proposal", ("store", "action"), ()),
    ("A7-3", "mindagent.library", "load_library", ("store",), ()),
    ("A7-4", "mindagent.config", "AgentConfig", None, None),
    ("A8-1", "controlplane.lock", "acquire_repo_lock", ("repo_root",), ()),
    ("A8-2", "controlplane.lock", "RepoLockedError", None, None),
]
# A5-4 的字段集:scheduler.yaml 恰好九个键(计划 5 Ruling 8)
SCHEDULER_KEYS = ("confirm_directives", "allow_interact", "accept_mock_evidence", "perturbable", "min_gain",
                  "cost_gain_window", "recent_decision_window", "max_plan_attempts", "template_version",
                  "strategy_experience")


def check(module: str, name: str, pos, kw) -> str:
    try:
        obj = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        return f"MISSING ({type(exc).__name__}: {exc})"
    if pos is None:
        return "OK"
    params = list(inspect.signature(obj).parameters.values())
    got_pos = tuple(p.name for p in params if p.kind in POS)
    got_kw = tuple(p.name for p in params if p.kind == KW)
    if got_pos != pos or got_kw != kw:
        return f"MISMATCH (positional {got_pos}, keyword-only {got_kw}; expected {pos}, {kw})"
    return "OK"


def main() -> int:
    bad = 0
    for code, module, name, pos, kw in ASSUMPTIONS:
        verdict = check(module, name, pos, kw)
        bad += verdict != "OK"
        print(f"{code} {module}.{name}: {verdict}")
    try:
        from dataclasses import fields
        from trainscheduler.config import SchedulerConfig
        got = tuple(f.name for f in fields(SchedulerConfig))
        verdict = "OK" if got == SCHEDULER_KEYS else f"MISMATCH (fields {got}; expected {SCHEDULER_KEYS})"
    except ImportError as exc:
        verdict = f"MISSING ({type(exc).__name__}: {exc})"
    bad += verdict != "OK"
    print(f"A5-4f trainscheduler.config.SchedulerConfig fields: {verdict}")
    print("all assumptions hold" if not bad else f"{bad} assumption(s) do not hold; stop and report")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
