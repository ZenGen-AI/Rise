# 智能体记忆(Starling 接入)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把计划 A 里只用替身跑通的记忆换成 Starling:先在 Starling 仓库补齐本仓库需要的公开接口并经 PR 合入,再在本仓库接上适配器、树外记忆配置与训练路副本,最后把设计文档的状态与措辞收尾。

**Architecture:** Starling 一侧按它自己的约定把状态机与算法写进 C++、经 pybind 暴露,门面只做转发(三段写入的事件表、逻辑摘要、备份、只嵌入不维护、召回放宽到 volatile 与降级即抛)。本仓库一侧,`actio/memory_starling.py` 是唯一导入 `starling` 的模块,`StarlingMemory` 与 `StarlingDeploymentStore` 过计划 A 的同一套契约测试;`factory/memory_config.py` 读树外配置并在装配期拒绝不合法的环境;未设 `RISMFORGE_MEMORY_CONFIG` 时一切照计划 A 的替身跑。

**Tech Stack:** Python 3.11+、pytest;Starling 侧另有 C++17、CMake ≥3.27、Ninja、pybind11、GoogleTest、SQLite ≥3.46。CMake 与 Ninja 是 pip 轮子(Starling 的 `requirements-build.txt`),不装系统包;系统只需要 C++ 编译器(本机 Apple Clang)。本仓库 `uv` 装 `memory` extra 时由 scikit-build-core 在构建隔离里自取 cmake 与 ninja。

**Spec:** `docs/superpowers/specs/2026-10-25-actio-starling-memory-design.md`(已并入 `docs/design/` 的 00、20、30、40、50、90 六篇)。本计划是施工顺序的第 6 到 8 步,前提是 `docs/superpowers/plans/2026-10-27-agent-memory-standin.md`(第 1 到 5 步)已合入。

## Global Constraints

- 本仓库源文件首条语句 `from __future__ import annotations`。
- 本仓库测试命令:`PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest <path> -q -p no:cacheprovider`;需要 Starling 的用例加 `--extra memory`。
- Starling 仓库(`/Users/jaredguo-mini/develop/memory/starling`)的构建与测试:首次 `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`;动了 C++、绑定或迁移后先 `.venv/bin/python scripts/configure_build.py --build --test`,再 `.venv/bin/python scripts/configure_build.py --python-editable`;Python 测试 `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python`;提交门是全量 ctest、`pytest tests/python` 全绿再跑 `python scripts/ci_static_scan.py`。
- Starling 的状态机、算法、编排一律进 C++(它的 `CLAUDE.md`),Python 门面只转发。
- 依赖方向:本仓库只有 `src/actio/memory_starling.py` 导入 `starling`;`simulator`、`mindtrainer`、`mindagent` 不导入 `starling`。
- 本仓库只加可选 extra `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<合并提交>"]`,合并提交在第 6 步 PR 合入后才有。
- 凭证只从环境变量读(`OPENAI_API_KEY`),异常信息只报变量名或异常类名与 `failure_category`,不含端点地址与密钥。
- 真端点用例不进 CI,只在设 `RISMFORGE_MEMORY_LIVE=1` 时手动跑。
- 每个任务先写失败的测试、确认失败、再实现。每个任务提交前跑本任务改动的测试文件与相邻测试(各任务 Step 4 列出的那组);全套 `-m "not slow"`(约 10 分钟)由控制者在每个任务提交后于后台跑、与评审并行,每一步结束时与终审修复波结束时各跑一次必须全绿。全套发现某任务弄红了别处,由该任务的实施者在下一笔提交里修。
- 改 `docs/design` 的任务结束时 `check_docs.py`、`check_consistency.py` 都 0 错误 0 告警,`sync_docs.py --check` 通过;30、40、90 三篇贴着 900 行上限,只改写不净增;共享区块与图块改源头再同步,不手改。
- 推送分支、开 PR 前停下征得用户同意。
- 提交逐个文件暂存,提交信息正文写完即止,不加署名行;两个仓库都一样。

## Review Focus

- **设了记忆配置但没装 Starling**:装配期退出码 2,报安装提示,不建 `.owner`,不能退回替身静默跑。见第 7 步 `memory_config` 与组合根两个任务。
- **记忆根目录已被另一个控制面仓库占用**:两个仓库同时首次使用时只有一个能创建 `.owner`;另一个读回对方路径后以退出码 2 结束,报出两边路径。见第 7 步 `memory_config`。
- **外部嵌入端点同一模型名换了向量**:起点摘要变,续训的 `config_hash` 变、判 T3,不能当成同一实验接着跑。见第 7 步运行时身份任务,升 T3 那一半由计划 A Task 25 钉住。
- **Starling 调用还没返回时删副本**:端口未全关不删目录;`close(timeout=)` 超时后端口仍可再次关闭,副本判 `role_memory_failure`。见第 7 步训练副本与 `StarlingMemory` 两个任务。
- **`remember_event` 在第三段之前崩溃后重调**:按 `engram_ref` 补成 committed,不再抽取一次;engram 不在时用行里的原文重抽。见第 6 步 `remember_event` 与第 7 步部署管理面两个任务。
- **配置写 `max_retries: 0`**:抽取与嵌入都不重试。Starling 的 C++ 默认是 3,门面只在非零时覆盖会让 0 静默变成 3。见第 6 步第一个任务。

---

## 第 6 步:Starling 补公开接口并经 PR 合入(在 Starling 仓库)

