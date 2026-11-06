# ControlPlane + 工厂模拟器骨架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Rise 的 ControlPlane 核心与一套 7 子系统轻量 stub 替身，跑通一个假的端到端 RSI 编排闭环（人工写训练指令 → 调和循环下发 → SocialSimulator/MindTrainer stub 产出候选 checkpoint → 审核门①批准 → 登记 ZenGen 版本并写本轮结果），验证产物存储、状态机、调和循环、审核门 CLI 的编排逻辑正确。

**Architecture:** 控制层双语言分工——Python 负责 ControlPlane 核心（git 树产物存储、产物信封、状态机、纯函数 `reconcile()`、git CLI 封装、调和守护进程）与工厂模拟器 stub；TypeScript 负责人工审核门 CLI（`review/approve/reject/status`）。两种语言**互不直接调用**，只通过 git 树里的 YAML 产物 + `schemas/` 下的语言中立 JSON Schema 通信，这正是设计"元数据唯一真相"与"CLI 直接读本地文件"的落地。全部逻辑核心设计为纯函数，可在无 SSH/GPU/子系统的情况下用 fixture 测试。

**Tech Stack:**
- Python 3.11+ / uv（包管理）/ pytest — ControlPlane 核心 + 工厂模拟器 stub
- TypeScript / Node 20+ / vitest — 审核门 CLI
- git CLI（子进程调用，非语言 git 库）
- YAML（产物序列化，pyyaml + js-yaml）/ JSON Schema（校验，jsonschema + ajv）

**Spec:**
- `docs/superpowers/specs/2026-09-22-rise-architecture-design.md`（总体架构）
- `docs/superpowers/specs/2026-09-24-rise-subsystem-design.md`（七子系统详细设计，本计划实现其 §1 ControlPlane + §7 测试策略"工厂模拟器优先"）

## Global Constraints

每个任务的要求都隐含包含本节。以下值直接取自 spec，不可改动：

- **语言分工**：ControlPlane 核心与 stub 用 **Python 3.11+ / uv / pytest**；审核门 CLI 用 **TypeScript / Node 20+ / vitest**。两语言不互相调用，只经 git 树 YAML 产物 + `schemas/` JSON Schema 通信。
- **git 访问方式**：所有 git 操作通过**子进程调用系统 `git` 命令**，禁止引入 GitPython/libgit2/simple-git 等语言 git 库（spec 补充 B.2）。
- **调和触发**：**轮询**，可配置间隔默认 5–30 秒，禁止 inotify/文件系统原生监听（spec 补充 B.1）。
- **reconcile 纯函数**：`reconcile()` 必须是纯函数（给定 git 树状态 → 决定动作列表），可完全脱离真实 SSH/GPU/子系统用 fixture 测试（spec §1.7）。
- **单一真相源**：元数据（产物）存为 git 追踪的 YAML 文件；大体量产物（checkpoint、数据集）只存引用 `{uri, sha256, size_bytes}`，不入 git 树（spec §1.1、§1.6）。
- **V1 扩展性税（spec 补充 F.5，四项必做，字段必须存在即使 V1 不参与逻辑）**：
  1. 产物信封 `created_by` 为结构化归属 `{owner, visibility}`，V1 默认 `owner="default"`、`visibility="shared"`。
  2. 产物信封含 `schema_version` 整数字段，V1 恒为 `1`。
  3. 大体量引用含可选 `fetch_hint` 字段（V1 可为 `null`）。
  4. 读取任一产物必须走"迁移器链（V1 为空）后再消费"的结构，禁止直接反序列化后交给业务逻辑。
- **状态机（spec §1.3）**：需审核类产物状态 `draft → pending_review → approved → effective →（deprecated | superseded）`，另 `pending_review → rejected`；非审核类产物状态 `pending → executed → consumed`，另 `pending → failed`。负面状态（rejected/failed）保留不删除。
- **凭证绝不进 git 树（spec 补充 G.3，安全关键）**：git 树里的配置只允许出现**凭证的变量名引用**（如 `token_env: ANTHROPIC_API_KEY`），绝不出现凭证值。因为每次状态变更都会 `git commit`，凭证一旦写进配置文件就会进入 git 历史，事后清除代价极高。仓库 `.gitignore` 必须排除 `.env` / `*.local.yaml` / `secrets/`；凭证解析失败必须报明确错误（提示缺哪个环境变量），禁止静默降级或发送空 token。
- **V1 人机接口为纯 CLI（spec 补充 G.1）**：不做任何图形界面、不引入监控看板。
- **配置分层（spec 补充 G.2）**：影响可复现性的配置（后端选择/模型名/prompt 模板版本/RetryPolicy/单价表）进 git 树 `control-plane/config/`；凭证走环境变量。
- **标识与命名**：产物状态、字段名用英文小写下划线标识符；ZenGen 版本产物命名 `zengen-v<N>`。
- **目录结构**：git 仓库根为 `control-plane/`，子目录 `runs/ recipes/ skill_memory_proposals/ models/ compute/`，与 spec §1.2 一致。

---

## File Structure

**Python 包 `src/controlplane/`（ControlPlane 核心）**
- `envelope.py` — 产物通用信封（`OwnerRef`/`BigRef`/`Envelope`）+ 序列化，落实 F.5 前三项税
- `migration.py` — schema 迁移器链（V1 空链）+ `load_product()` 读取路径，落实 F.5 第四项税
- `states.py` — 产物状态机纯函数（两类产物的合法迁移）
- `paths.py` — `control-plane/` 目录布局与产物路径解析
- `gitstore.py` — git CLI 子进程封装（init/read/write/commit/list）
- `reconcile.py` — 纯函数 `reconcile(TreeState) -> list[Action]` + 视图数据类
- `daemon.py` — 轮询调和（一次性 `reconcile_once` + 守护 `run_daemon`）+ 动作执行器
- `config.py` — 三层配置加载 + 凭证按变量名从环境解析 + 日志脱敏，落实补充 G.3 安全边界
- `observe.py` — 只读可观测呈现（`trace`/`cost`），落实补充 G.4

**Python 包 `src/simulator/`（工厂模拟器 stub）**
- `stubs.py` — 子系统 stub（SocialSimulator/MindTrainer 产出真实产物，其余 no-op）

**语言中立 schema `schemas/`**
- `envelope.schema.json` — 信封 JSON Schema（Python 与 TS 共用）

**仓库根**
- `.gitignore` — 排除凭证文件（`.env` / `*.local.yaml` / `secrets/`），补充 G.3 硬约束

**TypeScript 包 `cli/`（审核门 CLI）**
- `src/gittree.ts` — 读写 YAML 产物 + git commit（子进程）
- `src/commands.ts` — `review/approve/reject/status` 命令实现
- `src/index.ts` — 命令分发入口
- `package.json` / `tsconfig.json` / `vitest.config.ts`

**测试**
- `tests/` (Python, pytest) + `cli/test/` (TS, vitest)

---

### Task 1: 双语言项目脚手架与工具链冒烟

**Files:**
- Create: `pyproject.toml`
- Create: `src/controlplane/__init__.py`
- Create: `src/simulator/__init__.py`
- Create: `cli/package.json`
- Create: `cli/tsconfig.json`
- Create: `cli/vitest.config.ts`
- Create: `cli/src/index.ts`
- Test: `tests/test_smoke.py`
- Test: `cli/test/smoke.test.ts`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: Python 包 `controlplane`、`simulator` 可导入；`uv run pytest` 可跑；`cli` 下 `npm test` 可跑

- [ ] **Step 1: 写 Python 冒烟测试**

`tests/test_smoke.py`:
```python
def test_controlplane_importable():
    import controlplane
    assert controlplane is not None


def test_simulator_importable():
    import simulator
    assert simulator is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL（`ModuleNotFoundError` / 无 pyproject）

- [ ] **Step 3: 写 Python 脚手架**

`pyproject.toml`:
```toml
[project]
name = "rise-controlplane"
version = "0.1.0"
description = "Rise ControlPlane core and factory simulator stubs"
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0",
    "jsonschema>=4.20",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/controlplane", "src/simulator"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

`src/controlplane/__init__.py`:
```python
"""Rise ControlPlane core."""
```

`src/simulator/__init__.py`:
```python
"""Rise factory simulator stubs."""
```

- [ ] **Step 4: 运行 Python 测试确认通过**

Run: `uv run --extra dev pytest tests/test_smoke.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 写 TS 冒烟测试**

`cli/test/smoke.test.ts`:
```typescript
import { describe, it, expect } from "vitest";
import { CLI_NAME } from "../src/index.js";

describe("cli smoke", () => {
  it("exposes the cli name", () => {
    expect(CLI_NAME).toBe("rise");
  });
});
```

- [ ] **Step 6: 写 TS 脚手架**

`cli/package.json`:
```json
{
  "name": "rise-cli",
  "version": "0.1.0",
  "type": "module",
  "bin": { "rise": "./dist/index.js" },
  "scripts": {
    "build": "tsc",
    "test": "vitest run"
  },
  "dependencies": {
    "js-yaml": "^4.1.0",
    "ajv": "^8.12.0"
  },
  "devDependencies": {
    "typescript": "^5.4.0",
    "vitest": "^1.6.0",
    "@types/js-yaml": "^4.0.9",
    "@types/node": "^20.11.0"
  }
}
```

`cli/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

