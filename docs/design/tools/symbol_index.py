#!/usr/bin/env python3
"""符号索引:核对设计文档里反引号包住的类名、函数名是否真实存在。

来源两处:已落地代码(src 下的 Python 与协议文件、cli/src 下的 TypeScript),以及施工计划里的代码块
(def、class、模块级常量、数据类字段)。判定:
  - 在代码里:通过。
  - 只在施工计划里:该行必须带 [计划中] 或 [推迟] 标记,否则告警。
  - 两处都没有:告警。
只核对三种写法,避免误伤:CamelCase 类名,带括号的调用 name(...),以及首段是本项目包名的点号路径。
"""
from __future__ import annotations

import ast
import builtins
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PKGS = {"controlplane", "simulator", "diagnostic", "mindtrainer", "actio", "recipestore", "trainplanner",
        "trainscheduler", "mindagent", "inference_bridge", "factory"}
# 项目专有名词与外部名称:不是代码符号,不核对
ALLOW = set(dir(builtins)) | {
    "RismForge", "ZenGen", "SocialSimulator", "MindTrainer", "MindAgent", "RecipeStore", "TrainScheduler",
    "TrainPlanner", "ControlPlane", "InteractTrain", "BatchTrain", "AgentRSI", "SoMEval", "SoMBench", "ToMBench",
    "EmoBench", "HiToM", "FANToM", "GitHub", "PyYAML", "TypeScript", "JavaScript", "SQLite", "DeepSeek",
    "TensorBoard", "OpenAI", "JsonSchema", "ClaudeCode", "VariantFamily", "QuickLook",
}

TOKEN = re.compile(r"`([^`\n]+)`")
CAMEL = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+$")
CALL = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\(.*\)$")
DOTTED = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
# 施工计划的代码块有三种写法:普通代码、整段替换、unified diff。diff 行以 + 开头,所以行首允许空白与 +。
PLAN_DEF = re.compile(
    r"^[ \t+]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)|^[ \t+]*class[ \t]+([A-Za-z_]\w*)"
    r"|^[ \t+]*([A-Z][A-Z0-9_]{2,})[ \t]*=", re.M)
PLAN_FIELD = re.compile(
    r"^[ \t+]{4,}([a-z_][a-z0-9_]*)[ \t]*:[ \t]*[A-Za-z_][\w\[\]\., |]*(?:[ \t]*=.*)?$", re.M)
PLAN_PATH = re.compile(r"`src/([\w/]+)\.py`")
TS_DEF = re.compile(r"\b(?:function|interface|class|const|type|enum)\s+([A-Za-z_]\w*)")
PROTO_DEF = re.compile(r"^\s*(?:message|service|enum)\s+(\w+)|^\s*rpc\s+(\w+)", re.M)

_cache: dict[str, set[str]] = {}


def _code() -> set[str]:
    if "code" in _cache:
        return _cache["code"]
    out: set[str] = set()
    for f in sorted((REPO / "src").rglob("*.py")):
        out.add(f.stem)
        if f.name == "__init__.py":
            out.add(f.parent.name)
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(node.name)
            elif isinstance(node, ast.Assign):
                out.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                out.add(node.target.id)
    for f in sorted((REPO / "src").rglob("*.proto")):
        for m in PROTO_DEF.finditer(f.read_text(encoding="utf-8")):
            out.update(g for g in m.groups() if g)
    for f in sorted((REPO / "cli" / "src").glob("*.ts")):
        out.update(TS_DEF.findall(f.read_text(encoding="utf-8")))
    _cache["code"] = out
    return out


def _plans() -> set[str]:
    if "plans" in _cache:
        return _cache["plans"]
    out: set[str] = set()
    for f in sorted((REPO / "docs" / "superpowers" / "plans").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        for m in PLAN_DEF.finditer(text):
            out.update(g for g in m.groups() if g)
        out.update(PLAN_FIELD.findall(text))
        for m in PLAN_PATH.finditer(text):
            out.update(m.group(1).split("/"))
    _cache["plans"] = out
    return out


def _candidate(token: str) -> str | None:
    """反引号里的内容若是三种可核对的写法之一,返回要查的符号名,否则 None。"""
    m = CALL.match(token)
    if m:
        parts = m.group(1).split(".")
        if len(parts) > 1 and parts[0] not in PKGS:
            return None
        return parts[-1]
    if DOTTED.match(token):
        parts = token.split(".")
        return parts[-1] if parts[0] in PKGS else None
    if CAMEL.match(token):
        return token
    return None


def check_line(line: str) -> list[str]:
    planned = "[计划中]" in line or "[推迟]" in line
    code, plans = _code(), _plans()
    msgs: list[str] = []
    seen: set[str] = set()
    for m in TOKEN.finditer(line):
        name = _candidate(m.group(1).strip())
        if not name or name in seen or name in ALLOW:
            continue
        seen.add(name)
        if name in code:
            continue
        if name in plans:
            if not planned:
                msgs.append(f"`{name}` 目前只在施工计划里,该行缺 [计划中] 标记")
        else:
            msgs.append(f"`{name}` 在代码与施工计划里都找不到")
    return msgs