本步全部在 Starling 仓库 `/Users/jaredguo-mini/develop/memory/starling` 里做,下文路径都相对该仓库根目录。设计源核对的是 `48e9f3b`;写计划时远端 `main` 已到 `be5ccf8`(多了 #106 到 #110),与本步重叠的只有 #107 改过的 `src/extractor/extractor.cpp`(Task 8)和 #110 改过的 `bindings/python/bind_06_extractor.cpp`(本步不改),迁移仍止于 0037。所以 Task 1 先拉最新 `main` 再建分支,Task 8 动手前重新 `grep -n` 定位行号。

本步的约定(只对本步生效):

- 架构边界:Starling 的 `CLAUDE.md` 规定状态机、算法、管线编排一律进 C++ 内核(`src/` 加 `include/starling/`),Python 层只做绑定转发与签名归一。所以事件表的三段写入、逻辑摘要、备份放进 C++,经 pybind 暴露,门面只转发。`strict`、`embedder=`、`embed_pending`、`close` 的锁只是转发或生命周期,留在 Python。
- 构建与测试(Starling 开发者的标准做法,见其 `README.md`、`CLAUDE.md`、`tests/README.md`):
  - 首次:`python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable`。
  - 动了 C++、绑定或 `migrations/` 之后:先 `.venv/bin/python scripts/configure_build.py --build --test`(配置、构建、全量 ctest;macOS 默认构建目录 `build-macos`),再 `.venv/bin/python scripts/configure_build.py --python-editable` 重装 `_core`。只 `pip install -e .` 不够。纯 Python 改动不用重装。
  - 单个 Python 用例:`PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest <path>::<name> -q -p no:cacheprovider`。
  - 单组 C++ 用例:`.venv/bin/ctest --test-dir build-macos -R <正则> --output-on-failure`。
  - 提交门:全量 ctest 加 `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python -q -p no:cacheprovider` 全绿,外加 `.venv/bin/python scripts/ci_static_scan.py`。
- git:只用显式路径 `git add`,不用 `git add .`、`-A`、`--no-verify`、`--amend`;提交信息 `feat(memory): <中文说明>`,不加 Co-Authored-By 行。
- 测试里用的离线夹具:抽取 LLM 用 `starling.make_stub_llm(default_response=CANNED_JSON)`(`CANNED_JSON` 照抄 `tests/python/test_memory_facade.py` 第 20 到 24 行);会失败的嵌入器用 `make_openai_embedder(model="offline", base_url="unsupported-starling-test://invalid", dim=8, timeout_ms=10)`,先 `monkeypatch.setenv("OPENAI_API_KEY", "test-key")`。不支持的协议是永久错误,不发网络请求(先例 `tests/python/test_openai_embedding_binding.py:14-26`)。会失败的抽取 LLM 用 `starling.make_stub_llm(default_response="")` 之后 `llm.set_default_response("", False, "stub extraction failure")`(本机探针:`outcome="accepted"`、`extraction_failed=True`、`failure_category="transport_failure"`,engram 已写)。
- 测试夹具:`CANNED_JSON`、`_mem`、`_bad_embedder`(Task 2)、`_failing_llm`、`_count`、`KEY`(Task 6)、`_db`、`_fill`、`_sql`(Task 9)在首次出现的任务里定义;后面的新测试文件在文件头照抄自己用到的那几个,不新建共享模块。

### Task 1: 建分支;`Memory.open(embedder=)` 与 `make_openai_embedder`

**Files:**
- Modify: `python/starling/memory.py`(`Memory.__init__` 第 140 行、`Memory.open` 第 151 行;在 `make_openai_llm` 之后新增 `make_openai_embedder`)
- Modify: `python/starling/__init__.py`(导出 `make_openai_embedder`)
- Test: `tests/python/test_memory_embedder.py`(新建)

**Interfaces:**
- Consumes: `_core.OpenAIEmbeddingConfig.from_env()`、`_core.OpenAIEmbeddingAdapter(cfg)`、`_core.StubEmbeddingAdapter(dim)`、`MemoryCore.set_embedder(emb)`(`bindings/python/bind_10_embedding.cpp:45-72`,`python/starling/_memory_core.py:128`)
- Produces: `make_openai_embedder(*, model: str, base_url: str, dim: int, timeout_ms: int, max_retries: int = 0) -> _core.OpenAIEmbeddingAdapter`;`Memory.open(db_path, *, agent="self", tenant_id="default", llm=None, extraction=None, embedder=None) -> Memory`

- [ ] **Step 0: 建分支**

```bash
cd /Users/jaredguo-mini/develop/memory/starling
git switch main && git pull --ff-only
git switch -c feat/memory-facade-for-rise
```

- [ ] **Step 1: 写失败的测试**

```python
import starling
from starling import _core


def test_open_without_embedder_keeps_stub_8(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice")
    assert mem._core.emb.model() == "stub" and mem._core.emb.dim() == 8
    mem.close()


def test_open_with_injected_embedder(tmp_path):
    emb = _core.StubEmbeddingAdapter(16)
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", embedder=emb)
    assert mem._core.emb is emb
    assert mem._core.semantic is not None and mem._core.worker is not None
    mem.close()


def test_make_openai_embedder_sets_fields_without_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = starling.make_openai_embedder(model="text-embedding-3-small",
                                        base_url="http://127.0.0.1:9/v1",
                                        dim=1536, timeout_ms=50)
    assert isinstance(emb, _core.OpenAIEmbeddingAdapter)
    assert emb.model() == "text-embedding-3-small" and emb.dim() == 1536
    assert (emb.request_count, emb.embed_calls, emb.batch_calls) == (0, 0, 0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_embedder.py -q -p no:cacheprovider`
Expected: FAIL,`TypeError: Memory.open() got an unexpected keyword argument 'embedder'` 与 `AttributeError: module 'starling' has no attribute 'make_openai_embedder'`

- [ ] **Step 3: 实现**

`make_openai_embedder` 与 `make_openai_llm`(`memory.py:48-70`)同形:从 `OpenAIEmbeddingConfig.from_env()` 起,`model`、`base_url`、`dim`、`timeout_ms`、`max_retries` 五个键都无条件覆盖(C++ 默认 `max_retries = 3`,`openai_embedding_adapter.hpp:21`;"非零才覆盖"会让配置里的 0 静默变成 3 次重试)。同一任务把 `make_openai_llm`(`memory.py:66` 的 `if max_retries:`)也改成无条件覆盖,加测试 `test_max_retries_zero_is_honoured_by_both_factories`;密钥只由 C++ 读 `OPENAI_API_KEY`,不收参数。`Memory.__init__` 加 keyword-only `embedder=None`,`None` 时照旧 `_core.StubEmbeddingAdapter(8)`,否则把传入的嵌入器交给 `self._core.set_embedder`;`Memory.open` 原样转发。`max_retries` 设计源 §8 的签名里没有,加它是为了与 `make_openai_llm` 同形,本仓库第 7 步的 `max_retries` 配置项同时喂给抽取与嵌入。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_embedder.py tests/python/test_memory_facade.py tests/python/test_openai_embedding_binding.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add python/starling/memory.py python/starling/__init__.py tests/python/test_memory_embedder.py
git commit -m "feat(memory): Memory.open 收 embedder,新增 make_openai_embedder"
```

### Task 2: `Memory.recall(holder=, strict=)` 与 `RecallDegradedError`

**Files:**
- Modify: `python/starling/_memory_core.py`(`MemoryCore.recall` 第 250 到 272 行:加 `strict`)
- Modify: `python/starling/memory.py`(`Memory.recall` 第 168 行;新增 `class RecallDegradedError(RuntimeError)`)
- Modify: `python/starling/__init__.py`(导出 `RecallDegradedError`)
- Test: `tests/python/test_memory_recall_strict.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `make_openai_embedder`;`_core.SemanticResult.degraded`(已绑定,`bindings/python/bind_10_embedding.cpp:174-177`;C++ 侧不用改)
- Produces: `Memory.recall(query, *, perspective="first_person", k=10, mode="semantic", holder: str | None = None, strict: bool = False) -> list[dict]`;`starling.RecallDegradedError`

- [ ] **Step 1: 写失败的测试**

```python
import pytest
import starling

CANNED_JSON = ...  # 照抄 tests/python/test_memory_facade.py:20-24


def _mem(tmp_path, **kw):
    return starling.Memory.open(str(tmp_path / "m.db"), agent="alice",
                                llm=starling.make_stub_llm(default_response=CANNED_JSON), **kw)


def _bad_embedder(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return starling.make_openai_embedder(model="offline", dim=8, timeout_ms=10,
                                         base_url="unsupported-starling-test://invalid")


def test_recall_forwards_holder(tmp_path):
    mem = _mem(tmp_path)
    mem.remember("Bob owns the auth module", holder="carol", now="2026-01-01T00:00:00Z")
    mem.tick("2026-01-01T00:00:01Z")          # 本任务时 recall 只见 consolidated 行,见 Task 4
    assert mem.recall("Bob owns the auth module") == []
    hits = mem.recall("Bob owns the auth module", holder="carol")
    assert len(hits) == 1 and hits[0]["row"].holder_id == "carol"


def test_strict_recall_raises_when_query_embedding_fails(tmp_path, monkeypatch):
    mem = _mem(tmp_path, embedder=_bad_embedder(monkeypatch))
    with pytest.raises(starling.RecallDegradedError):
        mem.recall("anything", strict=True)


def test_non_strict_recall_keeps_old_empty_result(tmp_path, monkeypatch):
    mem = _mem(tmp_path, embedder=_bad_embedder(monkeypatch))
    assert mem.recall("anything") == []
    assert mem.recall("anything", strict=False) == []


def test_strict_rejects_completion_mode(tmp_path):
    with pytest.raises(ValueError):
        _mem(tmp_path).recall("x", mode="completion", strict=True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_recall_strict.py -q -p no:cacheprovider`
Expected: FAIL,`TypeError: Memory.recall() got an unexpected keyword argument 'holder'` 与 `AttributeError: module 'starling' has no attribute 'RecallDegradedError'`

- [ ] **Step 3: 实现**

`MemoryCore.recall` 加 keyword-only `strict: bool = False`:语义模式下 `strict and res.degraded` 时抛 `RecallDegradedError("query embedding failed")`,信息里不带端点与密钥。`RecallDegradedError` 定义在 `_memory_core.py`(`LLMNotConfigured` 同处),由 `memory.py` 与 `__init__` 转出。`completion` 模式没有 `degraded` 信号,`strict=True` 时直接抛 `ValueError`,不静默。`Memory.recall` 原样转发 `holder`、`strict`。`dashboard/engine.py` 调 `MemoryCore.recall` 不传 `strict`,行为不变。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_recall_strict.py tests/python/test_memory_facade.py tests/python/test_episodic_recall.py tests/python/test_holder_reattribution.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add python/starling/_memory_core.py python/starling/memory.py python/starling/__init__.py tests/python/test_memory_recall_strict.py
git commit -m "feat(memory): recall 转发 holder,strict 模式下查询词嵌入失败抛 RecallDegradedError"
```

### Task 3: `Memory.embed_pending(now) -> EmbedStats`

**Files:**
- Modify: `include/starling/memory/memory_ops.hpp`、`src/memory/memory_ops.cpp`(新增 `embed_pending`,放在 `tick_all` 旁)
- Modify: `bindings/python/bind_13_memory_ops.cpp`(新增 `m.def("memory_embed_pending", ...)`,`py::call_guard<py::gil_scoped_release>()`)
- Modify: `python/starling/_memory_core.py`(新增 `MemoryCore.embed_pending`)、`python/starling/memory.py`(新增 `EmbedStats`、`Memory.embed_pending`)、`python/starling/__init__.py`(导出 `EmbedStats`)
- Test: `tests/python/test_memory_embed_pending.py`(新建);`tests/cpp/test_memory_ops.cpp`(加一个 `TEST`)

**Interfaces:**
- Consumes: `EmbeddingWorker::tick_one_batch(conn, now) -> EmbeddingStats`(`embedded`、`failed`,`embedding_worker.cpp:215-318`)
- Produces: C++ `memoryops::EmbedPendingOutcome embed_pending(persistence::SqliteAdapter&, embedding::EmbeddingWorker&, std::string_view now_iso)`(字段 `int embedded, failed`);Python `@dataclass EmbedStats(embedded: int = 0, failed: int = 0)`;`Memory.embed_pending(now: str) -> EmbedStats`

- [ ] **Step 1: 写失败的测试**

```python
def test_embed_pending_embeds_new_statements(tmp_path):
    mem = _mem(tmp_path)                       # 同 Task 2 的夹具
    mem.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    st = mem.embed_pending("2026-01-01T00:00:01Z")
    assert isinstance(st, starling.EmbedStats)
    assert st.embedded >= 1 and st.failed == 0
    assert mem.embed_pending("2026-01-01T00:00:02Z") == starling.EmbedStats(0, 0)


def test_embed_pending_counts_failures_without_raising(tmp_path, monkeypatch):
    mem = _mem(tmp_path, embedder=_bad_embedder(monkeypatch))
    mem.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    st = mem.embed_pending("2026-01-01T00:00:01Z")
    assert st.embedded == 0 and st.failed > 0


def test_embed_pending_runs_no_maintenance(tmp_path):
    import sqlite3
    mem = _mem(tmp_path)
    mem.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    mem.embed_pending("2026-01-01T00:00:01Z")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        assert c.execute("SELECT DISTINCT consolidation_state, replay_count FROM statements").fetchall() == [("volatile", 0)]
```

C++ 侧:`TEST(MemoryOps, EmbedPendingDrainsAllBatches)` 写 40 条语句(超过 `WorkerConfig::batch_size` 默认 32),一次 `embed_pending` 后 `embedded == 40`。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_embed_pending.py -q -p no:cacheprovider`
Expected: FAIL,`AttributeError: 'Memory' object has no attribute 'embed_pending'`

- [ ] **Step 3: 实现**

C++ `embed_pending` 反复调 `worker.tick_one_batch(adapter.connection(), now_iso)`,把 `embedded`、`failed` 累加,某一批 `embedded == 0` 时停(只剩失败行时不在同一次调用里反复重试)。不碰回放、承诺、投影、派发,也不看健康门控:这是设计源 4.3 节"不调 `tick`"的那一段。循环放 C++ 是照 Starling `CLAUDE.md` 的边界(编排进内核)。`MemoryCore.embed_pending(now)` 转发 `self.worker`,`Memory.embed_pending` 包成 `EmbedStats`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_embed_pending.py tests/python/test_embedding_health.py tests/python/test_tick_stage_timings.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add include/starling/memory/memory_ops.hpp src/memory/memory_ops.cpp bindings/python/bind_13_memory_ops.cpp python/starling/_memory_core.py python/starling/memory.py python/starling/__init__.py tests/python/test_memory_embed_pending.py tests/cpp/test_memory_ops.cpp
git commit -m "feat(memory): 新增 embed_pending,只跑嵌入阶段并返回成功与失败计数"
```

### Task 4: 只嵌入不 tick 时 recall 能命中(`include_volatile`)

> 取舍:走放宽召回,不走嵌入后巩固。巩固会改进摘要的 `activation` 等列,并把 tick 的一部分拉进每轮。

本机探针(`48e9f3b`):`remember` 写下的语句 `consolidation_state='volatile'`;`tick_one_batch` 之后 `recall` 返回 0 条,手工把它改成 `consolidated` 后返回 1 条。召回候选的可见性守卫是 `consolidation_state IN ('consolidated','archived') AND review_status NOT IN ('rejected','pending_review')`(`src/vector/sqlite_blob_vector_index.cpp:67-68`,`src/store/sqlite_meta_store.cpp:99-101`)。把 `volatile` 改成 `consolidated` 的只有 `tick` 的回放段。设计源 4.3 节"只 `embed_pending`、不调 `tick`"照现状走,本轮写的记忆永远召回不到;事实 S4 的"嵌入后就能被 recall 命中"不成立。本任务按"召回时显式放宽到 `volatile`"写:不跑回放,确定性不受影响,`review_status` 守卫不动,holder 与视角的隐私谓词也不动。另一条路是 `embed_pending` 之后跑一次确定性的巩固,它会改 `activation`、`replay_count` 等列,还受批大小 30 限制。

**Files:**
- Modify: `include/starling/vector/vector_index.hpp`(`SearchScope` 加 `bool include_volatile = false`)
- Modify: `src/vector/sqlite_blob_vector_index.cpp`(第 60 到 68 行的 SQL 加一个绑定参数)、`src/vector/zvec_vector_index.cpp`(第 145 行同一守卫)
- Modify: `include/starling/retrieval/semantic_retriever.hpp`(`SemanticRetrieverParams` 加 `include_volatile`)、`src/retrieval/semantic_retriever.cpp`(第 37 到 60 行:传给 `scope`;二次校验的 `StatementFilter.consolidation_states` 在放宽时取 `{"volatile","consolidated","archived"}`)
- Modify: `bindings/python/bind_10_embedding.cpp`(`SemanticRetrieverParams` 构造第 140 到 168 行加 `include_volatile=False`)
- Modify: `python/starling/_memory_core.py`(`recall`)、`python/starling/memory.py`(`Memory.recall`)
- Test: `tests/python/test_memory_recall_strict.py`(追加);`tests/cpp/test_sqlite_blob_vector_index.cpp`、`tests/cpp/test_zvec_vector_index.cpp`、`tests/cpp/test_semantic_retriever.cpp` 各加一个 `TEST`(默认不见 `volatile`,放宽后见;`pending_review` 两种都不见)

**Interfaces:**
- Consumes: Task 3 的 `Memory.embed_pending`
- Produces: `Memory.recall(..., include_volatile: bool = False)`;本仓库第 7 步的适配器传 `strict=True, include_volatile=True`

- [ ] **Step 1: 写失败的测试**

```python
def test_recall_sees_volatile_after_embed_only(tmp_path):
    mem = _mem(tmp_path)
    mem.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    mem.embed_pending("2026-01-01T00:00:01Z")
    assert mem.recall("Bob owns the auth module") == []          # 旧行为不变
    hits = mem.recall("Bob owns the auth module", include_volatile=True)
    assert len(hits) >= 1 and hits[0]["row"].consolidation_state == "volatile"


def test_include_volatile_keeps_review_guard(tmp_path):
    import sqlite3
    mem = _mem(tmp_path)
    mem.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    mem.embed_pending("2026-01-01T00:00:01Z")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        c.execute("UPDATE statements SET review_status='pending_review'")
    assert mem.recall("Bob owns the auth module", include_volatile=True) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_recall_strict.py -q -p no:cacheprovider`
Expected: FAIL,`TypeError: ... unexpected keyword argument 'include_volatile'`

- [ ] **Step 3: 实现**

SQL 守卫改为 `(?4 = 0 OR ((s.consolidation_state IN ('consolidated','archived') OR (?5 = 1 AND s.consolidation_state = 'volatile')) AND s.review_status NOT IN ('rejected','pending_review')))`。默认 `false`,既有调用方(看板、`query`、`converse`)行为不变。`SearchScope` 的注释"privacy-first: NEVER widen"指的是 holder 与 review 守卫,在注释里写明 `volatile` 是巩固状态,不是隐私状态。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_recall_strict.py tests/python/test_episodic_recall.py tests/python/test_retrieval_planner_e2e.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add include/starling/vector/vector_index.hpp src/vector/sqlite_blob_vector_index.cpp src/vector/zvec_vector_index.cpp include/starling/retrieval/semantic_retriever.hpp src/retrieval/semantic_retriever.cpp bindings/python/bind_10_embedding.cpp python/starling/_memory_core.py python/starling/memory.py tests/python/test_memory_recall_strict.py tests/cpp/test_sqlite_blob_vector_index.cpp tests/cpp/test_zvec_vector_index.cpp tests/cpp/test_semantic_retriever.cpp
git commit -m "feat(memory): recall 可显式放宽到 volatile 语句,只嵌入不巩固时也能命中"
```

### Task 5: 迁移 0038 建 `events` 表;`next_seq`、`pending_events`;`clear_tenant` 覆盖新表

现有迁移到 `0037_source_unit_meta.sql`,新迁移编号 0038。迁移 SQL 在构建期嵌进二进制(`CMakeLists.txt:217-227`,`configure_build.py` 会重新配置),没有运行期迁移步骤。

**Files:**
- Create: `migrations/0038_memory_events.sql`
- Create: `include/starling/memory/memory_events.hpp`、`src/memory/memory_events.cpp`(本任务只放读侧;Task 6、Task 7 往里加写侧)
- Modify: `CMakeLists.txt`(第 164 行 `src/memory/memory_ops.cpp` 之后加 `src/memory/memory_events.cpp`)
- Modify: `src/memory/memory_ops.cpp`(`clear_tenant` 第 808 到 866 行的 `Step` 表加 `events`)
- Modify: `bindings/python/bind_13_memory_ops.cpp`(新增 `memory_next_seq`、`memory_pending_events`)
- Modify: `python/starling/_memory_core.py`、`python/starling/memory.py`(新增 `PendingEvent`、`Memory.next_seq`、`Memory.pending_events`)、`python/starling/__init__.py`
- Create: `tests/cpp/test_memory_events.cpp`;Modify: `tests/cpp/CMakeLists.txt`(`add_executable(starling_tests ...)` 列表加该文件)
- Test: `tests/python/test_migration_0038.py`(新建,照 `tests/python/test_migration_0037.py` 的夹具写法);`tests/cpp/test_memory_ops.cpp` 里 `ClearTenant` 两个用例(第 444、546 行附近)会自动把 `events` 纳入"租户作用域表必须被清"的断言,不用改,跑通即可

**Interfaces:**
- Consumes: 无
- Produces: 表 `events(tenant_id, key, text, interlocutor, text_sha256, engram_ref, statement_ids_json, seq, state, failure_category)`;C++ `memoryops::next_seq(persistence::Connection&, std::string_view tenant) -> std::int64_t`、`memoryops::pending_events(conn, tenant) -> std::vector<PendingEventRow>`;Python `@dataclass(frozen=True) PendingEvent(key: str, text: str, interlocutor: str, seq: int, failure_category: str)`、`Memory.next_seq() -> int`、`Memory.pending_events() -> list[PendingEvent]`(按 `seq` 升序)

- [ ] **Step 1: 写失败的测试**

```python
import sqlite3
import pytest
from starling import runtime


def test_events_table_shape(tmp_path):
    rt = runtime._build_local_store_sqlite_runtime(tmp_path / "s.db"); rt.start()
    with sqlite3.connect(str(rt.adapter.db_path)) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        pk = [r[1] for r in sorted(c.execute("PRAGMA table_info(events)"), key=lambda r: r[5]) if r[5]]
        assert c.execute("SELECT name FROM schema_migrations WHERE version=38").fetchone() is not None
    assert cols == {"tenant_id", "key", "text", "interlocutor", "text_sha256", "engram_ref",
                    "statement_ids_json", "seq", "state", "failure_category"}
    assert pk == ["tenant_id", "key"]


def test_events_state_check_rejects_unknown(tmp_path):
    rt = runtime._build_local_store_sqlite_runtime(tmp_path / "s.db"); rt.start()
    with sqlite3.connect(str(rt.adapter.db_path)) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO events(tenant_id,key,text,interlocutor,text_sha256,seq,state) "
                  "VALUES('default','k','t','bob','h',0,'done')")


def test_empty_library_next_seq_zero_and_no_pending(tmp_path):
    mem = _mem(tmp_path)
    assert mem.next_seq() == 0
    assert mem.pending_events() == []
```

C++ 侧 `TEST(MemoryEvents, NextSeqIsMaxPlusOnePerTenant)`:直接插三行(`default` 的 seq 0、1,`other` 的 seq 0),断言 `next_seq(conn,"default") == 2`、`next_seq(conn,"other") == 1`、`next_seq(conn,"none") == 0`;`TEST(MemoryEvents, PendingEventsOrderedBySeqOnlyPending)`。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_migration_0038.py -q -p no:cacheprovider`
Expected: FAIL,`cols` 为空集(表不存在)与 `AttributeError: 'Memory' object has no attribute 'next_seq'`

- [ ] **Step 3: 实现**

```sql
-- 0038_memory_events.sql:按调用方给的稳定 key 记一次写入的收据(rise 设计二 §8)。
CREATE TABLE IF NOT EXISTS events (
    tenant_id          TEXT NOT NULL,
    key                TEXT NOT NULL,
    text               TEXT NOT NULL,
    interlocutor       TEXT NOT NULL,
    text_sha256        TEXT NOT NULL,
    engram_ref         TEXT,
    statement_ids_json TEXT NOT NULL DEFAULT '[]',
    seq                INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('pending','committed','abandoned')),
    failure_category   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, key),
    UNIQUE (tenant_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_state ON events(tenant_id, state);
```

`next_seq` 定义为 `COALESCE(MAX(seq) + 1, 0)`,含 `abandoned` 行,所以序号只增不复用;空库为 0,与设计源 4.3 节"训练副本从 0 起"一致:第一条事件的 `seq` 是 0。`tenant_id` 设计源列表里没有,加它是因为门面按 tenant 隔离,且 `clear_tenant` 的覆盖测试要求每张带 `tenant_id` 的表都被清。`clear_tenant` 的 `Step` 加 `{.table = "events", .sql = "DELETE FROM events WHERE tenant_id=?1"}`。读侧查询放 C++,绑定只转发,门面包成 `PendingEvent`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_migration_0038.py tests/python/test_migration_0037.py tests/python/test_m0_9_migrations.py tests/python/test_p2c_migrations.py tests/python/test_dashboard_clear_memory.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿(含 `ClearTenant.*`、`MigrationRunner.*`)

- [ ] **Step 5: 提交**

```bash
git add migrations/0038_memory_events.sql include/starling/memory/memory_events.hpp src/memory/memory_events.cpp CMakeLists.txt src/memory/memory_ops.cpp bindings/python/bind_13_memory_ops.cpp python/starling/_memory_core.py python/starling/memory.py python/starling/__init__.py tests/cpp/test_memory_events.cpp tests/cpp/CMakeLists.txt tests/python/test_migration_0038.py
git commit -m "feat(memory): 迁移 0038 新增 events 表,门面加 next_seq 与 pending_events"
```

### Task 6: `Memory.remember_event` 三段写入与 `EventConflict`

**Files:**
- Modify: `include/starling/memory/memory_events.hpp`、`src/memory/memory_events.cpp`(新增 `remember_event`、`class EventConflict : public std::runtime_error`)
- Modify: `bindings/python/bind_13_memory_ops.cpp`(新增 `memory_remember_event`,整段 `py::gil_scoped_release`;`py::register_exception<starling::memoryops::EventConflict>(m, "EventConflict", PyExc_RuntimeError)`)
- Modify: `python/starling/_memory_core.py`(`MemoryCore.remember_event`)、`python/starling/memory.py`(`Memory.remember_event`)、`python/starling/__init__.py`(导出 `EventConflict`)
- Test: `tests/python/test_memory_remember_event.py`(新建);`tests/cpp/test_memory_events.cpp`(追加)

**Interfaces:**
- Consumes: Task 5 的 `events` 表与 `next_seq`;`memoryops::remember_all(adapter, llm, params, prompts, policy)`(`include/starling/memory/memory_ops.hpp:153`);`memory_ops.cpp:39` 的 engram 存在性查询写法;`statement_writer.cpp:135` 按 `evidence_json` 里 `"engram_ref":"<id>"` 找语句的写法
- Produces: C++ `memoryops::remember_event(persistence::SqliteAdapter&, extractor::LLMAdapter&, const RememberParams& base, const RememberPrompts&, std::string_view key, std::string_view text, std::string_view epoch_iso, const extractor::ValidationPolicy&) -> RememberOutcome`;Python `Memory.remember_event(key: str, text: str, *, interlocutor: str, epoch: str = "2026-01-01T00:00:00Z") -> RememberResult`;`starling.EventConflict`

- [ ] **Step 1: 写失败的测试**

```python
import sqlite3
import pytest
import starling

KEY = "r1/deploy-m-s-0#3"          # 含 "/":Starling 不校验 key 字符集,原样存取


def _failing_llm():
    llm = starling.make_stub_llm(default_response="")
    llm.set_default_response("", False, "stub extraction failure")
    return llm


def _count(tmp_path, sql):
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        return c.execute(sql).fetchone()[0]


def test_new_key_commits_and_assigns_seq(tmp_path):
    mem = _mem(tmp_path)
    res = mem.remember_event(KEY, "bob:我负责鉴权模块", interlocutor="bob")
    assert res.outcome == "accepted" and not res.extraction_failed and res.statement_ids
    assert mem.next_seq() == 1 and mem.pending_events() == []
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        row = c.execute("SELECT state, seq, key FROM events").fetchone()
        payload = c.execute("SELECT payload_inline FROM engrams").fetchone()[0]
    assert row == ("committed", 0, KEY)
    assert payload.decode() == f"[{KEY}] bob:我负责鉴权模块"


def test_same_key_same_text_returns_stored_ids_without_extraction(tmp_path):
    mem = _mem(tmp_path)
    first = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    n = _count(tmp_path, "SELECT COUNT(*) FROM statements")
    mem._core.llm = _failing_llm()                 # 若再抽取就会 extraction_failed
    again = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert again.outcome == "idempotent" and not again.extraction_failed
    assert again.statement_ids == first.statement_ids
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements") == n
    assert mem.next_seq() == 1


def test_same_key_different_text_conflicts(tmp_path):
    mem = _mem(tmp_path)
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:bye", interlocutor="bob")


def test_extraction_failure_leaves_pending_with_category_then_retry_commits(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert res.extraction_failed
    [p] = mem.pending_events()
    assert (p.key, p.text, p.interlocutor, p.seq) == (KEY, "bob:hi", "bob", 0)
    assert p.failure_category == res.failure_category != ""
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    ok = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert not ok.extraction_failed and ok.statement_ids
    assert mem.pending_events() == [] and mem.next_seq() == 1      # 重试不另分配 seq


def test_crash_before_phase_three_is_completed_from_engram(tmp_path):
    # 模拟"② 落了语句、③ 没写成":先留一条 pending 行,再用裸 remember 写同一 payload。
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    landed = mem.remember(f"[{KEY}] bob:hi", interlocutor="bob",
                          now="2026-01-01T00:00:00Z")
    assert landed.statement_ids
    n = _count(tmp_path, "SELECT COUNT(*) FROM statements")
    mem._core.llm = _failing_llm()                 # 补齐不得再抽取
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert not res.extraction_failed
    assert sorted(res.statement_ids) == sorted(landed.statement_ids)
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements") == n
    assert mem.pending_events() == []


def test_pending_without_engram_reextracts_from_stored_text(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:   # python sqlite3 默认不开外键
        c.execute("DELETE FROM engrams")
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert res.statement_ids and not res.extraction_failed
    assert _count(tmp_path, "SELECT COUNT(*) FROM engrams") == 1


def test_event_time_derives_from_seq(tmp_path):
    mem = _mem(tmp_path)
    mem.remember_event("a#0", "bob:one", interlocutor="bob")
    mem.remember_event("a#1", "bob:two", interlocutor="bob")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        times = [r[0] for r in c.execute("SELECT created_at FROM engrams ORDER BY created_at")]
    assert times == ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"]


def test_remember_event_requires_llm(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice")
    with pytest.raises(starling._memory_core.LLMNotConfigured):
        mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert mem.next_seq() == 0                      # 没写 pending 行
```

C++ 侧在 `tests/cpp/test_memory_events.cpp` 用 `FakeLLMAdapter` 写同样四条主路径(新 key、同文本已提交、异文本冲突、补齐不抽取)。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_remember_event.py -q -p no:cacheprovider`
Expected: FAIL,`AttributeError: 'Memory' object has no attribute 'remember_event'`

- [ ] **Step 3: 实现**

C++ `remember_event` 的三段(设计源 §8):

1. 短事务(`TransactionGuard`)按 `(tenant_id, key)` 读行。无行:`seq = next_seq`,插 `state='pending'`、`text`、`interlocutor`、`text_sha256 = crypto::sha256_hex(text)` 的行后提交。有行:`abandoned` 或 `text_sha256` 不同 → 抛 `EventConflict`;`committed` → 返回 `outcome="idempotent"`、`engram_ref` 与 `statement_ids` 取行里存的,不调 LLM;`pending` → 进补齐分支。
2. 事务外:payload 是 `"[" + key + "] " + text`(设计源 4.3 节"key 作为前缀写进 Starling 的文本",前缀在 Starling 侧加,本仓库适配器只传原文);`created_at_iso8601 = epoch + seq 秒`;调 `remember_all`。补齐分支先按 payload 的 engram `content_hash` 找 `engrams` 行(由文本决定,S7;用 `evidence::compute_engram_content_hash`,`declared_transformations` 与 `remember_prepare` 构造 `EngramInput` 时一致,`src/memory/memory_ops.cpp:59-78`),找到且有 `evidence_json` 指向它、`holder_id` 相同的语句时,直接把这批 id(按 `rowid` 升序)当结果、不调 LLM;否则用行里存的原文照新 key 走 `remember_all`。
3. `extraction_failed` 为真:短事务写 `failure_category`(有 `engram_ref` 时一并写),行留 `pending`,返回原结果。成功(含补齐):短事务把行改成 `committed`,填 `engram_ref` 与 `statement_ids_json`,清空 `failure_category`。`remember_all` 抛异常时行留 `pending`,异常原样上抛。

`MemoryCore.remember_event` 在调 C++ 之前查 `self.llm is None` → `LLMNotConfigured`,与 `remember` 一致(`_memory_core.py:183-187`)。`epoch` 是本 PR 新加的参数,设计源 §8 的签名里没有:时钟要由事件行的 `seq` 推出(4.3 节),而 `seq` 在第 1 段才分配,调用方事先拿不到;重试 `pending` 行时用的是行里原来的 `seq`,不会漂移。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_remember_event.py tests/python/test_memory_facade.py tests/python/test_remember_phases_binding.py tests/python/test_remember_lock_release.py tests/python/test_gil_release.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add include/starling/memory/memory_events.hpp src/memory/memory_events.cpp bindings/python/bind_13_memory_ops.cpp python/starling/_memory_core.py python/starling/memory.py python/starling/__init__.py tests/python/test_memory_remember_event.py tests/cpp/test_memory_events.cpp
git commit -m "feat(memory): remember_event 按 key 三段写入,存下语句 id,同 key 异文本抛 EventConflict"
```

### Task 7: `Memory.abandon_event(key)`

**Files:**
- Modify: `include/starling/memory/memory_events.hpp`、`src/memory/memory_events.cpp`(新增 `abandon_event`)
- Modify: `bindings/python/bind_13_memory_ops.cpp`(新增 `memory_abandon_event`)、`python/starling/_memory_core.py`、`python/starling/memory.py`
- Test: `tests/python/test_memory_remember_event.py`(追加);`tests/cpp/test_memory_events.cpp`(追加)

**Interfaces:**
- Consumes: Task 6 的 `events` 行状态与 `EventConflict`
- Produces: C++ `memoryops::abandon_event(persistence::SqliteAdapter&, std::string_view tenant, std::string_view key) -> void`;`Memory.abandon_event(key: str) -> None`

- [ ] **Step 1: 写失败的测试**

```python
def test_abandon_pending_then_same_key_conflicts(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem.abandon_event(KEY)
    assert mem.pending_events() == []
    assert _count(tmp_path, "SELECT COUNT(*) FROM events WHERE state='abandoned'") == 1
    assert mem.next_seq() == 1                       # 放弃不回收 seq
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:bye", interlocutor="bob")


def test_abandon_committed_is_refused(tmp_path):
    mem = _mem(tmp_path)
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with pytest.raises(starling.EventConflict):
        mem.abandon_event(KEY)
    assert _count(tmp_path, "SELECT state FROM events") == "committed"


def test_abandon_unknown_key_raises_keyerror_and_twice_is_noop(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    with pytest.raises(KeyError):
        mem.abandon_event("nope#0")
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem.abandon_event(KEY)
    mem.abandon_event(KEY)                           # 已是终态,幂等
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_remember_event.py -q -p no:cacheprovider -k abandon`
Expected: FAIL,`AttributeError: 'Memory' object has no attribute 'abandon_event'`

- [ ] **Step 3: 实现**

一个短事务:`pending` → `UPDATE events SET state='abandoned'`;`abandoned` → 不动;`committed` → `EventConflict`;无行 → C++ 抛 `std::out_of_range`,pybind 默认译成 `KeyError`。只改事件行,不动语句与 engram:`pending` 行本来就没有语句(有的话 Task 6 的补齐分支会先把它补成 `committed`)。设计源 §8 没说未知 key 怎么办,这里取 `KeyError`,不静默。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_remember_event.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add include/starling/memory/memory_events.hpp src/memory/memory_events.cpp bindings/python/bind_13_memory_ops.cpp python/starling/_memory_core.py python/starling/memory.py tests/python/test_memory_remember_event.py tests/cpp/test_memory_events.cpp
git commit -m "feat(memory): abandon_event 把 pending 事件改成终态 abandoned,committed 不许放弃"
```

### Task 8: 语句的 `observed_at` 取 engram 的 `created_at`,不取墙钟

本机探针:同一 `now` 写同一句话进两个库,语句行除 `id`、`created_at`、`updated_at`、`last_accessed` 外还有 `observed_at`、`evidence_json`、`source_spans_json` 不同。后两者是 engram UUID,Task 9 在规范化里处理;`observed_at` 是墙钟:门面默认的旧抽取路径在 `src/extractor/json_parser.cpp:159` 写 `now_iso8601_utc()`,只有语义声明路径在 `src/extractor/extractor.cpp:744` 改成 `engram->created_at_iso8601`。`observed_at` 参与检索规划的时间排序(`StatementFilter.order_by` 白名单),不能排除出摘要,所以在写入端修。

**Files:**
- Modify: `src/extractor/extractor.cpp`(`Extractor::persist` 的旧路径,第 860 行 `for (auto& stmt : parsed.statements)` 循环:进循环前按 `engram_ref_id` 取一次 `evidence::EngramStore::get`,取到且 `created_at_iso8601` 非空时覆盖 `stmt.observed_at`)
- Test: `tests/cpp/test_extractor_orchestrator.cpp`(追加一个 `TEST`);`tests/python/test_memory_facade.py`(追加)

**Interfaces:**
- Consumes: 无
- Produces: 行为:`Memory.remember(text, now=T)` 写下的语句 `observed_at == T`(秒级 ISO,UTC)

- [ ] **Step 1: 写失败的测试**

```python
def test_observed_at_follows_now_not_wall_clock(tmp_path):
    import sqlite3
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=llm)
    mem.remember("Bob owns the auth module", now="2026-01-01T00:00:07Z")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        assert {r[0] for r in c.execute("SELECT observed_at FROM statements")} == {"2026-01-01T00:00:07Z"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_facade.py::test_observed_at_follows_now_not_wall_clock -q -p no:cacheprovider`
Expected: FAIL,集合里是当天墙钟时间

- [ ] **Step 3: 实现**

只改旧路径的覆盖,不动 `json_parser`(`tests/cpp/test_json_parser.cpp:76` 断言解析层 `observed_at` 非空,照旧成立)。engram 不在时保留解析层的值,与语义声明路径"engram 缺失则失败"不同,因为旧路径今天不要求 engram 存在。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python -q -p no:cacheprovider -k "observed or episodic or retrieval_planner or facade"`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add src/extractor/extractor.cpp tests/cpp/test_extractor_orchestrator.cpp tests/python/test_memory_facade.py
git commit -m "fix(extractor): 旧抽取路径的 observed_at 取 engram 的 created_at,不取墙钟"
```

### Task 9: `Memory.digest()` 与召回命中的内容 id

**Files:**
- Create: `include/starling/memory/memory_digest.hpp`、`src/memory/memory_digest.cpp`;Modify: `CMakeLists.txt`(加源文件)
- Modify: `bindings/python/bind_13_memory_ops.cpp`(新增 `memory_digest`、`memory_content_ids`)
- Modify: `python/starling/_memory_core.py`(`digest`;`recall` 的每个命中加 `"content_id"`)、`python/starling/memory.py`(`Memory.digest`;公开 `Memory.content_ids(statement_ids: list[str]) -> dict[str, str]`,本仓库适配器把 `remember` 返回的语句 UUID 换成内容 id 要用它;不存在的 id 映射为 `"missing"`,与摘要里的约定一致)
- Create: `tests/cpp/test_memory_digest.cpp`;Modify: `tests/cpp/CMakeLists.txt`
- Test: `tests/python/test_memory_digest.py`(新建)

**Interfaces:**
- Consumes: Task 5 的 `events` 表;Task 8 后语句行在两个库间只剩 UUID 与簿记列不同
- Produces: C++ `memoryops::digest(persistence::Connection&, std::string_view tenant) -> std::string`(64 位小写十六进制)、`memoryops::content_ids(conn, tenant, const std::vector<std::string>& stmt_ids) -> std::map<std::string, std::string>`;`Memory.digest() -> str`;`Memory.recall` 每个命中多一个键 `content_id: str`(64 位十六进制;本仓库第 7 步取前 16 位加 `mem-`)

- [ ] **Step 1: 写失败的测试**

```python
import hashlib
import re
import sqlite3
import starling

EMPTY = hashlib.sha256(b'{"events":[],"statement_vectors":[],"statements":[]}').hexdigest()


def _db(tmp_path, name):
    return starling.Memory.open(str(tmp_path / name), agent="alice",
                                llm=starling.make_stub_llm(default_response=CANNED_JSON))


def _fill(mem):
    mem.remember_event("e#0", "bob:我负责鉴权模块", interlocutor="bob")
    mem.embed_pending("2026-01-01T00:01:00Z")


def _sql(tmp_path, name, sql):
    with sqlite3.connect(str(tmp_path / name)) as c:
        c.execute(sql)


def test_empty_library_digest_is_fixed(tmp_path):
    assert _db(tmp_path, "a.db").digest() == EMPTY == _db(tmp_path, "b.db").digest()


def test_same_content_different_uuids_same_digest(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    _fill(a); _fill(b)
    assert a.digest() == b.digest() != EMPTY


def test_excluded_columns_do_not_change_digest(tmp_path):
    a = _db(tmp_path, "a.db"); _fill(a); d0 = a.digest()
    for sql in ("UPDATE statements SET access_count=access_count+5, last_accessed='2030-01-01T00:00:00Z',"
                " updated_at='2030-01-01T00:00:00Z', replay_count=replay_count+1,"
                " last_replayed='2030-01-01T00:00:00Z', last_replay_batch_id='x', inferred_at='2030-01-01T00:00:00Z'",
                "UPDATE statement_vectors SET retry_count=9, last_attempt_at='2030-01-01T00:00:00Z',"
                " embedded_at='2030-01-01T00:00:00Z'"):
        _sql(tmp_path, "a.db", sql)
    assert a.digest() == d0


def test_digest_columns_change_digest(tmp_path):
    a = _db(tmp_path, "a.db"); _fill(a); d0 = a.digest()
    _sql(tmp_path, "a.db", "UPDATE statements SET activation=activation+0.5")   # S18
    d1 = a.digest(); assert d1 != d0
    _sql(tmp_path, "a.db", "UPDATE statement_vectors SET raw_embedding=zeroblob(32)")
    d2 = a.digest(); assert d2 != d1
    _sql(tmp_path, "a.db", "UPDATE events SET failure_category='timeout'")
    assert a.digest() != d2


def test_statement_ids_json_uuids_are_mapped_to_content_ids(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    _fill(a); _fill(b)
    with sqlite3.connect(str(tmp_path / "a.db")) as c:
        ids = [r[0] for r in c.execute("SELECT statement_ids_json FROM events")]
    with sqlite3.connect(str(tmp_path / "b.db")) as c:
        assert [r[0] for r in c.execute("SELECT statement_ids_json FROM events")] != ids
    assert a.digest() == b.digest()


def test_dangling_reference_maps_to_missing(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    _fill(a); _fill(b)
    _sql(tmp_path, "a.db", "UPDATE statements SET supersedes_id='no-such-1'")
    _sql(tmp_path, "b.db", "UPDATE statements SET supersedes_id='no-such-2'")
    assert a.digest() == b.digest()


def test_abandoned_event_counts_only_key_and_state(tmp_path):
    def make(name, text):
        m = starling.Memory.open(str(tmp_path / name), agent="alice", llm=_failing_llm())
        m.remember_event("e#0", text, interlocutor="bob"); m.abandon_event("e#0"); return m
    a, b = make("a.db", "bob:one"), make("b.db", "bob:two")
    assert a.digest() == b.digest() != EMPTY


def test_rows_invisible_to_recall_still_count(tmp_path):
    # 全行进摘要:pending_review、volatile、forgotten、被取代后 archived 的行都进。
    a = _db(tmp_path, "a.db"); _fill(a); d0 = a.digest()
    _sql(tmp_path, "a.db", "UPDATE statements SET review_status='pending_review'")
    d1 = a.digest(); assert d1 != d0
    assert a.recall("bob", include_volatile=True) == []
    _sql(tmp_path, "a.db", "UPDATE statements SET object_value='billing'")
    assert a.digest() != d1


def test_content_ids_maps_statement_uuids_and_marks_unknown_ones_missing(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    ids_a = list(a.remember_event("e#0", "bob:我负责鉴权模块", interlocutor="bob").statement_ids)
    ids_b = list(b.remember_event("e#0", "bob:我负责鉴权模块", interlocutor="bob").statement_ids)
    got = a.content_ids(ids_a + ["no-such-id"])
    assert set(got) == set(ids_a) | {"no-such-id"} and got["no-such-id"] == "missing"
    assert all(re.fullmatch(r"[0-9a-f]{64}", got[i]) for i in ids_a)       # 与召回命中的 content_id 同一形状
    assert sorted(got[i] for i in ids_a) == sorted(b.content_ids(ids_b).values())   # UUID 不同,内容 id 相同


def test_recall_hits_carry_same_content_id_across_libraries(tmp_path):
    a, b = _db(tmp_path, "a.db"), _db(tmp_path, "b.db")
    _fill(a); _fill(b)
    ha = a.recall("我负责鉴权模块", include_volatile=True)
    hb = b.recall("我负责鉴权模块", include_volatile=True)
    assert ha and [h["content_id"] for h in ha] == [h["content_id"] for h in hb]
    assert ha[0]["row"].id != hb[0]["row"].id
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_digest.py -q -p no:cacheprovider`
Expected: FAIL,`AttributeError: 'Memory' object has no attribute 'digest'`

- [ ] **Step 3: 实现**

规范化全部在 C++(Starling `CLAUDE.md` 的边界),用 nlohmann::json(对象键天然有序,`dump()` 紧凑输出)。规则:

- 只读本 tenant 的行;列名从 `PRAGMA table_info` 取,按排除清单去列(设计源 4.5 节:`id`、`stmt_id`、`created_at`、`updated_at`、`last_accessed`、`access_count`、`last_replayed`、`replay_count`、`last_replay_batch_id`、`retry_count`、`last_attempt_at`、`embedded_at`、`inferred_at`)。新加列默认进摘要。BLOB 转小写十六进制串,NULL 转 `null`,REAL 原样。
- engram UUID:语句行的 JSON 列(`evidence_json`、`source_spans_json`、`semantic_claim_json`、`confidence_history_json`)里键为 `engram_ref`、`evidence_engram_ref` 的值,以及 `events.engram_ref`,换成 `"engram:" + engrams.content_hash`;engram 不在时换成 `missing`。`confidence_history_json` 每项的 `ts` 是墙钟(`src/bus/bus.cpp:209`),规范化时去掉。设计源 4.5 节的排除清单没写这两条,探针证明不处理就做不到"两个库内容相同、摘要相同"。
- 内容 id:语句行按上两条规范化,再去掉引用列 `supersedes_id`、`derived_from_json`,`dump()` 后取 sha256。引用列不进内容 id,避免沿取代链递归,也让被指向行的内容 id 先算得出。
- 摘要里的语句行:内容 id 的规范化行加回引用列,`supersedes_id` 与 `derived_from_json` 里的每个 UUID 换成被指向语句的内容 id,不存在的换成 `missing`。向量行:`stmt_id` 换成键 `stmt` 的值为所属语句的内容 id。事件行:`statement_ids_json` 按原顺序换成内容 id;`abandoned` 行只留 `{"key","state"}`。
- 整体:`{"events": [...], "statement_vectors": [...], "statements": [...]}`,语句行与向量行按内容 id 加规范化串排序,事件行按 `key` 排序,`dump()` 后取 sha256。空库就是测试里的 `EMPTY`。
- 全行进摘要:不按召回可见性过滤。被取代的旧行被归档为 `archived`(`src/reconsolidation/arbitration.cpp:405`),仍是召回候选(`src/vector/sqlite_blob_vector_index.cpp:67`);`volatile` 与 `pending_review` 行此刻召回不到,但仍参与写入时的冲突探测与强化(`src/bus/conflict_probe.cpp:96-109` 只看 `volatile`、`consolidated`,不看 review 状态),会改后续写入的结果。按 4.5 节"加列默认进摘要,漏掉只会过敏感"的原则,这些行一律进摘要。

`MemoryCore.recall` 拿到命中后用一次 `memory_content_ids` 批量补 `content_id`,不逐条查。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_digest.py tests/python/test_memory_recall_strict.py tests/python/test_memory_remember_event.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add include/starling/memory/memory_digest.hpp src/memory/memory_digest.cpp CMakeLists.txt bindings/python/bind_13_memory_ops.cpp python/starling/_memory_core.py python/starling/memory.py tests/cpp/test_memory_digest.cpp tests/cpp/CMakeLists.txt tests/python/test_memory_digest.py
git commit -m "feat(memory): digest 按排除清单算三张表的逻辑摘要,召回命中带内容 id"
```

### Task 10: `Memory.backup_to(path)`

**Files:**
- Modify: `include/starling/memory/memory_ops.hpp`、`src/memory/memory_ops.cpp`(新增 `backup_to`,照 `src/governance/world_store.cpp:815-850` 的 `VACUUM INTO` 加只读打开 `PRAGMA integrity_check` 的写法)
- Modify: `bindings/python/bind_13_memory_ops.cpp`(新增 `memory_backup_to`,`py::gil_scoped_release`)、`python/starling/_memory_core.py`、`python/starling/memory.py`
- Test: `tests/python/test_memory_backup.py`(新建);`tests/cpp/test_memory_ops.cpp`(追加)

**Interfaces:**
- Consumes: Task 9 的 `Memory.digest()`;Task 5 的迁移 0038
- Produces: C++ `memoryops::backup_to(persistence::SqliteAdapter&, const std::filesystem::path& dest) -> void`;`Memory.backup_to(path: str | os.PathLike) -> None`

- [ ] **Step 1: 写失败的测试**

```python
import sqlite3
import pytest
import starling


def test_backup_is_self_contained_and_same_digest(tmp_path):
    a = _db(tmp_path, "a.db"); _fill(a)
    a.backup_to(tmp_path / "copy.db")
    assert not (tmp_path / "copy.db-wal").exists()
    with sqlite3.connect(str(tmp_path / "copy.db")) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    b = _db(tmp_path, "copy.db")
    assert b.digest() == a.digest()
    assert [h["content_id"] for h in b.recall("我负责鉴权模块", include_volatile=True)] == \
           [h["content_id"] for h in a.recall("我负责鉴权模块", include_volatile=True)]
    assert b.next_seq() == a.next_seq()


def test_backup_refuses_existing_destination(tmp_path):
    a = _db(tmp_path, "a.db"); (tmp_path / "copy.db").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        a.backup_to(tmp_path / "copy.db")
    assert (tmp_path / "copy.db").read_bytes() == b"x"
    assert not (tmp_path / "copy.db.partial").exists()


def _downgrade_to_0037(path):
    # 造一份"0038 之前的 Starling 写下的库":去掉新表与它的迁移记录。
    with sqlite3.connect(str(path)) as c:
        c.execute("DROP TABLE events")
        c.execute("DELETE FROM schema_migrations WHERE version = 38")


def test_old_schema_library_backup_opens_with_new_version(tmp_path):
    old = _db(tmp_path, "old.db")
    old.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    old.embed_pending("2026-01-01T00:00:01Z"); old.close()
    _downgrade_to_0037(tmp_path / "old.db")
    with sqlite3.connect(str(tmp_path / "old.db")) as c:      # 旧版本自己做的副本
        c.execute(f"VACUUM INTO '{tmp_path / 'old-copy.db'}'")
    reopened = _db(tmp_path, "old.db")                       # 新版本打开原库,自动迁到 0038
    reopened.backup_to(tmp_path / "new-copy.db")
    for name in ("old-copy.db", "new-copy.db"):
        m = _db(tmp_path, name)
        assert m.next_seq() == 0
        assert m.digest() == reopened.digest()
        assert m.recall("Bob owns the auth module", include_volatile=True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_backup.py -q -p no:cacheprovider`
Expected: FAIL,`AttributeError: 'Memory' object has no attribute 'backup_to'`

- [ ] **Step 3: 实现**

目标已存在 → C++ 抛 `std::filesystem::filesystem_error`(`errc::file_exists`),绑定里译成 Python `FileExistsError`。先 `VACUUM INTO "<dest>.partial"`,只读打开它跑 `PRAGMA integrity_check`,不是 `ok` 就删掉 `.partial` 并抛 `std::runtime_error("backup integrity_check failed")`;通过后 `fs::rename` 到 `dest`。`VACUUM INTO` 的产物是 rollback 日志模式的单文件,不带 `-wal`。§13 的未核实项"旧迁移版本的库备份后用新版本打开"由第三个用例钉住;旧版本库是用删表造出来的,不是真跑一份旧二进制,这一点写进 PR 描述。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_backup.py tests/python/test_memory_digest.py tests/python/test_dashboard_worlds.py -q -p no:cacheprovider`
Expected: PASS,ctest 全绿

- [ ] **Step 5: 提交**

```bash
git add include/starling/memory/memory_ops.hpp src/memory/memory_ops.cpp bindings/python/bind_13_memory_ops.cpp python/starling/_memory_core.py python/starling/memory.py tests/python/test_memory_backup.py tests/cpp/test_memory_ops.cpp
git commit -m "feat(memory): backup_to 用 VACUUM INTO 加 integrity_check 生成自洽单文件副本"
```

### Task 11: `Memory.close()` 等在途调用退出并真正关库

今天 `Memory.close()` 只把 `MemoryCore.conn` 置空(`_memory_core.py:475-478`),连接要等对象被回收才关,WAL 不合并(S9)。门面自己不起线程;"自己的工作线程"在门面这里就是别的 Python 线程上正在跑、已释放 GIL 的 C++ 调用(抽取、嵌入)。`close` 要等它们返回,再合并 WAL、关连接。

**Files:**
- Modify: `include/starling/persistence/sqlite_adapter.hpp`、`src/persistence/sqlite_adapter.cpp`(新增 `void close()`:`PRAGMA wal_checkpoint(TRUNCATE)` 后释放句柄;关后 `connection()` 抛 `std::logic_error("adapter closed")`);`include/starling/persistence/connection.hpp`、`src/persistence/connection.cpp`(新增 `close()` 与 `bool is_open() const`)
- Modify: `bindings/python/bind_02_persistence.cpp`(`SqliteAdapter.close`)
- Modify: `python/starling/memory.py`(`Memory` 加在途计数与 `threading.Condition`;公开方法进出计数;新增 `class MemoryClosed(RuntimeError)`)、`python/starling/__init__.py`(导出 `MemoryClosed`)
- Test: `tests/python/test_memory_close.py`(新建);`tests/cpp/test_persistence_stmt.cpp`(追加 `SqliteAdapter.close` 的用例)

**Interfaces:**
- Consumes: Task 6 的 `remember_event`;`FakeLLMAdapter.set_delay_ms(ms)`(C++ 内睡眠,GIL 已释放,先例 `tests/python/test_remember_lock_release.py`)
- Produces: `Memory.close(timeout: float | None = None) -> None`(超时抛 `TimeoutError`,库保持打开);关后任何公开方法抛 `starling.MemoryClosed`;二次 `close()` 无操作

- [ ] **Step 1: 写失败的测试**

```python
import threading
import time
import pytest
import starling


def test_close_waits_for_in_flight_remember(tmp_path):
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    llm.set_delay_ms(400)
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=llm)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=mem.remember_event("e#0", "bob:hi", interlocutor="bob")))
    t.start(); time.sleep(0.1)
    mem.close()
    assert "r" in out and out["r"].statement_ids             # close 返回时在途调用已完成
    t.join(timeout=5)
    assert not (tmp_path / "m.db-wal").exists() or (tmp_path / "m.db-wal").stat().st_size == 0
    again = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=llm)
    assert again.next_seq() == 1 and again.pending_events() == []


def test_close_timeout_keeps_library_open(tmp_path):
    llm = starling.make_stub_llm(default_response=CANNED_JSON); llm.set_delay_ms(600)
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=llm)
    t = threading.Thread(target=lambda: mem.remember_event("e#0", "bob:hi", interlocutor="bob"))
    t.start(); time.sleep(0.1)
    with pytest.raises(TimeoutError):
        mem.close(timeout=0.05)
    t.join(timeout=5)
    mem.close()


def test_calls_after_close_raise_and_close_is_idempotent(tmp_path):
    mem = _db(tmp_path, "m.db")
    mem.close(); mem.close()
    for call in (lambda: mem.recall("x"), lambda: mem.digest(), lambda: mem.next_seq(),
                 lambda: mem.embed_pending("2026-01-01T00:00:00Z"),
                 lambda: mem.remember_event("e#0", "bob:hi", interlocutor="bob"),
                 lambda: mem.backup_to(tmp_path / "c.db")):
        with pytest.raises(starling.MemoryClosed):
            call()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python/test_memory_close.py -q -p no:cacheprovider`
Expected: FAIL,`AttributeError: module 'starling' has no attribute 'MemoryClosed'`,且第一个用例 `-wal` 文件非空

- [ ] **Step 3: 实现**

`Memory` 的每个公开方法(`remember`、`remember_event`、`recall`、`query`、`tick`、`embed_pending`、`digest`、`backup_to`、`next_seq`、`pending_events`、`abandon_event`、`render_working_set`、`latest_event_location`)用同一个上下文管理器包:持 `Condition` 检查 `_closed`,为真抛 `MemoryClosed`,否则在途数加一;退出时减一并 `notify_all`。`close(timeout)`:置 `_closed`,`wait_for(lambda: self._in_flight == 0, timeout)`,超时把 `_closed` 复原并抛 `TimeoutError`;等到之后调 `self._core.close()` 与 `self._rt.adapter.close()`。不改 `MemoryCore.close`,看板 `dashboard/engine.py:1134` 的关闭行为不变。C++ `SqliteAdapter::close()` 只做检查点与释放句柄,不管并发:并发由宿主(这里是门面)负责,与 `CLAUDE.md` 里"写入纪律归写入器"不冲突,因为关闭不是写入。

- [ ] **Step 4: 跑测试确认通过,并跑提交门**

Run: `.venv/bin/python scripts/configure_build.py --build --test && .venv/bin/python scripts/configure_build.py --python-editable && PYTHONDONTWRITEBYTECODE=1 STARLING_RUN_LLM_E2E= .venv/bin/python -m pytest tests/python -q -p no:cacheprovider && .venv/bin/python scripts/ci_static_scan.py`
Expected: ctest 全绿;pytest 全部通过(历史评测按 `historical_eval` 标记跳过,报告里分开列);静态扫描 0 违规。这是本步最后一次全量回归,结果写进 Task 12 的 PR 描述。

- [ ] **Step 5: 提交**

```bash
git add include/starling/persistence/sqlite_adapter.hpp src/persistence/sqlite_adapter.cpp include/starling/persistence/connection.hpp src/persistence/connection.cpp bindings/python/bind_02_persistence.cpp python/starling/memory.py python/starling/__init__.py tests/python/test_memory_close.py tests/cpp/test_persistence_stmt.cpp
git commit -m "feat(memory): close 等在途调用返回后合并 WAL 并关连接,关后调用抛 MemoryClosed"
```

### Task 12: 推送分支并开 PR(先征得用户同意)

**Files:**
- 无代码改动

**Interfaces:**
- Consumes: Task 1 到 Task 11 的全部提交;Task 11 Step 4 的全量回归结果
- Produces: Starling PR 的合并提交 SHA(40 位),供第 7 步写进本仓库 `pyproject.toml` 的 `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<合并提交>"]`

- [ ] **Step 1: 停下,把分支名、提交列表与 PR 描述草稿给用户看,得到明确同意后才继续**

不推送、不开 PR,先整理三样东西给用户:

```bash
cd /Users/jaredguo-mini/develop/memory/starling
git fetch origin main
git branch --show-current                          # 应为 feat/memory-facade-for-rise
git log --oneline origin/main..HEAD                # 提交列表,应为 11 笔
git diff --stat origin/main...HEAD
```

PR 描述草稿(标题 `feat(memory): 门面补齐按 key 写入、逻辑摘要、备份与严格召回`,70 字以内):

```markdown
### 动机
rise 的智能体记忆(设计二)要把 Starling 当每个角色的记忆后端:每轮先召回再写入,
训练副本按逻辑摘要判定状态,部署库跨轮次持久并可回滚。今天的公开门面缺这些接口。

### 改动
- `Memory.open(embedder=)`、`make_openai_embedder`:注入真嵌入器,`None` 维持 8 维 stub
- `Memory.recall(holder=, strict=, include_volatile=)`:`strict` 时查询词嵌入失败抛 `RecallDegradedError`;`include_volatile` 让只嵌入不 tick 的语句可召回(review 与 holder 守卫不变)
- `Memory.embed_pending(now) -> EmbedStats`:只跑嵌入阶段,返回成功与失败计数
- 迁移 0038 `events` 表;`remember_event`、`abandon_event`、`next_seq`、`pending_events`;`EventConflict`
- `Memory.digest()`:statements、statement_vectors、events 三张表按排除清单算的逻辑摘要,UUID 换内容 id;召回命中带 `content_id`
- `Memory.backup_to(path)`:`VACUUM INTO` 加 `integrity_check`
- `Memory.close(timeout=)`:等在途调用返回,合并 WAL 并关连接;关后抛 `MemoryClosed`
- 旧抽取路径的 `observed_at` 取 engram 的 `created_at`,不再取墙钟

### 测试
- 全量 ctest:<通过数>
- `pytest tests/python`:<通过数> 通过、<历史跳过数> 历史跳过、<原有跳过数> 原有跳过
- `scripts/ci_static_scan.py`:0 违规
- "旧迁移版本库备份后用新版本打开"用删掉 0038 的库模拟,不是真跑旧二进制

### 兼容性
所有新参数默认值保持旧行为;`clear_tenant` 多清一张 `events` 表。
```

把这三样贴给用户,问"是否推送 `feat/memory-facade-for-rise` 并按此描述开 PR"。用户明确说可以之前不执行 Step 2;用户要改描述或提交就先改,改完再问一次。

- [ ] **Step 2: 推送分支并开 PR**

```bash
cd /Users/jaredguo-mini/develop/memory/starling
git push -u origin feat/memory-facade-for-rise
gh pr create --base main --head feat/memory-facade-for-rise \
  --title "feat(memory): 门面补齐按 key 写入、逻辑摘要、备份与严格召回" \
  --body-file <Step 1 定稿描述存成的临时文件>
```

临时文件写在仓库外(例如 `$TMPDIR`),开完 PR 删掉。本分支的提交信息都不含 Co-Authored-By 行,推送前用 `git log origin/main..HEAD --format=%B | grep -c Co-Authored-By` 确认为 0。

- [ ] **Step 3: 等 CI 与评审**

Run: `gh pr checks <PR 号> --watch`
Expected: `cpp-build-test`、`python-test`、`static-scan` 三项通过。评审意见按需在同一分支追加新提交,不 `--amend`、不强推。

- [ ] **Step 4: 合入后记下合并提交**

合并由仓库所有者操作,本步不自行合并。合入后:

```bash
gh pr view <PR 号> --json state,mergeCommit --jq '.state + " " + .mergeCommit.oid'
```

Expected: `MERGED <40 位 SHA>`。把这个 SHA 原样交给第 7 步。Starling 仓库在本步之后切回 `main` 并 `git pull --ff-only`,不删本地分支。

---

## 第 7 步:本仓库接入 Starling

> 第 7 步的前提:Task 12 的 Starling PR 已合入,合并提交已记下。本步凡是要真 Starling 的用例都用 `pytest.importorskip("starling")`,所以全局约束里的测试命令(只带 `--extra dev`)照常全绿、只是跳过它们;要跑它们用 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest ... -q -p no:cacheprovider`。下文写"(带 memory)"的 Run 指这条命令。装 Starling 会从源码编 C++ 内核,需要本机有 CMake 与编译器(与 Starling 仓库的 `scripts/configure_build.py` 同一套)。

### Task 13: `memory` extra、离线假端点与 `starling` 的导入边界

**Files:**
- Modify: `pyproject.toml`(`[project.optional-dependencies]` 加 `memory`;`[tool.pytest.ini_options].markers` 加 `memory_live`)、`uv.lock`(`uv lock` 重新生成)
- Create: `tests/starling_fakes.py`(本机回环的 OpenAI 兼容假端点,本步后续任务共用)
- Modify: `tests/test_actio_instance.py`(`_ALLOWED` 之外,`starling` 只许出现在 `memory_starling.py`)
- Modify: `tests/test_factory_cross_plan.py`(新增"全 `src/` 只有 `actio/memory_starling.py` 导入 `starling`")
- Test: `tests/test_starling_fakes.py`(新)

**Interfaces:**
- Consumes: Task 12 记下的 Starling 合并提交 SHA;Task 1 `starling.make_openai_embedder(*, model, base_url, dim, timeout_ms, max_retries=0)`;Starling 现有 `make_openai_llm(*, model, base_url, timeout_ms, max_retries, max_tokens)`(`python/starling/memory.py:48`);`actio.text.tokens`。
- Produces:
  - extra `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<Task 12 的合并提交>"]`;pytest 标记 `memory_live`。
  - `tests/starling_fakes.py`:`class FakeOpenAI`(上下文管理器,`with FakeOpenAI(dim=256) as fake:`),属性 `base_url: str`(`http://127.0.0.1:<port>/v1`)、`dim: int`、`chat_calls: int`、`embedding_calls: int`,可在运行中改的开关 `fail_embeddings: bool = False`、`fail_keys: set[str]`(段落 key 在其中则 `/chat/completions` 回 500)、`chat_delay_s: float = 0.0`、`salt: str = ""`(改它就改向量);`shared_fake_openai() -> FakeOpenAI`(进程内唯一一个、懒启动、daemon 线程,给 `PORT_FACTORIES` 这种拿不到夹具的地方用);`fake_vector(text: str, *, dim: int, salt: str = "") -> list[float]`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_starling_fakes.py
"""离线假端点本身的钉子:它被 Starling 的真适配器调用时,行为与本步各用例的假设一致。"""
import math

import pytest

from starling_fakes import FakeOpenAI, fake_vector

starling = pytest.importorskip("starling")


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_vectors_follow_token_overlap_and_change_with_the_salt():
    a = fake_vector("我担心预算超支", dim=256)
    assert math.isclose(sum(x * x for x in a), 1.0)
    assert _cos(a, fake_vector("预算超支怎么办", dim=256)) > 0
    assert _cos(a, fake_vector("天气", dim=256)) == 0.0
    assert fake_vector("天气", dim=256, salt="b") != fake_vector("天气", dim=256)


def test_starling_extracts_the_utterance_as_one_belief_through_the_fake(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    with FakeOpenAI(dim=256) as fake:
        llm = starling.make_openai_llm(model="fake-extract", base_url=fake.base_url, timeout_ms=2000)
        emb = starling.make_openai_embedder(model="fake-embed", base_url=fake.base_url, dim=256, timeout_ms=2000)
        mem = starling.Memory.open(str(tmp_path / "pa.db"), agent="pa", llm=llm, embedder=emb)
        res = mem.remember_event("ep#0", "pb：我担心预算超支", interlocutor="pb")
        assert not res.extraction_failed and len(res.statement_ids) == 1
        assert mem.embed_pending("2026-01-01T00:00:01Z").embedded == 1
        hits = mem.recall("预算超支怎么办", k=3, strict=True, include_volatile=True)
        assert [h["row"].object_value for h in hits] == ["我担心预算超支"]
        mem.close()


def test_a_failing_key_and_a_failing_embedder_surface_as_starling_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    with FakeOpenAI(dim=256) as fake:
        fake.fail_keys.add("ep#0")
        llm = starling.make_openai_llm(model="fake-extract", base_url=fake.base_url, timeout_ms=2000)
        emb = starling.make_openai_embedder(model="fake-embed", base_url=fake.base_url, dim=256, timeout_ms=2000)
        mem = starling.Memory.open(str(tmp_path / "pa.db"), agent="pa", llm=llm, embedder=emb)
        res = mem.remember_event("ep#0", "pb：我担心预算超支", interlocutor="pb")
        assert res.extraction_failed and res.failure_category
        fake.fail_embeddings = True
        with pytest.raises(starling.RecallDegradedError):
            mem.recall("预算", k=3, strict=True, include_volatile=True)
        mem.close()
```

```python
# tests/test_factory_cross_plan.py 追加
def test_only_actio_memory_starling_imports_starling():
    """设计二全局约束:全 src/ 只有 actio/memory_starling.py 导入 starling(factory 经它间接用)。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [f"{p.relative_to(src)}:{n}" for p in sorted(src.rglob("*.py"))
                 for n, top in _top_level_imports(p)
                 if top == "starling" and p.relative_to(src).as_posix() != "actio/memory_starling.py"]
    assert offenders == []
```

`tests/test_actio_instance.py` 的 `test_actio_only_imports_itself_simulator_datamodel_and_the_stdlib` 改为:`path.name == "memory_starling.py"` 时另许 `starling`(含子模块)、`os`、`tempfile`(Task 15 的原子替换与 Task 17 的空库摘要要用),其余文件不变。

- [ ] **Step 2: 跑测试确认失败**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_starling_fakes.py tests/test_factory_cross_plan.py -q -p no:cacheprovider`
Expected: FAIL,`ModuleNotFoundError: No module named 'starling_fakes'`;改 `pyproject.toml` 之前 `--extra memory` 本身报 `Extra 'memory' is not defined`

- [ ] **Step 3: 实现**

- `pyproject.toml`:`memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<SHA>"]`,`<SHA>` 填 Task 12 记下的 40 位合并提交;`markers` 加 `"memory_live: 连真抽取与真嵌入端点的用例,只在设了 RISMFORGE_MEMORY_LIVE=1 时跑,不进 CI"`。跑 `uv lock` 更新 `uv.lock`。dev extra 不加 Starling:CI 不装它,本步所有真 Starling 用例都跳过,契约测试只跑替身(设计源第 11 节"Starling 未装时只跑替身")。
- `tests/starling_fakes.py`:`http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)` 在 daemon 线程里 `serve_forever`,先例是 Starling 仓库的 `tests/python/test_evidence_answer_native.py:28-45`。
  - `POST .../embeddings`:`fail_embeddings` 为真回 503;否则对 `input`(字符串或列表)逐条 `fake_vector`,回 `{"data": [{"index": i, "embedding": v}], "model": ...}`。
  - `POST .../chat/completions`:取唯一一条 user 消息的 `content`;按开头判断是哪一遍抽取:以 `"You are an extractor for a Statement-based memory system."` 起头的是信念抽取,其余两遍(`general_fact_prompt.py`、`episodic_prompt.py` 的开头)一律回内容 `"[]"`。信念抽取用 `re.search(r"\[([^\]\n]+)\] ([^：\n]+)：([^\n]+)", content)` 取 key、说话方、原话:key 在 `fail_keys` 里回 500;否则回 `[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"cog-<说话方>","predicate":"responsible_for","object":"<原话>","modality":"BELIEVES","polarity":"POS","nesting_depth":0}]`。回之前 `time.sleep(chat_delay_s)`。每次请求把 `chat_calls` 或 `embedding_calls` 加一(持锁)。
  - `fake_vector`:对 `actio.text.tokens(text)` 的每个词面,`int(sha256((salt + "\0" + tok).encode()).hexdigest()[:8], 16) % dim` 处加 1,再归一化;空词面集合给全零向量。这样余弦只来自共有词面,与替身 `TupleMemory` 的重叠打分同向,规范样例的命中顺序两边可比;没有共有词面时余弦恰为 0。
  - 不发外网请求;`OPENAI_API_KEY` 由各用例 `monkeypatch.setenv` 成占位值(C++ 适配器从环境读它)。
- Starling 的提示词开头与段落里 `[<key>] ` 前缀的形状,实施时先在 Starling 仓库 `grep -n '^EXTRACTION_PROMPT' python/starling/extractor/prompts.py` 与 Task 6 的 payload 拼法核对;对不上就改假端点的判别式,不改适配器。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_starling_fakes.py tests/test_factory_cross_plan.py tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: PASS
Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_starling_fakes.py tests/test_factory_cross_plan.py tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: PASS,`test_starling_fakes.py` 整个文件 skipped

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock
git add tests/starling_fakes.py tests/test_starling_fakes.py tests/test_actio_instance.py tests/test_factory_cross_plan.py
git commit -m "build(memory): 加 memory extra 与离线 OpenAI 兼容假端点,钉住只有 memory_starling 导入 starling"
```

### Task 14: `StarlingMemory`:端口适配器、穷举失败映射、过共享契约

**Files:**
- Create: `src/actio/memory_starling.py`(`StarlingEndpoints`、`StarlingMemory`、`translate_failure`;本任务不含部署管理面,Task 15 加)
- Modify: `tests/test_actio_memory_contract.py`(`PORT_FACTORIES` 加 `"starling"`)
- Modify: `tests/starling_fakes.py`(加 `offline_api_key()` 与 `FakeOpenAI.endpoints()`)
- Test: `tests/test_actio_memory_starling.py`(新)

**Interfaces:**
- Consumes:
  - 第 1 步:`MemoryHit`、`MemoryEvent`、`RememberOutcome`、`MemoryFailureError(message, *, transient)`、`MemoryEventConflict(message)`、`MemoryClosedError(message)`、`MemoryBackend`;`tests/test_actio_memory_contract.py` 的 `PORT_FACTORIES` 与 `make_port`。
  - 第 6 步:`Memory.open(db_path, *, agent, llm, embedder)`、`Memory.recall(query, *, k, strict, include_volatile)`(命中是 `{"row", "score", "content_id"}`)、`Memory.remember_event(key, text, *, interlocutor, epoch="2026-01-01T00:00:00Z") -> RememberResult`(`statement_ids`、`extraction_failed`、`failure_category`、`outcome`)、`Memory.embed_pending(now) -> EmbedStats(embedded, failed)`、`Memory.next_seq()`、`Memory.digest()`、`Memory.close(timeout=)`(超时抛 `TimeoutError`);异常 `starling.RecallDegradedError`、`starling.EventConflict`、`starling.MemoryClosed`。**另需**把语句 UUID 换成内容 id 的公开门面 `Memory.content_ids(statement_ids: list[str]) -> dict[str, str]`(Task 9 只写了 C++ `content_ids` 与绑定 `memory_content_ids`,门面上没有;待裁定,见最终报告)。
  - Task 13:`tests/starling_fakes.py` 的 `FakeOpenAI`、`shared_fake_openai()`。
- Produces:
  - `actio.memory_starling.StarlingEndpoints(extraction_base_url: str, extraction_model: str, embedding_base_url: str, embedding_model: str, embedding_dim: int, timeout_ms: int, max_retries: int)`,frozen dataclass;方法 `make_llm()`、`make_embedder()`。
  - `actio.memory_starling.StarlingMemory(memory, *, close_timeout_s: float = 60.0, on_close: Callable[[], None] | None = None)`,满足 `MemoryBackend`;类方法 `StarlingMemory.open(path: Path, *, persona_id: str, endpoints: StarlingEndpoints, close_timeout_s: float = 60.0) -> StarlingMemory`。
  - `actio.memory_starling.translate_failure(exc: BaseException) -> MemoryFailureError`(第 10 节的表,Task 15 复用)。
  - `actio.memory_starling.EPOCH = "2026-01-01T00:00:00Z"`。
  - `tests/starling_fakes.py`:`offline_api_key() -> AbstractContextManager[None]`(只在块内把 `OPENAI_API_KEY` 设成占位值,退出时还原);`FakeOpenAI.endpoints(*, timeout_ms: int = 2000, max_retries: int = 0) -> StarlingEndpoints`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_memory_contract.py(PORT_FACTORIES 加一项;导入补 pytest 已有、starling_fakes)
from starling_fakes import offline_api_key, shared_fake_openai


def _starling_port(fresh_dir):
    pytest.importorskip("starling")            # 未装时这一参数跳过,契约只跑替身(设计源第 11 节)
    from actio.memory_starling import StarlingMemory
    fresh_dir.mkdir(parents=True)
    with offline_api_key():                      # C++ 适配器在构造时读 OPENAI_API_KEY
        return StarlingMemory.open(fresh_dir / "pa.db", persona_id="pa",
                                   endpoints=shared_fake_openai().endpoints())


PORT_FACTORIES = {
    "tuple": lambda fresh_dir: TupleMemory(),
    "file": lambda fresh_dir: FileTupleStore(fresh_dir).open("pa"),
    "starling": _starling_port,
}
```

```python
# tests/test_actio_memory_starling.py
"""StarlingMemory 的专属用例:失败映射(第 10 节的表)、渲染(4.4 节)、close 的期限。契约用例在共享契约里。"""
import re
import threading

import pytest

from actio.memory_port import MemoryClosedError, MemoryEvent, MemoryEventConflict, MemoryFailureError
from starling_fakes import FakeOpenAI, offline_api_key

starling = pytest.importorskip("starling")
from actio.memory_starling import StarlingMemory, translate_failure  # noqa: E402

EV = MemoryEvent("ep-1#0", "pb：我担心预算超支", "pb")


class FakeStarling:
    """只有 StarlingMemory 用到的门面方法;raise_on[name] 给定时该方法抛它。"""

    def __init__(self, **raise_on):
        self.raise_on = raise_on
        self.closed_with = []

    def _maybe(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def recall(self, query, **kw):
        self._maybe("recall"); return []

    def remember_event(self, key, text, *, interlocutor):
        self._maybe("remember_event")

    def embed_pending(self, now):
        self._maybe("embed_pending"); return starling.EmbedStats(embedded=0, failed=self.raise_on.get("failed", 0))

    def next_seq(self):
        return 0

    def digest(self):
        self._maybe("digest"); return "0" * 64

    def close(self, timeout=None):
        self.closed_with.append(timeout); self._maybe("close")


@pytest.mark.parametrize("exc, cls, transient", [
    (starling.RecallDegradedError("x"), MemoryFailureError, True),
    (starling.EventConflict("x"), MemoryEventConflict, False),
    (starling.MemoryClosed("x"), MemoryClosedError, False),
    (KeyError("ep-1#0"), MemoryFailureError, False),           # abandon_event 未知 key()
    (__import__("sqlite3").OperationalError("disk I/O error"), MemoryFailureError, False),
    (OSError(5, "EIO"), MemoryFailureError, False),
    (RuntimeError("boom"), MemoryFailureError, False),
    (TimeoutError(), MemoryFailureError, False),
])
def test_every_failure_has_a_fixed_transient_value_and_names_only_the_class(exc, cls, transient):
    out = translate_failure(exc)
    assert type(out) is cls and out.transient is transient
    assert type(exc).__name__ in str(out)
    assert "127.0.0.1" not in str(out) and "disk I/O" not in str(out)   # 不带异常原文


def test_a_degraded_recall_is_a_transient_failure_not_an_empty_hit_list():
    port = StarlingMemory(FakeStarling(recall=starling.RecallDegradedError("query embedding failed")))
    with pytest.raises(MemoryFailureError) as info:
        port.recall("预算", limit=3)
    assert info.value.transient is True


def test_embed_failures_raise_transient_even_though_starling_only_counts_them():
    port = StarlingMemory(FakeStarling(failed=2))
    with pytest.raises(MemoryFailureError) as info:
        port.embed_pending()
    assert info.value.transient is True and "2" in str(info.value)


def test_recall_asks_starling_for_strict_volatile_semantic_hits(tmp_path, monkeypatch):
    seen = {}

    class Spy(FakeStarling):
        def recall(self, query, **kw):
            seen.update(kw); return []

    assert StarlingMemory(Spy()).recall("预算", limit=3) == []
    assert seen == {"k": 3, "mode": "semantic", "strict": True, "include_volatile": True}


def test_close_waits_with_its_timeout_once_and_a_timeout_is_non_transient():
    fake = FakeStarling(close=TimeoutError())
    port = StarlingMemory(fake, close_timeout_s=0.5)
    with pytest.raises(MemoryFailureError) as info:
        port.close()
    assert info.value.transient is False and fake.closed_with == [0.5]


def test_real_hits_are_rendered_with_content_ids(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        port = StarlingMemory.open(tmp_path / "pa.db", persona_id="pa", endpoints=fake.endpoints())
        out = port.remember(EV)
        assert out.failed is False and len(out.stored) == 1 and re.fullmatch(r"mem-[0-9a-f]{16}", out.stored[0])
        assert port.embed_pending() == 1
        hits = port.recall("预算超支怎么办", limit=3)
        assert [h.id for h in hits] == list(out.stored)
        assert hits[0].text.startswith("cog-pb responsible_for 我担心预算超支（")
        assert port.recall("天气", limit=3) == []             # 零余弦不算命中
        port.close()


def test_a_failed_extraction_is_counted_not_raised(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        fake.fail_keys.add(EV.key)
        port = StarlingMemory.open(tmp_path / "pa.db", persona_id="pa", endpoints=fake.endpoints())
        out = port.remember(EV)
        assert (out.stored, out.failed) == ((), True) and out.category
        port.close()


def test_close_with_a_call_in_flight_times_out_and_the_port_stays_usable_for_a_second_close(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        fake.chat_delay_s = 1.5
        port = StarlingMemory.open(tmp_path / "pa.db", persona_id="pa", endpoints=fake.endpoints(), close_timeout_s=0.2)
        t = threading.Thread(target=port.remember, args=(EV,)); t.start()
        fake.wait_for_chat_call(timeout_s=2)                 # 抽取请求已发出,remember_event 在途
        with pytest.raises(MemoryFailureError) as info:
            port.close()
        assert info.value.transient is False
        t.join()
        port.close()                                          # 在途调用返回后再关能关上
        with pytest.raises(MemoryClosedError):
            port.digest()
```

(`hits[0].text` 只钉前缀:`modality` 的大小写与 `confidence` 的取值实施时按真 `StatementRow` 读出后改成整串相等;`FakeOpenAI.wait_for_chat_call` 是本任务给假端点加的小钩子:`threading.Event` 在第一次收到 `/chat/completions` 时置位。)

- [ ] **Step 2: 跑测试确认失败**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_actio_memory_starling.py tests/test_actio_memory_contract.py -q -p no:cacheprovider`
Expected: FAIL,`ModuleNotFoundError: No module named 'actio.memory_starling'`;契约用例的 `starling` 参数同一原因报错

- [ ] **Step 3: 实现**

`src/actio/memory_starling.py`,首条语句 `from __future__ import annotations`;导入 `datetime`、`pathlib`、`dataclasses`、`typing`、`actio.memory_port` 与 `starling`(全仓库唯一一处)。

- `StarlingEndpoints.make_llm()`:`starling.make_openai_llm(model=..., base_url=..., timeout_ms=..., max_retries=...)`;`make_embedder()`:`starling.make_openai_embedder(model=..., base_url=..., dim=..., timeout_ms=..., max_retries=...)`。两个公开工厂在 Task 1 已改成无条件覆盖 `max_retries`(C++ 默认是 3,`include/starling/extractor/openai_adapter.hpp:28`;旧的"非零才覆盖"会让配置的 0 静默变成 3 次重试,打破 6.4 节"每次网络调用至多 4 秒"),所以不碰私有的 `starling._core` 配置类。测试加一条 `test_endpoints_with_zero_retries_do_not_retry`:假端点第一次返回 500,断言只收到一次请求。密钥仍只由 C++ 从 `OPENAI_API_KEY` 读。
- `StarlingMemory.open`:`starling.Memory.open(str(path), agent=persona_id, llm=endpoints.make_llm(), embedder=endpoints.make_embedder())`,任何异常经 `translate_failure` 改抛。
- 每个公开方法先查 `self._closed`,为真抛 `MemoryClosedError("memory port is closed")`;再在 `try` 里调门面,`except Exception as exc: raise translate_failure(exc) from None`(`from None`:原文可能含端点地址)。
- `recall(query, *, limit)`:`limit` 不是非负 int(排除 bool)抛 `ActioDataError`;`limit == 0` 直接返回 `[]`;否则 `memory.recall(query, k=limit, mode="semantic", strict=True, include_volatile=True)`,丢掉 `score <= 0.0` 的命中(余弦不为正就是不相似;没有这条,Starling 的 top-k 会把零分行也补满 k 条,契约样例"天气只命中 #4"过不了,待裁定),渲染成 `MemoryHit("mem-" + hit["content_id"][:16], text)`,按 id 去重保序。`text` 照 4.4 节:`f"{row.subject_id} {neg}{row.predicate} {row.object_value}（{row.modality}，置信度 {row.confidence:.2f}）"`,`neg` 在 `polarity` 为负时是"不"(取值先 `grep -n 'polarity' include/starling/retrieval/*.hpp` 确认是 `"neg"` 还是枚举名)。
- `remember(event)`:`res = memory.remember_event(event.key, event.text, interlocutor=event.interlocutor)`(`epoch` 用默认值,即 4.3 节的时钟起点);`res.extraction_failed` 为真返回 `RememberOutcome((), True, res.failure_category or "unknown")`;否则 `ids = memory.content_ids(res.statement_ids)`,返回 `RememberOutcome(tuple("mem-" + ids[s][:16] for s in res.statement_ids), False, None)`。`[key] ` 前缀由 Starling 加,这里只传原文。
- `embed_pending()`:`now = EPOCH + memory.next_seq() 秒`(`datetime` 算,秒级 ISO,带 `Z`);`stats.failed > 0` 抛 `MemoryFailureError(f"embed_pending: {stats.failed} statement(s) failed to embed", transient=True)`;否则返回 `stats.embedded`。
- `digest()` 转发。`close()`:已关为空操作;`memory.close(timeout=self._close_timeout_s)`;成功后置 `_closed` 并调 `on_close`;`TimeoutError` 或其他异常经 `translate_failure` 抛(都是非瞬态),**不**置 `_closed`,所以在途调用返回后可以再关一次(Task 18 的副本收尾靠这一点决定删不删目录)。
- `translate_failure`:按序 `MemoryFailureError` 原样;`starling.RecallDegradedError` → `MemoryFailureError(f"{name}", transient=True)`;`starling.EventConflict` → `MemoryEventConflict(name)`;`starling.MemoryClosed` → `MemoryClosedError(name)`;其余一切(含 `KeyError`、`sqlite3.Error`、`OSError`、`TimeoutError`)→ `MemoryFailureError(f"starling memory failed: {name}", transient=False)`,`name = type(exc).__name__`。消息只有类名(第 10 节)。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_actio_memory_starling.py tests/test_actio_memory_contract.py tests/test_actio_instance.py tests/test_factory_cross_plan.py -q -p no:cacheprovider`
Expected: PASS(契约用例三个参数都跑)
Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_memory_contract.py tests/test_actio_memory_starling.py -q -p no:cacheprovider`
Expected: PASS,`starling` 参数与专属文件 skipped

- [ ] **Step 5: 提交**

```bash
git add src/actio/memory_starling.py
git add tests/test_actio_memory_contract.py tests/test_actio_memory_starling.py tests/starling_fakes.py
git commit -m "feat(actio): StarlingMemory 适配器,召回严格并放宽到 volatile,失败按表映射瞬态,过共享契约"
```

### Task 15: `StarlingDeploymentStore`:部署管理面,`remember_event` 崩溃后重调

**Files:**
- Modify: `src/actio/memory_starling.py`(加 `StarlingDeploymentStore`)
- Test: `tests/test_actio_starling_store.py`(新)

**Interfaces:**
- Consumes:
  - Task 14:`StarlingEndpoints`、`StarlingMemory(memory, *, close_timeout_s, on_close)`、`translate_failure`。
  - 第 1 步:`DeploymentMemoryStore` 协议、`PERSONA_ID_PATTERN`、`ActioDataError`、`RememberOutcome`;FileTupleStore 的管理面语义(`restore_from` 在该人格有打开的端口时抛 `MemoryFailureError(transient=False)`;`backup_to`/`restore_from` 返回逻辑摘要;人格还没有库时等同空库)。
  - 第 6 步:`Memory.remember_event`、`abandon_event(key)`(未知 key 抛 `KeyError`,已放弃再放弃空操作,`committed` 抛 `EventConflict`)、`pending_events() -> list[PendingEvent]`(按 `seq` 升序)、`backup_to(path)`(目标已存在抛 `FileExistsError`)、`digest()`、`close(timeout=)`。
- Produces:
  - `actio.memory_starling.StarlingDeploymentStore(root: Path, *, endpoints: StarlingEndpoints, close_timeout_s: float = 60.0)`,满足 `DeploymentMemoryStore`;库在 `root / f"{persona_id}.db"`(第 5 步约定传入的是 `<记忆根>/deploy`,所以落在 5.3 节的 `deploy/<persona_id>.db`)。
  - `StarlingDeploymentStore.digest_of(path: Path) -> str`:只读地算一个库文件的摘要(Task 16 的解析器与 Task 18 的种子都用;打开、`digest()`、`close`)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_starling_store.py
import sqlite3

import pytest

from actio.errors import ActioDataError
from actio.memory_port import DeploymentMemoryStore, MemoryBackend, MemoryEventConflict, MemoryFailureError
from starling_fakes import FakeOpenAI, offline_api_key

pytest.importorskip("starling")
from actio.memory_starling import StarlingDeploymentStore  # noqa: E402

KEY = "r1/deploy-m-s-0#1"                      # 部署 key 带轮次前缀、含 "/",只进事件 key


@pytest.fixture
def fake():
    with FakeOpenAI(dim=256) as f, offline_api_key():
        yield f


def _store(tmp_path, fake):
    return StarlingDeploymentStore(tmp_path / "deploy", endpoints=fake.endpoints())


def test_the_store_and_its_ports_satisfy_the_protocols_and_live_under_deploy(tmp_path, fake):
    store = _store(tmp_path, fake)
    assert isinstance(store, DeploymentMemoryStore)
    port = store.open("pa")
    assert isinstance(port, MemoryBackend) and (tmp_path / "deploy" / "pa.db").exists()
    assert port.digest() == store.digest("pa")
    port.close()


@pytest.mark.parametrize("bad", ["", "a/b", "../pa", ".hidden", "p a"])
def test_persona_ids_are_checked_before_any_path_is_built(tmp_path, fake, bad):
    store = _store(tmp_path, fake)
    for call in (lambda: store.open(bad), lambda: store.digest(bad),
                 lambda: store.remember_event(bad, KEY, "pb：你好", interlocutor="pb"),
                 lambda: store.backup_to(bad, tmp_path / "x.db")):
        with pytest.raises(ActioDataError, match="persona_id"):
            call()
    assert not (tmp_path / "deploy").exists() or list((tmp_path / "deploy").iterdir()) == []


def test_a_failed_admission_stays_pending_and_abandon_closes_it(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys.add(KEY)
    out = store.remember_event("pa", KEY, "pb：我担心预算超支", interlocutor="pb")
    assert out.failed and out.stored == ()
    assert store.pending_events("pa") == [KEY]
    store.abandon_event("pa", KEY)
    assert store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):
        store.remember_event("pa", KEY, "pb：我担心预算超支", interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", "never-written#0")          # 未知 key:KeyError → 非瞬态()
    assert info.value.transient is False


def test_remember_event_after_a_crash_before_phase_three_completes_without_extracting_again(tmp_path, fake):
    """Review Focus:第三段之前崩溃后重调。Starling 侧补齐不抽取(Task 6);这里钉住适配器原样转发,
    重调返回同一批内容 id、库里只多出事件行的状态变化。"""
    store = _store(tmp_path, fake)
    first = store.remember_event("pa", KEY, "pb：我担心预算超支", interlocutor="pb")
    db = tmp_path / "deploy" / "pa.db"
    with sqlite3.connect(db) as c:                            # 模拟"语句已落、③ 没写成"
        c.execute("UPDATE events SET state='pending', statement_ids_json='[]' WHERE key=?", (KEY,))
    calls = fake.chat_calls
    again = store.remember_event("pa", KEY, "pb：我担心预算超支", interlocutor="pb")
    assert again.stored == first.stored and not again.failed
    assert fake.chat_calls == calls                           # 没有再调抽取
    assert store.pending_events("pa") == []


def test_backup_and_restore_return_logical_digests_and_roll_back(tmp_path, fake):
    store = _store(tmp_path, fake)
    before = store.backup_to("pa", tmp_path / "snap" / "pa.db")     # 人格还没有库:备份空库
    assert before == store.digest("pa")
    store.remember_event("pa", KEY, "pb：我担心预算超支", interlocutor="pb")
    assert store.digest("pa") != before
    assert store.restore_from("pa", tmp_path / "snap" / "pa.db") == before == store.digest("pa")


def test_restore_refuses_while_a_port_is_open_and_leaves_the_library_alone(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store.backup_to("pa", snap)
    port = store.open("pa")
    port.remember(__import__("actio.memory_port", fromlist=["MemoryEvent"]).MemoryEvent(KEY, "pb：你好", "pb"))
    d = store.digest("pa")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", snap)
    assert info.value.transient is False and store.digest("pa") == d
    port.close()
    assert store.restore_from("pa", snap) != d


def test_a_corrupt_library_or_backup_is_a_non_transient_failure(tmp_path, fake):
    store = _store(tmp_path, fake)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", bad)
    assert info.value.transient is False
    assert not (tmp_path / "deploy" / "pa.db").exists() or store.digest("pa")   # 原库不被半截覆盖
```

(`test_remember_event_after_a_crash_before_phase_three...` 里"模拟崩溃"的 SQL 要与 Task 6 的补齐分支一致:实施时先在 Starling 仓库 `grep -n 'crash_before_phase_three' tests/python/test_memory_remember_event.py`,照它造同样的中间状态;Task 6 的做法是先留一条 `pending` 行、再用裸 `remember` 写同一 payload,这里无法调裸 `remember` 时就照它的 SQL 状态改。)

- [ ] **Step 2: 跑测试确认失败**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_actio_starling_store.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'StarlingDeploymentStore' from 'actio.memory_starling'`

- [ ] **Step 3: 实现**

- 每个收 `persona_id` 的方法第一句 `PERSONA_ID_PATTERN.fullmatch`,不合法抛 `ActioDataError(f"persona_id: {persona_id!r}")`(R6),之后才拼 `root / f"{persona_id}.db"`;`root` 在第一次写时 `mkdir(parents=True, exist_ok=True)`。
- `open(persona_id)`:`StarlingMemory.open(path, persona_id=..., endpoints=..., close_timeout_s=...)`,构造时传 `on_close`,把实例内的 `self._open[persona_id]` 计数减一(加一在打开成功后),计数与 FileTupleStore 同样只在实例内,所以组合根只造一个 store(计划 A Task 35 已经如此)。
- `remember_event`、`abandon_event`、`pending_events`、`digest`:各自 `Memory.open(...)` 一个短命连接、调一次、`close(timeout=close_timeout_s)`,异常经 `translate_failure`。不复用端口的连接:管理面由门② 与 MindAgent 在人格库锁下单线程调用(计划 A Task 34),短命连接最简单;端口开着时同库再开一条连接是 SQLite WAL 允许的,门② 在有未提交起点副本时已延后(计划 A Task 34),不会与在途 episode 并发写。`remember_event` 的返回照 Task 14 `StarlingMemory.remember` 同一译法(抽出一个模块私有函数 `_outcome(memory, res)` 两处共用)。`pending_events` 返回 `[p.key for p in memory.pending_events()]`。
- `backup_to(persona_id, path)`:人格还没有库时先 `Memory.open` 建空库;`path.parent.mkdir(parents=True, exist_ok=True)`;`path` 已存在先 `unlink`(起点副本由 MindAgent 管,覆盖是它要的语义;Starling 拒绝已存在的目标);`memory.backup_to(path)`;返回 `digest_of(path)`。
- `restore_from(persona_id, path)`:`self._open.get(persona_id, 0) > 0` 抛 `MemoryFailureError("restore_from: persona has an open port", transient=False)`;`d = digest_of(path)`(坏文件在这里抛,原库不动);把 `path` 经 `Memory.open(path).backup_to(<库>.restore-partial)` 复制成自洽单文件,`os.replace` 到库路径,并删掉库旁残留的 `-wal`、`-shm`;返回 `d`。复制走 Starling 的 `backup_to`,不直接 `shutil.copy`:起点副本可能是带 WAL 的库。
- `digest_of(path)`:`path` 不存在抛 `MemoryFailureError("digest_of: no such library", transient=False)`;否则把 `path`(连同同名 `-wal` 文件,有的话)复制进 `tempfile.TemporaryDirectory()`,在副本上 `Memory.open(..., agent="digest", llm=None)`、`digest()`、`close`。不在原地打开:`Memory.open` 会跑迁移、写 WAL,而种子库只读(5.3 节)、起点副本是提交点,算摘要不能改它们的字节。Starling 的 `Memory.open` 会对不存在的路径建新库,所以必须先查存在。`agent` 不进摘要(摘要按 tenant 全行算,Task 9),实施时用两个不同 `agent` 打开同一库各算一次确认相等。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_actio_starling_store.py tests/test_actio_memory_starling.py tests/test_actio_memory_contract.py tests/test_actio_file_tuple_store.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/actio/memory_starling.py tests/test_actio_starling_store.py
git commit -m "feat(actio): StarlingDeploymentStore 部署管理面,库在 deploy/<persona_id>.db,人格 id 拼路径前校验"
```

### Task 16: `factory/memory_config.py`:树外记忆配置、密钥、根目录归属与 Starling 安装来源

**Files:**
- Create: `src/factory/memory_config.py`
- Test: `tests/test_factory_memory_config.py`(新)

**Interfaces:**
- Consumes: `factory.errors.FactoryConfigError`(`factory.cli.main` 把它译成退出码 2,`cli.py:162-164`);`factory.configio.key_names`;`factory.eval_config` 的写法(`_check_private`、`_check_absolute` 照抄一份,不导入它的私有函数)。
- Produces:
  - `MEMORY_CONFIG_ENV = "RISMFORGE_MEMORY_CONFIG"`、`API_KEY_ENV = "OPENAI_API_KEY"`、`STARLING_DIST = "starling-memory"`、`REQUIRED_KEYS = ("root", "extraction_base_url", "extraction_model", "embedding_base_url", "embedding_model", "embedding_dim")`、`OPTIONAL_KEYS = ("timeout_ms", "max_retries")`、`DEFAULT_TIMEOUT_MS = 4000`、`DEFAULT_MAX_RETRIES = 0`。
  - `MemoryConfig(root: Path, extraction_base_url: str, extraction_model: str, embedding_base_url: str, embedding_model: str, embedding_dim: int, timeout_ms: int = 4000, max_retries: int = 0)`,frozen dataclass;两个 `*_base_url` 存规范化后的值。
  - `read_memory_config(*, env: Mapping[str, str] | None = None, repo_root: Path) -> MemoryConfig | None`:变量未设置或为空串返回 `None`(替身);其余不合格一律 `FactoryConfigError`。
  - `normalize_base_url(key: str, value: object) -> str`。
  - `check_api_key(env: Mapping[str, str]) -> None`。
  - `claim_memory_root(root: Path, repo_root: Path, *, read_back_timeout_s: float = 2.0) -> None`。
  - `starling_install_source(*, distribution: importlib.metadata.Distribution | None = None) -> str`:`"git:<commit_id>"` 或 `"record:<sha256>"`;未安装、editable、没有 `RECORD` 都抛 `FactoryConfigError`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_memory_config.py
"""树外记忆配置(设计二 5.1、5.3 节):报错只写键名、路径与变量名,不回显端点地址与密钥。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from factory.errors import FactoryConfigError
from factory.memory_config import (
    MEMORY_CONFIG_ENV, MemoryConfig, check_api_key, claim_memory_root, normalize_base_url,
    read_memory_config, starling_install_source,
)

SRC = Path(__file__).resolve().parents[1] / "src"
SECRET_URL = "https://alice:s3cret@embed.example/v1"


def _payload(tmp_path, **over):
    data = {
        "root": str(tmp_path / "mem"),
        "extraction_base_url": "https://Extract.Example/v1/",
        "extraction_model": "gpt-4o-mini",
        "embedding_base_url": "https://embed.example/v1",
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not ...}


def _write(tmp_path, payload, mode=0o600):
    path = tmp_path / "memory.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _read(tmp_path, path):
    return read_memory_config(env={MEMORY_CONFIG_ENV: str(path)}, repo_root=tmp_path / "repo")


def test_an_unset_variable_means_the_stand_in(tmp_path):
    assert read_memory_config(env={}, repo_root=tmp_path) is None
    assert read_memory_config(env={MEMORY_CONFIG_ENV: ""}, repo_root=tmp_path) is None


def test_a_clean_config_is_read_with_defaults_and_normalized_urls(tmp_path):
    cfg = _read(tmp_path, _write(tmp_path, _payload(tmp_path)))
    assert cfg == MemoryConfig(
        root=tmp_path / "mem", extraction_base_url="https://extract.example/v1",
        extraction_model="gpt-4o-mini", embedding_base_url="https://embed.example/v1",
        embedding_model="text-embedding-3-small", embedding_dim=1536, timeout_ms=4000, max_retries=0,
    )


@pytest.mark.parametrize("raw, want", [
    ("HTTPS://API.Example.COM/v1/", "https://api.example.com/v1"),
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
    ("https://api.example.com", "https://api.example.com"),
    ("https://api.example.com/Path/Keeps/Case/", "https://api.example.com/Path/Keeps/Case"),
])
def test_base_urls_are_normalized(raw, want):
    assert normalize_base_url("embedding_base_url", raw) == want


@pytest.mark.parametrize("raw", [SECRET_URL, "https://token@embed.example/v1", "ftp://embed.example/v1",
                                 "embed.example/v1", "https:///v1", "https://embed.example/v1?k=1", "", 3])
def test_bad_base_urls_are_refused_without_echoing_the_value(raw):
    with pytest.raises(FactoryConfigError) as info:
        normalize_base_url("embedding_base_url", raw)
    assert "embedding_base_url" in str(info.value)
    assert "s3cret" not in str(info.value) and "alice" not in str(info.value) and "token" not in str(info.value)


@pytest.mark.parametrize("change, needle", [
    ({"root": ...}, "missing ['root']"),
    ({"extra": 1}, "unknown ['extra']"),
    ({"root": "relative/mem"}, "root must be an absolute path"),
    ({"embedding_dim": 0}, "embedding_dim"),
    ({"embedding_dim": True}, "embedding_dim"),
    ({"timeout_ms": 0}, "timeout_ms"),
    ({"max_retries": -1}, "max_retries"),
    ({"extraction_model": ""}, "extraction_model"),
    ({"embedding_base_url": SECRET_URL}, "user:password"),
])
def test_bad_keys_are_refused(tmp_path, change, needle):
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, **change)))
    assert needle in str(info.value) and "s3cret" not in str(info.value)


def test_the_file_must_be_private_absolute_present_and_outside_the_repo(tmp_path):
    good = _payload(tmp_path)
    with pytest.raises(FactoryConfigError, match="chmod 600"):
        _read(tmp_path, _write(tmp_path, good, mode=0o644))
    with pytest.raises(FactoryConfigError, match="absolute"):
        read_memory_config(env={MEMORY_CONFIG_ENV: "memory.yaml"}, repo_root=tmp_path / "repo")
    with pytest.raises(FactoryConfigError, match="does not exist"):
        _read(tmp_path, tmp_path / "missing.yaml")
    inside = tmp_path / "repo" / "memory.yaml"
    inside.parent.mkdir()
    inside.write_text(yaml.safe_dump(good), encoding="utf-8"); os.chmod(inside, 0o600)
    with pytest.raises(FactoryConfigError, match="outside the control-plane repository"):
        _read(tmp_path, inside)
    with pytest.raises(FactoryConfigError, match="root must live outside"):
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, root=str(tmp_path / "repo" / "mem"))))


def test_the_api_key_must_be_non_empty_and_only_its_name_is_reported():
    check_api_key({"OPENAI_API_KEY": "sk-x"})
    for env in ({}, {"OPENAI_API_KEY": ""}, {"OPENAI_API_KEY": "  "}):
        with pytest.raises(FactoryConfigError) as info:
            check_api_key(env)
        assert str(info.value).startswith("OPENAI_API_KEY ")


def test_the_root_is_claimed_once_and_a_second_repo_is_refused_naming_both_paths(tmp_path):
    root, a, b = tmp_path / "mem", tmp_path / "repo-a", tmp_path / "repo-b"
    claim_memory_root(root, a)
    claim_memory_root(root, a)                                 # 同一仓库再来:读回相同即放行
    assert (root / ".owner").read_text(encoding="utf-8") == f"{a.resolve()}\n"
    assert (root / ".owner").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FactoryConfigError) as info:
        claim_memory_root(root, b)
    assert str(a.resolve()) in str(info.value) and str(b.resolve()) in str(info.value)


_CLAIM = """
import sys, time
from pathlib import Path
from factory.errors import FactoryConfigError
from factory.memory_config import claim_memory_root
go = Path(sys.argv[3])
while not go.exists():
    time.sleep(0.005)
try:
    claim_memory_root(Path(sys.argv[1]), Path(sys.argv[2]))
except FactoryConfigError:
    sys.exit(2)          # 与 factory.cli.main 对 FactoryConfigError 的译法相同
"""


@pytest.mark.slow
def test_two_repos_racing_for_a_fresh_root_leave_exactly_one_owner(tmp_path):
    """Review Focus:记忆根目录被另一个仓库占用。两个子进程同时首次使用,只有一个创建 .owner。"""
    root, go = tmp_path / "mem", tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", _CLAIM, str(root), str(tmp_path / name), str(go)], env=env)
             for name in ("repo-a", "repo-b")]
    go.touch()
    codes = sorted(p.wait(timeout=30) for p in procs)
    assert codes == [0, 2]
    owner = (root / ".owner").read_text(encoding="utf-8").strip()
    assert owner in {str((tmp_path / n).resolve()) for n in ("repo-a", "repo-b")}