`cli/vitest.config.ts`:
```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["test/**/*.test.ts"] },
});
```

`cli/src/index.ts`:
```typescript
export const CLI_NAME = "rise";
```

- [ ] **Step 7: 运行 TS 测试确认通过**

Run: `cd cli && npm install && npm test`
Expected: PASS（1 passed）

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml src/ tests/test_smoke.py cli/package.json cli/tsconfig.json cli/vitest.config.ts cli/src/index.ts cli/test/smoke.test.ts
git commit -m "chore: scaffold python controlplane + typescript cli packages"
```

---

### Task 2: 产物通用信封（落实 F.5 前三项扩展性税）

**Files:**
- Create: `src/controlplane/envelope.py`
- Test: `tests/test_envelope.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `OwnerRef(owner: str = "default", visibility: str = "shared")` dataclass
  - `BigRef(uri: str, sha256: str, size_bytes: int, fetch_hint: dict | None = None)` dataclass
  - `Envelope(id: str, type: str, version: int, created_at: str, created_by: OwnerRef, status: str, schema_version: int, links: list[str], payload: dict)` dataclass
  - `Envelope.to_dict() -> dict` / `Envelope.from_dict(d: dict) -> Envelope`
  - `CURRENT_SCHEMA_VERSION: int = 1`

- [ ] **Step 1: 写失败测试**

`tests/test_envelope.py`:
```python
from controlplane.envelope import Envelope, OwnerRef, BigRef, CURRENT_SCHEMA_VERSION


def test_envelope_defaults_carry_extensibility_tax():
    env = Envelope(
        id="run-001",
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    assert env.created_by.owner == "default"
    assert env.created_by.visibility == "shared"
    assert env.schema_version == 1
    assert env.links == []


def test_envelope_round_trip():
    env = Envelope(
        id="cand-001",
        type="candidate_checkpoint",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(owner="alice", visibility="private"),
        status="pending_review",
        schema_version=1,
        links=["run-001"],
        payload={"note": "hi"},
    )
    restored = Envelope.from_dict(env.to_dict())
    assert restored == env


def test_bigref_fetch_hint_optional():
    ref = BigRef(uri="file:///ckpt", sha256="abc", size_bytes=10)
    assert ref.fetch_hint is None
    d = ref.to_dict()
    assert d["fetch_hint"] is None
    assert BigRef.from_dict(d) == ref
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_envelope.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.envelope`）

- [ ] **Step 3: 实现信封**

`src/controlplane/envelope.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field

CURRENT_SCHEMA_VERSION = 1


@dataclass
class OwnerRef:
    """归属标识（F.5 多租户税）。V1 单开发者恒为默认值，不参与逻辑判断。"""

    owner: str = "default"
    visibility: str = "shared"  # "private" | "shared"

    def to_dict(self) -> dict:
        return {"owner": self.owner, "visibility": self.visibility}

    @classmethod
    def from_dict(cls, d: dict) -> "OwnerRef":
        return cls(owner=d["owner"], visibility=d["visibility"])


@dataclass
class BigRef:
    """大体量产物引用（不入 git 树）。fetch_hint 为 F.5 导入导出税，V1 可为 None。"""

    uri: str
    sha256: str
    size_bytes: int
    fetch_hint: dict | None = None  # {"type": ..., "url": ...} or None

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "fetch_hint": self.fetch_hint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BigRef":
        return cls(
            uri=d["uri"],
            sha256=d["sha256"],
            size_bytes=d["size_bytes"],
            fetch_hint=d.get("fetch_hint"),
        )


@dataclass
class Envelope:
    """所有产物的通用信封。schema_version 为 F.5 迁移税，必须存在。"""

    id: str
    type: str
    version: int
    created_at: str
    created_by: OwnerRef
    status: str
    schema_version: int
    links: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "created_at": self.created_at,
            "created_by": self.created_by.to_dict(),
            "status": self.status,
            "schema_version": self.schema_version,
            "links": list(self.links),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Envelope":
        return cls(
            id=d["id"],
            type=d["type"],
            version=d["version"],
            created_at=d["created_at"],
            created_by=OwnerRef.from_dict(d["created_by"]),
            status=d["status"],
            schema_version=d["schema_version"],
            links=list(d.get("links", [])),
            payload=d.get("payload", {}),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_envelope.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/envelope.py tests/test_envelope.py
git commit -m "feat: product envelope with V1 extensibility tax fields"
```

---

### Task 3: 语言中立 JSON Schema 与信封校验

**Files:**
- Create: `schemas/envelope.schema.json`
- Create: `src/controlplane/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `Envelope.to_dict()`（Task 2）
- Produces:
  - `schemas/envelope.schema.json`（Python 与 TS 共用的校验契约）
  - `validate_envelope(d: dict) -> None`（校验失败抛 `EnvelopeValidationError`）
  - `EnvelopeValidationError(Exception)`

- [ ] **Step 1: 写失败测试**

`tests/test_validate.py`:
```python
import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.validate import validate_envelope, EnvelopeValidationError


def _valid_dict() -> dict:
    return Envelope(
        id="run-001",
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
    ).to_dict()


def test_valid_envelope_passes():
    validate_envelope(_valid_dict())  # 不抛异常


def test_missing_schema_version_fails():
    d = _valid_dict()
    del d["schema_version"]
    with pytest.raises(EnvelopeValidationError):
        validate_envelope(d)


def test_missing_owner_fails():
    d = _valid_dict()
    del d["created_by"]["owner"]
    with pytest.raises(EnvelopeValidationError):
        validate_envelope(d)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_validate.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.validate`）

- [ ] **Step 3: 写 JSON Schema**

`schemas/envelope.schema.json`:
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Rise Product Envelope",
  "type": "object",
  "required": ["id", "type", "version", "created_at", "created_by", "status", "schema_version"],
  "properties": {
    "id": { "type": "string", "minLength": 1 },
    "type": { "type": "string", "minLength": 1 },
    "version": { "type": "integer", "minimum": 1 },
    "created_at": { "type": "string", "minLength": 1 },
    "created_by": {
      "type": "object",
      "required": ["owner", "visibility"],
      "properties": {
        "owner": { "type": "string", "minLength": 1 },
        "visibility": { "type": "string", "enum": ["private", "shared"] }
      }
    },
    "status": { "type": "string", "minLength": 1 },
    "schema_version": { "type": "integer", "minimum": 1 },
    "links": { "type": "array", "items": { "type": "string" } },
    "payload": { "type": "object" }
  }
}
```

- [ ] **Step 4: 实现校验器**

`src/controlplane/validate.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "envelope.schema.json"


class EnvelopeValidationError(Exception):
    """产物信封不符合 JSON Schema。"""


def _load_schema() -> dict:
    with _SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def validate_envelope(d: dict) -> None:
    try:
        jsonschema.validate(instance=d, schema=_load_schema())
    except jsonschema.ValidationError as exc:
        raise EnvelopeValidationError(str(exc)) from exc
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_validate.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: 提交**

```bash
git add schemas/envelope.schema.json src/controlplane/validate.py tests/test_validate.py
git commit -m "feat: language-neutral envelope json schema + python validator"
```

---

### Task 4: schema 迁移器链与读取路径（落实 F.5 第四项税）

**Files:**
- Create: `src/controlplane/migration.py`
- Test: `tests/test_migration.py`

**Interfaces:**
- Consumes: `Envelope.from_dict()`（Task 2）、`validate_envelope()`（Task 3）、`CURRENT_SCHEMA_VERSION`（Task 2）
- Produces:
  - `Migrator = Callable[[dict], dict]`（单次版本跃迁纯函数）
  - `MIGRATORS: dict[int, Migrator]`（键为源版本号 N，值为 vN→vN+1；V1 为空 `{}`）
  - `migrate(d: dict) -> dict`（惰性升到当前版本）
  - `load_product(d: dict) -> Envelope`（迁移 → 校验 → 反序列化，业务读取的唯一入口）
  - `UnknownSchemaVersionError(Exception)`

- [ ] **Step 1: 写失败测试**

`tests/test_migration.py`:
```python
import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.migration import migrate, load_product, MIGRATORS, UnknownSchemaVersionError


def _v1_dict() -> dict:
    return Envelope(
        id="run-001",
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
    ).to_dict()


def test_v1_chain_is_empty():
    assert MIGRATORS == {}


def test_current_version_passthrough():
    d = _v1_dict()
    assert migrate(d) == d


def test_load_product_returns_envelope():
    env = load_product(_v1_dict())
    assert isinstance(env, Envelope)
    assert env.id == "run-001"


def test_future_version_beyond_current_raises():
    d = _v1_dict()
    d["schema_version"] = 99
    with pytest.raises(UnknownSchemaVersionError):
        migrate(d)


def test_migrate_applies_registered_migrator(monkeypatch):
    # 模拟未来存在 v1->v2 迁移器：读取路径必须能把旧版本升上来
    def v1_to_v2(doc: dict) -> dict:
        doc = dict(doc)
        doc["schema_version"] = 2
        doc.setdefault("payload", {})["migrated"] = True
        return doc

    monkeypatch.setattr("controlplane.migration.MIGRATORS", {1: v1_to_v2})
    monkeypatch.setattr("controlplane.migration.CURRENT_SCHEMA_VERSION", 2)
    out = migrate(_v1_dict())
    assert out["schema_version"] == 2
    assert out["payload"]["migrated"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_migration.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.migration`）

- [ ] **Step 3: 实现迁移链与读取路径**

`src/controlplane/migration.py`:
```python
from __future__ import annotations

from typing import Callable

from controlplane.envelope import Envelope, CURRENT_SCHEMA_VERSION
from controlplane.validate import validate_envelope

Migrator = Callable[[dict], dict]

# 键为源版本号 N，值为把 vN 升到 vN+1 的纯函数。V1 无历史版本，空链。
MIGRATORS: dict[int, Migrator] = {}


class UnknownSchemaVersionError(Exception):
    """产物 schema_version 高于当前版本，或缺少对应迁移器。"""


def migrate(d: dict) -> dict:
    """惰性迁移：把任意历史版本产物在内存里升到当前版本。不改写 git 树原文件。"""
    version = d["schema_version"]
    if version > CURRENT_SCHEMA_VERSION:
        raise UnknownSchemaVersionError(
            f"schema_version {version} > current {CURRENT_SCHEMA_VERSION}"
        )
    current = d
    while current["schema_version"] < CURRENT_SCHEMA_VERSION:
        v = current["schema_version"]
        migrator = MIGRATORS.get(v)
        if migrator is None:
            raise UnknownSchemaVersionError(f"no migrator registered for schema_version {v}")
        current = migrator(current)
    return current


def load_product(d: dict) -> Envelope:
    """业务读取产物的唯一入口：迁移 → 校验 → 反序列化。禁止绕过本函数直接反序列化。"""
    migrated = migrate(d)
    validate_envelope(migrated)
    return Envelope.from_dict(migrated)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_migration.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/migration.py tests/test_migration.py
git commit -m "feat: schema migrator chain + load_product read path (F.5 tax)"
```

---

### Task 5: 目录布局与产物状态机（纯函数）

**Files:**
- Create: `src/controlplane/paths.py`
- Create: `src/controlplane/states.py`
- Test: `tests/test_states.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `paths.py`: `CONTROL_PLANE_ROOT = "control-plane"`；`run_dir(run_id) -> str`；`model_file(n: int) -> str`；`backends_config_file() -> str`；`SUBDIRS: list[str]`（含 `config`）
  - `states.py`:
    - `REVIEW_TRANSITIONS: dict[str, set[str]]` / `NONREVIEW_TRANSITIONS: dict[str, set[str]]`
    - `ProductKind`（`"review"` | `"nonreview"` 字符串常量）
    - `can_transition(kind: str, frm: str, to: str) -> bool`
    - `next_states(kind: str, frm: str) -> set[str]`
    - `InvalidTransitionError(Exception)`
    - `assert_transition(kind: str, frm: str, to: str) -> None`

- [ ] **Step 1: 写 paths 失败测试**

`tests/test_paths.py`:
```python
from controlplane import paths


def test_run_dir():
    assert paths.run_dir("run-001") == "control-plane/runs/run-001"


def test_model_file():
    assert paths.model_file(3) == "control-plane/models/zengen-v3.yaml"


def test_backends_config_file():
    assert paths.backends_config_file() == "control-plane/config/backends.yaml"


def test_subdirs_match_spec():
    assert paths.SUBDIRS == ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config"]
```

- [ ] **Step 2: 写 states 失败测试**

`tests/test_states.py`:
```python
import pytest

from controlplane.states import (
    can_transition,
    next_states,
    assert_transition,
    InvalidTransitionError,
)


def test_review_happy_path():
    assert can_transition("review", "draft", "pending_review")
    assert can_transition("review", "pending_review", "approved")
    assert can_transition("review", "approved", "effective")
    assert can_transition("review", "effective", "deprecated")
    assert can_transition("review", "effective", "superseded")


def test_review_reject_path():
    assert can_transition("review", "pending_review", "rejected")


def test_review_illegal_transition():
    assert not can_transition("review", "draft", "effective")
    assert not can_transition("review", "rejected", "approved")


def test_nonreview_paths():
    assert can_transition("nonreview", "pending", "executed")
    assert can_transition("nonreview", "pending", "failed")
    assert can_transition("nonreview", "executed", "consumed")
    assert not can_transition("nonreview", "consumed", "executed")


def test_next_states():
    assert next_states("review", "pending_review") == {"approved", "rejected"}


def test_assert_transition_raises():
    with pytest.raises(InvalidTransitionError):
        assert_transition("review", "draft", "effective")
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_paths.py tests/test_states.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 实现 paths**

`src/controlplane/paths.py`:
```python
from __future__ import annotations

CONTROL_PLANE_ROOT = "control-plane"
SUBDIRS = ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config"]


def run_dir(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/runs/{run_id}"


def model_file(n: int) -> str:
    return f"{CONTROL_PLANE_ROOT}/models/zengen-v{n}.yaml"


def backends_config_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/backends.yaml"
```

- [ ] **Step 5: 实现 states**

`src/controlplane/states.py`:
```python
from __future__ import annotations

REVIEW = "review"
NONREVIEW = "nonreview"

# spec §1.3 需审核类：draft → pending_review → approved → effective →（deprecated|superseded）
#                       pending_review → rejected
REVIEW_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_review"},
    "pending_review": {"approved", "rejected"},
    "approved": {"effective"},
    "effective": {"deprecated", "superseded"},
    "rejected": set(),
    "superseded": set(),
    "deprecated": set(),
}

# spec §1.3 非审核类：pending → executed → consumed；pending → failed
NONREVIEW_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"executed", "failed"},
    "executed": {"consumed"},
    "consumed": set(),
    "failed": set(),
}


class InvalidTransitionError(Exception):
    """产物状态迁移不合法。"""


def _table(kind: str) -> dict[str, set[str]]:
    if kind == REVIEW:
        return REVIEW_TRANSITIONS
    if kind == NONREVIEW:
        return NONREVIEW_TRANSITIONS
    raise ValueError(f"unknown product kind: {kind}")


def next_states(kind: str, frm: str) -> set[str]:
    return set(_table(kind).get(frm, set()))


def can_transition(kind: str, frm: str, to: str) -> bool:
    return to in next_states(kind, frm)


def assert_transition(kind: str, frm: str, to: str) -> None:
    if not can_transition(kind, frm, to):
        raise InvalidTransitionError(f"{kind}: {frm} -> {to} not allowed")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_paths.py tests/test_states.py -v`
Expected: PASS（9 passed）

- [ ] **Step 7: 提交**

```bash
git add src/controlplane/paths.py src/controlplane/states.py tests/test_paths.py tests/test_states.py
git commit -m "feat: directory layout + product state machine pure functions"
```

---

### Task 6: git 树存储（git CLI 子进程封装）

**Files:**
- Create: `src/controlplane/gitstore.py`
- Test: `tests/test_gitstore.py`

**Interfaces:**
- Consumes: `Envelope`（Task 2）、`load_product()`（Task 4）、`paths.SUBDIRS`（Task 5）
- Produces:
  - `GitStore(repo_root: str)` 类
  - `GitStore.init() -> None`（`git init` + 建 `control-plane/` 子目录）
  - `GitStore.write_product(rel_path: str, env: Envelope, message: str) -> None`（写 YAML + `git add` + `git commit`）
  - `GitStore.read_product(rel_path: str) -> Envelope`（读 YAML，经 `load_product()`）
  - `GitStore.list_products(rel_dir: str) -> list[str]`（列 `.yaml` 相对路径）
  - `GitStore.exists(rel_path: str) -> bool`

- [ ] **Step 1: 写失败测试**

`tests/test_gitstore.py`:
```python
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore


def _env(pid: str, status: str) -> Envelope:
    return Envelope(
        id=pid,
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status=status,
        schema_version=1,
    )