class _Dist:
    def __init__(self, files):
        self.files_ = files

    def read_text(self, name):
        return self.files_.get(name)


def test_install_source_comes_from_the_git_commit_or_the_record():
    commit = "a" * 40
    git = _Dist({"direct_url.json": json.dumps({"url": "https://github.com/x/starling", "vcs_info": {"vcs": "git", "commit_id": commit}})})
    assert starling_install_source(distribution=git) == f"git:{commit}"
    record = "starling/__init__.py,sha256=AAA,10\nstarling/_core.so,sha256=BBB,99\nstarling-0.0.1.dist-info/RECORD,,\n"
    a = starling_install_source(distribution=_Dist({"direct_url.json": json.dumps({"url": "file:///src", "dir_info": {}}), "RECORD": record}))
    b = starling_install_source(distribution=_Dist({"RECORD": "\n".join(reversed(record.strip().split("\n")))}))
    assert a == b and a.startswith("record:") and len(a) == len("record:") + 64
    assert starling_install_source(distribution=_Dist({"RECORD": record.replace("BBB", "CCC")})) != a


def test_an_editable_or_unknown_install_is_refused():
    editable = _Dist({"direct_url.json": json.dumps({"url": "file:///src", "dir_info": {"editable": True}})})
    with pytest.raises(FactoryConfigError, match="editable"):
        starling_install_source(distribution=editable)
    with pytest.raises(FactoryConfigError, match="RECORD"):
        starling_install_source(distribution=_Dist({}))