def test_init_creates_subdirs(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    assert (tmp_path / "control-plane" / "runs").is_dir()
    assert (tmp_path / ".git").is_dir()


def test_write_then_read_round_trip(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    env = _env("run-001", "pending")
    store.write_product("control-plane/runs/run-001/directive.yaml", env, "add directive")
    got = store.read_product("control-plane/runs/run-001/directive.yaml")
    assert got == env


def test_write_commits(tmp_path):
    import subprocess

    store = GitStore(str(tmp_path))
    store.init()
    store.write_product("control-plane/runs/run-001/directive.yaml", _env("run-001", "pending"), "add directive")
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "add directive" in log


def test_list_products(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    store.write_product("control-plane/runs/run-001/directive.yaml", _env("run-001", "pending"), "m1")
    store.write_product("control-plane/runs/run-001/outcome.yaml", _env("run-001", "pending"), "m2")
    found = store.list_products("control-plane/runs/run-001")
    assert set(found) == {
        "control-plane/runs/run-001/directive.yaml",
        "control-plane/runs/run-001/outcome.yaml",
    }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_gitstore.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.gitstore`）

- [ ] **Step 3: 实现 GitStore**

`src/controlplane/gitstore.py`:
```python
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from controlplane.envelope import Envelope
from controlplane.migration import load_product
from controlplane.paths import CONTROL_PLANE_ROOT, SUBDIRS


class GitStore:
    """git 树产物存储。所有 git 操作走子进程调用 git CLI（禁止语言 git 库）。"""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = Path(repo_root)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def init(self) -> None:
        self.repo_root.mkdir(parents=True, exist_ok=True)
        if not (self.repo_root / ".git").is_dir():
            self._git("init")
            # 保证提交可用（测试与 CI 环境可能无全局 git 身份）
            self._git("config", "user.email", "rise@example.com")
            self._git("config", "user.name", "rise")
        for sub in SUBDIRS:
            (self.repo_root / CONTROL_PLANE_ROOT / sub).mkdir(parents=True, exist_ok=True)

    def exists(self, rel_path: str) -> bool:
        return (self.repo_root / rel_path).is_file()

    def write_product(self, rel_path: str, env: Envelope, message: str) -> None:
        abs_path = self.repo_root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with abs_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(env.to_dict(), f, allow_unicode=True, sort_keys=False)
        self._git("add", rel_path)
        self._git("commit", "-m", message)

    def read_product(self, rel_path: str) -> Envelope:
        abs_path = self.repo_root / rel_path
        with abs_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return load_product(data)  # 走迁移器链（F.5 第四项税）

    def list_products(self, rel_dir: str) -> list[str]:
        abs_dir = self.repo_root / rel_dir
        if not abs_dir.is_dir():
            return []
        return [
            str(p.relative_to(self.repo_root)).replace("\\", "/")
            for p in sorted(abs_dir.glob("*.yaml"))
        ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_gitstore.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/gitstore.py tests/test_gitstore.py
git commit -m "feat: git tree store via git CLI subprocess"
```

---

### Task 7: reconcile() 纯函数

**Files:**
- Create: `src/controlplane/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: 无（纯函数，输入为视图数据类）
- Produces:
  - `DirectiveView(id: str, status: str, target_dimension: str, recipe_ref: str)` dataclass
  - `CandidateView(id: str, status: str, run_id: str)` dataclass
  - `TreeState(directives: list[DirectiveView], candidates: list[CandidateView], model_count: int)` dataclass
  - `Action(kind: str, target_id: str, detail: dict)` dataclass；`kind ∈ {"dispatch_directive", "await_review", "promote_candidate", "noop"}`
  - `reconcile(state: TreeState) -> list[Action]`（纯函数）

- [ ] **Step 1: 写失败测试**

`tests/test_reconcile.py`:
```python
from controlplane.reconcile import (
    DirectiveView,
    CandidateView,
    TreeState,
    Action,
    reconcile,
)


def test_pending_directive_dispatched():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="pending", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    actions = reconcile(state)
    assert Action(kind="dispatch_directive", target_id="run-001", detail={}) in actions


def test_executed_directive_not_redispatched():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="executed", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    assert reconcile(state) == []


def test_pending_review_candidate_awaits_human():
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status="pending_review", run_id="run-001")],
        model_count=0,
    )
    actions = reconcile(state)
    assert actions == [Action(kind="await_review", target_id="cand-001", detail={})]


def test_approved_candidate_promoted_with_next_version():
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status="approved", run_id="run-001")],
        model_count=2,
    )
    actions = reconcile(state)
    assert actions == [
        Action(kind="promote_candidate", target_id="cand-001", detail={"run_id": "run-001", "model_version": 3})
    ]


def test_reconcile_is_pure_no_mutation():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="pending", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    reconcile(state)
    reconcile(state)
    assert state.directives[0].status == "pending"  # 未被修改
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_reconcile.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.reconcile`）

- [ ] **Step 3: 实现 reconcile**

`src/controlplane/reconcile.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DirectiveView:
    id: str
    status: str
    target_dimension: str
    recipe_ref: str


@dataclass(frozen=True)
class CandidateView:
    id: str
    status: str
    run_id: str


@dataclass
class TreeState:
    directives: list[DirectiveView] = field(default_factory=list)
    candidates: list[CandidateView] = field(default_factory=list)
    model_count: int = 0


@dataclass(frozen=True)
class Action:
    kind: str
    target_id: str
    detail: dict


def reconcile(state: TreeState) -> list[Action]:
    """纯函数：给定 git 树当前状态，返回下一步动作列表。不做任何 IO、不修改入参。

    - directive=pending → dispatch_directive（交执行器经 SSH 下发，本骨架交 stub）
    - candidate=pending_review → await_review（等待人工审核门①，无动作）
    - candidate=approved → promote_candidate（登记新 ZenGen 版本 + 写 outcome）
    """
    actions: list[Action] = []
    for d in state.directives:
        if d.status == "pending":
            actions.append(Action(kind="dispatch_directive", target_id=d.id, detail={}))
    for c in state.candidates:
        if c.status == "pending_review":
            actions.append(Action(kind="await_review", target_id=c.id, detail={}))
        elif c.status == "approved":
            actions.append(
                Action(
                    kind="promote_candidate",
                    target_id=c.id,
                    detail={"run_id": c.run_id, "model_version": state.model_count + 1},
                )
            )
    return actions
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_reconcile.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/reconcile.py tests/test_reconcile.py
git commit -m "feat: pure reconcile() function over tree state"
```

---

### Task 8: 子系统 stub 与动作执行器

**Files:**
- Create: `src/simulator/stubs.py`
- Test: `tests/test_stubs.py`

**Interfaces:**
- Consumes: `Envelope`/`OwnerRef`（Task 2）、`GitStore`（Task 6）、`paths.run_dir`（Task 5）、`Action`（Task 7）
- Produces:
  - `social_simulator_stub(run_id: str, target_dimension: str) -> Envelope`（返回 `type="scenario_pool"`, `status="executed"` 的产物）
  - `mind_trainer_stub(run_id: str, scenario_id: str) -> Envelope`（返回 `type="candidate_checkpoint"`, `status="pending_review"`, `links=[run_id, scenario_id]`，payload 含 `BigRef` checkpoint 引用）
  - `execute_dispatch(store: GitStore, run_id: str, target_dimension: str) -> None`（跑 SocialSimulator stub → 写 scenario；跑 MindTrainer stub → 写 candidate pending_review）

- [ ] **Step 1: 写失败测试**

`tests/test_stubs.py`:
```python
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir
from simulator.stubs import social_simulator_stub, mind_trainer_stub, execute_dispatch


def test_social_simulator_stub_produces_scenario():
    env = social_simulator_stub("run-001", "tom")
    assert env.type == "scenario_pool"
    assert env.status == "executed"
    assert "run-001" in env.links


def test_mind_trainer_stub_produces_pending_candidate():
    env = mind_trainer_stub("run-001", "scen-001")
    assert env.type == "candidate_checkpoint"
    assert env.status == "pending_review"
    assert env.links == ["run-001", "scen-001"]
    # checkpoint 作为大体量引用，不入产物本体
    assert env.payload["checkpoint_ref"]["sha256"]
    assert env.payload["checkpoint_ref"]["fetch_hint"] is None


def test_execute_dispatch_writes_scenario_and_candidate(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    execute_dispatch(store, "run-001", "tom")
    files = store.list_products(run_dir("run-001"))
    kinds = {store.read_product(f).type for f in files}
    assert "scenario_pool" in kinds
    assert "candidate_checkpoint" in kinds
    # 候选处于待审，等待人工审核门①
    cand = next(store.read_product(f) for f in files if store.read_product(f).type == "candidate_checkpoint")
    assert cand.status == "pending_review"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_stubs.py -v`
Expected: FAIL（`ModuleNotFoundError: simulator.stubs`）

- [ ] **Step 3: 实现 stub 与执行器**

`src/simulator/stubs.py`:
```python
from __future__ import annotations

from controlplane.envelope import Envelope, OwnerRef, BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir

_NOW = "2026-09-26T00:00:00Z"


def social_simulator_stub(run_id: str, target_dimension: str) -> Envelope:
    """SocialSimulator 批量模块 stub：假装生成了一个情境池。"""
    return Envelope(
        id=f"scen-{run_id}",
        type="scenario_pool",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=1,
        links=[run_id],
        payload={"target_dimension": target_dimension, "episode_count": 0},
    )


def mind_trainer_stub(run_id: str, scenario_id: str) -> Envelope:
    """MindTrainer stub：假装训练出一个候选 checkpoint，置为待审。"""
    ckpt = BigRef(
        uri=f"file:///compute/checkpoints/{run_id}.safetensors",
        sha256="0" * 64,
        size_bytes=0,
        fetch_hint=None,
    )
    return Envelope(
        id=f"cand-{run_id}",
        type="candidate_checkpoint",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="pending_review",
        schema_version=1,
        links=[run_id, scenario_id],
        payload={"checkpoint_ref": ckpt.to_dict(), "train_curve_summary": {"final_loss": 0.0}},
    )


def execute_dispatch(store: GitStore, run_id: str, target_dimension: str) -> None:
    """动作执行器：本骨架用 stub 顶替计算层。真实实现会经 SSH 下发到 GPU。"""
    scenario = social_simulator_stub(run_id, target_dimension)
    store.write_product(
        f"{run_dir(run_id)}/scenario_pool.yaml", scenario, f"stub: scenario for {run_id}"
    )
    candidate = mind_trainer_stub(run_id, scenario.id)
    store.write_product(
        f"{run_dir(run_id)}/candidate_checkpoint.yaml",
        candidate,
        f"stub: candidate checkpoint for {run_id}",
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_stubs.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/simulator/stubs.py tests/test_stubs.py
git commit -m "feat: subsystem stubs + dispatch action executor"
```

---

### Task 9: 调和守护进程（一次性 + 轮询两种模式）

**Files:**
- Create: `src/controlplane/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `GitStore`（Task 6）、`reconcile`/`TreeState`/`DirectiveView`/`CandidateView`/`Action`（Task 7）、`execute_dispatch`（Task 8）、`states.assert_transition`（Task 5）、`paths`（Task 5）、`Envelope`/`OwnerRef`（Task 2）
- Produces:
  - `read_tree_state(store: GitStore) -> TreeState`（扫 git 树 → 视图）
  - `apply_action(store: GitStore, action: Action) -> None`（执行单个动作：dispatch → stub；promote → 写 model + outcome，推进 candidate 到 effective）
  - `reconcile_once(store: GitStore) -> list[Action]`（读状态 → reconcile → 执行非人工动作 → 返回本轮动作）
  - `run_daemon(store: GitStore, interval_s: float, max_ticks: int | None = None) -> None`（轮询循环，`max_ticks` 供测试限次）

- [ ] **Step 1: 写失败测试**

`tests/test_daemon.py`:
```python
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir, model_file
from controlplane.daemon import read_tree_state, reconcile_once


def _write_directive(store: GitStore, run_id: str) -> None:
    env = Envelope(
        id=run_id,
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def test_read_tree_state_sees_pending_directive(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    state = read_tree_state(store)
    assert any(d.id == "run-001" and d.status == "pending" for d in state.directives)


def test_reconcile_once_dispatches_then_awaits(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    # 第一轮：下发 → stub 产出 scenario + candidate(pending_review)，directive 置 executed
    reconcile_once(store)
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "pending_review"
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "executed"
    # 第二轮：候选待审，等待人工，不推进
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["await_review"]


def test_reconcile_once_promotes_after_approval(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    reconcile_once(store)
    # 模拟人工审核门①批准（CLI 做的事：flip 状态 + commit）
    cand = store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml")
    cand.status = "approved"
    store.write_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml", cand, "human: approve")
    # 下一轮调和：登记模型 + 写 outcome + 候选置 effective
    reconcile_once(store)
    assert store.exists(model_file(1))
    assert store.read_product(f"{run_dir('run-001')}/outcome.yaml").status == "effective"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "effective"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_daemon.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.daemon`）

- [ ] **Step 3: 实现 daemon**

`src/controlplane/daemon.py`:
```python
from __future__ import annotations

import time

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, run_dir, model_file
from controlplane.reconcile import (
    DirectiveView,
    CandidateView,
    TreeState,
    Action,
    reconcile,
)
from controlplane.states import assert_transition
from simulator.stubs import execute_dispatch

_NOW = "2026-09-26T00:00:00Z"


def _iter_run_ids(store: GitStore) -> list[str]:
    runs_dir = store.repo_root / CONTROL_PLANE_ROOT / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if p.is_dir())


def read_tree_state(store: GitStore) -> TreeState:
    """扫 git 树，投影成 reconcile() 需要的纯视图。"""
    directives: list[DirectiveView] = []
    candidates: list[CandidateView] = []
    for run_id in _iter_run_ids(store):
        d_path = f"{run_dir(run_id)}/directive.yaml"
        if store.exists(d_path):
            d = store.read_product(d_path)
            directives.append(
                DirectiveView(
                    id=d.id,
                    status=d.status,
                    target_dimension=d.payload.get("target_dimension", ""),
                    recipe_ref=d.payload.get("recipe_ref", ""),
                )
            )
        c_path = f"{run_dir(run_id)}/candidate_checkpoint.yaml"
        if store.exists(c_path):
            c = store.read_product(c_path)
            candidates.append(CandidateView(id=c.id, status=c.status, run_id=run_id))
    model_count = len(store.list_products(f"{CONTROL_PLANE_ROOT}/models"))
    return TreeState(directives=directives, candidates=candidates, model_count=model_count)


def apply_action(store: GitStore, action: Action) -> None:
    if action.kind == "dispatch_directive":
        run_id = action.target_id
        execute_dispatch(store, run_id, "tom")
        directive = store.read_product(f"{run_dir(run_id)}/directive.yaml")
        assert_transition("nonreview", directive.status, "executed")
        directive.status = "executed"
        store.write_product(
            f"{run_dir(run_id)}/directive.yaml", directive, f"reconcile: dispatched {run_id}"
        )
    elif action.kind == "promote_candidate":
        run_id = action.detail["run_id"]
        version = action.detail["model_version"]
        # 1) 登记 ZenGen 版本产物（仅引用）
        model = Envelope(
            id=f"zengen-v{version}",
            type="zengen_model",
            version=1,
            created_at=_NOW,
            created_by=OwnerRef(),
            status="effective",
            schema_version=1,
            links=[action.target_id, run_id],
            payload={"promoted_from": action.target_id},
        )
        store.write_product(model_file(version), model, f"reconcile: promote zengen-v{version}")
        # 2) 候选 approved → effective
        cand = store.read_product(f"{run_dir(run_id)}/candidate_checkpoint.yaml")
        assert_transition("review", cand.status, "effective")
        cand.status = "effective"
        store.write_product(
            f"{run_dir(run_id)}/candidate_checkpoint.yaml", cand, f"reconcile: {run_id} effective"
        )
        # 3) 写本轮结果摘要，供下一轮决策读取
        outcome = Envelope(
            id=f"outcome-{run_id}",
            type="outcome",
            version=1,
            created_at=_NOW,
            created_by=OwnerRef(),
            status="effective",
            schema_version=1,
            links=[action.target_id, f"zengen-v{version}"],
            payload={"promoted_model": f"zengen-v{version}"},
        )
        store.write_product(f"{run_dir(run_id)}/outcome.yaml", outcome, f"reconcile: outcome {run_id}")
    # await_review / noop：无自动动作，等待人工


def reconcile_once(store: GitStore) -> list[Action]:
    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。"""
    state = read_tree_state(store)
    actions = reconcile(state)
    for action in actions:
        apply_action(store, action)
    return actions


def run_daemon(store: GitStore, interval_s: float, max_ticks: int | None = None) -> None:
    """轮询守护进程（spec 补充 B.1：轮询而非 inotify）。max_ticks 供测试限次。"""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        reconcile_once(store)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(interval_s)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_daemon.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/daemon.py tests/test_daemon.py
git commit -m "feat: reconciliation daemon (one-shot + polling) with action executor"
```

---

### Task 10: 审核门 CLI（TypeScript：review/approve/reject/status）

**Files:**
- Create: `cli/src/gittree.ts`
- Create: `cli/src/commands.ts`
- Modify: `cli/src/index.ts`（加命令分发）
- Test: `cli/test/commands.test.ts`

**Interfaces:**
- Consumes: git 树里的 YAML 产物（Python 侧 `GitStore` 写出）、`schemas/envelope.schema.json`
- Produces:
  - `gittree.ts`: `readProduct(repoRoot, relPath): Envelope`；`writeProduct(repoRoot, relPath, env, message): void`（js-yaml + git CLI commit）；`Envelope` interface
  - `commands.ts`: `review(repoRoot, id): string`；`approve(repoRoot, id): void`；`reject(repoRoot, id, reason): void`；`status(repoRoot): string`
  - `index.ts`: `main(argv): void` 分发 `review|approve|reject|status`

- [ ] **Step 1: 写失败测试**

`cli/test/commands.test.ts`:
```typescript
import { describe, it, expect, beforeEach } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { dump } from "js-yaml";
import { approve, reject, review, status } from "../src/commands.js";
import { readProduct } from "../src/gittree.js";

function initRepo(): string {
  const root = mkdtempSync(join(tmpdir(), "cp-"));
  execFileSync("git", ["-C", root, "init"]);
  execFileSync("git", ["-C", root, "config", "user.email", "t@e.com"]);
  execFileSync("git", ["-C", root, "config", "user.name", "t"]);
  return root;
}

function writeCandidate(root: string, id: string, status: string): string {
  const rel = `control-plane/runs/${id}/candidate_checkpoint.yaml`;
  const dir = join(root, "control-plane", "runs", id);
  mkdirSync(dir, { recursive: true });
  const env = {
    id: `cand-${id}`,
    type: "candidate_checkpoint",
    version: 1,
    created_at: "2026-09-26T00:00:00Z",
    created_by: { owner: "default", visibility: "shared" },
    status,
    schema_version: 1,
    links: [id],
    payload: {},
  };
  writeFileSync(join(root, rel), dump(env));
  execFileSync("git", ["-C", root, "add", "."]);
  execFileSync("git", ["-C", root, "commit", "-m", "seed"]);
  return rel;
}

describe("gate cli", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("approve flips pending_review to approved and commits", () => {
    const rel = writeCandidate(root, "run-001", "pending_review");
    approve(root, "cand-run-001");
    expect(readProduct(root, rel).status).toBe("approved");
    const log = execFileSync("git", ["-C", root, "log", "--oneline"], { encoding: "utf-8" });
    expect(log).toContain("approve cand-run-001");
  });

  it("reject flips pending_review to rejected preserving reason", () => {
    const rel = writeCandidate(root, "run-001", "pending_review");
    reject(root, "cand-run-001", "reward hacking");
    const env = readProduct(root, rel);
    expect(env.status).toBe("rejected");
    expect(env.payload.reject_reason).toBe("reward hacking");
  });

  it("approve refuses illegal transition from effective", () => {
    writeCandidate(root, "run-001", "effective");
    expect(() => approve(root, "cand-run-001")).toThrow(/not allowed/);
  });

  it("review prints candidate summary", () => {
    writeCandidate(root, "run-001", "pending_review");
    expect(review(root, "cand-run-001")).toContain("pending_review");
  });

  it("status lists candidates awaiting review", () => {
    writeCandidate(root, "run-001", "pending_review");
    expect(status(root)).toContain("cand-run-001");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd cli && npm test`
Expected: FAIL（`Cannot find module ../src/commands.js`）

- [ ] **Step 3: 实现 gittree.ts**

`cli/src/gittree.ts`:
```typescript
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { load, dump } from "js-yaml";

export interface OwnerRef {
  owner: string;
  visibility: string;
}

export interface Envelope {
  id: string;
  type: string;
  version: number;
  created_at: string;
  created_by: OwnerRef;
  status: string;
  schema_version: number;
  links: string[];
  payload: Record<string, unknown>;
}

export function readProduct(repoRoot: string, relPath: string): Envelope {
  const raw = readFileSync(join(repoRoot, relPath), "utf-8");
  return load(raw) as Envelope;
}

export function writeProduct(
  repoRoot: string,
  relPath: string,
  env: Envelope,
  message: string,
): void {
  writeFileSync(join(repoRoot, relPath), dump(env, { sortKeys: false }));
  execFileSync("git", ["-C", repoRoot, "add", relPath]);
  execFileSync("git", ["-C", repoRoot, "commit", "-m", message]);
}
```

- [ ] **Step 4: 实现 commands.ts**

`cli/src/commands.ts`:
```typescript
import { execFileSync } from "node:child_process";
import { readProduct, writeProduct, Envelope } from "./gittree.js";

// spec §1.3 需审核类合法迁移，与 Python controlplane/states.py 保持一致
const REVIEW_TRANSITIONS: Record<string, string[]> = {
  draft: ["pending_review"],
  pending_review: ["approved", "rejected"],
  approved: ["effective"],
  effective: ["deprecated", "superseded"],
  rejected: [],
  superseded: [],
  deprecated: [],
};

function assertTransition(from: string, to: string): void {
  if (!(REVIEW_TRANSITIONS[from] ?? []).includes(to)) {
    throw new Error(`review: ${from} -> ${to} not allowed`);
  }
}

function findCandidatePath(repoRoot: string, id: string): string {
  // 用 git ls-files 定位含该 id 的候选产物，避免绑定语言 fs 遍历库
  const listed = execFileSync(
    "git",
    ["-C", repoRoot, "ls-files", "control-plane/runs/*/candidate_checkpoint.yaml"],
    { encoding: "utf-8" },
  )
    .split("\n")
    .filter((line) => line.length > 0);
  for (const rel of listed) {
    if (readProduct(repoRoot, rel).id === id) {
      return rel;
    }
  }
  throw new Error(`candidate ${id} not found`);
}

export function review(repoRoot: string, id: string): string {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  return `${env.id} [${env.status}] type=${env.type} links=${env.links.join(",")}`;
}

export function approve(repoRoot: string, id: string): void {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  assertTransition(env.status, "approved");
  env.status = "approved";
  writeProduct(repoRoot, rel, env, `approve ${id}`);
}

export function reject(repoRoot: string, id: string, reason: string): void {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  assertTransition(env.status, "rejected");
  env.status = "rejected";
  env.payload.reject_reason = reason;
  writeProduct(repoRoot, rel, env, `reject ${id}`);
}

export function status(repoRoot: string): string {
  const listed = execFileSync(
    "git",
    ["-C", repoRoot, "ls-files", "control-plane/runs/*/candidate_checkpoint.yaml"],
    { encoding: "utf-8" },
  )
    .split("\n")
    .filter((line) => line.length > 0);
  const lines = listed
    .map((rel) => readProduct(repoRoot, rel))
    .filter((env) => env.status === "pending_review")
    .map((env) => `${env.id} [${env.status}]`);
  return lines.length > 0 ? lines.join("\n") : "no candidates awaiting review";
}
```

- [ ] **Step 5: 更新 index.ts 分发**

`cli/src/index.ts`:
```typescript
import { review, approve, reject, status } from "./commands.js";

export const CLI_NAME = "rise";

export function main(argv: string[]): void {
  const [cmd, ...rest] = argv;
  const repoRoot = process.cwd();
  switch (cmd) {
    case "review":
      console.log(review(repoRoot, rest[0]));
      break;
    case "approve":
      approve(repoRoot, rest[0]);
      console.log(`approved ${rest[0]}`);
      break;
    case "reject":
      reject(repoRoot, rest[0], rest.slice(1).join(" ") || "no reason");
      console.log(`rejected ${rest[0]}`);
      break;
    case "status":
      console.log(status(repoRoot));
      break;
    default:
      console.error(`usage: ${CLI_NAME} <review|approve|reject|status> [id] [reason]`);
      process.exitCode = 1;
  }
}

if (process.argv[1] && process.argv[1].endsWith("index.js")) {
  main(process.argv.slice(2));
}
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd cli && npm test`
Expected: PASS（smoke 1 + commands 5 = 6 passed）

- [ ] **Step 7: 提交**

```bash
git add cli/src/gittree.ts cli/src/commands.ts cli/src/index.ts cli/test/commands.test.ts
git commit -m "feat: review gate cli (review/approve/reject/status) in typescript"
```

---

### Task 11: 端到端假闭环集成测试

**Files:**
- Create: `tests/test_e2e_loop.py`

**Interfaces:**
- Consumes: `GitStore`（Task 6）、`reconcile_once`（Task 9）、`Envelope`/`OwnerRef`（Task 2）、`paths`（Task 5）
- Produces: 无（验收测试，锁定完整编排闭环）

- [ ] **Step 1: 写端到端测试**

说明：审核门批准这一步，人工路径是 TS CLI（Task 10）。为让集成测试保持 hermetic（不依赖 node 是否安装），此处用 Python `GitStore` 执行与 CLI 等价的"flip 状态 + commit"——CLI 与本测试都只是翻状态位并提交，二者行为一致，TS 侧行为已由 Task 10 的 vitest 单独覆盖。

`tests/test_e2e_loop.py`:
```python
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir, model_file
from controlplane.daemon import reconcile_once


def _write_directive(store: GitStore, run_id: str, dimension: str) -> None:
    env = Envelope(
        id=run_id,
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
        payload={"target_dimension": dimension, "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _approve_like_cli(store: GitStore, run_id: str) -> None:
    cand = store.read_product(f"{run_dir(run_id)}/candidate_checkpoint.yaml")
    assert cand.status == "pending_review"
    cand.status = "approved"
    store.write_product(f"{run_dir(run_id)}/candidate_checkpoint.yaml", cand, "human: approve cand")


def test_full_fake_rsi_loop(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()

    # 1. 人工写训练指令（决策先人工写死，spec §7）
    _write_directive(store, "run-001", "high_order_belief")

    # 2. 调和 tick 1：下发 → stub 产出 scenario + candidate(pending_review)，directive→executed
    reconcile_once(store)
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir('run-001')}/scenario_pool.yaml").status == "executed"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "pending_review"

    # 3. 调和 tick 2：候选待审，仅 await_review，不越过审核门自动晋级
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["await_review"]
    assert not store.exists(model_file(1))  # 未经批准不得晋级

    # 4. 人工审核门①批准（等价 CLI approve）
    _approve_like_cli(store, "run-001")

    # 5. 调和 tick 3：晋级 → 登记 zengen-v1 + 写 outcome + 候选→effective
    reconcile_once(store)
    assert store.exists(model_file(1))
    model = store.read_product(model_file(1))
    assert model.id == "zengen-v1"
    assert model.type == "zengen_model"
    outcome = store.read_product(f"{run_dir('run-001')}/outcome.yaml")
    assert outcome.payload["promoted_model"] == "zengen-v1"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "effective"

    # 6. 调和 tick 4：闭环稳定，无剩余动作
    assert reconcile_once(store) == []


def test_rejected_candidate_never_promoted(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-002", "emotion")
    reconcile_once(store)
    # 人工拒绝（等价 CLI reject）
    cand = store.read_product(f"{run_dir('run-002')}/candidate_checkpoint.yaml")
    cand.status = "rejected"
    cand.payload["reject_reason"] = "reward hacking suspected"
    store.write_product(f"{run_dir('run-002')}/candidate_checkpoint.yaml", cand, "human: reject")
    # 调和：被拒候选不产生任何晋级动作，记录保留
    assert reconcile_once(store) == []
    assert not store.exists(model_file(1))
    assert store.read_product(f"{run_dir('run-002')}/candidate_checkpoint.yaml").status == "rejected"
```

- [ ] **Step 2: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_e2e_loop.py -v`
Expected: PASS（2 passed）

- [ ] **Step 3: 运行全部 Python 测试确认无回归**

Run: `uv run --extra dev pytest -v`
Expected: PASS（全部任务的测试通过）

- [ ] **Step 4: 提交**

```bash
git add tests/test_e2e_loop.py
git commit -m "test: end-to-end fake RSI orchestration loop"
```

---

### Task 12: 配置加载、凭证边界与日志脱敏（安全关键）

**Files:**
- Create: `src/controlplane/config.py`
- Modify: `.gitignore`（追加凭证文件排除规则）
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `GitStore`（Task 6）、`paths.backends_config_file()`（Task 5）
- Produces:
  - `BackendConfig(backend: str, invocation_form: str, model_name: str, prompt_template_version: str, token_env: str, retry_policy: dict)` dataclass
  - `MissingCredentialError(Exception)`
  - `CredentialInGitTreeError(Exception)`
  - `load_backend_config(raw: dict) -> BackendConfig`（拒绝含凭证值的配置，抛 `CredentialInGitTreeError`）
  - `resolve_credential(cfg: BackendConfig, env: dict[str, str]) -> str`（按变量名从环境解析，缺失抛 `MissingCredentialError`）
  - `redact(text: str, secrets: list[str]) -> str`（日志脱敏，把秘密值替换为 `***REDACTED***`）

**背景（spec 补充 G.3）**：系统每次状态变更都 `git commit`，凭证写进 git 树里的配置就会进入 git 历史，事后清除代价极高。因此配置只存变量名，值从环境解析；L3 日志记录外部 CLI 完整请求/响应时必须脱敏。

- [ ] **Step 1: 写失败测试**

`tests/test_config.py`:
```python
import pytest

from controlplane.config import (
    BackendConfig,
    load_backend_config,
    resolve_credential,
    redact,
    MissingCredentialError,
    CredentialInGitTreeError,
)


def _raw() -> dict:
    return {
        "backend": "claude_cli",
        "invocation_form": "cli",
        "model_name": "claude-sonnet-4",
        "prompt_template_version": "v1",
        "token_env": "ANTHROPIC_API_KEY",
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 2},
    }


def test_load_backend_config_keeps_only_var_name():
    cfg = load_backend_config(_raw())
    assert cfg.token_env == "ANTHROPIC_API_KEY"
    # 配置对象里没有任何存放凭证值的字段
    assert not hasattr(cfg, "token")


def test_config_with_literal_token_is_rejected():
    raw = _raw()
    raw["token"] = "sk-ant-secret"
    with pytest.raises(CredentialInGitTreeError):
        load_backend_config(raw)


def test_config_with_api_key_field_is_rejected():
    raw = _raw()
    raw["api_key"] = "sk-ant-secret"
    with pytest.raises(CredentialInGitTreeError):
        load_backend_config(raw)


def test_resolve_credential_reads_env():
    cfg = load_backend_config(_raw())
    assert resolve_credential(cfg, {"ANTHROPIC_API_KEY": "sk-live"}) == "sk-live"


def test_missing_credential_raises_with_var_name():
    cfg = load_backend_config(_raw())
    with pytest.raises(MissingCredentialError) as exc:
        resolve_credential(cfg, {})
    # 错误必须指明缺哪个环境变量，不得静默降级
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_empty_credential_also_raises():
    cfg = load_backend_config(_raw())
    with pytest.raises(MissingCredentialError):
        resolve_credential(cfg, {"ANTHROPIC_API_KEY": ""})


def test_redact_removes_secret_from_log():
    log = "calling api with token sk-live-123 and retry"
    out = redact(log, ["sk-live-123"])
    assert "sk-live-123" not in out
    assert "***REDACTED***" in out


def test_redact_ignores_empty_secrets():
    log = "no secrets here"
    assert redact(log, ["", None]) == log
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.config`）

- [ ] **Step 3: 实现配置与凭证边界**

`src/controlplane/config.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

# 出现在 git 树配置里即视为凭证泄漏的字段名（spec 补充 G.3）
_FORBIDDEN_SECRET_KEYS = {"token", "api_key", "apikey", "password", "secret", "private_key"}

_REDACTED = "***REDACTED***"


class MissingCredentialError(Exception):
    """按变量名解析凭证失败：环境变量缺失或为空。"""


class CredentialInGitTreeError(Exception):
    """git 树配置里出现了凭证值本身，而非变量名引用。"""


@dataclass(frozen=True)
class BackendConfig:
    """外部推理后端配置（L-A 层，进 git 树）。只存凭证变量名，不存值。"""

    backend: str
    invocation_form: str  # "cli" | "api"
    model_name: str
    prompt_template_version: str
    token_env: str  # 环境变量名，如 "ANTHROPIC_API_KEY"
    retry_policy: dict


def load_backend_config(raw: dict) -> BackendConfig:
    leaked = _FORBIDDEN_SECRET_KEYS & set(raw.keys())
    if leaked:
        raise CredentialInGitTreeError(
            f"credential value fields found in git-tree config: {sorted(leaked)}; "
            "store only the env var name via 'token_env'"
        )
    return BackendConfig(
        backend=raw["backend"],
        invocation_form=raw["invocation_form"],
        model_name=raw["model_name"],
        prompt_template_version=raw["prompt_template_version"],
        token_env=raw["token_env"],
        retry_policy=raw.get("retry_policy", {}),
    )


def resolve_credential(cfg: BackendConfig, env: dict[str, str]) -> str:
    """按变量名从环境解析凭证。缺失/为空即明确失败，禁止静默降级。"""
    value = env.get(cfg.token_env)
    if not value:
        raise MissingCredentialError(
            f"environment variable {cfg.token_env} is not set or empty; "
            f"required by backend {cfg.backend}"
        )
    return value


def redact(text: str, secrets: list[str | None]) -> str:
    """日志脱敏：把秘密值替换掉。L3 记录外部 CLI 请求/响应前必须过它。"""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, _REDACTED)
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_config.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 追加 .gitignore 凭证排除规则**

在 `.gitignore` 末尾追加（保留已有的 `.DS_Store` 行）：
```gitignore

# 凭证绝不进 git 树（spec 补充 G.3）
.env
.env.*
*.local.yaml
secrets/

# 派生产物
control-plane/index.sqlite
```

- [ ] **Step 6: 验证凭证文件确实被忽略**

Run: `printf 'ANTHROPIC_API_KEY=sk-test\n' > .env && git check-ignore -v .env && rm .env`
Expected: 输出含 `.gitignore` 行号与 `.env`，证明被忽略

- [ ] **Step 7: 提交**

```bash
git add src/controlplane/config.py tests/test_config.py .gitignore
git commit -m "feat: config loading with credential boundary + log redaction"
```

---

### Task 13: 可观测只读命令（trace / cost）

**Files:**
- Create: `src/controlplane/observe.py`
- Test: `tests/test_observe.py`

**Interfaces:**
- Consumes: `GitStore`（Task 6）、`paths.run_dir`（Task 5）、`Envelope`/`OwnerRef`/`BigRef`（Task 2）
- Produces:
  - `trace(store: GitStore, run_id: str) -> str`（打印该轮各产物状态 + `links` 溯源 + L2/L3 引用清单）
  - `cost(store: GitStore, run_id: str) -> str`（打印本轮成本明细与汇总）
  - `sum_costs(records: list[dict]) -> dict`（纯函数：汇总 GPU·时与 token 数）

**设计约束（spec 补充 G.4）**：只做"定位 + 呈现"，不重新计算、不聚合出新真相。引用指向的计算层文件不可达时，须明确报告"引用存在但文件不可达"及其 `uri`，不得假装没有数据。

- [ ] **Step 1: 写失败测试**

`tests/test_observe.py`:
```python
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir
from controlplane.observe import trace, cost, sum_costs


def _seed_run(store: GitStore, run_id: str) -> None:
    directive = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", directive, "seed directive")
    run_trace = Envelope(
        id=f"trace-{run_id}", type="run_trace", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        links=[run_id],
        payload={
            "wall_clock_s": 120,
            "log_refs": [{"uri": "file:///compute/logs/run-001/task-1.log", "sha256": "a" * 64, "size_bytes": 10, "fetch_hint": None}],
            "cost_records": [
                {"run_id": run_id, "task_id": "task-1", "cost_type": "gpu", "gpu_hours": 1.5, "tokens": 0},
                {"run_id": run_id, "task_id": "task-2", "cost_type": "external_inference", "gpu_hours": 0.0, "tokens": 3200},
            ],
        },
    )
    store.write_product(f"{run_dir(run_id)}/run_trace.yaml", run_trace, "seed run trace")


def test_sum_costs_is_pure_aggregation():
    records = [
        {"cost_type": "gpu", "gpu_hours": 1.5, "tokens": 0},
        {"cost_type": "gpu", "gpu_hours": 0.5, "tokens": 0},
        {"cost_type": "external_inference", "gpu_hours": 0.0, "tokens": 3200},
    ]
    assert sum_costs(records) == {"gpu_hours": 2.0, "tokens": 3200}


def test_trace_lists_products_and_links(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _seed_run(store, "run-001")
    out = trace(store, "run-001")
    assert "directive" in out
    assert "executed" in out
    assert "run_trace" in out


def test_trace_reports_unreachable_reference(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _seed_run(store, "run-001")
    out = trace(store, "run-001")
    # 引用指向计算层文件，本地不可达时必须明说，而非静默省略
    assert "unreachable" in out
    assert "file:///compute/logs/run-001/task-1.log" in out


def test_cost_prints_detail_and_summary(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _seed_run(store, "run-001")
    out = cost(store, "run-001")
    assert "task-1" in out
    assert "1.5" in out
    assert "3200" in out
    assert "gpu_hours=2.0" in out


def test_cost_on_run_without_trace(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    out = cost(store, "run-404")
    assert "no run_trace" in out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --extra dev pytest tests/test_observe.py -v`
Expected: FAIL（`ModuleNotFoundError: controlplane.observe`）

- [ ] **Step 3: 实现只读呈现**

`src/controlplane/observe.py`:
```python
from __future__ import annotations

from urllib.parse import urlparse
from pathlib import Path

from controlplane.gitstore import GitStore
from controlplane.paths import run_dir


def sum_costs(records: list[dict]) -> dict:
    """纯函数汇总。只加总物理量，不换算金额（单价表可选，见 spec D.4）。"""
    return {
        "gpu_hours": sum(r.get("gpu_hours", 0.0) for r in records),
        "tokens": sum(r.get("tokens", 0) for r in records),
    }


def _reachable(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return False
    return Path(parsed.path).is_file()


def trace(store: GitStore, run_id: str) -> str:
    """打印该轮全链路：各产物状态 + links 溯源 + L2/L3 引用可达性。只读。"""
    lines: list[str] = [f"run {run_id}"]
    products = store.list_products(run_dir(run_id))
    if not products:
        return f"run {run_id}: no products found"
    for rel in products:
        env = store.read_product(rel)
        links = ",".join(env.links) if env.links else "-"
        lines.append(f"  {env.type:<22} [{env.status}] id={env.id} links={links}")
        for ref in env.payload.get("log_refs", []):
            uri = ref["uri"]
            mark = "ok" if _reachable(uri) else "unreachable"
            lines.append(f"      log_ref [{mark}] {uri}")
        for ref in env.payload.get("metric_stream_refs", []):
            uri = ref["uri"]
            mark = "ok" if _reachable(uri) else "unreachable"
            lines.append(f"      metric_ref [{mark}] {uri}")
    return "\n".join(lines)


def cost(store: GitStore, run_id: str) -> str:
    """打印本轮成本明细与汇总。只读，不重新计算真相。"""
    rel = f"{run_dir(run_id)}/run_trace.yaml"
    if not store.exists(rel):
        return f"run {run_id}: no run_trace (nothing recorded yet)"
    env = store.read_product(rel)
    records = env.payload.get("cost_records", [])
    lines = [f"run {run_id} cost detail"]
    for r in records:
        lines.append(
            f"  {r.get('task_id', '-'):<10} {r.get('cost_type', '-'):<20} "
            f"gpu_hours={r.get('gpu_hours', 0.0)} tokens={r.get('tokens', 0)}"
        )
    total = sum_costs(records)
    lines.append(f"  total: gpu_hours={total['gpu_hours']} tokens={total['tokens']}")
    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_observe.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 运行全部 Python 测试确认无回归**

Run: `uv run --extra dev pytest -v`
Expected: PASS（全部通过）

- [ ] **Step 6: 提交**

```bash
git add src/controlplane/observe.py tests/test_observe.py
git commit -m "feat: read-only observability commands (trace/cost)"
```

---

## Self-Review

**1. Spec coverage（对照 spec §1 ControlPlane + §7 测试策略 + 补充 B/F）**

| Spec 要求 | 实现任务 |
|---|---|
| §1.1 git 树唯一真相 + 大体量引用 | Task 6 GitStore；Task 2 BigRef |
| §1.2 目录结构 `runs/recipes/.../models/compute` | Task 5 paths；Task 6 init 建目录 |
| §1.3 两类产物状态机 | Task 5 states |
| §1.4 调和循环（发现 pending→下发、待审→等人工、完成→同步回写） | Task 7 reconcile + Task 9 daemon |
| §1.5 审核门①统一交互（review/approve/reject + commit） | Task 10 CLI |
| §1.6 产物通用信封 | Task 2 Envelope |
| §1.7 reconcile 纯函数可 fixture 测试 | Task 7（`test_reconcile_is_pure_no_mutation`） |
| §7 工厂模拟器优先（stub 替身验证编排） | Task 8 stubs + Task 11 e2e |
| §7 决策先人工写死 | Task 11（人工写 directive） |
| 补充 B.1 轮询非 inotify | Task 9 run_daemon（含 constraint 注释） |
| 补充 B.2 git CLI 非语言库 | Task 6（subprocess）；Task 10（execFileSync git） |
| 补充 F.5 税①归属维度 | Task 2 OwnerRef |
| 补充 F.5 税② schema_version | Task 2 Envelope |
| 补充 F.5 税③ fetch_hint | Task 2 BigRef |
| 补充 F.5 税④ 迁移器链读取路径 | Task 4 load_product + Task 6 read_product 调用它 |
| 补充 G.1 V1 纯 CLI 无界面 | Task 10 CLI（全局约束已声明不做 GUI） |
| 补充 G.2 配置分层（L-A 进 git `config/`） | Task 5 `backends_config_file()` + Task 12 `BackendConfig` |
| 补充 G.3 凭证不进 git 树 + 脱敏 | Task 12（`CredentialInGitTreeError`、`resolve_credential`、`redact`、`.gitignore`） |
| 补充 G.4 可观测只读命令 | Task 13 `trace`/`cost`（含"引用不可达"分支） |

**明确不在本计划范围（后续计划）**：SQLite 索引（RecipeStore 计划）、指令确认门 `confirm`（TrainScheduler 计划）、审核门②③（RecipeStore/MindAgent 计划）、真实 SSH 传输与计算层、SocialSimulator/MindTrainer/MindAgent/RecipeStore/TrainScheduler/TrainPlanner 真实实现、诊断评测、gRPC 实时环境服务、`rise logs`（需真实计算层日志才有意义，本计划只实现 `trace`/`cost`，`logs` 留到接入真实 SSH 的计划）、外部推理桥接的实际调用（Task 12 只建配置与凭证边界，真实调用在 TrainPlanner 计划）。这些在本计划里以 stub 或"不实现"占位，符合 §7"逐步替换"策略。

**2. Placeholder scan**：无 TBD/TODO；每个 code 步骤含完整可运行代码；无"类似 Task N"引用（各任务代码自包含）；错误处理为具体实现（`assert_transition` 抛具体异常、CLI `assertTransition` 抛 `not allowed`）。

**3. Type consistency**：
- `Envelope` 字段（id/type/version/created_at/created_by/status/schema_version/links/payload）在 Task 2 定义，Task 6/8/9/11 一致使用；TS `Envelope` interface（Task 10）字段名与之对齐。
- 状态机迁移表在 Python（Task 5 `REVIEW_TRANSITIONS`）与 TS（Task 10 同名表）两处保持同一份内容——这是双语言的已知重复，靠共享 spec §1.3 约束一致性（未来可考虑从 schema 生成，V1 接受重复）。
- `reconcile()` 产出的 `Action.kind`（`dispatch_directive`/`await_review`/`promote_candidate`）在 Task 7 定义、Task 9 `apply_action` 全部消费，无遗漏分支（`noop` 未产生故 daemon 不需处理，`await_review` 显式落在"无自动动作"注释）。
- `promote_candidate` 的 `detail={"run_id", "model_version"}` 在 Task 7 产出、Task 9 消费，键名一致。

发现并修正：Task 9 `apply_action` 对 `await_review`/`noop` 无分支——已在实现末尾注释说明这是有意的"无自动动作"，非遗漏。类型与签名一致，无需返工。

---

## Execution Handoff

计划已保存至 `docs/superpowers/plans/2026-09-26-controlplane-factory-simulator.md`。这是 Rise 计划序列的第 1 个（ControlPlane + 工厂模拟器骨架），后续计划：SocialSimulator → MindTrainer → RecipeStore+TrainScheduler → MindAgent+Actio → TrainPlanner → InteractTrain 闭环。