def test_a_missing_starling_is_refused_with_the_install_hint(monkeypatch):
    """Review Focus:设了记忆配置但 Starling 未安装 → 装配期退出码 2,信息说明 pip install '.[memory]'。"""
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    with pytest.raises(FactoryConfigError) as info:
        starling_install_source()
    assert "pip install '.[memory]'" in str(info.value) and MEMORY_CONFIG_ENV in str(info.value)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_config.py -q -p no:cacheprovider`
Expected: FAIL,`ModuleNotFoundError: No module named 'factory.memory_config'`

- [ ] **Step 3: 实现**

- 文件头 docstring 说明与 `eval_config.py` 同一做法(5.1 节、F15);首条语句 `from __future__ import annotations`。不导入 `starling`(全局约束:只有 `actio/memory_starling.py` 导入它),安装信息一律经 `importlib.metadata` 读。
- `read_memory_config`:顺序与 `read_eval_config` 相同:变量为空 → `None`;不是绝对路径;不存在;`_check_private`(符号链接、非普通文件、`S_IMODE & 0o077` 非零都拒绝,文字含 `chmod 600`);配置文件自己的 `resolve()` 落在 `repo_root.resolve()` 下拒绝("must live outside the control-plane repository",评测配置没有这一条,5.1 节要求有);YAML 不是映射;缺键多键按 `f"{path}: missing {key_names(missing)}, unknown {key_names(unknown)}"`。`root` 照 `_check_absolute`,文字 `"memory config: root must be an absolute path"` / `"memory config: root must live outside the control-plane repository"`;比较前对 `root` 也 `resolve()`,防符号链接绕进仓库。两个模型名是非空 `str`;`embedding_dim` 是正 int、`timeout_ms` 是正 int、`max_retries` 是非负 int,三者都排除 `bool`。
- `normalize_base_url`:不是非空 `str` → `f"memory config: {key} must be a non-empty URL"`;`urllib.parse.urlsplit`;`username` 或 `password` 不为 `None`(即 netloc 里有 `@`)→ `f"memory config: {key} must not carry user:password@ (value not shown)"`;scheme 不是 `http`/`https`、没有主机、带 query 或 fragment → `f"memory config: {key} must be an http(s) endpoint URL without query or fragment (value not shown)"`(Starling 在它后面直接拼 `/chat/completions` 与 `/embeddings`,带 query 的地址拼出来是错的;这一条是本计划加的,不在 5.1 节键表里)。返回 `f"{scheme.lower()}://{hostname.lower()}{':' + str(port) if port else ''}{path.rstrip('/')}"`。所有报错都不含取值。
- `check_api_key`:`env.get("OPENAI_API_KEY", "").strip()` 为空 → `FactoryConfigError("OPENAI_API_KEY is empty or unset; the Starling memory reads its key from it")`。
- `claim_memory_root`:`root.mkdir(parents=True, exist_ok=True)`;`me = f"{repo_root.resolve()}\n"`;`os.open(root / ".owner", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`,写 `me`、`os.fsync`、关闭,返回;`FileExistsError` 时读回:内容为空就每 20 毫秒再读,至多 `read_back_timeout_s`(对方刚建好还没写完的窗口),仍空 → `FactoryConfigError(f"memory root {root}: .owner is empty; another repository may be claiming it")`;读到的与 `me` 不同 → `FactoryConfigError(f"memory root {root} belongs to {theirs.strip()}, not {repo_root.resolve()}")`(第 10 节"信息写两个路径")。
- `starling_install_source`:`importlib.metadata.distribution(STARLING_DIST)`,`PackageNotFoundError` → `FactoryConfigError(f"{MEMORY_CONFIG_ENV} is set but Starling is not installed; run pip install '.[memory]' (or uv sync --extra memory)")`。`direct_url.json` 有 `dir_info.editable` 为真 → `FactoryConfigError("Starling is an editable install; its code can change under a running directive, so install it from git or a wheel (pip install '.[memory]')")`;有 `vcs_info.commit_id` → `f"git:{commit_id}"`;其余(非 editable 的源码目录、wheel)读 `RECORD`:`csv.reader` 逐行取 `(path, hash)`,丢掉 hash 为空的行(`RECORD` 自身、无哈希的 `.pyc`),按 path 排序后 `json.dumps(..., separators=(",", ":"))` 取 sha256,返回 `f"record:{hex}"`;没有 `RECORD` → `FactoryConfigError("Starling's install has no RECORD; cannot identify the installed code")`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_config.py tests/test_factory_eval_config.py tests/test_factory_cross_plan.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_config.py tests/test_factory_memory_config.py
git commit -m "feat(factory): 读树外记忆配置,校验密钥变量、根目录归属与 Starling 安装来源"
```

### Task 17: Starling 的记忆运行时身份与 `memory_resolver`

**Files:**
- Modify: `src/factory/memory_runtime.py`(第 4、5 步建的模块;加 `starling_memory_runtime`、`starling_memory_resolver`、`StarlingLayout`)
- Modify: `src/actio/memory_starling.py`(加 `empty_digest() -> str` 与 `extraction_prompts_digest() -> str`)
- Test: `tests/test_factory_memory_runtime.py`(追加)

**Interfaces:**
- Consumes:
  - Task 15:`StarlingDeploymentStore.digest_of(path)`;Task 14:`StarlingEndpoints`。
  - Task 16:`MemoryConfig`、`starling_install_source()`。
  - 第 4 步:`factory.memory_runtime.STAND_IN_MEMORY_RUNTIME`(五个键)、`stand_in_memory_resolver(seed, persona_ids)` 的形状;计划 A Task 25 的约定:解析器抛 `DirectiveFailedError` 时原因原样保留,其余异常只报类名;解析后再跑 `validate_training_config`,runtime 键集合要等于 5.2 节五个键。
  - Starling:`starling.__version__`、`starling.ExtractionConfig`(frozen dataclass,三段提示词与阈值都是字段,`python/starling/extractor/config.py:21-50`)。
- Produces:
  - `actio.memory_starling.empty_digest() -> str`:在临时目录里新建一个空库算出的摘要(进程内缓存)。
  - `actio.memory_starling.extraction_prompts_digest() -> str`:`dataclasses.asdict(ExtractionConfig())` 规范化 JSON 的 sha256。
  - `factory.memory_runtime.StarlingLayout(root: Path)`,frozen dataclass:`deploy_dir() -> Path`、`seed(seed_id, persona_id) -> Path`(`root/"seeds"/seed_id/f"{persona_id}.db"`)、`runs_dir() -> Path`、`session_copy(run_id, session_id, slot_id) -> Path`、`client_copy(run_id, episode_id, slot_id) -> Path`(`root/"runs"/run_id/"client"/episode_id/f"{slot_id}.db"`);每段名字先过 `simulator.realtime.validation.validate_episode_id`(同一白名单:`[A-Za-z0-9._-]{1,128}` 且不是 `.`、`..`),人格 id 过 `PERSONA_ID_PATTERN`,不合法抛 `ValueError`(`seed_id` 不合法时是 `DirectiveFailedError`,见下)。
  - `factory.memory_runtime.starling_memory_runtime(config: MemoryConfig, *, version: str, install_source: str, prompts_digest: str) -> dict`:5.2 节五个键,值见下。
  - `factory.memory_runtime.starling_memory_resolver(layout: StarlingLayout, runtime: dict) -> Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_memory_runtime.py 追加
import copy
from pathlib import Path

from controlplane.daemon import DirectiveFailedError
from factory.memory_config import MemoryConfig
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME, StarlingLayout, starling_memory_runtime


def _cfg(tmp_path, **over):
    base = dict(root=tmp_path / "mem", extraction_base_url="https://extract.example/v1", extraction_model="m-x",
                embedding_base_url="https://embed.example/v1", embedding_model="m-e", embedding_dim=256,
                timeout_ms=4000, max_retries=0)
    base.update(over)
    return MemoryConfig(**base)


def test_the_starling_runtime_identity_has_the_five_keys_and_no_secrets(tmp_path):
    rt = starling_memory_runtime(_cfg(tmp_path), version="0.0.1", install_source="git:" + "a" * 40, prompts_digest="p" * 64)
    assert set(rt) == set(STAND_IN_MEMORY_RUNTIME)
    assert rt == {
        "backend": "starling",
        "starling_version": "0.0.1+git:" + "a" * 40,
        "extraction": {"base_url": "https://extract.example/v1", "model": "m-x", "timeout_ms": 4000,
                       "max_retries": 0, "prompts": "p" * 64},
        "embedding": {"base_url": "https://embed.example/v1", "model": "m-e", "dim": 256,
                      "timeout_ms": 4000, "max_retries": 0},
        "trace_schema": "actio.trace.v2",
    }
    assert "root" not in repr(rt) and str(tmp_path) not in repr(rt)        # 根目录路径不进哈希


@pytest.mark.parametrize("change", [
    {"embedding_model": "m-e2"}, {"embedding_dim": 512}, {"embedding_base_url": "https://other.example/v1"},
    {"extraction_model": "m-x2"}, {"timeout_ms": 3000}, {"max_retries": 1},
])
def test_any_endpoint_change_changes_the_identity(tmp_path, change):
    a = starling_memory_runtime(_cfg(tmp_path), version="0.0.1", install_source="git:x", prompts_digest="p")
    b = starling_memory_runtime(_cfg(tmp_path, **change), version="0.0.1", install_source="git:x", prompts_digest="p")
    assert a != b


@pytest.mark.parametrize("bad", ["..", "a/b", "", "x" * 129])
def test_layout_refuses_unsafe_segments(tmp_path, bad):
    with pytest.raises(ValueError):
        StarlingLayout(tmp_path).client_copy("run-1", bad, "a")
    with pytest.raises(ValueError):
        StarlingLayout(tmp_path).session_copy(bad, "sess-1", "a")


def test_layout_paths_follow_section_5_3(tmp_path):
    lay = StarlingLayout(tmp_path)
    assert lay.seed("s1", "pa") == tmp_path / "seeds" / "s1" / "pa.db"
    assert lay.session_copy("run-1", "sess-1", "b") == tmp_path / "runs" / "run-1" / "sess-1" / "b.db"
    assert lay.client_copy("run-1", "run-1-grpo0-s1-r0", "a") == tmp_path / "runs" / "run-1" / "client" / "run-1-grpo0-s1-r0" / "a.db"
    assert lay.deploy_dir() == tmp_path / "deploy"


starling_only = pytest.mark.skipif(__import__("importlib.util").util.find_spec("starling") is None,
                                   reason="Starling 未安装")


@starling_only
def test_the_resolver_digests_seed_libraries_and_empty_starts(tmp_path):
    from actio.memory_starling import StarlingDeploymentStore, empty_digest
    from factory.memory_runtime import starling_memory_resolver
    from starling_fakes import FakeOpenAI, offline_api_key
    lay = StarlingLayout(tmp_path)
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        seeds = StarlingDeploymentStore(tmp_path / "seeds" / "s1", endpoints=fake.endpoints())
        seeds.remember_event("pa", "seed#0", "pb：我担心预算超支", interlocutor="pb")
        seeded = seeds.digest("pa")
        before = (tmp_path / "seeds" / "s1" / "pa.db").read_bytes()
        resolve = starling_memory_resolver(lay, {"backend": "starling"})
        digests, rt = resolve("s1", ("pa", "pb"))
        assert digests == {"pa": seeded, "pb": empty_digest()}    # 种子里没有的人格从空库起
        assert rt == {"backend": "starling"} and rt is not resolve("s1", ("pa",))[1]
        assert (tmp_path / "seeds" / "s1" / "pa.db").read_bytes() == before   # 种子只读
        assert resolve(None, ("pa",)) == ({"pa": empty_digest()}, {"backend": "starling"})


@starling_only
def test_a_missing_or_unsafe_seed_is_a_readable_t2(tmp_path):
    from factory.memory_runtime import starling_memory_resolver
    resolve = starling_memory_resolver(StarlingLayout(tmp_path), {"backend": "starling"})
    with pytest.raises(DirectiveFailedError, match="memory.seed 'nope' has no directory"):
        resolve("nope", ("pa",))
    with pytest.raises(DirectiveFailedError, match="memory.seed"):
        resolve("../deploy", ("pa",))


@starling_only
def test_a_changed_embedding_vector_changes_the_seed_digest(tmp_path):
    """Review Focus:嵌入端点换了向量 → 起点摘要变 → config_hash 变 → 续训升 T3(T3 本身由 计划 A Task 25 的
    test_the_resolved_memory_is_part_of_the_config_hash 钉住;这里钉住前半段:同模型名、不同向量,摘要不同)。"""
    from actio.memory_starling import StarlingDeploymentStore
    from starling_fakes import FakeOpenAI, offline_api_key
    out = []
    for salt in ("", "v2"):
        with FakeOpenAI(dim=256) as fake, offline_api_key():
            fake.salt = salt
            store = StarlingDeploymentStore(tmp_path / salt / "seeds", endpoints=fake.endpoints())
            store.remember_event("pa", "seed#0", "pb：我担心预算超支", interlocutor="pb")
            store.open("pa").embed_pending()
            out.append(store.digest("pa"))
    assert out[0] != out[1]
```

(`test_a_changed_embedding_vector_changes_the_seed_digest` 里 `store.open("pa").embed_pending()` 之后要 `close()`;实施时写成 `with` 不了就两行。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_runtime.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'StarlingLayout' from 'factory.memory_runtime'`

- [ ] **Step 3: 实现**

- `starling_memory_runtime`:返回上面测试里的形状。`starling_version = f"{version}+{install_source}"`(`version` 由调用方传 `starling.__version__` 的值,`install_source` 是 Task 16 的 `git:` 或 `record:` 串,5.2 节"`__version__` 加安装来源")。两个 `base_url` 用配置里已规范化的值;`trace_schema` 取 `actio.trace.TRACE_SCHEMA_V2`。`root` 不进身份:同一份库搬了目录不是另一个实验,而路径进了哈希会让换机即 T3。
- 本模块不 `import starling`:`version` 与 `prompts_digest` 由 Task 19 的装配函数从 `actio.memory_starling` 取(那里导入 `starling`),再传进来;`actio.memory_starling` 加 `starling_version() -> str`(返回 `starling.__version__`),与 `empty_digest`、`extraction_prompts_digest` 并列。
- `extraction_prompts_digest`:`hashlib.sha256(json.dumps(dataclasses.asdict(starling.ExtractionConfig()), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()`。本仓库从不传 `extraction=`,所以默认配置就是实际在用的配置;提示词或阈值随 Starling 升级变化,身份就变。
- `starling_memory_resolver(layout, runtime)` 返回的闭包 `resolve(seed, persona_ids)`:
  - `seed is None`:每个人格都是 `empty_digest()`。
  - 否则先 `validate_episode_id(seed)`,不合格抛 `DirectiveFailedError(f"memory.seed {seed!r} is not a safe directory name")`;`layout.root/"seeds"/seed` 不是目录抛 `DirectiveFailedError(f"memory.seed {seed!r} has no directory under the memory root's seeds/")`(只写种子 id,不写根目录绝对路径)。每个人格:`seed(seed, pid)` 存在就 `StarlingDeploymentStore.digest_of(path)`,不存在就 `empty_digest()`。种子里没有某个人格的库表示该人格从空库起,与 `seed: null` 对单个人格的含义相同。
  - 返回 `(digests, copy.deepcopy(runtime))`。打不开、迁移失败、摘要算不出的异常不接,计划 A Task 25 的 `_resolve_memory` 只报类名、判 T2(第 10 节)。
- 种子库怎么产生不在本计划里(5.3 节只规定位置与只读);实施时在 `memory_runtime.py` 的模块 docstring 写一句"种子库由人放进 `seeds/<seed_id>/`,本仓库只读"。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_factory_memory_runtime.py tests/test_mt_dispatch.py -q -p no:cacheprovider -k "memory"`
Expected: PASS
Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_runtime.py -q -p no:cacheprovider`
Expected: PASS,三条 `starling_only` 用例 skipped

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/actio/memory_starling.py tests/test_factory_memory_runtime.py
git commit -m "feat(factory): Starling 的记忆运行时身份与按种子算起点摘要的解析器"
```

### Task 18: 训练路的私有副本:对手位按会话、训练位按 rollout,终态即删

**Files:**
- Modify: `src/factory/memory_runtime.py`(加 `TrainingCopies`)
- Modify: `src/factory/agent.py`(`AgentRuntime.__init__` 加 keyword-only `training_copies`;`_backends(info)` 里 计划 A Task 17 造对手位记忆那一行)
- Modify: `src/factory/interact_policy.py`(`make_policy_factory` 加 keyword-only `training_copies`;`scope` 里登记会话那段;计划 A Task 21 的 `policy_for_rollout`)
- Test: `tests/test_factory_training_copies.py`(新)

**Interfaces:**
- Consumes:
  - Task 17:`StarlingLayout`(`seed`、`session_copy`、`client_copy`、`runs_dir`);Task 15 的模块私有复制函数 `_copy_library(src: Path, dest: Path)`(Task 15 `digest_of` 里"连同 `-wal` 复制"那一步,本任务把它提成 `actio.memory_starling.copy_library` 公开名);Task 14 `StarlingMemory.open(path, *, persona_id, endpoints, close_timeout_s)`、`StarlingEndpoints`。
  - 第 3 步:`memory_role_backend(inner, *, memory, episode_id, interlocutors)`、`MemoryTranslatingBackend.close()`(close 失败抛 `FactoryMemoryFailureError`);计划 A Task 17 的 `AgentRuntime._backends(info) -> SessionResources`(`closers = (*每个对手位的 backend.close, drop)`);计划 A Task 21 的 `policy_for_rollout(episode_id)`;计划 A Task 15 的 `_session_runs`。
  - 第 2 步:`SessionInfo(scenario, assignment, personas, client_slot, session_id, episode_id)`;`SessionResources.close()` 逐个跑 closer、各自 `try`、失败只记 warning;join 超时的资源不 close(滞留表),所以 closer 跑起来时在途后端调用都已返回。
- Produces:
  - `factory.memory_runtime.TrainingCopies(layout: StarlingLayout, endpoints: StarlingEndpoints, *, close_timeout_s: float = 60.0)`,线程安全(一把锁护绑定表),方法:
    - `bind(run_id: str, seed: str | None) -> None`、`unbind(run_id: str) -> None`;`seed_of(run_id) -> str | None`(没绑定抛 `KeyError`)
    - `open_session_copy(run_id, session_id, slot_id, persona_id) -> StarlingMemory`
    - `open_client_copy(run_id, episode_id, slot_id, persona_id) -> StarlingMemory`(同名目录先删后建)
    - `release(ports: Sequence[object], directory: Path) -> bool`:逐个 `close()`,全部成功才 `shutil.rmtree(directory)` 并返回真;任一 close 抛异常,记 `logger.warning("memory copy %s kept: a port did not close (%s)", directory.name, 类名)`、不删、返回假,并重抛第一个异常
    - `session_dir(run_id, session_id) -> Path`、`client_dir(run_id, episode_id) -> Path`
    - `sweep() -> int`:删 `layout.runs_dir()` 下的每个子项,返回删了几项;`deploy/`、`seeds/`、`snapshots/` 不碰
  - `AgentRuntime(store, opponent_lines, *, ..., training_copies: TrainingCopies | None = None)`。
  - `make_policy_factory(*, ..., training_copies: TrainingCopies | None = None)`。
  - `training_copies is None`(替身)时两处行为与第 3 步完全相同。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_training_copies.py
"""训练路副本(设计二 5.3、6.2、7.2 节):副本从同一起点出发、目录名里有 session_id、终态即删,
端口没关上就不删;清扫只动 runs/。"""
import logging
import threading

import pytest

pytest.importorskip("starling")
from actio.memory_port import MemoryEvent, MemoryFailureError  # noqa: E402
from actio.memory_starling import StarlingDeploymentStore, empty_digest  # noqa: E402
from factory.memory_runtime import StarlingLayout, TrainingCopies  # noqa: E402
from starling_fakes import FakeOpenAI, offline_api_key  # noqa: E402

EV = MemoryEvent("run-1-grpo0-s1-r0#1", "pb：我担心预算超支", "pb")


@pytest.fixture
def env(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        lay = StarlingLayout(tmp_path / "mem")
        seeds = StarlingDeploymentStore(tmp_path / "mem" / "seeds" / "s1", endpoints=fake.endpoints())
        seeds.remember_event("pa", "seed#0", "pb：人手也不够", interlocutor="pb")
        yield lay, TrainingCopies(lay, fake.endpoints(), close_timeout_s=0.2), fake, seeds.digest("pa")


def test_copies_of_one_group_start_from_the_same_seed_and_leave_the_seed_alone(env):
    lay, copies, _, seeded = env
    copies.bind("run-1", "s1")
    before = lay.seed("s1", "pa").read_bytes()
    a = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    b = copies.open_client_copy("run-1", "run-1-grpo0-s1-r1", "a", "pa")
    c = copies.open_session_copy("run-1", "sess-1", "b", "pb")              # 种子里没有 pb:空库
    assert a.digest() == b.digest() == seeded and c.digest() == empty_digest()
    a.remember(EV)
    assert b.digest() == seeded                                                # 两份互不可见
    assert lay.client_copy("run-1", "run-1-grpo0-s1-r0", "a").exists()
    assert lay.session_copy("run-1", "sess-1", "b").exists()                  # session_id 在目录名里
    assert lay.seed("s1", "pa").read_bytes() == before
    for p in (a, b, c):
        p.close()


def test_release_removes_the_directory_only_after_every_port_closed(env):
    lay, copies, _, _ = env
    copies.bind("run-1", None)
    p = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    q = copies.open_session_copy("run-1", "sess-1", "c", "pc")
    assert copies.release([p, q], copies.session_dir("run-1", "sess-1")) is True
    assert not copies.session_dir("run-1", "sess-1").exists()


def test_a_copy_whose_starling_call_has_not_returned_is_not_deleted(env, caplog):
    """Review Focus:副本在 Starling 调用未返回时被删。close(timeout) 等不到在途调用 → 不删目录,
    重抛非瞬态失败;调用返回后再 release 才删。"""
    lay, copies, fake, _ = env
    copies.bind("run-1", None)
    port = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    fake.chat_delay_s = 1.0
    t = threading.Thread(target=port.remember, args=(EV,)); t.start()
    fake.wait_for_chat_call(timeout_s=2)
    d = copies.session_dir("run-1", "sess-1")
    with caplog.at_level(logging.WARNING, logger="factory.memory_runtime"), pytest.raises(MemoryFailureError):
        copies.release([port], d)
    assert d.exists() and "kept" in caplog.text
    t.join()
    assert copies.release([port], d) is True and not d.exists()


def test_a_client_retry_recreates_its_copy_from_the_start(env):
    lay, copies, _, seeded = env
    copies.bind("run-1", "s1")
    first = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    first.remember(EV); first.close()                     # 上一次尝试:副本留着(close 后才轮到 release)
    again = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    assert again.digest() == seeded
    again.close()


def test_sweep_only_empties_runs(env):
    lay, copies, _, _ = env
    copies.bind("run-1", None)
    copies.open_session_copy("run-1", "sess-1", "b", "pb").close()
    (lay.root / "snapshots" / "r1").mkdir(parents=True)
    assert copies.sweep() == 1
    assert list(lay.runs_dir().iterdir()) == []
    assert (lay.root / "seeds" / "s1").exists() and (lay.root / "snapshots" / "r1").exists()
```

```python
# tests/test_factory_training_copies.py 追加:接到运行时与策略工厂上
from factory.agent import AgentRuntime  # noqa: E402


def test_a_registered_training_session_gets_starling_opponents_and_its_copy_goes_at_close(env, store_with_scenario):
    """store_with_scenario、_info、_assignment 照 tests/test_factory_handlers.py 里 计划 A Task 17 用例的同名辅助写
    (实施时先 grep -n 'def _info\\|def _assignment' tests/test_factory_handlers.py,把要用的复制过来)。"""
    lay, copies, _, _ = env
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    runtime.register_session(scenario.id, "run-1")
    copies.bind("run-1", None)
    res = runtime._backends(_info(scenario, assignment, session_id="sess-9", client_slot="a"))
    opponents = [s for s in res.backends]
    assert opponents and all(type(res.backends[s].instance.memory).__name__ == "StarlingMemory" for s in opponents)
    assert copies.session_dir("run-1", "sess-9").exists()
    res.close()
    assert not copies.session_dir("run-1", "sess-9").exists() and runtime._session_runs == {}


def test_an_unregistered_deployment_session_keeps_empty_tuple_opponents(env, store_with_scenario):
    lay, copies, _, _ = env
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    res = runtime._backends(_info(scenario, assignment, session_id="sess-d", client_slot="a"))
    assert all(type(b.instance.memory).__name__ == "TupleMemory" for b in res.backends.values())
    assert not lay.runs_dir().exists() or list(lay.runs_dir().iterdir()) == []
    res.close()
```

策略工厂一侧在 `tests/test_factory_interact_wire.py` 追加一条(用该文件现有的 `world`、`stub`、`fake_serve` 与 计划 A Task 21 的 `per_rollout_factory` 用例同一套搭法,带 `pytest.importorskip("starling")`):`make_policy_factory(..., training_copies=copies)` 进入 `scope("run-1", cfg)` 后,`with policy_for_rollout("run-1-grpo0-s1-r0") as backend:` 里 `type(backend.instance.memory).__name__ == "StarlingMemory"` 且 `copies.client_dir("run-1", "run-1-grpo0-s1-r0")` 存在;退出后目录不在;`scope` 退出后 `copies.seed_of("run-1")` 抛 `KeyError`。用例名 `test_the_trained_slot_gets_a_starling_copy_per_rollout_and_it_is_removed_after`。

- [ ] **Step 2: 跑测试确认失败**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_factory_training_copies.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'TrainingCopies' from 'factory.memory_runtime'`

- [ ] **Step 3: 实现**

- `TrainingCopies._open(run_id, path, persona_id)`:`seed = self.seed_of(run_id)`;`path.parent.mkdir(parents=True, exist_ok=True)`;`src = layout.seed(seed, persona_id)`,`seed` 非 None 且 `src` 存在时 `copy_library(src, path)`;然后 `StarlingMemory.open(path, persona_id=persona_id, endpoints=..., close_timeout_s=...)`(没有复制就是新建空库)。从种子取副本用文件复制,不用 `backup_to`:`backup_to` 要先把种子打开,而 `Memory.open` 会跑迁移、写 WAL,种子只读(5.3 节);种子没有写者,复制 `.db` 加 `-wal` 就是一致的。这样副本的摘要与 Task 17 解析器经 `digest_of`(同一个复制函数)算出的起点摘要相同。待裁定:设计源 7.2 节写的是"从起点库 `backup_to`"。
- `open_client_copy` 先 `shutil.rmtree(client_dir, ignore_errors=True)`(7.2 节:重试在同一线程顺序发生,上一次尝试的上下文已退出,同名目录先删后建是安全的)。
- `release` 的"全关才删"是 Review Focus"副本在 Starling 调用未返回时被删"的第二道闸:第一道是 servicer 的 `join`,join 超时的会话进滞留表、根本不跑 closer(6.2 节);第二道是 `StarlingMemory.close(timeout)` 等不到在途调用就抛,`release` 见到异常就不删。留下的目录由下次启动的 `sweep` 兜底(第 10 节"副本删除失败:记日志;清扫兜底")。`rmtree` 自身失败同样只记 warning、返回假、不抛。
- `AgentRuntime._backends(info)`:计划 A Task 17 那一行改为:`run_id is not None and self._training_copies is not None` 时 `memory = self._training_copies.open_session_copy(run_id, info.session_id, slot, persona_id)`,`persona_id` 取该位绑定的人格;否则照旧 `TupleMemory()`。Starling 一路的 `closers` 改为 `(release_all, drop)`,`release_all = lambda: copies.release([b for b in 已造后端], copies.session_dir(run_id, info.session_id))`,`release` 逐个调的是 `MemoryTranslatingBackend.close`(经实例转发给端口);它抛的异常由 `SessionResources.close` 记 warning 吞掉。造到一半失败:先 `release` 已造的(异常吞掉,只 warning),再 `drop()`,再原样重抛(6.2 节"工厂自己抛异常时不留半开的库")。`run_id` 有、`training_copies` 有、但 `seed_of(run_id)` 抛 `KeyError`(策略上下文还没 `bind`)按造后端失败处理,servicer 判 `role_backend_failure`;正常顺序里 `bind` 先于 `register_session`,不会发生。
- `make_policy_factory`:`scope` 里 `register_session` 循环之前 `training_copies.bind(run_id, cfg.memory["seed"])`(`training_copies` 非 None 时),`finally` 里 `unregister_session` 之后 `unbind(run_id)`。`policy_for_rollout(episode_id)`:有 `training_copies` 时 `memory = training_copies.open_client_copy(run_id, episode_id, cfg.trained_slot, trained_persona)`,`trained_persona = {b.slot_id: b.persona_id for b in cfg.bindings}[cfg.trained_slot]`;`finally` 里由 `training_copies.release([backend], training_copies.client_dir(run_id, episode_id))` 取代 计划 A Task 21 的 `backend.close()`,它重抛的 `FactoryMemoryFailureError` 照 计划 A Task 21 不接(7.2 节:`close` 超时这条 rollout 判 `role_memory_failure`、不重试)。
- 部署会话(没登记给训练 run)不碰副本:对手位照 7.4 节每会话一份空 `TupleMemory`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_factory_training_copies.py tests/test_factory_interact_wire.py tests/test_factory_handlers.py -q -p no:cacheprovider`
Expected: PASS
Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_interact_parity.py -q -p no:cacheprovider`
Expected: PASS(替身路径不变)

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/factory/agent.py src/factory/interact_policy.py src/actio/memory_starling.py
git add tests/test_factory_training_copies.py tests/test_factory_interact_wire.py
git commit -m "feat(factory): 训练路副本从种子复制,对手位按会话、训练位按 rollout,端口全关才删"
```

### Task 19: 组合根按记忆配置选后端:装配期拒绝项、部署 40 秒、清扫经 `on_locked`

**Files:**
- Modify: `src/factory/memory_runtime.py`(加 `MemoryWiring`、`memory_wiring`、`DEPLOY_MEMORY_TURN_TIMEOUT_S`、`DEPLOY_MEMORY_PER_TURN_S`;计划 A Task 35 的 `stand_in_deployment_memory` 里那行启动日志移进 `memory_wiring`)
- Modify: `src/factory/wire.py`(`Assembly` 加字段 `sweep`;`assemble` 在 计划 A Task 35 造部署记忆处改调 `memory_wiring`;`_shared_runtime` 收 `turn_timeout_s` 与 `training_copies`;`_interact_wiring` 收 `wiring`,去掉 计划 A Task 26 那行无条件的 `memory backend: stand-in` 日志与写死的 `stand_in_memory_resolver`;`run` 传 `on_locked`;`reconcile_one_tick` 在锁内装配后调 `sweep`)
- Modify: `src/factory/handlers.py`(`_agent(ctx)`:Starling 时传 `stream_deadline_s`)
- Create: `tests/conftest.py`(autouse 夹具:除 `memory_live` 标记的用例外,一律 `monkeypatch.delenv("RISMFORGE_MEMORY_CONFIG", raising=False)`。既有的 `assemble(...)`/`main(...)` 用例有不传 `env` 的,会读 `os.environ`;开发机设了这个变量就会让它们换成 Starling 路径)
- 随本任务改的既有测试:`tests/test_factory_interact_wire.py` 里 计划 A Task 26 的 `test_the_interact_assembly_logs_the_stand_in_memory_backend` 不改断言(日志仍出现一次);实施时先 `grep -rn 'stand_in_memory_resolver\|memory backend' tests src`,凡是断言 `_interact_wiring` 返回字典逐键相等的用例补上新形状
- Test: `tests/test_factory_memory_wiring.py`(新)

**Interfaces:**
- Consumes:Task 16 `read_memory_config`、`check_api_key`、`claim_memory_root`、`starling_install_source`、`MEMORY_CONFIG_ENV`;Task 17 `StarlingLayout`、`starling_memory_runtime`、`starling_memory_resolver`、`actio.memory_starling.starling_version`/`extraction_prompts_digest`;Task 18 `TrainingCopies`、`AgentRuntime(..., training_copies=)`、`make_policy_factory(..., training_copies=)`;Task 15 `StarlingDeploymentStore`;第 4 步 `stand_in_memory_resolver`;第 5 步 `DeploymentMemory(store, layout, runtime, scratch_store_factory)`、`stand_in_deployment_memory(repo_root)`、`MemoryLayout(root, suffix=".db")`、`make_start_round_handler(..., stream_deadline_s=None)`;第 3 步 `run_daemon(..., on_locked=None)`、`factory.wire.INTERACT_TURN_TIMEOUT_S = 90.0`;`factory.cli.main(argv, *, env, builders) -> int`(`FactoryConfigError` → 2)。
- Produces:
  - `factory.memory_runtime.DEPLOY_MEMORY_TURN_TIMEOUT_S = 40.0`、`DEPLOY_MEMORY_PER_TURN_S = 40.0`。
  - `factory.memory_runtime.MemoryWiring(backend: str, deployment: DeploymentMemory | None, memory_resolver: Callable, training_copies: TrainingCopies | None, sweep: Callable[[], int] | None)`,frozen dataclass。
  - `factory.memory_runtime.memory_wiring(repo_root: Path, env: Mapping[str, str], *, deployment: bool) -> MemoryWiring`。
  - `factory.wire.deploy_turn_timeout_s(*, interact: bool, starling: bool) -> float | None`:`interact` 为真 → `INTERACT_TURN_TIMEOUT_S`(90,两开关同开取较大值);否则 `starling` 为真 → `DEPLOY_MEMORY_TURN_TIMEOUT_S`(40);否则 `None`(`AgentRuntime` 默认 5 秒)。
  - `Assembly.sweep: Callable[[], int] | None = None`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_memory_wiring.py
"""组合根的记忆后端选择(设计二 5.1、6.2、6.4 节,第 10 节拒绝项)。"""
import logging
import os

import pytest
import yaml

from factory import wire
from factory.cli import main
from factory.errors import FactoryConfigError
from factory.memory_config import MEMORY_CONFIG_ENV
from factory.memory_runtime import memory_wiring
from factory.wire import FactoryOptions, assemble, deploy_turn_timeout_s


def _memory_env(tmp_path, root=None, **extra):
    path = tmp_path / "memory.yaml"
    path.write_text(yaml.safe_dump({
        "root": str(root or tmp_path / "mem"), "extraction_base_url": "http://127.0.0.1:9/v1",
        "extraction_model": "m-x", "embedding_base_url": "http://127.0.0.1:9/v1",
        "embedding_model": "m-e", "embedding_dim": 256,
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return {MEMORY_CONFIG_ENV: str(path), "OPENAI_API_KEY": "offline-test-key", **extra}


def test_turn_timeouts_follow_section_6_4():
    assert deploy_turn_timeout_s(interact=False, starling=False) is None     # 替身:保持 5 秒
    assert deploy_turn_timeout_s(interact=False, starling=True) == 40.0
    assert deploy_turn_timeout_s(interact=True, starling=True) == 90.0       # 同开取较大的 90
    assert deploy_turn_timeout_s(interact=True, starling=False) == 90.0


def test_without_the_variable_the_wiring_is_the_stand_in_and_logs_it_once(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="factory.memory_runtime"):
        w = memory_wiring(tmp_path / "repo", {}, deployment=True)
    assert w.backend == "stand-in" and w.training_copies is None and w.sweep is None
    assert caplog.text.count("memory backend: stand-in") == 1


def test_a_set_config_without_starling_installed_is_exit_code_2(tmp_path, monkeypatch, store, capsys):
    """Review Focus:Starling 未安装但设了记忆配置。store 是 tests/test_factory_handlers.py 同名夹具
    (带 agent.yaml 的临时仓库;实施时先 grep -n 'def store' tests/test_factory_handlers.py 照搬或经 factory_fixtures 导入)。"""
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    code = main(["reconcile", str(store.repo_root), "--enable", "agent"], env=_memory_env(tmp_path))
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install '.[memory]'" in err and "offline-test-key" not in err
    assert not (tmp_path / "mem" / ".owner").exists()        # 拒绝在碰根目录之前


def test_an_empty_api_key_is_refused_by_name(tmp_path, store):
    with pytest.raises(FactoryConfigError, match="^OPENAI_API_KEY "):
        memory_wiring(store.repo_root, _memory_env(tmp_path, OPENAI_API_KEY=""), deployment=True)


def test_a_root_owned_by_another_repo_is_exit_code_2(tmp_path, store, capsys):
    """Review Focus:记忆根目录被另一个仓库占用(两个子进程并发的用例在 Task 16)。"""
    pytest.importorskip("starling")
    root = tmp_path / "mem"
    root.mkdir()
    (root / ".owner").write_text("/somewhere/else/repo\n", encoding="utf-8")
    assert main(["reconcile", str(store.repo_root), "--enable", "agent"], env=_memory_env(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "/somewhere/else/repo" in err and str(store.repo_root.resolve()) in err


def test_with_starling_the_round_handler_gets_starling_memory_and_a_40s_budget(tmp_path, store, monkeypatch):
    pytest.importorskip("starling")
    from actio.memory_starling import StarlingDeploymentStore
    import factory.handlers as fh
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent", "admit")), env=_memory_env(tmp_path))
    try:
        r = seen["round"]
        assert isinstance(r["memory_store"], StarlingDeploymentStore)
        assert r["memory_layout"].suffix == ".db" and r["memory_layout"].root == tmp_path / "mem"
        assert r["memory_runtime"]["backend"] == "starling"
        assert r["stream_deadline_s"] == r["config"].max_turns * 40.0
        assert r["client_factory"].__self__._timeout == 40.0           # AgentRuntime 的服务端等提交
        assert (tmp_path / "mem" / ".owner").read_text(encoding="utf-8").strip() == str(store.repo_root.resolve())
        assert callable(asm.sweep)
    finally:
        asm.close()


def test_run_passes_the_sweep_as_on_locked_and_the_stand_in_passes_none(tmp_path, store, monkeypatch):
    seen = []
    monkeypatch.setattr(wire, "run_daemon", lambda store_, interval_s, **kw: seen.append(kw.get("on_locked")))
    marker = lambda: 0
    monkeypatch.setattr(wire, "memory_wiring", lambda repo_root, env, *, deployment: _fake_wiring(sweep=marker))
    wire.run(str(store.repo_root), FactoryOptions(switches=("agent",)), max_ticks=1, env={})
    monkeypatch.undo()
    monkeypatch.setattr(wire, "run_daemon", lambda store_, interval_s, **kw: seen.append(kw.get("on_locked")))
    wire.run(str(store.repo_root), FactoryOptions(switches=("agent",)), max_ticks=1, env={})
    assert seen == [marker, None]


def test_reconcile_sweeps_after_taking_the_repo_lock_and_before_the_tick(tmp_path, store, monkeypatch):
    order = []
    real_lock = wire.acquire_repo_lock

    def lock(root):
        order.append("lock"); return real_lock(root)

    monkeypatch.setattr(wire, "acquire_repo_lock", lock)
    monkeypatch.setattr(wire, "memory_wiring",
                        lambda repo_root, env, *, deployment: _fake_wiring(sweep=lambda: order.append("sweep") or 0))
    monkeypatch.setattr(wire, "reconcile_once", lambda *a, **kw: order.append("tick") or [])
    wire.reconcile_one_tick(str(store.repo_root), FactoryOptions(switches=("agent",)), env={})
    assert order == ["lock", "sweep", "tick"]
```

`_fake_wiring(sweep)` 是本文件的辅助:`MemoryWiring(backend="stand-in", deployment=stand_in_deployment_memory(store.repo_root), memory_resolver=stand_in_memory_resolver, training_copies=None, sweep=sweep)`(要 `store`,所以写成接 `repo_root` 的闭包;实施时按夹具的实际形状调)。`client_factory.__self__._timeout` 依赖 `AgentRuntime` 的私有属性,若 S2/S3 已给它加了只读属性就改用属性。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_wiring.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'memory_wiring' from 'factory.memory_runtime'`

- [ ] **Step 3: 实现**

- `memory_wiring(repo_root, env, *, deployment)`:
  1. `config = read_memory_config(env=env, repo_root=repo_root)`;`None` → `_log.info("memory backend: stand-in")`,返回 `MemoryWiring("stand-in", stand_in_deployment_memory(repo_root) if deployment else None, stand_in_memory_resolver, None, None)`。
  2. 否则按序:`install = starling_install_source()`(未安装、editable 在这里退出码 2,先于任何对 `actio.memory_starling` 的导入与对根目录的写);`check_api_key(env)`;`claim_memory_root(config.root, repo_root)`;惰性 `from actio.memory_starling import StarlingDeploymentStore, StarlingEndpoints, extraction_prompts_digest, starling_version`。`ImportError`(装了包但 C++ 扩展载不进来)也译成同一条 `pip install '.[memory]'` 的 `FactoryConfigError`。
  3. `endpoints = StarlingEndpoints(...)` 取自配置;`runtime = starling_memory_runtime(config, version=starling_version(), install_source=install, prompts_digest=extraction_prompts_digest())`;`layout = StarlingLayout(config.root)`;`copies = TrainingCopies(layout, endpoints)`。
  4. `deployment` 为真时 `DeploymentMemory(StarlingDeploymentStore(layout.deploy_dir(), endpoints=endpoints), MemoryLayout(config.root, suffix=".db"), copy.deepcopy(runtime), lambda p: StarlingDeploymentStore(p, endpoints=endpoints))`。
  5. `_log.info("memory backend: starling")`,返回 `MemoryWiring("starling", deployment, starling_memory_resolver(layout, runtime), copies, copies.sweep)`。
  - 日志只写后端名,不写根目录与端点(第 10 节)。
- `assemble`:原来 计划 A Task 35 造部署记忆的位置改为:需要记忆时(`options.interact` 或 `keys & {"start_agent_round", "admit_proposal"}`)调一次 `wiring = memory_wiring(store.repo_root, environ, deployment=bool(keys & {...}))`,早于 `_shared_runtime`;不需要时不读变量(与 `mindtrainer.wire.run` 逐字节相同的那条路径不变)。`_shared_runtime(store, options, keys, *, turn_timeout_s, training_copies)`:`turn_timeout_s = deploy_turn_timeout_s(interact=options.interact, starling=wiring.backend == "starling")`;为 `None` 时不传,保持 `AgentRuntime` 默认值;`training_copies` 只在 `options.interact` 时传。`BuildContext.deployment_memory = wiring.deployment`。`_interact_wiring` 返回的 `memory_resolver` 取 `wiring.memory_resolver`,`make_policy_factory(..., training_copies=wiring.training_copies)`。三处 `Assembly(...)` 都带 `sweep=wiring.sweep`(不需要记忆的那条是 `None`)。
- `run`:两处 `run_daemon(...)` 加 `on_locked=a.sweep`(为 `None` 时与第 3 步相同)。`on_locked` 抛异常时 `run_daemon` 不进第一个 tick(计划 A Task 23),`a.close()` 照旧在 `finally` 里。
- `reconcile_one_tick`:`with acquire_repo_lock(repo_root):` 里装配之后、`try:` 之内第一句 `if a.sweep is not None: a.sweep()`。"清扫放在装配里"(6.2 节)按字面放进 `assemble` 会让 `run` 在取锁之前清扫,所以 `assemble` 只交出函数,由持锁的一方调用。
- `handlers._agent(ctx)`:`mem.runtime["backend"] == "starling"` 时给 `make_start_round_handler` 加 `stream_deadline_s=config.max_turns * DEPLOY_MEMORY_PER_TURN_S`;替身照 计划 A Task 35 不传。`--interact` 与 `--enable agent` 同开时部署仍用自己的 `max_turns × 40`,训练用 `max_turns × 90`,各自经 `client_factory(deadline_s=)` 传。
- `tests/conftest.py`:首条语句 `from __future__ import annotations`;一个 autouse 夹具,`request.node.get_closest_marker("memory_live")` 为空时 `monkeypatch.delenv("RISMFORGE_MEMORY_CONFIG", raising=False)`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_wiring.py tests/test_factory_wire.py tests/test_factory_wire_someval.py tests/test_factory_interact_wire.py tests/test_factory_handlers.py tests/test_factory_e2e_loop.py tests/test_ma_e2e.py tests/test_repo_lock.py -q -p no:cacheprovider`
Expected: PASS(要 Starling 的两条 skipped)
Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_factory_memory_wiring.py tests/test_factory_training_copies.py -q -p no:cacheprovider`
Expected: PASS
再跑全量 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider`,Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/factory/wire.py src/factory/handlers.py
git add tests/conftest.py tests/test_factory_memory_wiring.py tests/test_factory_interact_wire.py
git commit -m "feat(factory): 按记忆配置装配 Starling,部署等提交 40 秒,清扫在持仓库锁后进行"
```

### Task 20: 部署路在真 Starling 上的集成用例,真端点用例只手动跑

**Files:**
- Test: `tests/test_ma_deployment_starling.py`(新;部署驱动器与门② 换成 `StarlingDeploymentStore` 再跑第 5 步的关键用例)
- Test: `tests/test_memory_live.py`(新;`memory_live` 标记,设计源 §13 的两项未核实)

**Interfaces:**
- Consumes:第 5 步 `run_deployment_episodes(store, make_instance, client_factory, *, memory_store, memory_layout, run_id, round_id, library_version, episode_digests, on_episode_start, scenario_ids, personas, bindings, model_id, max_turns, episodes_per_scenario=1, max_retries_per_episode=1, stream_deadline_s=None)` 与 `tests/test_ma_deployment.py` 里 计划 A Task 32 的搭法(实施时先 `grep -n 'def _mem\|def _run\|FileTupleStore(' tests/test_ma_deployment.py`,把 `_mem` 夹具参数化成 `file`/`starling` 两种,不复制用例);`make_admit_proposal_handler(*, memory_store, memory_layout)` 与 `tests/test_ma_admit.py` 的 计划 A Task 34 用例;Task 15 `StarlingDeploymentStore`;Task 13 `FakeOpenAI`(`fail_keys` 注入抽取失败,取代 `FileTupleStore(extraction_failure=)`)。
- Produces:无新接口。

- [ ] **Step 1: 写测试**

```python
# tests/test_ma_deployment_starling.py
"""第 5 步的部署记忆语义在真 Starling 上再跑一遍(设计源第 11 节"真 Starling 或替身"的集成行)。"""
import pytest

pytest.importorskip("starling")
from actio.memory_starling import StarlingDeploymentStore  # noqa: E402
from mindagent.memory_layout import MemoryLayout  # noqa: E402
from starling_fakes import FakeOpenAI, offline_api_key  # noqa: E402


@pytest.fixture
def starling_mem(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        root = tmp_path / "mem"
        yield StarlingDeploymentStore(root / "deploy", endpoints=fake.endpoints()), MemoryLayout(root, suffix=".db"), fake


def test_a_retry_rolls_the_library_back_to_the_same_digest(starling_mem):
    """第一次尝试写了记忆后失败,第二次尝试前 restore_from 起点副本:库摘要与起点相同。"""


def test_a_pending_row_from_a_failed_attempt_does_not_conflict_on_the_retry(starling_mem):
    """第一次尝试 fake.fail_keys 让 #1 抽取失败留 pending 行后整条失败;清空 fail_keys,
    第二次尝试同一逻辑 id 成功、不抛 MemoryEventConflict;成功后 pending_events 为空。"""


def test_the_second_round_reuses_the_episode_id_but_not_the_event_keys(starling_mem):
    """两轮用同一 deploy-{model}-{scenario}-{k},事件 key 带轮次前缀,第二轮不撞第一轮已 committed 的 key。"""


def test_no_pending_rows_survive_an_episode_and_the_next_start_snapshot_is_clean(starling_mem):
    ...


def test_admission_extraction_failure_rejects_the_proposal_and_abandons_the_event(starling_mem):
    ...
```

每个用例的函数体照 `tests/test_ma_deployment.py`、`tests/test_ma_admit.py` 里同名语义的替身用例写,差别只有三处:库换成夹具里的 `StarlingDeploymentStore` 与 `suffix=".db"` 的 `MemoryLayout`;抽取失败用 `fake.fail_keys`;摘要比较用 `store.digest(pid)`,不比 JSON 文件内容。若第 5 步那几个用例已经把库做成夹具参数,本任务改为给那个参数加 `"starling"` 一项(带 `importorskip`),不新建文件;实施时先 `grep -n 'parametrize\|def _mem' tests/test_ma_deployment.py tests/test_ma_admit.py` 决定走哪条。

```python
# tests/test_memory_live.py
"""设计源 §13 的两项未核实:真端点的单次延迟、真嵌入下的召回质量。连真网络、要真密钥,不进 CI:
只有 RISMFORGE_MEMORY_LIVE=1 且 RISMFORGE_MEMORY_CONFIG 指向真配置时才跑。"""
import os
import statistics
import time

import pytest

pytestmark = [
    pytest.mark.memory_live,
    pytest.mark.skipif(os.environ.get("RISMFORGE_MEMORY_LIVE") != "1", reason="set RISMFORGE_MEMORY_LIVE=1 to run"),
]
pytest.importorskip("starling")

TURNS = [f"pb：第{i}轮,我担心预算超支,人手也不够" for i in range(10)]


@pytest.fixture
def live_port(tmp_path):
    from actio.memory_starling import StarlingEndpoints, StarlingMemory
    from factory.memory_config import check_api_key, read_memory_config
    cfg = read_memory_config(env=os.environ, repo_root=__import__("pathlib").Path(__file__).resolve().parents[1])
    assert cfg is not None, "RISMFORGE_MEMORY_CONFIG must point at a real config"
    check_api_key(os.environ)
    port = StarlingMemory.open(tmp_path / "live.db", persona_id="pa", endpoints=StarlingEndpoints(
        cfg.extraction_base_url, cfg.extraction_model, cfg.embedding_base_url, cfg.embedding_model,
        cfg.embedding_dim, cfg.timeout_ms, cfg.max_retries))
    yield port, cfg.timeout_ms / 1000.0
    port.close()


def test_ten_turns_stay_within_the_per_call_budget(live_port, capsys):
    from actio.memory_port import MemoryEvent
    port, limit = live_port
    took = {"recall": [], "remember": [], "embed_pending": []}
    for i, text in enumerate(TURNS):
        for name, call in (("recall", lambda: port.recall(text, limit=3)),
                           ("remember", lambda: port.remember(MemoryEvent(f"live#{i}", text, "pb"))),
                           ("embed_pending", port.embed_pending)):
            t0 = time.monotonic(); call(); took[name].append(time.monotonic() - t0)
    with capsys.disabled():
        for name, xs in took.items():
            print(f"{name}: max {max(xs):.2f}s median {statistics.median(xs):.2f}s")
    assert max(took["recall"]) <= limit and max(took["embed_pending"]) <= limit
    assert max(took["remember"]) <= 3 * limit          # 三段抽取,每段至多一个 timeout_ms(6.4 节)


def test_recall_brings_back_the_related_sentence(live_port):
    from actio.memory_port import MemoryEvent
    port, _ = live_port
    port.remember(MemoryEvent("q#0", "pb：我负责鉴权模块", "pb"))
    port.remember(MemoryEvent("q#1", "pb：明天下雨", "pb"))
    port.embed_pending()
    hits = port.recall("谁负责鉴权", limit=1)
    assert hits and "鉴权" in hits[0].text
```

- [ ] **Step 2: 跑测试**

Run(带 memory): `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_ma_deployment_starling.py tests/test_memory_live.py -q -p no:cacheprovider`
Expected: 部署用例 PASS(它们复用第 5 步已实现的行为,本任务不改源码;哪条红了,先按 Task 15 的管理面语义排查,不改第 5 步的驱动器);`test_memory_live.py` 两条 skipped
手动(不进 CI,结果记进第 8 步的 §13 收尾): `RISMFORGE_MEMORY_LIVE=1 RISMFORGE_MEMORY_CONFIG=<真配置> OPENAI_API_KEY=<真密钥> PYTHONDONTWRITEBYTECODE=1 uv run --extra dev --extra memory pytest tests/test_memory_live.py -q -s -p no:cacheprovider`。`tests/conftest.py` 的 autouse 夹具对 `memory_live` 用例不清环境变量(Task 19)。

- [ ] **Step 3: 跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_deployment.py tests/test_ma_admit.py tests/test_ma_rounds.py tests/test_ma_e2e.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_ma_deployment_starling.py tests/test_memory_live.py
git commit -m "test(memory): 部署记忆语义在 Starling 上再跑一遍,真端点延迟与召回质量用例只手动跑"
```

---

## 第 8 步:文档与图收尾

> 门② 部分收录的规则在写计划之后改过(Codex 第九轮):前几条已提交、后一条抽取失败时,**不放弃**失败的那条,它留 `pending`,提案留 `approved`,处理器抛 `DeferredError` 退避重试,抽取恢复后收录完。00、50 两篇与设计源已按新规则改好。下面各任务里写着"这一条 `abandon_event`、写 T2、留 `approved` 等人处理"的写回条目已过时,跳过。

> 开工前已经追平过一次:`docs/design` 六篇的设计措辞在提交 `6af53eb` 到 `9d1eb64` 里按设计源与写计划时的裁定改过。下面各任务的"写回台账裁定"一步,先 `git log -p 6af53eb^..9d1eb64 -- <该篇>` 看哪些已经改了,只做还没改的;已改的条目跳过,不要重复改写。施工中又改了设计措辞的,照同样的办法写回。

本步改 `docs/design/` 的 00、20、30、40、50、70、90 七篇、设计源 spec,以及(只核对)`docs/design/diagrams/` 的生成图。**不改源码与测试**,所以没有"先写失败的测试"的循环;每个任务的"测试"是两个检查器加 `sync_docs --check`,"提交"逐个文件暂存。

### 工具与规则(写计划时核对过,`docs/design/tools/` 三个脚本)

- `python3 docs/design/tools/check_docs.py [文档名...]`:单篇检查。有错误退出码 1,告警不影响退出码。要点:
  - 共享区块 `<!-- shared:begin NAME -->` 与图块 `<!-- figure:begin NAME -->` 必须与源同步,否则报错。**区块里的内容是生成的,不许手改**:`context-block` 的源是 `docs/design/_shared/context-block.md`(现含一处 `[计划中]`,第 15 行:Actio 与 Starling 那句),图块的源是 `docs/design/diagrams/fig_*.py`,改完跑 `python3 docs/design/tools/sync_docs.py` 重写文档。
  - 正文反引号包着的仓库路径(`src|tests|scripts|cli|schemas` 开头)必须真实存在,**同一行带 `[计划中]` 或 `[推迟]` 的除外**。所以把一行的 `[计划中]` 改成 `[已落地]` 时,这一行里的路径必须已存在(`src/actio/memory_port.py` 等第 1 到 7 步建好的文件)。
  - 反引号里的类名(CamelCase)、带括号的调用、以包名开头的点号路径,在代码或施工计划里都找不到会告警;**只在施工计划里的符号所在行必须带 `[计划中]`**。第 8 步反过来:符号落地后进了代码,`[计划中]` 摘掉不告警;但如果某个符号**最终没按计划落地**(改了名),文档里的旧名既不在代码也不在计划里,会告警,所以改名要同步改文档。
  - 禁用措辞(出现即报错):`计划 N`(引用实施计划编号)、`docs/superpowers`、`详见`、`参见`、`上游设计|前置文档|旧设计`、指向其他 `.md` 的链接。**文档里不许写计划文件路径**。
  - 子系统文档章节(`## 0.` 到 `## 10.` 与 `## 附 本文术语`)必须齐全且顺序固定;头部元信息缺行报错。
- `python3 docs/design/tools/check_consistency.py`:跨文档检查。要点:
  - 头部「状态」行的标记要覆盖 `STATUS_REQUIRED`(20、30、40、50、70、90 之中除 90 外都要含 `[已落地]`,90 也含 `[已落地]`;已满足,别删光)。
  - 状态标记只许 `[已落地]`、`[计划中]`、`[推迟]` 三种,圆括号写法告警。
  - 测试数:正文里"三到四位数 + 个/条 + 测试/用例"的数字,必须是 `pytest --collect-only` 实测的单文件数、分组和、总数之一(`known_test_numbers`),否则告警。它调的是 `uv run --quiet pytest --collect-only -q`,不带 `--extra dev`,第 8 步跑检查器前先确认能收集(见 Task 29 的前置检查)。
  - 行数:每篇 250 到 900 行,超出告警(不是错误)。**但本步把 900 当硬上限**:30 与 90 现在恰好 900 行(`len(text.split("\n"))`),40 是 895,所以这三篇只许改写、不许净增。
  - 决策编号:`R1` 到 `R21` 是总览决策表专用的编号范围,正文里写 `R22` 以上会报"引用了不存在的决策"。**本步不要在任何文档正文里写台账的 `R18`、`R28` 这样的编号**(台账编号是计划内部的,不进设计文档);文档只写措辞。
  - 术语:`§` 告警(改成就地写出内容),`状态库`、`旁路`、`审批`、`闸门` 等告警,`v1`、`v2` 小写版本号告警(写 `actio.trace.v1` 这种带点号的不受影响,因为规则排除了前后带 `.`、字母的情形)。
  - 图的引用:每个 `diagrams/*.svg` 都必须被某篇文档引用(`--final` 才报错)。
- `python3 docs/design/tools/sync_docs.py [文档名]` 重写区块;`--check` 只检查,不同步时退出码 1。
- 单篇的 `[计划中]` 行内带路径的例外只对**同一行**生效;跨行表格单元格不算。

### 现状数字(写计划时 `grep -c` 与 `len(split("\n"))` 实测,HEAD 为 d1b0bcb)

| 文档 | 行数 | 余量到 900 | `[计划中]` 行数 | 设计二合入之前就有的 `[计划中]` 行 |
|---|---|---|---|---|
| 00-overview.md | 869 | 31 | 67 | 4(第 7 行图例、500 行、793 行、33 行图例类) |
| 20-socialsimulator.md | 845 | 55 | 112 | 6 |
| 30-mindtrainer.md | 900 | 0 | 71 | 13 |
| 40-actio.md | 895 | 5 | 119 | 1(图例) |
| 50-mindagent.md | 844 | 56 | 154 | 1(图例) |
| 90-factory-and-cli.md | 900 | 0 | 112 | 3 |
| 70-trainscheduler.md | 714 | 186 | 2(一处是共享区块里的那句,一处是图例) | 1 |
| 10-controlplane.md | 702 | 198 | 3 | 0(一处在共享区块,一处图例,一处 331 行 `on_locked`) |

(每篇的图例行"**[计划中]** 表示……"是定义,永远保留,不算待改标记;每篇共享区块那一句 `Actio ... 可选 extra memory [计划中]` 也在 `context-block` 里,改源头一次、七篇同步。)

### 设计二合入后设计源又改了四次,文档还没跟上(Task 21 先补)

`git log f053cd0..HEAD -- docs/superpowers/specs/2026-10-25-actio-starling-memory-design.md` 有四个提交(`128eefe`、`a31af64`、`70d0d1a`、`d1b0bcb`),而 `docs/design` 最后一次改动是 `f053cd0`。这四个提交改了:`close()` 不再放弃事件(放弃改由 MindAgent 在 episode 最终结束时做)、`TupleMemory.recall` 的 id 一律是内容 id、部署事件 key 的轮次前缀、`embed_pending` 返回成功条数且有失败就抛、`recall` 抛 `RecallDegradedError`、`.owner` 用 `O_CREAT | O_EXCL`、整条流 deadline 按调用方传、S4 改为 volatile、摘要按 `snapshot` 后的条目算。这些在文档里是**旧措辞**,和 、、 同属"写回"的范围。Task 21 先把它们补齐再处理台账裁定,否则后面按台账改一遍、再按设计源改一遍,会改两次同一行。

### 任务顺序与依赖

Task 21 补设计源已变而文档没跟的措辞 → Task 22 总览 → Task 23 模拟器 → Task 24 MindTrainer → Task 25 Actio → Task 26 MindAgent → Task 27 组合根 → Task 28 TrainScheduler 与 ControlPlane 的小改 → Task 29 设计源 spec 同步 → Task 30 图核对 → Task 31 收尾(全量测试、检查器、删 `.parts/`)。

**文档任务之间的耦合**:共享区块 `context-block` 一处改动影响七篇文档的同步状态,所以放在 Task 22 第一步(改源 + `sync_docs.py` 全量重写);之后每个任务只动自己那一篇,不再碰共享区块。

---

### Task 21: 前置核对:第 1 到 7 步已合入、检查器基线全绿、测试收集可用

**Files:**
- 不改文件。本任务只做事实核对,把结果记进提交信息之外的实施笔记(不提交)。

**Interfaces:**
- Consumes: 第 1 到 7 步全部合入当前分支(`git log --oneline -40` 里能看到第 1 到 7 步各任务的提交)。
- Produces: 一份"哪些 `[计划中]` 可以摘"的实测清单,供后面每个文档任务照着改。**没有合入的步骤对应的 `[计划中]` 一律不动。**

- [ ] **Step 1: 确认基线全绿(改文档之前)**

Run: `python3 docs/design/tools/check_docs.py; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: `0 个错误,0 条告警`、`一致性检查 13/13 篇:0 个错误,0 条告警`、`所有文档的共享区块与图都已同步`。基线不绿就先停下查原因,不要在红的基线上改文档。

- [ ] **Step 2: 确认测试收集可用(`check_consistency` 要靠它核对测试数)**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --quiet pytest --collect-only -q -p no:cacheprovider 2>&1 | tail -2`
Expected: 末行形如 `3816 tests collected`(落地后数字更大)。`check_consistency.known_test_numbers()` 内部跑的是 `uv run --quiet pytest --collect-only -q`(不带 `--extra dev`、不带 `-p no:cacheprovider`),设了超时 180 秒;收集失败它只告警"没能运行 pytest 收集,跳过了测试数核对",不报错,但那会让测试数无人把关。若这里报缺依赖,先 `uv sync --extra dev`。

- [ ] **Step 3: 逐步核对已落地范围(决定哪些标记能摘)**

对每一步,用 `git log --oneline --grep` 或文件存在性确认,写成清单:

```bash
ls src/actio/memory_port.py src/actio/memory_starling.py src/factory/memory_config.py \
   src/factory/memory_translate.py src/factory/memory_runtime.py src/simulator/realtime/resources.py \
   src/mindagent/memory_layout.py 2>&1
git log --oneline -60 | grep -n "memory\|记忆\|Starling" | head -40
python3 - <<'PY'
import importlib
for name in ["actio.memory_port", "actio.memory", "actio.trace", "simulator.realtime.resources",
             "factory.memory_translate", "factory.memory_runtime", "mindagent.memory_layout"]:
    try:
        importlib.import_module(name); print("ok  ", name)
    except Exception as exc:
        print("MISS", name, type(exc).__name__)
PY
```

Expected: 第 1 到 5 步的模块全 `ok`。`actio.memory_starling`、`factory.memory_config` 属第 7 步;它们的 `ls` 报 `No such file` 表示第 7 步未落地,则 Task 25/Task 26/Task 27 里标注"依第 7 步"的行**保持 `[计划中]`**,其余照改。

- [ ] **Step 4: 把"第 7 步是否落地"的结论写进实施笔记**

两种情形的处理差别只在三个任务(Task 25、Task 26、Task 27)里每个任务的"依第 7 步"小节;每个任务都分了"必改"与"依第 7 步才改"两张清单。第 7 步没合时只做必改清单。

- [ ] **Step 5: 无提交**

本任务不产生提交。

---

### Task 22: 共享区块、总览(00)与设计二的状态口径

**Files:**
- Modify: `docs/design/_shared/context-block.md`(第 15 行)
- Modify: `docs/design/00-overview.md`(67 个 `[计划中]` 行,见下表;`context-block` 之外的 33 处设计二措辞里有 6 处要写回台账裁定)
- Regenerate: `python3 docs/design/tools/sync_docs.py`(重写所有含 `context-block` 的文件:10、20、21、30、40、50、55、60、70、71、80、90 共 12 篇加 `TEMPLATE.md`;00 篇没有这个区块;**不手改**区块内容)
- 行数:869,余量 31;本任务净增不得超过 +20,目标 ≤ +6。

**Interfaces:**
- Consumes: Task 21 的落地清单。
- Produces: 00 篇的状态口径(其余六篇的头部「状态」行与它一致);`context-block.md` 去掉 Actio/Starling 那句的 `[计划中]`(Starling 适配器与 `memory` extra 在第 7 步落地才摘,见下)。

- [ ] **Step 1: 列出本篇每条 `[计划中]`,分成"摘"、"留"、"改措辞"三类**

Run: `grep -n "\[计划中\]" docs/design/00-overview.md | cut -c1-110`

按下表处理(行号是写计划时的行号,实施时以 `grep` 结果为准,表里用行首的前几个字对位):

| 行 | 内容 | 处理 |
|---|---|---|
| 7 | 状态标记定义(图例) | 保留 |
| 8 | 头部「状态」:`智能体记忆 [计划中]` | 摘成 `[已落地]`;句子改成"智能体记忆 [已落地]:场景二每个角色位都是带记忆的 Actio 智能体,记忆端口的生产实现是 Starling 适配器 [已落地/计划中 依第 7 步],替身是内存元组与落盘的 FileTupleStore。……"(措辞定稿在 Step 2) |
| 13 | 一页读懂里的 `[计划中]` | 摘;若同句含 Starling 才有的内容且第 7 步未合,拆成两个标记 |
| 36、41 | 一页读懂表与段落 | 摘 |
| 140、144、162-164、175 | 分层表、Starling 段、模块表、依赖表(`starling` 只限 `memory_starling.py`) | 第 7 步合了就摘;没合就留 |
| 184 | 依赖表后一句 | 同上 |
| 205 | 产物树表里 `memory_library/` 一行 | 摘(第 5 步已让 `load_library` 不读它、装配期拒绝非空) |
| 267-269 | `approved → rejected` 边 | 摘(第 5 步 计划 A Task 29 落地) |
| 321、331、337-343、347-351 | 受限直连与实时服务:会话资源、准入、清扫 | 第 2、3 步已落地的摘;清扫(`run_daemon(on_locked=)` 调清扫函数)在第 7 步 |
| 373 | 失败分类表的新终止原因 | 摘(第 2 步) |
| 492-494 | 场景二的记忆段 | 摘;措辞见 Step 3 的  条 |
| 500 | `[计划中]:真机上的单轮延迟与两个 LoRA 的开销未测` | **保留**(未核实,要真机) |
| 522-526 | 场景三的部署记忆 | 摘;措辞见 Step 3 的 、 两条 |
| 590、613、618 | 部署与配置 | `RISMFORGE_MEMORY_CONFIG` 相关的依第 7 步 |
| 644 | 测试策略表的契约行 | 摘(第 1 步契约测试已落地) |
| 674-676 | 数据生命周期 | 摘;清扫行依第 7 步 |
| 689 | 代码地图 | 摘或留依各模块 |
| 740、744-751 | 路线图第 9 章的八步表 | 第 1 到 5 步摘;第 6 步取决于 Starling PR 是否已合并(Task 12 先征得用户同意才推送,所以**很可能还在评审**:PR 未合就留 `[计划中]`);第 7 步依落地;第 8 步在本任务全部完成、Task 31 通过之后摘 |
| 792-799 | 风险与未核实项第 21 到 28 项 | **全部保留**(要真机或真端点) |
| 832-836 | 决策表 SYS-27 到 SYS-30 | 摘;SYS-27(一个人格一个库)依第 7 步,SYS-28 到 SYS-30 的措辞要写回,见 Step 3 |
| 863-868 | 术语表 | 摘;"记忆端口"、"逻辑摘要"、"内容 id"、"记忆运行时身份"各一行 |

- [ ] **Step 2: 改 `_shared/context-block.md` 第 15 行并全量同步**

只改源头这一行:`记忆由内嵌的 **Starling** 提供,一个人格一个树外 SQLite 库,可选 extra \`memory\` [计划中]` → 第 7 步已合入时去掉标记(写成 `[已落地]`),未合入时把句子改成"记忆端口与替身记忆 [已落地],Starling 适配器与可选 extra `memory` [计划中]"(把 `[计划中]` 缩到仍未落地的那一半,不要整句留着)。

Run: `python3 docs/design/tools/sync_docs.py && python3 docs/design/tools/sync_docs.py --check`
Expected: 重写上述 12 篇与 `TEMPLATE.md` 里的 `context-block`(逐字相同),`--check` 报"所有文档的共享区块与图都已同步"。`git diff --stat docs/design` 应只显示 `_shared/context-block.md` 一行改动加每个含区块的文件同一处的一行改动。

- [ ] **Step 3: 00 篇里写回台账裁定的措辞(先 grep 旧措辞,再改)**

每条先用括号里的命令定位,再改写。**不要在正文写 `R28` 之类的台账编号。**

1. 派发器不回写指令。00 篇没有"写回"该意义的句子(`grep -n "写回" docs/design/00-overview.md` 只有 18、280 两处,是调和循环的"写回产物树",与记忆无关),**不改**。要写回的是 30 篇与 90 篇,见 Task 24、Task 27。
2. 替身部署记忆根目录不是临时目录。`grep -n "临时目录" docs/design/00-overview.md` 只有 78、825 两行,都是外部推理命令行的"空临时目录",**不改**。要改的是 90 篇 374、733 行,50 篇 407 行。
3. 召回放宽到 volatile():00 篇 494 行"每轮先召回,回复后写入对方的话,再只做嵌入,不做后台维护"→"每轮先召回,回复后写入对方的话,再只做嵌入,不做后台维护。新写的语句在 Starling 里是 `volatile`,只嵌入不巩固,所以召回要显式放宽到 `volatile` 行(代价是这些行没经回放去重,召回里可能有近重复)"。净增约 1 行。
4. 风险表 27 项(798 行)"不跑后台维护时,承诺、共同点等分区不更新,对召回质量的影响未测"保留,不改。
5. 门② 部分失败留 approved():00 篇 526 行"抽取失败时先放弃该事件,再把提案改为 `rejected`,被拒的提案不会复活"→"抽取失败时先放弃该事件;一条记忆增量都没提交过,整份提案改为 `rejected`,被拒的提案不会复活;前面已有增量提交过、后一条才抽取失败,提案留在 `approved` 等人处理,因为已提交的事件放弃不了。混合提案(记忆加技能)的记忆增量先写,抽取失败则整份拒绝、技能条目也不落"。净增约 2 行。
6. 摘要口径:SYS-28(834 行)"摘要覆盖语句表、向量表、事件表的全部列,按排除清单去掉纯簿记列"不变;逻辑摘要术语(866 行)"读语句、向量、事件三张表的全部列,去掉纯簿记列后规范化再哈希"后面加"。替身记忆按召回实际看到的条目算(旧版本被取代的条目不进);Starling 按全行算(被取代的行仍是召回候选,`volatile` 与 `pending_review` 行影响后续写入的冲突探测)"。净增 0 行(并进同一单元格)。

- [ ] **Step 4: 在表格里改,不新增行;控制净增**

00 篇余量 31 行,上面六条合计净增约 5 行。若第 7 步没合,路线图 744-751 八行表整张留 `[计划中]` 的第 6、7 行(分别对应 Starling PR、适配器),其余行改 `[已落地]`。

- [ ] **Step 5: 跑检查器**

Run: `python3 docs/design/tools/check_docs.py 00-overview.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。常见失败:

- `路径不存在且未标 [计划中]`:该行反引号里的路径文件还没建。把该行的标记留着或把路径删掉,不要凭空建文件。
- `符号告警 ... 在代码与施工计划里都找不到`:文档里的符号名与最终代码不一致。去代码里找真名改文档,不要改代码。
- 测试数告警:00 篇头部「代码基线」的"3109 个 Python 测试"与 646 行"3109 个测试的分布"是**基线提交 `b826440` 的实测数**,**不要改**;它们不是当前总数。

- [ ] **Step 6: 提交**

```bash
git add docs/design/_shared/context-block.md docs/design/00-overview.md docs/design/TEMPLATE.md \
        docs/design/10-controlplane.md docs/design/20-socialsimulator.md docs/design/21-diagnostic-evaluation.md \
        docs/design/30-mindtrainer.md docs/design/40-actio.md docs/design/50-mindagent.md \
        docs/design/55-strategy-experience.md docs/design/60-recipestore.md docs/design/70-trainscheduler.md \
        docs/design/71-trainplanner.md docs/design/80-inference-bridge.md docs/design/90-factory-and-cli.md
git commit -m "docs(design): 总览与共享区块按第 1 到 5 步落地摘去计划中标记,写回召回放宽与门② 部分失败的措辞"
```

(`git status` 先看:只有这 15 个文件有改动才一起暂存;多出别的文件说明 `sync_docs.py` 动了不该动的东西,停下查。提交信息不加署名行。)

---

### Task 23: SocialSimulator(20)——会话资源、准入、客户端限时、终止原因落地

**Files:**
- Modify: `docs/design/20-socialsimulator.md`(112 个 `[计划中]` 行,行数 845,余量 55;本任务目标净增 ≤ +8)
- Test: 两个检查器 + `sync_docs --check`(无 pytest 用例)

**Interfaces:**
- Consumes: Task 22 的 `context-block` 已同步;第 2 步的落地(`simulator.realtime.resources`、`validation.validate_episode_id`、`create_server(..., resource_join_timeout_s=, linger_retry_s=)`、`EnvironmentServicer.lingering_count()`、`SessionClosedHook`)。
- Produces: 20 篇头部「状态」行与第 10 章表里的第 2、3 步内容全部 `[已落地]`;`create_server` 签名块补上第 2 步新增的三个关键字参数。

20 篇的 `[计划中]` 绝大多数是第 2 步的内容(会话资源、准入、滞留表、`episode_id` 白名单、两个记忆失败终止原因、客户端相邻消息限时),少数是第 3、4 步在本篇登记的接缝和第 7 步的清扫。**一律按"这一行讲的东西属于哪一步"判断**,不要整篇一把梭。

- [ ] **Step 1: 列清单并分类**

Run: `grep -n "\[计划中\]" docs/design/20-socialsimulator.md | cut -c1-100`

写计划时的行段(`awk` 合并相邻行后得到,实施时以 grep 为准):6、31-33(图例,保留)、111-114、139、146-151、211-219、229、241-245、336-338、345-351、354、361、366、370、404、418、424、435-438、454-455、529、535-537、541-545、599、607-614、624、627-630、651、654、661-671、691-698、718、733-739、776、791-795、801、806、817-820、842-844。

| 分类 | 行段 | 处理 |
|---|---|---|
| 第 2 步,摘 | 6(头部状态里的会话资源等)、111-114、139、146-151(时间预算表里的服务端、客户端、记忆行)、211-219、229、241-245、336-338、345-351、354、361、366、370、404、418、424、435-438(`episode_id` 校验)、599、607-614(资源八阶段中 1 到 7)、627-630、651、654、661-671(SS-19 到 SS-29)、691-698(故障表)、718、733-739(测试)、791-795(第 10 章表)、806 | `[计划中]` → `[已落地]` |
| 第 3 步,摘 | 454-455(MindTrainer 与 MindAgent 客户端的新字段)、529、535-537、541-545(终止原因登记进两份瞬态集合) | 摘 |
| 第 7 步,依第 7 步 | 614(八阶段中的第 8 阶段"清扫",由组合根经 `run_daemon(on_locked=)` 做)、672 附近 SS-28(启动清扫只删 `runs/`) | 第 7 步未合则留 |
| 保留 | 817-820(外部 API 对手速率、站内 vLLM 双 LoRA 延迟、每会话开一份库对 GRPO 一步的影响、抽取嵌入延迟)、801(`role_backend_failure` 不区分 T1 与 T2)、776 里仍未核实的那半句 | 这些要真机或真端点才能核实,**不摘** |

(817-818 两条是设计二合入之前就有的 `[计划中]`,与第 8 步无关,不动。)

- [ ] **Step 2: 写回台账里属于本篇的裁定**

先 grep 旧措辞再改:

1. 终止原因会话 abort 时机(R13):20 篇 545 行"客户端相邻两条服务端消息的间隔超过 90 秒 [计划中] | T1 | 客户端判 `timeout` 并 abort,服务端按断连判 FAILED"——代码里的实现是**客户端本地合成一条 `EndSession(reason="timeout")` 走既有收尾**,不是抛异常;`grep -n "超时视作" docs/design/20-socialsimulator.md` 得到的 636、698 行措辞"视作 `timeout`(T1,按条重跑)并 abort 会话"与此一致,**不改**;只在 636 行末补半句"(客户端本地合成 `EndSession(reason=\"timeout\")`,由既有收尾处理;不抛异常,否则会被分类成不可重跑的 `client_protocol_error`)"。
2. `TimeoutBackend` 登记线程的位置(R15):610 行"`TimeoutBackend` 在每次 `respond` 开始时把调用线程加入资源的在途集合"→"`TimeoutBackend` 在调用方线程里先登记(`quiesce` 之后直接抛 SessionClosingError,不起线程),daemon 线程退出时再移出;超时只是调用方放弃等待,线程仍在集合里"。同一单元格改写,不新增行。
3. `create_server` 新参数(R14):390-393 行的签名块加三个关键字参数,不新增行——在第三行 `backend_timeout_s=60.0,` 之后把 `keepalive_time_ms=30_000, keepalive_timeout_ms=10_000` 那行改成 `keepalive_time_ms=30_000, keepalive_timeout_ms=10_000,` 并在其后另起一行 `resource_join_timeout_s=60.0, linger_retry_s=60.0) -> grpc.Server`(净增 1 行)。404 行"`create_server` 的其余参数不变"改成"`create_server` 另加 `resource_join_timeout_s`、`linger_retry_s` 两个参数(滞留表的 join 等待与重试间隔,默认都是 60 秒),`max_workers` 透传给 servicer 算名额"。`EnvironmentServicer` 的 `lingering_count()` 写进 607-614 表的"6 join 超时"行:"滞留数可由 `lingering_count()` 读,每分钟一行日志"。
4. 会话副本目录删除失败:697 行"会话副本目录删除失败 [计划中] | 不分类 | 记日志,继续收尾"已正确,**不改**("起点副本删除失败上抛"是 MindAgent 的 `snapshots/` 下的起点副本,不是 `runs/` 下的会话副本;此处的措辞正是它要区分的那一半)。
5. 记忆根目录的清扫位置:614 行第 8 阶段措辞"由组合根"保持。
6. 训练位逻辑 episode id 的真实形状:436 行"训练是 `{run}-{stage}-{scenario}-r{k}`"→"训练是 `{run_id}-grpo{step}-{scenario_id}-r{k}`(同一情境在不同 GRPO step 反复出现,id 里带 step 才不撞)"。同一单元格改写;668 行 SS-25 与 844 行术语表没有写 id 形状,不改。

- [ ] **Step 3: 头部「状态」行(第 6 行)**

状态行是一整个很长的单元格。只做两件事:把"会话资源……客户端相邻消息限时"那几项的标记摘成 `[已落地]`;第 8 阶段清扫若第 7 步未合,把那一项单独留成 `……启动清扫 [计划中]`。**不要整行重写**。

- [ ] **Step 4: 测试表补用例数(不是估,实测)**

20 篇第 9 章有两张表:上半是"层次/对象/替身"的叙述表(733-739 行有 `[计划中]` 的几行要摘),下半是"测试文件/用例数/状态"表(742-770 行)。第 2 步新增的测试文件要补进下半表,既有文件因本步改而增加的用例也要更新数字。先实测:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -qq -p no:cacheprovider \
  tests/test_realtime_session_resources.py tests/test_realtime_resources.py \
  tests/test_realtime_servicer_limits.py tests/test_realtime_client_transport.py \
  tests/test_realtime_driver.py tests/test_realtime_validation.py \
  tests/test_realtime_backend_timeout.py tests/test_realtime_servicer_e2e.py \
  tests/test_realtime_proto_smoke.py tests/test_realtime_codec.py 2>&1 | tail -12
```

输出每个文件一行 `文件: 数`,抄进表里:已有的文件改数字,没有的文件(`test_realtime_session_resources.py`、`test_realtime_resources.py`)加行。`合计:批量侧 76 个,实时侧 119 个` 和 723 行"184 个用例"两处汇总数要**重新相加**后改(批量侧不变,实时侧 = 下半表所有 `test_realtime_*` 与 `test_runner_*` 行之和)。`check_consistency` 的 `NUM_PAT` 会把 723 行的"N 个用例"拿去和实测单文件数、分组和、总数比,对不上就告警;分组和里 `realtime_` 与 `runner_stop_predicate` 归 `sim`/`realtime`,所以汇总数必须是"所有 `sim_` 加 `realtime_` 文件之和"才通过——用 Step 5 的校验确认。

- [ ] **Step 5: 跑检查器**

Run: `python3 docs/design/tools/check_docs.py 20-socialsimulator.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。若出现"测试数 N 不在实测或计划数里",N 就是上面没重新相加的汇总数。

- [ ] **Step 6: 提交**

```bash
git add docs/design/20-socialsimulator.md
git commit -m "docs(design): 模拟器篇——会话资源、准入、客户端限时、记忆失败终止原因标为已落地,补测试数"
```

---

### Task 24: MindTrainer(30)——训练路换成 Actio 实例、`memory` 块与哈希落地(只改写,不净增)

**Files:**
- Modify: `docs/design/30-mindtrainer.md`(71 个 `[计划中]` 行,**行数 900,余量 0**;净增必须 ≤ 0)
- Test: 两个检查器 + `sync_docs --check`

**Interfaces:**
- Consumes: 第 3、4 步落地的名字:`mindtrainer.trajectory_source.PER_TURN_DEADLINE_S = 90.0`、`InteractTrajectorySource(..., *, run_id)`、`logical_episode_id(run_id, step, scenario_id, k)`、`TrainingConfig.memory`、`MISSING_MEMORY_REASON`、`make_mindtrainer_dispatcher(..., memory_resolver=)`、`factory.memory_runtime.STAND_IN_MEMORY_RUNTIME`。
- Produces: 30 篇头部「状态」与「对应实施计划」两行;`memory` 块的措辞(`digests`、`runtime` 由派发器写进本次派发的配置,**不回写指令**)。

**行数纪律**:30 篇多一行就破 900。每次改完立刻 `python3 - <<<'...'` 或 `wc -l` 看一眼(用 `python3 -c "print(len(open('docs/design/30-mindtrainer.md').read().split(chr(10))))"`,与检查器口径一致)。需要增加的句子,一律并入同一单元格;要净增超过 0 行时,先从下面的"可合并或删减"清单里腾地方。

- [ ] **Step 1: 列清单并分类**

Run: `grep -n "\[计划中\]" docs/design/30-mindtrainer.md | cut -c1-100`

行段(写计划时):6、12、31-33(图例,保留)、101、117、127(设计二之前就有,保留)、135、190-192、199-200、211、223、315、325、335、388(设计二之前就有的 `validate_sft_data`,保留)、400(同上,保留)、427、430-432、473、479-481、489-493、536、541-545、548、552、599、603、667-669、721、725-730、749-750、763-766、772、793-795、798-800、841、853-854(保留)、857(保留)、870(保留)、872-874、897-899。

| 分类 | 行段 | 处理 |
|---|---|---|
| 第 3、4 步,摘 | 6、12(头部)、101、117、135、190-192、199-200、211、223(`TrainingConfig` 的 `memory` 块)、315、325、335、427、430-432、473、479-481、489-493、536、541-545、548、552、599(派发步骤 1b)、603、667-669、721、725-730(MT-21 到 MT-26)、749-750、763-766、772、793-795、798-800(测试表)、841(第 10 章表) | `[计划中]` → `[已落地]` |
| 保留(设计二之前就有,与本设计无关) | 127、388、400、800 的 `remote` 冒烟、853、854、857、870 | 不动 |
| 保留(要真机) | 872(真模型单轮延迟够不够 90 秒)、873(真抽取与真嵌入的延迟)、874(每会话开库对 GRPO 一步的影响) | **不摘** |
| 保留(依第 7 步) | 897-899 术语表里依赖 Starling 真实后端的条目 | 第 7 步未合则留 |

- [ ] **Step 2: 写回台账裁定(逐条定位 + 改写,均不净增)**

1. 派发器不回写指令:旧措辞在 200、223、492、599、795 行,逐条改。
   - 200 行:`进入策略上下文之前解析起点记忆、写回 \`memory\` 块` → `进入策略上下文之前解析起点记忆、把摘要与运行时身份写进本次派发的配置`。
   - 223 行:`\`digests\` 与 \`runtime\` 由派发器写回,调度器写入时留空` → `\`digests\` 与 \`runtime\` 由派发器写进本次派发的配置,不回写 \`directive.yaml\`;调度器写入时留空(下称未解析)。续训要重新算,回写会盖掉 T3 比较用的原值`。
   - 492 行:`返回的摘要与身份写回 \`memory.digests\` 与 \`memory.runtime\`` → `返回的摘要与身份只进本次派发的 \`cfg\`(\`memory.digests\`、\`memory.runtime\`),不回写指令;训练记录的 \`policy_backend.memory\` 记下它们`。同一行里"派发器在生成路回写人格 id 并复校之后调它"中的"回写"指**人格 id 与情境 id 的回写**(那是已落地的真回写,生成路写进指令),**保留**。
   - 599 行(派发步骤 1b):`写回配置,之后才算 config_hash` → `写进本次派发的配置(不回写指令),之后才算 config_hash`。
   - 795 行(测试表):`起点摘要与运行时身份在进入策略上下文之前写回配置` → `……写进本次派发的配置`。
   - 新增一句:严格校验放行"未解析"的 memory 段(`digests` 与 `runtime` 都为空),只填一半拒绝;解析后再校验一次——并进 223 行同一单元格,不新开行。
2. 错误原因口径:492 行末"它抛的任何异常也判 T2"→"`DirectiveFailedError` 保留它自己的原因(替身拒绝种子要说清原因),其余异常只报类名,一律 T2"。
3. 逻辑 episode id 的真实形状:旧措辞在 493、899 行,`{run}-{stage}-{scenario}-r{k}` → 实际是 `{run_id}-grpo{step}-{scenario_id}-r{k}`(`logical_episode_id(run_id, step, scenario_id, k)`):同一情境在不同 GRPO step 反复出现,只写阶段名会撞。493 行加半句"续训从同一 step 重跑时 id 与上次相同,同名 episode 文件被覆盖,批只认本次产出的 id";899 行术语只改形状。(20 篇 436、40 篇无此形状,20 篇的改动在 Task 23。)
4. 时间预算:543 行"每条指令按 `max_turns_per_episode × 90 秒` 算(`PER_TURN_DEADLINE_S` 取 90 秒),指令结束后还原"。代码里**没有还原**:流 deadline 由轨迹源的常量 `mindtrainer.trajectory_source.PER_TURN_DEADLINE_S` 算,经 `client_factory(deadline_s=)` 传,不改运行时 `interact_deadline_s`。改成"……由轨迹源按 `max_turns_per_episode × 90 秒` 算(常量 `PER_TURN_DEADLINE_S` 取 90 秒),经 `client_factory(deadline_s=)` 传入;不改运行时的 `interact_deadline_s`,所以同开时部署会话不借用训练的值"。同一单元格改写。
5. 副本 close 失败:548 行"训练位副本 close 的等待 | 不适用 | 60 秒,与服务端 join 同值;超时判 `role_memory_failure`,这条 rollout 不重试"后补半句"。close 抛异常同样判 `role_memory_failure`、不重试,即使 rollout 本身已结束:close 失败说明副本库状态不可信"。
6. 角色位 Actio 实例的 `library_version`():训练路不读技能库,取 `{"skill": "0", "memory": "0"}`。30 篇没写,在 491 行"交出什么与轨迹源"单元格末尾补一句。
7. 排除清单、摘要口径等属 40 篇,不在本篇。

- [ ] **Step 3: 腾地方——可合并或删减的段落候选(本篇必须净增 ≤ 0)**

写计划时挑出的候选,按"删了不丢信息"的顺序:

| 候选 | 位置 | 做法 | 可省 |
|---|---|---|---|
| 时间预算表后的重复叙述 | 550 行"两个 90 秒方向相反,互不嵌套……"与 20 篇 634 行逐字重复(写计划时 `grep -n "两个 90 秒方向相反"` 核对:20 篇 634、30 篇 550、90 篇 548 行是同义改写) | 删 30 篇 550 行及其后的空行;表内两行已讲清,20 篇保留原文 | 2 行 |
| 552 行里"对手 API 回 429、5xx……"罗列 | 与 20 篇 697-698 行故障表同内容(20 篇 693-698 行是准入、白名单、join、副本删除、相邻消息超时) | 删 552 行中从"对手 API 回 429、5xx 或连不上"到"记录保持 `pending` [已落地]。"的整句,保留"单次后端调用超时……"与后面"对手位的记忆失败另有两个终止原因"两句 | 0(同一段内缩短,不省行;只在这一行留出新增措辞的字数) |
| 798 行(带记忆的端到端,摘后 `[已落地]`)与 797 行(已落地的 `--interact` 端到端) | 两行都是 `factory reconcile --once --interact` 的端到端,差别只在是否带记忆 | 合成一行:"端到端 | `factory reconcile --once --interact` 走到候选 `pending_review`,episode 都在产物树里;带记忆时训练位与对手位都是 Actio,episode 每轮带激活轨迹,训练记录的 `policy_backend` 带 `memory` 块 | 假 vLLM、替身记忆" | 1 行 |
| 721-730 决策表 MT-21 到 MT-26 的"备选"列 | 备选只是"保持原状"的重复 | 把 MT-22、MT-25、MT-26 的备选列压成一句 | 0(缩单元格,不省行) |
| 897-899 术语表 | 设计二术语(逻辑 episode id 等)各缩一行 | 缩写,不删行 | 0 |

只有前三行真能省行(共 3 行),其余是缩字数、给新增措辞腾位置。Step 2 的写回条目(223、491、492、543、548 行)都在**同一单元格内**补字:表格行不换行,行数按 `split("\n")` 计,补字不增行数。真正会增行的只有另起一段的写法,Step 2 没有这样的条目,所以**预期净增为 0**。下面的追加候选是实施时万一多出行的预案,不是必做:

| 追加候选 | 位置 | 做法 | 可省 |
|---|---|---|---|
| 摘标记之后相邻且措辞相同的两张表行 | 763、764、765 行(训练位 recall 失败瞬态、其余类别、`role_memory_*` 的合并说明) | 764 与 765 两行合成"训练位的记忆失败:瞬态类别(转换后端改抛 RoleMemoryUnavailableError)T1;其余类别(改抛 RoleMemoryFailureError)T2" | 1 行 |
| 749、750 行(interact 缺 `memory`/起点库打不开 T2;续训摘要或身份变了 T3) | 两行都是 `memory` 块的失败 | 合成一行,T2 与 T3 在同一单元格分写 | 1 行 |
| 841 行第 10 章表"带记忆的 interact" | 摘标记后这一行与第 9 章的"interact"测试行信息重复 | 把"状态"列写 `[已落地]`,说明列压到一句 | 0 |

合计可再省 2 行。**如果仍超,不要删独有信息**(MT 决策、故障表的其他行),回来找控制者。

先做 Step 2 的改写,量一次行数;若净增 > 0,再按表顺序腾,直到 `len(split("\n")) ≤ 900`。**不删其他篇没有的独有信息**(例如 MT 决策、故障表行)。

- [ ] **Step 4: 测试表补数**

30 篇第 9 章 802-822 行是"测试文件/用例数/状态"表,第 3、4 步改了、新增了这些文件,先实测再写:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -qq -p no:cacheprovider \
  tests/test_mt_config.py tests/test_mt_dispatch.py tests/test_mt_orchestrator.py \
  tests/test_mt_trajectory_interact.py tests/test_mt_dispatch_synthesis.py \
  tests/test_factory_memory_translate.py tests/test_factory_memory_runtime.py \
  tests/test_daemon_dispatcher.py tests/test_repo_lock.py 2>&1 | tail -10
```

既有文件(`test_mt_config.py` 171、`test_mt_dispatch.py` 42、`test_mt_orchestrator.py` 57、`test_mt_trajectory_interact.py` 29)改数字;`合计 503` 要重新相加(那一行的合计覆盖 `mt_` 前缀的前几组文件,**以表里原来加起来等于 503 的那批为准**,逐个换成新数再加);`test_factory_memory_*` 属 `factory_` 分组,不在 30 篇表里写(写进 90 篇 Task 27)。`check_consistency` 要求数字出自实测,换完后用 Step 5 校验。

- [ ] **Step 5: 跑检查器与行数**

Run:
```bash
python3 -c "print(len(open('docs/design/30-mindtrainer.md', encoding='utf-8').read().split('\n')))"
python3 docs/design/tools/check_docs.py 30-mindtrainer.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check
```
Expected: 行数 ≤ 900;0 错误 0 告警。

- [ ] **Step 6: 提交**

```bash
git add docs/design/30-mindtrainer.md
git commit -m "docs(design): MindTrainer 篇——训练路换成 Actio、memory 块与哈希标为已落地,摘要与身份写进本次派发的配置,不回写指令"
```

---

### Task 25: Actio(40)——记忆端口、替身、轨迹 v2 落地;`close()` 不放弃、摘要与召回口径写回(只改写,不净增)

**Files:**
- Modify: `docs/design/40-actio.md`(119 个 `[计划中]` 行,**行数 895,余量 5**;净增必须 ≤ +5,目标 ≤ 0)
- Test: 两个检查器 + `sync_docs --check`

**Interfaces:**
- Consumes: 第 1 步落地的名字:`actio.memory_port`(`MemoryHit`、`MemoryEvent`、`RememberOutcome`、`MemoryBackend`、`DeploymentMemoryStore`、`MemoryFailureError`、`MemoryEventConflict`、`MemoryClosedError`、`PERSONA_ID_PATTERN`)、`actio.memory`(`content_id`、`TupleMemory`、`FileTupleStore(root, *, extraction_failure=)`、私有 `_Ledger`)、`actio.trace`(`TRACE_SCHEMA_V1`、`TRACE_SCHEMA_V2`、`TRACE_KEYS_V2`、`MEMORY_TRACE_KEYS`)、`instantiate(..., memory=, episode_id=, interlocutors=, replay_read_only=)`、`ActioInstance.close()`。第 7 步(依之而定):`actio/memory_starling.py` 的 `StarlingMemory`、`StarlingDeploymentStore`。
- Produces: 40 篇头部「状态」「代码位置」「被依赖」「对应实施计划」四行;第 3、4、5、8 章与第 9、10 章的状态与措辞。

40 篇是最贴近代码的一篇,台账裁定最多落在这里。**先做 Step 2 的措辞写回(每条都是同一单元格改写),再做 Step 1 的摘标记**,否则摘标记后路径检查会要求该行路径存在。

- [ ] **Step 1: 列清单并分类**

Run: `grep -n "\[计划中\]" docs/design/40-actio.md | cut -c1-100`

行段:6-12(头部)、31-33(图例,保留)、68-70、108-110、115、127-128、135-136、140、146-151、156、206、209、221-226、244-245、251、258-263、273、280、311、332、335、354-355、363、386-388、392、412、443-448、451-453、465-467、479、488-489、493、539-541、550-553、558、605、608-609、612-614、674-677、711、740-751(决策 AC-13 到 AC-24)、755、764-769、783-784、806、825、836-838、845-846、859-862、866、883-888。

| 分类 | 行段 | 处理 |
|---|---|---|
| 第 1 步,摘 | 6 的"设计二 [计划中]:记忆改经记忆端口访问……"里的前半(端口、替身、每轮读写、轨迹 v2)、68-70、108-110、115、127-128、135-136、140、146-151、156、206、209、221-226 中 `memory_port.py`、`memory.py` 的 TupleMemory/FileTupleStore 两行、244-245、251、258-263、273、280、311、332、335、354-355、363、386-388、392、412、443-448、451-453、465-467、479、488-489、493、539-541、550-552、558、605、608-609、612-614、674-677、711、740-748、750-751(AC-13 到 AC-24 中不涉及 Starling 适配器的)、755、764-769、783-784、806、825、836-837 | `[计划中]` → `[已落地]` |
| 依第 7 步 | 6 的后半(Starling 适配器)、7-12 里的 `memory_starling.py`、`StarlingMemory`、`StarlingDeploymentStore`、extra `memory` 的行,221-226 里 `memory_starling.py` 一行、553(Starling 公开接口)、838、845-846(Starling 路径语义召回)、866、883-888 里的 Starling 术语 | 第 7 步合了就摘;没合就保留 |
| 保留(要真端点) | 859(抽取与嵌入延迟)、860(命中质量)、861(不跑 tick 的召回质量)、862(外部端点向量稳定性) | 不摘 |

- [ ] **Step 2: 写回台账裁定(旧措辞逐条定位)**

1. **`close()` 不放弃事件**(设计源 f053cd0 之后改的,同属  的前提):旧措辞在 363、377、388、421、448、677、818、836 行,逐行改:
   - 363 行:`五个方法里 \`remember\` 推进它,\`close\` 放弃本实例留下的 \`pending\` 行` → `五个方法里 \`remember\` 推进它;\`close\` 只关库,不放弃任何事件(放弃是终态,同 key 再写会抛冲突,放早了就堵死重试)`。
   - 377 行:`放弃不进端口签名:\`close()\` 先逐个放弃本实例写过的 \`pending\` key,再关库。所以部署 episode 结束后库里没有它的 \`pending\` 行;门② 的事件不经实例,由管理面写与放弃` → `放弃不进端口签名,也不在 \`close()\` 里做:要等调用方知道这条 episode 最终成功还是失败。部署库由 MindAgent 经管理面放弃:episode 成功提交后对它留下的 \`pending\` 行逐个 \`abandon_event\`,最终失败前先用起点副本 \`restore_from\`,回滚后库里本来就没有这条 episode 的事件行。门② 的事件不经实例,由管理面写与放弃`。
   - 388 行(不变量 8):`\`close\` 先放弃本实例留下的 \`pending\` 事件,再关库` → `\`close\` 只关库,不放弃事件`。
   - 421 行代码注释:`# 先放弃本实例留下的 pending 事件,再关库` → `# 只关库,不放弃事件`。
   - 448 行:`转发给端口的 close:先对本实例写过、仍 \`pending\` 的 key 逐个放弃,再关库` → `转发给端口的 close:只关库,不放弃事件`。
   - 677 行:`episode 结束时 \`close()\` 放弃仍未推进的` → `episode 最终结束时由 MindAgent 经管理面放弃仍未推进的,\`close()\` 不放弃`。
   - 818 行(测试):`\`close()\` 放弃仍 \`pending\` 的 key 再关库,部署库里不留该实例的 \`pending\` 行` → `\`close()\` 只关库,\`pending\` 行原样留在库里(由 MindAgent 放弃)`。
   - 836 行:`关闭语义(\`close()\` 放弃本实例留下的 \`pending\` 事件)` → `关闭语义(\`close()\` 只关库)`。
   - 551、553 行及其他含"关闭实例会放弃 \`pending\` 事件"的单元格(552 行):`关闭实例会放弃 \`pending\` 事件并关闭它持有的端口` → `关闭实例会关闭它持有的端口(不放弃事件)`。
2. **`embed_pending` 返回成功条数、有失败就抛**(设计源 128eefe):419 行代码注释 `# 只做嵌入,返回条数` → `# 只做嵌入,返回成功条数;有失败就抛瞬态 MemoryFailureError`。
3. **`TupleMemory` 的 id 一律是内容 id**(设计源 cb32d8e 之后的改动):221 行 `TupleMemory:包 MemoryEntry 元组与 retrieve 的替身记忆` 后补半句"`recall` 返回的 `MemoryHit.id` 一律是 `content_id(subject, content)`,不用条目自带的 `id`";AC-16(743 行)后半"同一内容的两条语句得到同一个 id"保留,补"替身与 Starling 一致";`memories=` 简写实例的 `memory_hits` 与今天不同,既有断言 `mem-1` 字面量的测试已随第 1 步改写——这一句并进 AC-16 的"代价"列。
4. **摘要口径**():280-287 行摘要算法表,"替身"那行改成"TupleMemory 对 `snapshot(entries)` 的结果(召回实际看到的条目,不是原始列表)逐条 `to_dict()` 去掉 `id` 后的排序列表,加事件表,算同样形状的 sha256。Starling 一侧按全行:被取代的行改为 `archived` 仍是召回候选,`volatile` 与 `pending_review` 行影响后续写入的冲突探测,所以都进摘要。两边的原则一致:影响召回或后续写入的行进摘要"。把 284 行"进摘要"单元格里"全部列"后补"(全行,不按状态过滤)"。
5. **排除清单增补**():285 行"排除清单"单元格在 `inferred_at` 之后补:`confidence_history_json` 里每个条目的 `ts`(规范化时去掉)、engram 的 UUID(换成 \`engram:<content_hash>\`);`observed_at` 在写入端改取 engram 的 `created_at`,所以不再随墙钟变。同一单元格改写。
6. **召回放宽到 volatile**():
   - 386 行(不变量 6):`训练副本每轮只做嵌入,不做后台维护:后台维护有随机性和负载门控,会让同一起点得到不同状态` 后补一句"新写的语句在 Starling 里是 `volatile`,向量召回只取 `consolidated`、`archived`,只有 `tick` 的回放会巩固它;所以适配器的 `recall` 显式放宽到 `volatile`(review 与 holder 守卫不变),代价是这些行没经回放去重,召回里可能有近重复"。
   - 847 行(局限表)"训练里不做后台维护 | 没有 TTL、回放与巩固;召回只靠嵌入"→"召回含未巩固的 `volatile` 行(适配器放宽),可能有近重复"。
   - 861 行(未核实项)保留。
   - 553 行 Starling 公开接口列表:`Memory.recall` 后补"(带 `holder`、`strict`、`include_volatile`)"。
7. **`recall` 的严格模式**(设计源 128eefe):773 行 `recall`、`embed_pending` 的嵌入请求超时或连不上 | 真 | → 在 `recall` 一侧补"查询词嵌入失败抛 `RecallDegradedError`,适配器译成瞬态 MemoryFailureError;`embed_pending` 返回的 `failed` 大于 0 同样抛",并进同一单元格。
8. **`remember_event` 签名**():426-433 行 `DeploymentMemoryStore` 代码块与 553 行 Starling 接口列表里,`remember_event(persona_id, key, text, *, interlocutor)` 管理面签名**不变**(`epoch` 是 Starling 门面的参数,由适配器填,不进管理面);只在 553 行"`Memory.remember_event`"后补"(带 `epoch`)",`events` 表加 `tenant_id`、`failure_category`、主键 `(tenant_id, key)`、`[key] ` 前缀在 Starling 侧加,这几项只出现在 90 篇 470 行 Starling 接口列表,40 篇不展开。
9. **`abandon_event` 未知 key**():40 篇 275 行状态表 `abandoned` 行出口列补"未知 key 抛 MemoryFailureError(`transient` 为假),已放弃再放弃为空操作";`committed` 行不许放弃保持。
10. **`MemoryClosedError` 与 `close(timeout=)`**():388 行不变量 8 后补"`close` 等门面在途调用返回后合并 WAL 并关连接;关后调用抛 MemoryClosedError"。
11. **异常包装**(R5):764 行"`recall`、`embed_pending` 失败,或 `remember` 抛异常"后补"实例把端口抛出的非 MemoryFailureError 异常包成 MemoryFailureError(类名, `transient` 为假):未知异常判确定性失败,宁可 T2 不无限重试"。
12. **人格 id 字符集**(R6):258-263 行实体表加不了行,把 262 行 DeploymentMemoryStore 的"约束"列末尾补"人格 id 须匹配 `PERSONA_ID_PATTERN`(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`),两个部署管理面拼路径前都校验,不合法抛 ActioDataError"。
13. **空查询词与 `episode_id` 必填**(R3、R4):411-440 行入口签名之后的叙述里补一句:"查询词为空串时实例不调 `recall`;`episode_id` 必填按'会写库'判断,即 `replay_read_only` 为假时要求"(443 行单元格里已有"可写端口没给 `episode_id` 时抛 `ActioDataError`",核对一致,只补空查询词)。
14. **`TRACE_SCHEMA` 改名**(R9):469 行 `actio.trace` 模块表 `TRACE_SCHEMA` → `TRACE_SCHEMA_V1`、`TRACE_SCHEMA_V2`、`TRACE_KEYS_V2`。**这一行已在 `[已落地]` 行里**,所以改名后符号在代码里存在,检查器不告警;若不改,旧名在代码里已不存在,会告警"在代码与施工计划里都找不到"。
15. **工作包 9 的 `instantiate` 参数**(设计源 aeb3188):443 行已含 `interlocutors`,411-414 行代码块里 `instantiate` 签名 `memories=None, memory=None, episode_id=None, replay_read_only=False,` 缺 `interlocutors=None`,补进去(同一行,不增行):`memories=None, memory=None, episode_id=None, interlocutors=None, replay_read_only=None,`(`replay_read_only` 默认 `None`,`memories=` 简写默认只读,`memory=` 默认可写;核对 `src/actio/instance.py` 的真实默认值后抄)。

- [ ] **Step 3: 头部四行与第 9 章**

- 第 6 行「状态」:把"设计二 [计划中]:……"整段改成已落地口径,Starling 适配器那一半依第 7 步:第 7 步未合,写"……;Starling 适配器与树外记忆配置 [计划中]"。
- 第 9 行「代码位置」、第 10 行「被依赖」、第 12 行「对应实施计划」按同一规则改。**这一行不能写计划文件路径,也不能写带日期的文件名**:`check_docs` 的禁用规则对 `docs/superpowers` 报错,对 `计划 2026`(文件名里的"计划"加日期)报"引用实施计划编号";写计划时在一份 40 篇的副本上实测过,只有不带日期的短名能通过。所以这一行写成:`工作包 6(任务 70 至 78)[已落地]。设计二的 Actio 部分:替身路径施工计划(agent-memory-standin,第 1 到 5 步中属于 Actio 的第 1 步:记忆端口、替身、每轮读写、轨迹 actio.trace.v2)[已落地];Starling 路径施工计划(agent-memory-starling,第 7 步:Starling 适配器与树外记忆配置,依 Starling 的 PR 合入)[计划中 或 已落地,依第 7 步]`。完整路径只写进提交信息(见 Task 31 与本步末尾"需要控制者裁定"第 1 条)。
- 第 9 章 803-804 行:`合计 185 个用例` 是 `b826440` 基线数;第 1 步改了 `test_actio_instance.py`(30→新数)、新增 `tests/test_actio_memory_port.py`、`tests/test_actio_memory_contract.py`、`tests/test_actio_file_tuple_store.py`。先实测:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -qq -p no:cacheprovider \
  tests/test_actio_signals.py tests/test_actio_skills.py tests/test_actio_memory_experience.py \
  tests/test_actio_instance.py tests/test_actio_realtime_e2e.py tests/test_actio_memory_port.py \
  tests/test_actio_memory_contract.py tests/test_actio_file_tuple_store.py \
  tests/test_realtime_proto_submit_trace.py tests/test_runner_activation_trace.py \
  tests/test_realtime_activation_trace.py 2>&1 | tail -12
```

把新文件加进表(层次"契约"、"落盘替身"),既有 30 改新数,`合计 185 个用例` 重新相加,头部第 6 行与 825 行的"185 个用例"同步改。806 行"设计二的测试 [计划中]。不写用例数,等施工后按实测填"改成"设计二的测试 [已落地]",并把下面那张叙述表里已有测试的行并入上面的用例数表(新增文件那三行),叙述表只留 Starling 相关两行(真 Starling 的契约、摘要、事件状态)与依第 7 步的行——这样叙述表缩短,抵消表里新增的行数。

- [ ] **Step 4: 行数控制——可合并或删减的段落候选**

| 候选 | 位置 | 做法 | 可省 |
|---|---|---|---|
| 806-820 叙述表里已被用例数表覆盖的行 | 契约(两行)、每轮读写、轨迹 v2、重试与放弃、集成(decide 恒定) | 并入上面表的"对象"列,叙述表只留真 Starling 的三行 | 6-8 行 |
| 840-846 局限表 "二字切分检不到近义表达" 与 "Starling 没装时退化为替身" | 两行都在讲替身与 Starling 的差别 | 合成一行 | 1 行 |
| 574-600 图 3 文字版与 561-566 正文的重复 | 图块是生成的,**不能删**;但 561-566 的叙述段有两句和图说明逐字相同 | 删正文里重复的一句 | 1-2 行 |

先做 Step 2、3 的改写后量行数:`python3 -c "print(len(open('docs/design/40-actio.md', encoding='utf-8').read().split('\n')))"`,≤ 900 即可;超了按上表腾。

- [ ] **Step 5: 跑检查器**

Run: `python3 docs/design/tools/check_docs.py 40-actio.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。常见失败:`符号告警 ... 只在施工计划里` 说明某个依第 7 步的符号所在行误摘了标记;`测试数 N 不在实测...` 说明 185 没改全(头部、803、825 三处)。

- [ ] **Step 6: 提交**

```bash
git add docs/design/40-actio.md
git commit -m "docs(design): Actio 篇——记忆端口、替身、轨迹 v2 标为已落地,close 只关库、摘要按 snapshot 与全行、召回放宽到 volatile 写回,补测试数"
```

---

### Task 26: MindAgent(50)——部署记忆、轮次摘要、快照重放、门② 改路落地

**Files:**
- Modify: `docs/design/50-mindagent.md`(154 个 `[计划中]` 行,行数 844,余量 56;目标净增 ≤ +10)
- Test: 两个检查器 + `sync_docs --check`

**Interfaces:**
- Consumes: 第 5 步落地的名字:`mindagent.memory_layout.MemoryLayout(root, suffix=".json")`、`controlplane.lock.hold_file_lock`、`FileLockBusyError`、`mindagent.errors.RestoreFailedError`、`mindagent.deployment.rerun_episode_id(base, skill_version)`、`run_deployment_episodes(store, make_instance, client_factory, *, memory_store, memory_layout, round_id, library_version, episode_digests, on_episode_start, stream_deadline_s, ...)`、`make_admit_proposal_handler`、`check_memory_library_empty`、`factory.memory_runtime.stand_in_memory_root`、`controlplane.lock.git_dir`、`controlplane.states` 的 `approved → rejected`。
- Produces: 50 篇头部「状态」「依赖」「对应实施计划」与第 4 到 9 章。

- [ ] **Step 1: 列清单并分类**

Run: `grep -n "\[计划中\]" docs/design/50-mindagent.md | cut -c1-100`

行段:6、10-12、31-33(图例,保留)、66、102、118、124-125、137-138、192-194、197-200、207-208、213、220、224-226、237、240、244、248-253、259-261、265、293、297-298、302、309-319、323、342、360、395、402-407、412、465-475、499-504、510-511、528-529、580、584-586、589-591、599、610-618、640、645、657-673、677、692-699、714-715、719、726-736、771-776、782、798-803、817-822、835、839-843。

| 分类 | 处理 |
|---|---|
| 第 5 步全部(部署记忆、人格库锁、起点副本、摘要、复用新判据、实例工厂、验证快照、门② 改路、`approved → rejected`、载荷加字段) | 摘 |
| 依第 7 步:`StarlingDeploymentStore` 的选择(407 行"配置在场用 StarlingDeploymentStore")、224 行"树外 Starling 库文件"、225-226 行 | 第 7 步未合则把"Starling"相关半句留 `[计划中]`,FileTupleStore 半句摘 |
| 保留(要真端点或真 Starling) | 817(真抽取嵌入延迟)、818(`VACUUM INTO` 副本跨迁移版本)、819(召回质量)、820(向量分布)、803(验证重放与线上行为不一致,这是已知局限、第 5 步落地后写"已落地"而非保留——**摘**) |
| 设计二之前就有 | 33(图例),保留 |

(817-820 是风险表里"未核实"的四项,保留;800 到 803 是已知局限,第 5 步落地,摘。)

- [ ] **Step 2: 写回台账裁定**

1. **派发器之外,轮次产物写回口径**:298 行 `episode_digests` 说明"每条 episode 开始前写回一次,轮次产物本来就逐步回写"——这是轮次产物自己的逐步回写(已落地的真回写),**保留**。31R 不适用于此。
2. **轮次拿不到人格库锁判 T3**(,设计源 M20 写的是 `AgentRoundFailedError`):50 篇 482、665(MA-22)行 `轮次判 \`AgentRoundFailedError\`,T3` 指的是**覆盖失败**,真名是 `RestoreFailedError`(继承 `AgentRoundFailedError`,接住它时不回滚,库与副本都保留)。逐处改:
   - 482 行:`覆盖失败(文件系统错误、摘要对不上):轮次判 \`AgentRoundFailedError\`,T3` → `覆盖失败(文件系统错误、摘要对不上):抛 \`RestoreFailedError\`(继承 \`AgentRoundFailedError\`),轮次 T3`。
   - 665 行 MA-22:`覆盖失败判 \`AgentRoundFailedError\`(T3)` → `覆盖失败抛 \`RestoreFailedError\`(T3)`。
   - 703 行(异常列表)补 `RestoreFailedError`:`\`errors\` 模块里有 \`MindAgentDataError\`……、\`AgentRoundFailedError\`` 后补 `、\`RestoreFailedError\``。
   - 拿不到库锁:481 行与 MA-16(659 行)已写"轮次 `failed`,T3",补半句"(\`FileLockBusyError\` 在轮次处理器里改抛 \`AgentRoundFailedError\`)"。
3. **放弃 pending 的时机**():旧措辞在 488、664(MA-21)、699、801 行及 40 篇已改的对应处:
   - 487-488 行"episode 成功 | 先落盘 episode,再删它的起点副本;删起点副本是这条 episode 的提交点"与"episode 结束 | 对自己留下的 `pending` 事件行调 `abandon_event`"两行**合并并改序**:`episode 成功 | 先落盘 episode,再放弃本 episode 留下的 \`pending\` 行,最后删起点副本(提交点)`。崩在放弃与删副本之间:重入先用起点副本覆盖(覆盖后 abandoned 行一起回到起点状态)再重跑,不会撞已放弃的 key。这样 488 行那一行删掉,表少一行,净减 1 行。
   - 664 行 MA-21:`episode 结束时对自己留下的行调 \`abandon_event\`` → `episode 落盘后、删起点副本之前对自己留下的行调 \`abandon_event\``。
   - 起点副本**删除失败**():483-489 表里"episode 成功"行的失败列已写"副本没删掉:重入时这条 episode 按未提交处理,先覆盖再重跑",补半句"删除失败上抛,轮次留 `pending`:副本留着就是未提交;`runs/` 下的会话副本删除失败只记日志,是另一回事"。
4. **门② 部分失败**()和**混合提案**()和**瞬态分类**():589-599 行"记忆增量在第 2 步的失败有三条出口"表与 595 行、610、662(MA-19)、696-697 行。
   - 595 行(抽取失败)"先对该 key 调 `abandon_event`,……再写一条 T2 失败记录 | `approved → rejected`,带原因" → 两句:"一条记忆增量都没提交过:先对该 key 调 `abandon_event`,再写 T2 失败记录,整份提案 `approved → rejected`。前几条已 `committed`、后一条抽取失败:这一条 `abandon_event`,写 T2 失败记录(原因 `partial admission`),提案**不**转 `rejected`,留在 `approved` 等人处理——已提交的事件放弃不了,转了就是'被拒提案在库里留着记忆'"。把表的"提案状态"列改成"`approved → rejected`(一条都没提交时);留在 `approved`(部分已提交时)"。
   - 混合提案(记忆加技能)的记忆整份抽取失败时整份转 `rejected`、技能条目也不落:并进 585 行第 2 步"写库条目"单元格,"记忆先于技能写:抽取失败要整份拒绝,技能已写成 `effective` 就收不回了"。
   - 596 行(抛异常)"不放弃事件,只写失败记录 | 留在 `approved`" → 补"瞬态 MemoryFailureError 写 T1,其余 T2;提案都留 `approved` 等重入"。
   - 610、662(MA-19)、696、697 行同步:"事件先 `abandoned`,T2 失败记录,提案 `rejected`" → "……(一条都没提交时);部分已提交则提案留 `approved`";697 行"门② 记忆收录的其他异常 | T2" → "T1(瞬态)或 T2"。
   - 801 行"被 `rejected` 的记忆提案不能复活"保留;另补一项局限,**并入 801 行同一单元格**:"部分已提交的记忆提案留在 `approved`,要人决定"。
   - 727 行(测试)补"部分失败留 `approved`"。
5. **新一轮第一次运行用不带后缀的 base id**():237、309(MA-I2)、473、668(MA-25)行写"技能版本变后的重跑加后缀 `-s<技能版本>`":补半句"新一轮第一次运行用不带后缀的 `deploy-<model>-<scenario>-<k>`;只有 base 已提交且技能版本旧了才用 `-s<版本>`(`rerun_episode_id(base, skill_version)`)"。237 行单元格并入,不增行。
6. **人格库锁覆盖所有绑定人格、`episode_digests` 只记智能体位人格**():298 行 `episode_digests` 说明补"只记智能体位人格;部署对手位每会话一份空 TupleMemory,不进摘要。轮次锁覆盖全部绑定人格,保守,不改"。
7. **替身部署记忆根目录不是临时目录**():407 行 `根目录取 \`control-plane/\` 之外的临时目录` → `根目录是 \`<git 目录>/rismforge-memory\`(与 \`rismforge.lock\` 同处,不入树、跨重启保留)`。730 行测试表"人格库锁 | 临时目录"的"临时目录"指测试夹具,保留。**不用 `mkdtemp`:每次重启换空库会让崩溃重入与门② 收录失效**——这半句并进 407 行。
8. **门② 门锁与延后**():589 行"门② 与在途轮次"段与 MA-27(670 行)不变;补 `make_admit_proposal_handler`、`check_memory_library_empty`、`hold_file_lock` 三个名字只在 90 篇与代码里,50 篇不展开(`hold_file_lock` 在 `controlplane.lock`,50 篇依赖行已写 ControlPlane)。
9. **验证快照与"覆盖"关系**:无新措辞。

- [ ] **Step 3: 头部三行与测试表**

- 第 6 行「状态」:`部署记忆换成 Starling(经部署记忆管理面访问)、部署实例按 episode 新造、门② 的记忆收录改路 [计划中]` 拆成两半:部署实例按 episode 新造、门② 改路、人格库锁、起点副本 [已落地](替身 FileTupleStore 上);换成 Starling [计划中 依第 7 步]。
- 第 9 行「依赖」`Actio(……含记忆端口、管理面与 Starling 适配器 [计划中])`:端口与管理面摘,Starling 适配器依第 7 步。
- 第 12 行「对应实施计划」`智能体记忆设计的第 7 步(部署记忆、轮次摘要、快照重放、门② 改路)[计划中]`:写成"智能体记忆设计的第 5 步(部署记忆、轮次摘要、快照重放、门② 改路,替身路径)[已落地]"(原文写的"第 7 步"是旧施工顺序里的步号,现在 MindAgent 是第 5 步;施工顺序在 `f053cd0` 调过)。
- 第 9 章用例数:719 行 `共 152 个用例` 与 809 行 `九个测试文件共 152 个用例` 是基线实测数。第 5 步改了:`test_ma_admit.py`(22→新数)、`test_ma_rounds.py`(28)、`test_ma_deployment.py`(22),新增 `test_ma_memory_layout.py`,`test_ma_config_library.py`(27)也加了用例,`test_ma_e2e.py` 可能改。实测:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -qq -p no:cacheprovider \
  tests/test_ma_config_library.py tests/test_ma_deployment.py tests/test_ma_analysis.py \
  tests/test_ma_proposals.py tests/test_ma_verify.py tests/test_ma_packaging.py \
  tests/test_ma_admit.py tests/test_ma_rounds.py tests/test_ma_e2e.py tests/test_ma_memory_layout.py 2>&1 | tail -12
```

把新文件(`test_ma_memory_layout.py`,覆盖"目录布局与人格库锁")加进 744-755 行表,既有行数字换新,719、809 的"152"重新相加。**注意 `check_consistency` 的 `NUM_PAT` 会把 719 行"共 N 个用例"与实测的 `ma_` 分组和比对,所以汇总必须是所有 `test_ma_*.py` 文件之和**(`_group_of` 把 `ma_` 前缀归到一组)。719 行"部署记忆的测试对象列在下表后半 [计划中],不含用例数"改成"[已落地],用例数在下表"。

- [ ] **Step 4: 行数**

余量 56,上面写回合计净增约 8 行;若超 +10,可合并的候选:

| 候选 | 位置 | 做法 | 可省 |
|---|---|---|---|
| 487-488 两行合并 | 见 Step 2 第 3 条 | 已计入 | 1 行 |
| 727-737 测试叙述表与 744-755 用例数表中部署记忆的对象重复 | 并入用例数表"覆盖"列 | 删叙述表里已有对应文件的行 | 3-4 行 |
| 771-776 第 10 章已落地与计划中表 | 五行 `[计划中]` 摘成 `[已落地]` 后互相相似 | 合并成两行(部署记忆、门② 改路) | 3 行 |

- [ ] **Step 5: 跑检查器**

Run: `python3 docs/design/tools/check_docs.py 50-mindagent.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。

- [ ] **Step 6: 提交**

```bash
git add docs/design/50-mindagent.md
git commit -m "docs(design): MindAgent 篇——部署记忆、轮次摘要、快照重放、门② 改路标为已落地,放弃时机、部分失败、替身根目录写回,补测试数"
```

---

### Task 27: factory 与命令行(90)——记忆装配、转换后端、对手包 Actio、时间预算落地(只改写,不净增)

**Files:**
- Modify: `docs/design/90-factory-and-cli.md`(112 个 `[计划中]` 行,**行数 900,余量 0**;净增必须 ≤ 0)
- Test: 两个检查器 + `sync_docs --check`

**Interfaces:**
- Consumes: 第 3、4、5 步落地的名字:`factory.memory_translate`(`MemoryTranslatingBackend`、`FactoryMemoryUnavailableError`、`FactoryMemoryFailureError`)、`factory.memory_runtime`(`STAND_IN_MEMORY_RUNTIME`、`stand_in_memory_resolver`、`stand_in_memory_root`)、`AgentRuntime.client_factory(*, deadline_s=)`、`run_daemon(..., on_locked=)`、`PER_TURN_DEADLINE_S = 90.0` 在 `mindtrainer.trajectory_source`、`POLICY_TIMEOUT_S`、`INTERACT_TURN_TIMEOUT_S`、`controlplane.lock.git_dir`。第 7 步(依之而定):`factory.memory_config`、`RISMFORGE_MEMORY_CONFIG`、清扫函数、`.owner` 归属检查、`StarlingDeploymentStore` 的选择、Starling 运行时解析器。
- Produces: 90 篇头部三行(「代码位置」「依赖」「对应实施计划」)与第 3 到 10 章。

**一个关键事实**:90 篇里相当多的 `[计划中]` 讲的是**第 7 步**的东西(`factory.memory_config`、本机记忆配置、第 12 到 17 条拒绝项、`.owner`、Starling 解析器),它们依第 7 步。第 7 步未合入时这些行**全部保留**;第 3、4、5 步的东西(转换后端、对手包 Actio、`policy_for_rollout`、时间预算、替身解析器、替身部署根目录、部署实例工厂)摘。

- [ ] **Step 1: 列清单并分类**

Run: `grep -n "\[计划中\]" docs/design/90-factory-and-cli.md | cut -c1-100`

行段:6-10(头部)、31-33(图例,保留)、119、133、139、183、189、194-195、199-202、249-250、283-284、301-305、317、329、333、358、368-369、374-375、420、428-430、462、466-470、525、543-553、566、581-586、598、698、717、721、724-736、749-754、759-766、810-815、823、835-836、848-853、873-877、896-899。

| 分类 | 行段 | 处理 |
|---|---|---|
| 第 3 步,摘 | 194-195(`factory.agent`)、199(`factory.interact_policy`)、201(`factory.memory_translate`)、202(`respond_guided` 与超时)、543(策略工厂形态)、544(会话登记)、548(时间预算:等待上限)、549(内层与记忆)、550(对手位包 Actio)、724-736 中 FC-24、25、26、27(清扫的 `run_daemon(on_locked=)` 部分)、30、31、32、759-766 中对应故障行 | `[计划中]` → `[已落地]` |
| 第 4 步,摘 | 301-305(`interact` 段 `memory: {seed}`)、420、428-430、545(`memory_resolver` 的替身一路)、546(续训与替身)、547(每条 rollout 的私有副本)、698(`approved` 相关不在本篇)、717、721、749-754 中替身解析器相关行 | 摘 |
| 第 5 步,摘 | 374(替身部署根目录)、733(FC-29 的 FileTupleStore 半句)、325-327(目录布局表)、813(部署路装配测试)、874 | 摘 |
| 依第 7 步 | 6-10(头部)里 `src/factory/memory_config.py`、`memory_translate.py` 中的 `memory_config`、200(`factory.memory_config`)、283-284(本机记忆配置、记忆库目录)、329(`.owner`)、333、358、368-370(第 11 到 12 条中 Starling 部分)、375、462、466-470 中 Starling 的两行(469 的 `memory_starling`、470)、525、552(启动清扫的 `reconcile` 一半可摘:`run_daemon(on_locked=)` 已在第 3 步)、566、581-586(第 12 到 17 条拒绝项)、598、721 | 第 7 步未合则留 |
| 保留(要真端点或真机) | 848、849(Starling 未上 PyPI,源码构建)、850(记忆库必须与 Actio 同机)、851(挂死的对手后端占会话名额——属第 2 步,落地后改"已落地"并留作已知局限)、873(延迟)、874(每会话开库对 GRPO 一步)、875(向量稳定性)、876(两个开关同开取 90 秒)、877(旧版本 Starling 写下的库) | 851 摘,其余不摘 |

(853 行 `writeProduct` 那条是设计二之前就有的 `[计划中]`,与本步无关,不动。)

- [ ] **Step 2: 写回台账裁定**

1. **派发器不回写指令**():旧措辞在 545、441-443(`memory_resolver` 代码注释)行:
   - 545 行:`摘要与身份一起写回指令配置的 \`memory\` 块,之后才算 \`config_hash\`。训练记录的 \`policy_backend\` 带同一个 \`memory\` 块` → `摘要与身份一起写进本次派发的配置(不回写 \`directive.yaml\`),之后才算 \`config_hash\`。训练记录的 \`policy_backend.memory\` 带同一个块,读者由此看到记忆从哪来`。
   - 301 行:`调度器把它写进指令配置时,\`digests\` 与 \`runtime\` 留空,派发时才算` → 补"(称未解析;校验放行未解析的段,只填一半拒绝;解析后再校验一次)"。并入同一段,不增行。
2. **替身部署记忆根目录**():374、733 行:
   - 374 行:`它也落盘,根目录取控制面仓库之外的临时目录,不进产物树` → `它也落盘,根目录是 \`<git 目录>/rismforge-memory\`(与 \`rismforge.lock\` 同处,由 \`controlplane.lock.git_dir\` 给出;不入树,跨重启保留)。不用临时目录:每次重启换一个空库,崩溃重入与门② 收录都会失效`。
   - 733 行 FC-29:`根目录取控制面仓库之外的临时目录` → `根目录是 \`<git 目录>/rismforge-memory\`,不是临时目录(重启要保留)`;备选列"替身不落盘"保留。
   - 810、813 行测试表"临时目录"指测试夹具,保留。
3. **时间预算常量**():199、548、736 行 `(PER_TURN_DEADLINE_S 取 90)`:这个常量现在在 `mindtrainer.trajectory_source`,由轨迹源经 `client_factory(deadline_s=)` 传;`factory.interact_policy` 里原来那个常量与"改写运行时 `interact_deadline_s`"已删。在 199 行末补"`factory.interact_policy` 不再改写运行时的 `interact_deadline_s`;两开关同开时各自按自己传的值,部署会话不借用训练的 deadline"。848 行同理。
4. **两个开关同开**(设计源 128eefe):876 行"同开时服务端等提交取 90 秒 | 部署会话只是多等" → 补"整条流 deadline 今天也只有一份,所以改为按调用方给:`client_factory(*, deadline_s=)`;轨迹源与部署驱动器各自传 `max_turns × 90` 与 `max_turns × 40`"。并入同一单元格。
5. **`.owner` 原子创建**(设计源 128eefe):329、370、586、732(FC-28)行的"首次使用时写入"→"首次使用时以 `O_CREAT | O_EXCL` 原子创建,`FileExistsError` 时读回再比;两个仓库同时首次使用,只有一个能创建,另一个读到对方的路径而退出码 2"。**依第 7 步**:第 7 步未合则这几处保留 `[计划中]` 并仍改措辞。
6. **Starling PR 接口列表**(、):470 行"缺的公开接口经 PR 补进 Starling:`Memory.recall` 的 `holder`、嵌入器注入、`make_openai_embedder`、`embed_pending`、`digest`、`backup_to`、`close`、`remember_event`,以及事件表配套的 `next_seq`、`pending_events`、`abandon_event`" → 改成:`Memory.recall` 的 `holder`、`strict`(查询词嵌入失败抛 `RecallDegradedError`)、`include_volatile`(召回放宽到未巩固的 `volatile` 行);`Memory.open(embedder=)`、`make_openai_embedder`(带 `max_retries`);`embed_pending` 返回 `EmbedStats`;`digest`、`backup_to`、`close(timeout=)`;`remember_event(key, text, *, interlocutor, epoch)`;事件表 `events` 加 `tenant_id`、`failure_category`,主键 `(tenant_id, key)`,配套 `next_seq`、`pending_events`、`abandon_event`(未知 key 抛 `KeyError`)。业务逻辑(三段写入、摘要、备份、嵌入循环)进 C++ 内核,Python 门面只转发(Starling 的 `CLAUDE.md` 规定)。`[key] ` 文本前缀在 Starling 一侧加。**这一行的标记依第 7 步 PR 是否合并**:PR 未合,标记留着。这一行是单元格,净增约 0 到 1 行,用下面的候选腾地方。
7. **记忆单次调用口径**(设计源 128eefe):549 行"记忆单次网络调用 4 秒"保留;补"`make_openai_embedder` 带 `max_retries`(默认 0)"。
8. **嵌入失败不再静默**(设计源 128eefe):549 行或 FC-30 之后补一句"嵌入查询词失败抛 `RecallDegradedError`、`embed_pending` 返回 `failed` 大于 0,适配器都译成瞬态 MemoryFailureError,不静默"。并入 201 行 `factory.memory_translate` 的说明。

- [ ] **Step 3: 头部与测试表**

- 第 6 行「状态」末尾未见设计二的叙述;核对 `sed -n 6p`,若含设计二 `[计划中]` 就按上表摘。
- 第 9 行「代码位置」`src/factory/memory_config.py`、`src/factory/memory_translate.py` [计划中]:`memory_translate.py`、`memory_runtime.py` 摘并补 `memory_runtime.py`;`memory_config.py` 依第 7 步。
- 第 12 行「对应实施计划」:不写路径,见 Task 25 Step 3 的同一规则。写:`命令行泛化与 confirm:工作包 8(任务 96 至 97);组合根与端到端:工作包 9(任务 100 至 109);设计二的组合根部分:替身路径施工计划(agent-memory-standin,第 3、4、5 步里的装配)[已落地];Starling 路径施工计划(agent-memory-starling,第 7 步:本机记忆配置、Starling 解析器、清扫、extra)[计划中 或 已落地,依第 7 步]`。
- 第 9 章测试表:既有行 `tests/test_factory_handlers.py`(49)、`tests/test_factory_interact_wire.py`(含在 `--interact` 装配行)、`tests/test_mt_wire.py` 数字随第 2 到 5 步变了;新增 `tests/test_factory_memory_translate.py`、`tests/test_factory_memory_runtime.py`(第 3、4 步)。实测:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -qq -p no:cacheprovider \
  tests/test_factory_handlers.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py \
  tests/test_factory_wire.py tests/test_factory_cli.py tests/test_factory_opponents.py \
  tests/test_factory_memory_translate.py tests/test_factory_memory_runtime.py \
  tests/test_mt_wire.py tests/test_repo_lock.py 2>&1 | tail -12
```

810-815 行是 `施工时定` 的叙述行,每行的"测试文件"列写"施工时定"+状态 `[计划中]`:把落地了的行改成真实文件名与用例数(替身路径端到端、部署路装配、对手工厂包 Actio、起点哈希与副本、extra 缺失跳过各对应哪个文件,查 `grep -ln "stand-in\|STAND_IN" tests/test_factory_*.py` 确定),依第 7 步的"记忆配置读取"、"extra 缺失跳过"两行留着。

- [ ] **Step 4: 腾地方——可合并或删减的段落候选(本篇必须净增 ≤ 0)**

| 候选 | 位置 | 做法 | 可省 |
|---|---|---|---|
| 666-681 的 6.4"九个工作包的顺序与依赖"整节(16 行) | 九行表讲的是历史施工顺序,00 篇路线图已有同样信息;已全部落地 | 压成 4 行:小节标题、一句话("工作包 1、8 先于 9;6、7 并行")、空行 | 约 12 行 |
| 6.5"首次部署"表后一段(694 行) | **不删**:写计划时 `grep` 确认它是独有信息(基线无命令、第一条指令手写、MindAgent 空库不会长出第一条技能),别处没有 | 保留 | 0 |
| 810-815 六行叙述测试行 | 落地后与用例数表(792-808)合并 | 把"测试文件"列填实后,与上面既有行同类的并入 | 2-3 行 |
| 898-899 术语表末两条 | 与 00 篇术语重复 | 缩成一行 | 1 行 |

腾的顺序:先 666-681(约 12 行,够用),不够再合并 810-815 叙述测试行与 898-899 术语。**不删决策表 FC-* 与故障表行,不删 694 行。**

- [ ] **Step 5: 跑检查器与行数**

Run:
```bash
python3 -c "print(len(open('docs/design/90-factory-and-cli.md', encoding='utf-8').read().split('\n')))"
python3 docs/design/tools/check_docs.py 90-factory-and-cli.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check
```
Expected: 行数 ≤ 900;0 错误 0 告警。

- [ ] **Step 6: 提交**

```bash
git add docs/design/90-factory-and-cli.md
git commit -m "docs(design): 组合根篇——转换后端、对手包 Actio、替身解析器与部署根目录、时间预算标为已落地,替身根目录与 PR 接口列表写回"
```

---

### Task 28: TrainScheduler(70)的 interact 段样例与键,ControlPlane(10)的 `on_locked`

**Files:**
- Modify: `docs/design/70-trainscheduler.md`(约 237 行 YAML 样例;约 247-253 行"规则"表;约 600 行 TS-4;行数 714,余量 186)
- Modify: `docs/design/10-controlplane.md`(331 行代码注释 `# [计划中] 新增 keyword-only 参数 on_locked`;41 行状态机行;行数 702)
- Test: 两个检查器 + `sync_docs --check`

**Interfaces:**
- Consumes: 计划 A Task 27 落地的 `InteractDefaults.memory_seed`、`_INTERACT_REQUIRED = ("trained_slot", "slots", "max_turns_per_episode", "memory")`、`check_perturbable_key("memory")` 返回 `"perturbable key 'memory': 'memory' anchors the run and is never perturbable"`;计划 A Task 23 的 `run_daemon(..., on_locked=)`;计划 A Task 29 的 `approved → rejected`。
- Produces: 70 篇样例与规则表含 `memory`;10 篇的 `on_locked` 去掉 `[计划中]`,状态机行补 `approved → rejected`。

70 篇的"interact 段的键表":写计划时核对,这一篇**没有**按键逐行列 interact 段的表(`scheduler.yaml` 那张 216 行起的键表是另一个文件的键);interact 段的约束写在 247-253 行的"规则"表(列:规则、内容)。所以"键表加 `memory`"落实为:YAML 样例加一行,规则表加一行 `memory`,TS-4 补一句锚定。

- [ ] **Step 1: YAML 样例加 `memory: {seed: null}`**

在 70 篇第 237 行附近 `max_turns_per_episode: 4` 之后加一行:

```yaml
  max_turns_per_episode: 4
  # 记忆起点:seed 必填,null 表示每个人格从空库出发;非 null 是种子库 id
  memory: {seed: null}
```

(YAML 样例在 ``` 围栏里,`check_docs` 对围栏内不查路径与符号。)再核对样例与 `tests/test_ts_config.py` 的 `_defaults_with_interact` 夹具逐字段一致——`grep -n "memory" tests/test_ts_config.py | head`。

- [ ] **Step 2: 规则表加一行**

在 `| 展开后仍过校验 | ... |` 之前加:

```
| 记忆起点 | 必填键 `memory`,形如 `{seed}`,只许 `seed` 一个键;`seed` 为 `null` 或非空字符串。`resolve` 把它写进展开的训练配置:`{seed, digests: {}, runtime: {}}`,后两个由派发器解析,调度器不算。`memory` 是锚定键,永不可扰动 |
```

表头"规则|内容"的列数不变,不影响解析。

- [ ] **Step 3: 其余措辞**

1. TS-4(约 600 行)"锚定身份的字段永不可扰动"后补"(含 `memory`)"。同一单元格改写。
2. 221 行 `perturbable` 白名单示例不变。
3. 488 行"`trained_slot`、`max_turns_per_episode` | 取自 `interact` 段"后另起一行不必要;并进同一行:"`trained_slot`、`max_turns_per_episode`、`memory.seed` | 取自 `interact` 段"。
4. 头部「状态」与「对应实施计划」:70 篇已全 `[已落地]`;「对应实施计划」行末补"`interact` 段的 `memory` 键随智能体记忆的替身路径施工计划(agent-memory-standin,第 4 步)落地 [已落地]"——不写路径,理由同 Task 25 Step 3。
5. 配置 `training_defaults.yaml` 缺 `memory` 的 interact 段从此解析失败(夹具随 计划 A Task 27 改):70 篇 247 行"段可选"行补"段里缺 `memory` 抛 `SchedulerConfigError`"。

- [ ] **Step 4: 10 篇两处**

1. 331 行:`# [计划中] 新增 keyword-only 参数 on_locked:取到仓库锁之后、第一个 tick 之前调一次` → `# 新增 keyword-only 参数 on_locked:取到仓库锁之后、第一个 tick 之前调一次 [已落地]`(代码块内的注释不受 `check_docs` 的路径检查,但符号检查对围栏内行不跑——`in_fence` 为真时 `continue`;安全)。
2. 41 行状态机行"`pending_review` 也可到 `rejected`"后补"。`approved` 也可到 `rejected`,只许 MindAgent 收录处理器走(门② 记忆抽取失败的提案);命令行不走这条边,两张迁移表从此有意不同"。同一单元格改写;10 篇 260 行起图 3 的文字版是生成的,**不手改**;fig 源 `fig_10-controlplane-states.py` 是否要画这条虚线见 Task 30。
3. 10 篇 CP-10(546 行)"需审核类迁移表在 Python 与 TypeScript 各一份 | 两边改漏一处就会分叉"补"。唯一例外是 `approved → rejected`,只在 Python 一侧"。
4. 10 篇用例数表(614-619 行):`test_states.py 6` 是基线实测数,第 5 步 计划 A Task 29 加了用例;`test_repo_lock.py 12` 第 3 步 计划 A Task 23 加了;实测:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -qq -p no:cacheprovider \
  tests/test_states.py tests/test_repo_lock.py tests/test_daemon.py 2>&1 | tail -4
```

按实测改 614、619 行的两个数字。

- [ ] **Step 5: 跑检查器**

Run: `python3 docs/design/tools/check_docs.py 70-trainscheduler.md 10-controlplane.md; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。

- [ ] **Step 6: 提交**

```bash
git add docs/design/70-trainscheduler.md docs/design/10-controlplane.md
git commit -m "docs(design): 调度器篇的 interact 段加 memory 键与样例,控制面篇的 on_locked 与 approved → rejected 标为已落地"
```

---

### Task 29: 设计源 spec 按台账同步措辞

**Files:**
- Modify: `docs/superpowers/specs/2026-10-25-actio-starling-memory-design.md`(590 行;不受 900 行上限约束,也不受 `check_docs` 检查,但保持每条改动小)
- Test: 无自动检查。核对办法:每条改完用 `grep -n` 确认旧措辞已无。

**Interfaces:**
- Consumes: 台账里改了设计措辞的裁定。
- Produces: 设计源(历史来源)与 `docs/design` 六篇措辞一致。

设计源已经含的(`git log` 里 `128eefe`、`a31af64`、`70d0d1a`、`d1b0bcb` 四个提交):`close()` 只关库、部署事件 key 的轮次前缀、`TupleMemory` 摘要按 `snapshot`、 的 S4 改写与 `recall(include_volatile=)`、`.owner` 用 `O_EXCL`、`embed_pending` 返回 `EmbedStats`。**这些不用再改**,只补下面几条。每条先 `grep` 旧措辞(行号是写计划时的),改完再 `grep` 确认。

- [ ] **Step 1: 逐条改写**

1. R18——派发器不回写指令:`grep -n "写回" $S` 得 356、357、362、434。
   - 356 行:`派发时算出并写回` → `派发时算出,只进本次派发的配置(不回写 \`directive.yaml\`)`。
   - 357 行:`派发时写回` → `派发时算出,同上`。
   - 362 行:`写回 \`memory.digests\` 与 \`memory.runtime\`` → `写进本次派发的 \`cfg\` 的 \`memory.digests\` 与 \`memory.runtime\`,不回写指令:续训要重新算(7.1 节),回写会多一笔提交,还会盖掉 T3 比较用的原值;训练记录的 \`policy_backend.memory\` 记下它们`。并补两句 、R21:`严格校验放行"未解析"的 \`memory\` 段(\`digests\` 与 \`runtime\` 都为空),只填一半拒绝;解析后再跑一次校验,摘要的人格集合要等于绑定人格集合,\`runtime\` 键集合要等于 5.2 节的五个键。解析器抛 \`DirectiveFailedError\` 时保留它自己的原因(替身拒绝种子要说清原因),其余异常只报类名。`
   - 434 行(`agent_round` 载荷 `episode_digests` 的"每条 episode 开始前写回一次"):这是轮次产物自己的逐步回写,**保留**。
2. R28——替身部署记忆根目录:`grep -n "临时目录" $S` 得 176、521。176 行 `根目录取 \`control-plane/\` 之外的临时目录,不进树` → `根目录是 \`<git 目录>/rismforge-memory\`(与 \`rismforge.lock\` 同处,不入树,跨重启保留)。不用 \`mkdtemp\`:每次重启换空库会让崩溃重入与门② 收录失效`。521 行是测试夹具的"临时目录",保留。
3. R29——放弃 pending 的时机:200 行"部署库由 MindAgent 在一条 episode **最终**结束时放弃:成功提交后对这条 episode 留下的 `pending` 行逐个 `abandon_event`" → `……成功时先落盘 episode,再放弃本 episode 留下的 \`pending\` 行,最后删起点副本(提交点)。崩在放弃与删副本之间:重入先用起点副本覆盖(覆盖后 abandoned 行也回到起点状态)再重跑,不会撞已放弃的 key`。
4. R30——起点副本删除失败:482 行 `副本删除失败 | 不分类 | 记日志;清扫兜底` 是 `runs/` 下的会话副本,保留;在 435 行(episode 起点与重试,"成功时先落盘 episode,再删起点副本")后补:`起点副本删除失败上抛,轮次留 \`pending\`:副本留着就是未提交`。
5. R35——拿不到人格库锁判 T3:432 行 `拿不到锁的轮次判 \`AgentRoundFailedError\`(M20)` 保留(它说的正是 `AgentRoundFailedError`);440 行 `覆盖失败……轮次判 \`AgentRoundFailedError\`,T3` → `覆盖失败抛 \`RestoreFailedError\`(继承 \`AgentRoundFailedError\`),轮次 T3`。
6. R27——门② 部分失败留 approved:444 行 `……提案改为 \`rejected\` 并带原因` 后补:`一条记忆增量都没提交过时才整份拒绝;前几条已 \`committed\`、后一条抽取失败时,这一条 \`abandon_event\`,写 T2 失败记录(原因 \`partial admission\`),提案不转 \`rejected\`,留在 \`approved\` 等人处理——已提交的事件放弃不了,转了就是"被拒的提案在库里留着记忆"`。再补 R32:`混合提案(记忆加技能)的记忆整份抽取失败时整份转 \`rejected\`、技能条目也不落:记忆先写就是为了这个`;R33:`remember_event 抛瞬态 MemoryFailureError 写 T1,其余 T2;提案都留 approved 等重入`。
7. R34——新一轮第一次运行用不带后缀的 base id:`grep -n "\-s{" $S` 得 439 行,"只有技能版本变了才重跑,且重跑用新的逻辑 id `deploy-{model}-{scenario}-{k}-s{技能版本}`" 后补:`新一轮第一次运行用不带后缀的 base id;只有 base 已提交且技能版本旧了才用 \`-s<版本>\``。
8. R23——训练位逻辑 id 的真实形状:61、196 行 `训练 \`{run}-{stage}-{scenario}-r{k}\`` → `训练 \`{run_id}-grpo{step}-{scenario_id}-r{k}\``(同一情境在不同 GRPO step 反复出现,id 里带 step 才不撞;续训从同一 step 重跑时 id 相同,同名 episode 文件被覆盖,批只认本次产出的 id)。
9. R26——时间预算的常量归属:57、62 行 F17、F22 描述的是**现状**(`PER_TURN_DEADLINE_S` 30 秒在 `factory.interact_policy`),保留;341 行已写 `client_factory(*, deadline_s=)`,补半句:`常量改在 \`mindtrainer.trajectory_source.PER_TURN_DEADLINE_S = 90.0\`,\`factory.interact_policy\` 里的原常量与对运行时 \`interact_deadline_s\` 的改写一并删去(原改写会让同开时的部署会话借用训练的 deadline)`。
10. R38——Starling 侧逻辑进 C++:410 行 `只改 Python 门面与绑定,每项带测试` → `按 Starling 的 \`CLAUDE.md\`,状态机与算法进 C++ 内核(三段写入、逻辑摘要、备份、嵌入循环),Python 门面只转发与归一签名,每项带测试`。
11. R39——排除清单增补:221 行排除清单 `inferred_at` 之后补:`、\`confidence_history_json\` 里每个条目的 \`ts\`(规范化时去掉)、engram 的 UUID(换成 \`engram:<content_hash>\`)`;`observed_at` 的说明补"写入端改取 engram 的 `created_at`,不取墙钟"。
12. R37——Starling 一侧的摘要口径:4.5 节末句"`TupleMemory.digest()` 对 `snapshot(entries)` ……"之前补:`Starling 一侧按全行:被取代的行改为 \`archived\` 仍是召回候选,\`volatile\` 与 \`pending_review\` 行影响后续写入的冲突探测,所以都进摘要。两边的原则一致:影响召回或后续写入的行进摘要,只是适用到不同实现。`
13. R40——§8 签名增补:416 行 `make_openai_embedder(*, model, base_url, dim, timeout_ms)` → 加 `max_retries=0`;421 行 `remember_event(key, text, *, interlocutor)` → 加 `epoch`;事件表列表 `(列:key、text、……、state)` → 加 `tenant_id`、`failure_category`,主键 `(tenant_id, key)`;`[key] ` 文本前缀"在 Starling 侧加"(196 行 `key` 作为前缀写进 Starling 的文本:`[<key>] <text>` 已写,补"前缀由 Starling 侧加")。
14. R41——`abandon_event` 未知 key:424 行补 `未知 key 抛 KeyError(本仓库适配器映射为 MemoryFailureError,transient 为假);已放弃再放弃为空操作,与替身一致`。
15. R42——`close(timeout=)`:420 行 `Memory.close()` 的作用 `关闭 SQLite 连接并等待自己的工作线程退出` → 补`;\`close(timeout=)\` 等门面在途调用返回后合并 WAL 并关连接,关后调用抛 \`MemoryClosed\``。
16. R5、R6、R3、R4(第 1 步):`grep -n "MemoryFailureError" $S` 的 4.3、4.1 节附近补三句:端口抛出的非 `MemoryFailureError` 异常由实例包成 `MemoryFailureError(类名, transient=False)`;人格 id 字符集统一为 `PERSONA_ID_PATTERN = ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`(`actio/memory_port.py`),两个部署管理面拼路径前都校验;查询词为空串时实例不调 `recall`,`episode_id` 必填按"会写库"(`replay_read_only` 为假)判断。
17. R9:设计源 4.6 节没有 `TRACE_SCHEMA` 字样(grep 无),不改。
18. 状态头:第 3 行"状态:修订稿第七版,待审阅"→"状态:修订稿第七版,已审定并入 docs/design,按施工计划落地;本稿是历史来源,与 docs/design 冲突以 docs/design 为准"。

- [ ] **Step 2: 确认旧措辞已清**

Run:
```bash
S=docs/superpowers/specs/2026-10-25-actio-starling-memory-design.md
grep -n "派发时写回\|派发时算出并写回\|之外的临时目录\|成功提交后对这条\|{run}-{stage}" "$S" || echo "旧措辞已清"
```
Expected: `旧措辞已清`。

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/specs/2026-10-25-actio-starling-memory-design.md
git commit -m "docs(spec): 按施工中的裁定同步措辞——派发器不回写指令、替身根目录、放弃时机、门② 部分失败、Starling 侧进 C++"
```

---

### Task 30: 生成图与最终代码核对(预期不改图)

**Files:**
- 只读核对: `docs/design/diagrams/fig_00-architecture.py`、`fig_00-closed-loop.py`、`fig_00-states.py`、`fig_10-controlplane-states.py`、`fig_20-socialsimulator-states.py`、`fig_30-mindtrainer-flow.py`、`fig_40-actio-architecture.py`、`fig_40-actio-context.py`、`fig_40-actio-respond.py`、`fig_50-mindagent-admission.py`、`fig_50-mindagent-context.py`
- 可能 Modify(仅当模块名、函数名、状态与最终代码不符): 对应的 `fig_*.py`,然后 `python3 docs/design/diagrams/build_all.py <名字>` 与 `python3 docs/design/tools/sync_docs.py`
- Test: `check_docs.py`(图卫生与节点数)+ `sync_docs.py --check`

**Interfaces:**
- Consumes: 全部前序任务。
- Produces: 图里的名字与最终代码一致;图**不标进展**(用户偏好,`README.md` 状态标记节:"图只画目标系统,不标进展"),所以不管哪些落地了,**不往图里加"已落地""计划中"字样,也不为落地与否改图**。

写计划时 `grep -il "memory\|starling\|记忆" diagrams/*.py` 得 11 张图画了设计二的内容。逐张核对"图里的名字是否是最终代码里的名字"。

- [ ] **Step 1: 逐图核对表**

| 图 | 要核对的名字(来自源文件) | 核对办法 | 预期 |
|---|---|---|---|
| `fig_40-actio-architecture` | `memory.py`("TupleMemory 替身")、`memory_port.py`("MemoryBackend 协议 · MemoryHit · MemoryEvent")、`memory_starling.py`("StarlingMemory 适配器")、`starling` | `ls src/actio/`;`grep -n "^class " src/actio/memory_port.py src/actio/memory.py` | 名字都在。`FileTupleStore`、`DeploymentMemoryStore` 在图里**没画**:图上 `memory.py` 的副标题只写 "TupleMemory 替身",可以不改(图是模块组成,不是类清单)。若想补,改 `memory.py` 副标题为 "MemoryEntry · snapshot\nTupleMemory · FileTupleStore",节点宽度够(190)就改,**这是选改,不是必改** |
| `fig_40-actio-respond` | "recall 取前 memory_limit 条"、"逐条 remember 对方新发言\n再 embed_pending 一次\n只读重放时跳过"、"MemoryFailureError" | `sed -n '/def respond/,/return/p' src/actio/instance.py \| head -80` 看顺序:recall → 渲染 → 后端 → remember → embed_pending → 轨迹;空查询词不调 recall(R3) | 顺序一致则不改;若 R3 的"空查询词不 recall"值得画,在"记忆检索"副标题加一行"查询词为空则跳过",**选改** |
| `fig_40-actio-context` | "Starling 记忆库"、"每个人格一个 SQLite 库(树外)" | 无需核对代码 | 不改 |
| `fig_50-mindagent-admission` | "记忆增量只许三键:persona_id · text · interlocutor"、"记忆 remember_event 进该人格的 Starling 库"、"不合法或异常:留在 approved\n记忆抽取失败:提案 rejected\nfailure-admit-<提案>"、desc 里 "增量不合法或写记忆抛异常记 T2 失败记录,提案留在 approved。记忆抽取失败则提案改为 rejected。" | 对照 计划 A Task 34 的实现(`src/mindagent/admit.py`) | **这张图要改**: 说部分失败留 `approved`, 说瞬态异常写 T1。desc 与第 33 行的方框文字改为:"不合法或异常:留在 approved\n记忆抽取失败:一条都没提交才 rejected\n部分已提交:留在 approved"。方框高度 `hh = 90` 是三行的,四行会溢出,改成两行以内:"异常或部分已提交:留 approved\n一条都没提交且抽取失败:rejected\nfailure-admit-<提案>"(仍三行)。图说明 desc 同步。这是"图只画目标系统"的目标系统本身变了,不是进展标记 |
| `fig_50-mindagent-context` | "Actio:技能库 · 显式记忆" | 无需核对 | 不改 |
| `fig_00-states`、`fig_10-controlplane-states` | `approved → rejected` 虚线 | 对照 `controlplane/states.py` | `fig_00-states` 已画并标"仅收录处理器走"。**`fig_10-controlplane-states` 没画这条边**(上面 grep 只有 `appr`、`rej` 两个节点,没有 `edge(appr, rej)`):10 篇第 41 行 Task 28 已补文字,图要补一条虚线 `c.edge(appr, rej, ..., dashed=True, label="仅收录处理器")`,照 `fig_00-states.py` 第 29 行的写法,在 `c.edge(pend, rej, ...)` 之后加 `c.edge(appr, rej, "right", "bottom", label="仅收录处理器", label_at=0.35, dashed=True)`,并在 `legend` 里加一项虚线说明(`fig_00-states.py` 第 46 行同款)。这张图当前 12 个节点,边不是节点,不变。写计划时 `fig_10-controlplane-states.py` 第 14-31 行核对过只有 `pend → rej`、没有 `appr → rej` |
| `fig_20-socialsimulator-states` | "记忆失败" | 无需核对 | 不改 |
| `fig_30-mindtrainer-flow` | "interact 算起点记忆摘要" | 无需核对 | 不改 |
| `fig_00-architecture`、`fig_00-closed-loop` | "Starling 记忆库"、"门② 技能记忆纳入" | 无需核对 | 不改 |

- [ ] **Step 2: 改动两张图(若上表判定要改)**

对 `fig_50-mindagent-admission.py` 与 `fig_10-controlplane-states.py` 改完后:

```bash
python3 docs/design/diagrams/build_all.py 50-mindagent-admission 10-controlplane-states
python3 docs/design/tools/render_figs.py 50-mindagent-admission 10-controlplane-states
```

`render_figs.py` 打印 PNG 路径,用 Read 打开目检:文字不溢出方框,连线不穿框。再 `python3 docs/design/tools/sync_docs.py`,让两篇文档里的图块、图说明与文字版同步。

- [ ] **Step 3: 跑检查器**

Run: `python3 docs/design/tools/check_docs.py; python3 docs/design/tools/check_consistency.py | tail -3; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。`check_docs` 的 `svgkit.check` 会查图卫生,节点数上限 16(告警线 12)。

- [ ] **Step 4: 提交**

```bash
git add docs/design/diagrams/fig_50-mindagent-admission.py docs/design/diagrams/fig_10-controlplane-states.py \
        docs/design/diagrams/50-mindagent-admission.svg docs/design/diagrams/50-mindagent-admission.meta.json \
        docs/design/diagrams/10-controlplane-states.svg docs/design/diagrams/10-controlplane-states.meta.json \
        docs/design/50-mindagent.md docs/design/10-controlplane.md
git commit -m "docs(design): 审核链图补部分失败留 approved,控制面状态图补 approved → rejected 虚线"
```

(只改这两张时按上面暂存;核对后还改了别的图就一并加进去。`docs/design/diagrams/__pycache__/` 不入库,先 `git status` 看有没有被误暂存。)

---

### Task 31: 收尾——全量测试、检查器、`sync_docs --check`,删 `.parts/`

**Files:**
- Delete: `docs/superpowers/plans/.parts/`(目录,写计划的中间产物,当前未跟踪)
- Modify: `docs/design/00-overview.md`(路线图第 9 章第 8 步的标记,以及头部「状态」若还剩未摘的;只改这两处)
- Test: 全量 `-m "not slow"`、两个检查器、`sync_docs --check`

**Interfaces:**
- Consumes: Task 21 到 Task 30 全部提交。
- Produces: 干净的工作区、全绿的验证。

- [ ] **Step 1: 全量测试**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -m "not slow" -q -p no:cacheprovider 2>&1 | tail -5`
Expected: 全部通过,无失败。第 8 步不改代码与测试,这一步是确认前七步合并后整套仍绿。失败了不是文档的问题:回到失败测试所属的步骤。

- [ ] **Step 2: 两个检查器与同步检查**

Run:
```bash
python3 docs/design/tools/check_docs.py
python3 docs/design/tools/check_consistency.py
python3 docs/design/tools/check_consistency.py --final
python3 docs/design/tools/sync_docs.py --check
```
Expected: 前两条 `0 个错误,0 条告警`;`--final` 要求 13 篇齐全、README 与目录一一对应、没有孤立的图,也 0 错误;`sync_docs --check` 报同步。`check_consistency` 末尾的汇总表看"状态行标记"一列:20、30、40、50、70、90 都含 `已落地`。

- [ ] **Step 3: 行数硬上限**

Run:
```bash
python3 - <<'PY'
from pathlib import Path
for p in sorted(Path("docs/design").glob("[0-9][0-9]-*.md")):
    n = len(p.read_text(encoding="utf-8").split("\n"))
    print(f"{n:4d}  {'超限' if n > 900 else 'ok'}  {p.name}")
PY
```
Expected: 全部 `ok`,即没有任何一篇超过 900 行。30、40、90 是此前贴着上限的三篇。

- [ ] **Step 4: 剩余的 `[计划中]` 就地核对**

Run: `grep -c "\[计划中\]" docs/design/*.md`
对照本任务末尾的"预计剩余"表。多出来的逐行看:是"依第 7 步"、"要真机真端点"还是漏摘。**漏摘的补摘**,其余保留。

- [ ] **Step 5: 00 篇第 8 步标记**

路线图表 744-751 行的第 8 步"文档与图收尾 [计划中]"改 `[已落地]`;头部「状态」若仍有"智能体记忆 [计划中]"字样且所有步已落地,摘掉;若第 6、7 步(Starling PR 与适配器)仍未合入,头部写"替身路径 [已落地];Starling 路径 [计划中]"。

Run: `python3 docs/design/tools/check_docs.py 00-overview.md; python3 docs/design/tools/sync_docs.py --check`
Expected: 0 错误 0 告警。

- [ ] **Step 6: 删中间产物**

```bash
git status --short docs/superpowers/plans/.parts | head -3
rm -r docs/superpowers/plans/.parts
git status --short | head
```
Expected: `.parts/` 之前显示 `?? docs/superpowers/plans/.parts/`(未跟踪),删后 `git status` 不再显示。**两份计划文件要先由控制者拼好并保存**(`docs/superpowers/plans/2026-10-27-agent-memory-standin.md` 与 `2026-10-27-agent-memory-starling.md`),否则删 `.parts/` 就丢了分段。这一步是计划拼接之后才做的收尾,不是写计划时做。

- [ ] **Step 7: 提交**

```bash
git add docs/design/00-overview.md
git commit -m "docs(design): 智能体记忆文档与图收尾,路线图第 8 步标为已落地"
```

(删 `.parts/` 是删未跟踪目录,没有可提交的内容。)

---

### 附:各篇 `[计划中]` 现有数与改完后预计剩余


数字是写计划时(HEAD `d1b0bcb`)的 `grep -c "\[计划中\]"`(按行计)。"必留"是无论第 7 步合不合都保留的行:每篇的图例定义行(1 行)加要真机、真端点、真 Starling 才能核实的项,以及设计二合入之前就有、与本设计无关的 `[计划中]`。"依第 7 步"是用关键词粗筛的**上界**(命中 `StarlingMemory`、`memory_config`、`.owner`、清扫等词不等于整行都依第 7 步,实施时逐行看)。

| 文档 | 现有 | 必留 | 疑似依第 7 步(上界) | 第 7 步已合:预计剩余 | 第 7 步未合:预计剩余 |
|---|---|---|---|---|---|
| 00-overview | 67 | 10 | 24 | 约 10(第 7、21 到 28 项风险;SYS-27 等随第 7 步摘) | 约 20 到 34 |
| 20-socialsimulator | 112 | 5 | 8 | 约 5 | 约 8 到 13 |
| 30-mindtrainer | 71 | 12 | 13 | 约 12 | 约 14 到 25 |
| 40-actio | 119 | 5 | 30 | 约 5 | 约 15 到 35 |
| 50-mindagent | 154 | 5 | 25 | 约 5 | 约 12 到 30 |
| 90-factory-and-cli | 112 | 10 | 63 | 约 10 | 约 40 到 73 |
| 70-trainscheduler | 2 | 2(共享区块与图例) | 0 | 1(区块摘后) | 2 |
| 10-controlplane | 3 | 2 | 0 | 2(区块摘后 1) | 3 |

共享区块 `context-block.md` 里那一句,第 7 步合入时摘,**一处改动七篇同步**(Task 22 Step 2)。

**必留的具体项**(无论第 7 步):
- 00:风险表第 21 到 28 项(真机单轮延迟、两个 LoRA 的开销、真抽取嵌入的延迟、真嵌入下召回命中质量、`VACUUM INTO` 副本跨迁移版本能否打开、每会话开库对 GRPO 一步的影响、不跑后台维护对召回质量的影响、外部端点换向量);500 行"真机上的单轮延迟与两个 LoRA 的开销未测";图例。
- 20:817 到 820(外部 API 对手速率与成本、站内 vLLM 双 LoRA 延迟、每会话开库对 GRPO 一步的影响、抽取嵌入延迟)。
- 30:127、388、400、853、854、857、870(设计二之前就有)和 872、873、874(真机单轮延迟、抽取嵌入延迟、每会话开库)。
- 40:859 到 862(抽取嵌入延迟、召回命中质量、不跑 `tick` 的召回质量、外部端点向量稳定性)。
- 50:817 到 820(同上四项,`VACUUM INTO` 跨迁移版本在其中)。
- 90:848 到 850(Starling 未上 PyPI、记忆库必须与 Actio 同机)、853(设计二之前就有)、873 到 877(延迟、每会话开库、向量稳定性、两开关同开、旧版本库能否打开)。
- 全部六篇与 70、10 的图例定义行。

**设计源 §13 的"未核实"六项**(真抽取嵌入延迟 4 秒、真嵌入召回质量、`VACUUM INTO` 跨版本、每会话开库对一步时长、不跑 `tick` 的召回质量、外部端点向量稳定性)**在施工后仍然未核实**,文档里对应项保留 `[计划中]`;第 6 步 Task 10 的 `backup_to` 可以在 Starling 的 PR 里加"旧版本库备份后用新版本打开"的用例,若加了且通过,则 40 篇 859 之外的"`VACUUM INTO` 副本"项(50 篇 818、00 篇 796、90 篇 877)可摘。是否加了看 Task 10 实际落地的测试清单,不要凭计划摘。
