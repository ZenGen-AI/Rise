# 智能体记忆(替身路径)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 场景二的每个角色位都成为带记忆的 Actio 智能体,每轮读写记忆;训练路的起点记忆进 `config_hash`;MindAgent 的部署记忆有提交点与回滚。全部只用替身记忆跑通,不依赖 Starling。

**Architecture:** Actio 抽出记忆端口 `MemoryBackend` 与部署记忆管理面 `DeploymentMemoryStore`,替身是内存的 `TupleMemory` 与落盘的 `FileTupleStore`,两者过同一套契约测试。模拟器的会话改由 `SessionResources` 持有各角色位的智能体并有序收尾;训练位与对手位都换成 Actio 实例,记忆异常经组合根转换成模拟器认得的两个基类。训练配置加 `memory` 段,派发器经注入的解析器解出起点摘要与运行时身份。MindAgent 按 episode 新造实例,删起点副本即提交。

**Tech Stack:** Python 3.11+、pytest、grpc。无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-10-25-actio-starling-memory-design.md`(已并入 `docs/design/` 的 00、20、30、40、50、90 六篇)。本计划是施工顺序的第 1 到 5 步;第 6 到 8 步(Starling PR、接入、文档收尾)在 `docs/superpowers/plans/2026-10-27-agent-memory-starling.md`。

## Global Constraints

- 每个源文件首条语句 `from __future__ import annotations`。
- 测试命令:`PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest <path> -q -p no:cacheprovider`。
- 依赖方向:`simulator` 与 `mindtrainer` 不得导入 `actio`、`starling`;`actio` 只导入自身、`simulator.datamodel` 与标准库(本计划新增 `hashlib`、`json`、`pathlib`、`datetime`,`tests/test_actio_instance.py` 的 `_ALLOWED` 随之加);`mindagent` 不导入 `starling`;`trainscheduler` 不得导入 `simulator`、`actio`、`mindagent`、`factory`。
- 不新增第三方依赖。
- 凭证只从环境变量读,异常信息只报变量名或异常类名与 `failure_category`,不含端点地址与密钥。
- 每个任务先写失败的测试、确认失败、再实现。
- 每个任务结束时全套 `-m "not slow"` 仍绿;签名变化波及的既有测试随所在任务一起改,不另开任务。
- 不改 `docs/design`(文档收尾在第 8 步)与生成图(`docs/design/diagrams/*.py`、`.svg`)。
- 提交逐个文件暂存,提交信息正文写完即止,不加署名行。
- 名字与签名以各任务的 Interfaces 块为准;Interfaces 没写到的,以设计源为准。
- 任务里写的行号是写计划时的原始行号。同一文件被前面任务改过之后行号会漂移,以同时给出的函数名、类名为准定位。

## Review Focus

- **同一逻辑 episode id 的重试撞上还没收尾的旧会话**:客户端 abort 后立即重试,旧会话的资源还在 join。应判 `role_backend_unavailable`(T1)并在旧会话收尾后放行,不能判 `invalid_session`。见 Task 14。
- **部署 episode 中途崩溃后重入**:起点副本还在,说明上次在途。应先用起点副本覆盖部署库、核对摘要、再重跑;覆盖失败判 T3 并保留库与副本。同一逻辑 id 的第二次尝试不能撞 `EventConflict`。见第 5 步。
- **门② 与在途轮次落在同一个 tick**:reconcile 先收录、后跑轮次。收录写库前若该人格有未提交的起点副本,应抛 `DeferredError` 延后,否则轮次重入的覆盖会抹掉刚收录的记忆。见第 5 步。
- **替身下 `memory.seed` 非 null**:替身没有种子库。应在派发时判 T2,原因写明种子需要记忆配置,不能静默当空库跑。见 Task 26。
- **本设计落地前写下的 pending interact run 没有 `memory` 段**:应在派发器算哈希之前判 T2,原因是 `interact config has no memory section (written before memory support)`,不能续上旧 run 跑出记忆不一致的实验。见 Task 28。
- **门② 部分收录后抽取一直失败**:前几条已提交、后一条反复抽取失败时,动作一直按退避重试,不写树,也不会收敛。现在只能等抽取恢复,或由人手工经管理面放弃那条事件并处置提案;本计划不提供这条命令。Task 34 的测试只钉住"抽取恢复后收敛",一直失败的人工出口留给后续。
- **生成路产出的人格 id 不符合 `PERSONA_ID_PATTERN`**:人格 id 现在会拼进库文件名。现有生成器产出 `pers-auto-<n>`,符合;但仓库没有对人格 id 的入口校验,手写的人格可能含其他字符。`FileTupleStore` 遇到不合法 id 应抛 `ActioDataError`,派发与部署按 T2 失败,不能拼出越界路径。见 Task 4。

---

## 第 1 步:记忆端口、替身与实例读写

### Task 1: 记忆端口的值类型、异常与两个协议

**Files:**
- Create: `src/actio/memory_port.py`
- Test: `tests/test_actio_memory_port.py`(新)
- Modify: `tests/test_actio_instance.py`(`_ALLOWED` 加 `"pathlib"`)

**Interfaces:**
- Consumes: 无
- Produces(全部在 `actio.memory_port`):
  - `@dataclass(frozen=True) MemoryHit(id: str, text: str)`
  - `@dataclass(frozen=True) MemoryEvent(key: str, text: str, interlocutor: str)`(没有 `seq`)
  - `@dataclass(frozen=True) RememberOutcome(stored: tuple[str, ...], failed: bool, category: str | None)`
  - `class MemoryFailureError(Exception)`:`__init__(self, message: str, *, transient: bool)`,属性 `transient: bool`;不继承 `ActioDataError`/`ValueError`
  - `class MemoryEventConflict(MemoryFailureError)`、`class MemoryClosedError(MemoryFailureError)`:`__init__(self, message: str)`,`transient` 恒为 `False`
  - `@runtime_checkable class MemoryBackend(Protocol)`:`recall(query: str, *, limit: int) -> list[MemoryHit]`、`remember(event: MemoryEvent) -> RememberOutcome`、`embed_pending() -> int`、`digest() -> str`、`close() -> None`
  - `PERSONA_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")`:人格 id 拼进库文件名前的统一校验,两个部署管理面都用(导入白名单已有 `re`)。
  - `@runtime_checkable class DeploymentMemoryStore(Protocol)`:`open(persona_id: str) -> MemoryBackend`、`remember_event(persona_id: str, key: str, text: str, *, interlocutor: str) -> RememberOutcome`、`abandon_event(persona_id: str, key: str) -> None`、`pending_events(persona_id: str) -> list[str]`、`backup_to(persona_id: str, path: Path) -> str`、`restore_from(persona_id: str, path: Path) -> str`、`digest(persona_id: str) -> str`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_memory_port.py
import dataclasses

import pytest

from actio.errors import ActioDataError
from actio.memory_port import (
    DeploymentMemoryStore, MemoryBackend, MemoryClosedError, MemoryEvent, MemoryEventConflict,
    MemoryFailureError, MemoryHit, RememberOutcome,
)


def test_port_value_types_are_frozen_with_exact_fields():
    assert [f.name for f in dataclasses.fields(MemoryHit)] == ["id", "text"]
    assert [f.name for f in dataclasses.fields(MemoryEvent)] == ["key", "text", "interlocutor"]  # 没有 seq
    assert [f.name for f in dataclasses.fields(RememberOutcome)] == ["stored", "failed", "category"]
    for value in (MemoryHit("mem-a", "pb: x"), MemoryEvent("ep#0", "pb：x", "pb"),
                  RememberOutcome(("mem-a",), False, None)):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, "y")


def test_memory_failure_is_its_own_exception_and_carries_transient():
    exc = MemoryFailureError("RecallDegradedError", transient=True)
    assert exc.transient is True and str(exc) == "RecallDegradedError"
    assert not isinstance(exc, ActioDataError) and not isinstance(exc, ValueError)
    with pytest.raises(TypeError):
        MemoryFailureError("x")  # transient 必须显式给


@pytest.mark.parametrize("cls", [MemoryEventConflict, MemoryClosedError])
def test_conflict_and_closed_are_never_transient(cls):
    exc = cls("x")
    assert isinstance(exc, MemoryFailureError) and exc.transient is False


def test_both_protocols_are_checked_by_method_names():
    class Port:
        def recall(self, query, *, limit): return []
        def remember(self, event): return RememberOutcome((), False, None)
        def embed_pending(self): return 0
        def digest(self): return "0" * 64
        def close(self): return None

    class Store:
        def open(self, persona_id): return Port()
        def remember_event(self, persona_id, key, text, *, interlocutor): ...
        def abandon_event(self, persona_id, key): ...
        def pending_events(self, persona_id): return []
        def backup_to(self, persona_id, path): return ""
        def restore_from(self, persona_id, path): return ""
        def digest(self, persona_id): return ""

    assert isinstance(Port(), MemoryBackend) and not isinstance(object(), MemoryBackend)
    assert isinstance(Store(), DeploymentMemoryStore) and not isinstance(Port(), DeploymentMemoryStore)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_memory_port.py -q -p no:cacheprovider`
Expected: FAIL,`ModuleNotFoundError: No module named 'actio.memory_port'`

- [ ] **Step 3: 实现**

`src/actio/memory_port.py`:首条语句 `from __future__ import annotations`,只导入 `dataclasses`、`re`(`PERSONA_ID_PATTERN`)、`typing`(`Protocol`、`runtime_checkable`)与 `pathlib`(`Path`,两个协议的 `path` 参数与 Task 4 的 `FileTupleStore(root: Path)` 都用它)。`tests/test_actio_instance.py` 的 `_imports` 用 `ast.walk` 扫全部导入,`if TYPE_CHECKING:` 下的导入也会被扫到,所以 `_ALLOWED` 显式加 `"pathlib"`(仍是标准库,合同的依赖方向不变);不导入 `actio.errors`,因为 `MemoryFailureError` 刻意不是 `ActioDataError`:驱动器与轨迹源按 `transient` 分 T1/T2,数据错误另有分支。

两个协议的方法签名照 Interfaces 抄,docstring 写明设计源 4.1 的语义:`embed_pending` 只做嵌入、返回成功条数、有失败就抛瞬态 `MemoryFailureError`;`digest` 是逻辑摘要(sha256 小写十六进制);`close` 只关库、不放弃任何事件,关闭后再调 `recall`/`remember`/`embed_pending`/`digest` 抛 `MemoryClosedError`,再调 `close` 是空操作(调用方的 `finally` 可能各关一次);`restore_from` 在该人格还有打开的端口时抛 `MemoryFailureError(transient=False)`;`backup_to`/`restore_from` 返回副本/恢复后的逻辑摘要。`MemoryHit.id` 是内容 id(`mem-` 前缀),同一 `recall` 结果里不重复。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_memory_port.py tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: PASS(白名单测试 `test_actio_only_imports_itself_simulator_datamodel_and_the_stdlib` 在加了 `"pathlib"` 之后通过)

- [ ] **Step 5: 提交**

```bash
git add src/actio/memory_port.py tests/test_actio_memory_port.py tests/test_actio_instance.py
git commit -m "feat(actio): 记忆端口的值类型、异常与两个协议"
```

---

### Task 2: `TupleMemory`、内容 id 与共享契约测试

**Files:**
- Modify: `src/actio/memory.py`(新增 `content_id`、`TupleMemory` 与模块私有的账本 `_Ledger`;`MemoryEntry`、`snapshot`、`overlap`、`retrieve` 不动)
- Create: `tests/memory_contract_sample.yaml`(规范样例)
- Create: `tests/test_actio_memory_contract.py`(共享契约,本任务只有 `"tuple"` 一个工厂)
- Modify: `tests/test_actio_memory_experience.py`(追加 `TupleMemory` 专属用例;既有用例断言的是 `retrieve` 与条目自带 id,`retrieve` 行为不变,所以一条都不用改)
- Modify: `tests/test_actio_instance.py`(`_ALLOWED` 加 `"hashlib"`、`"json"`、`"datetime"`)

**Interfaces:**
- Consumes: Task 1 的 `MemoryHit`、`MemoryEvent`、`RememberOutcome`、`MemoryEventConflict`、`MemoryClosedError`、`MemoryBackend`
- Produces:
  - `actio.memory.content_id(subject: str, content: str) -> str`:`"mem-" + sha256(json.dumps([subject, content], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]`
  - `actio.memory.TupleMemory(entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = ())`,满足 `MemoryBackend`
  - `actio.memory._Ledger`(模块私有,Task 4 的 `FileTupleStore` 复用):条目列表、事件表 `dict[str, dict]`、`next_seq: int`,以及 `recall`、`begin(event)`、`commit(key)`、`fail(key, category)`、`abandon(key)`、`digest()`、`to_json()`/`from_json()`
  - `tests/test_actio_memory_contract.py` 的 `PORT_FACTORIES: dict[str, Callable[[Path], MemoryBackend]]`(参数是一个尚不存在的新目录)与夹具 `make_port`;Task 4 加 `"file"`,计划 B 加 `"starling"`

- [ ] **Step 1: 写失败的测试**

规范样例(hits 写事件 key,表示"该事件首次 `remember` 返回的 `stored[0]`",这样替身与 Starling 的内容 id 值不同也能比命中顺序;digest 写相对上一步是否变化):

```yaml
# tests/memory_contract_sample.yaml
# 记忆端口规范样例(设计二 4.1 节、M17)。替身与 Starling 跑同一份,比较命中顺序与摘要变化曲线。
empty_recall: 预算
steps:
  - remember: {key: "ep-sample#0", text: "pb：我担心预算超支", interlocutor: pb}
    digest: changed
    recall:
      - {query: 预算超支怎么办, limit: 3, hits: ["ep-sample#0"]}
  - remember: {key: "ep-sample#2", text: "pb：人手也不够", interlocutor: pb}
    digest: changed
    recall:
      - {query: 人手不够预算呢, limit: 3, hits: ["ep-sample#2", "ep-sample#0"]}
      - {query: 人手不够预算呢, limit: 1, hits: ["ep-sample#2"]}
  - remember: {key: "ep-sample#4", text: "pc：天气很好", interlocutor: pc}
    digest: changed
    recall:
      - {query: 天气, limit: 3, hits: ["ep-sample#4"]}
  - remember: {key: "ep-sample#0", text: "pb：我担心预算超支", interlocutor: pb}
    idempotent: true
    digest: same
    recall:
      - {query: 预算超支怎么办, limit: 3, hits: ["ep-sample#0"]}
  - remember: {key: "ep-sample#2", text: "pb：换了一句", interlocutor: pb}
    raises: MemoryEventConflict
    digest: same
    recall:
      - {query: 人手不够预算呢, limit: 3, hits: ["ep-sample#2", "ep-sample#0"]}
```

(按 `actio.text.tokens` 算过:第二步查询词与 `#2` 共有"人手""不够"两个词面、与 `#0` 共有"预算"一个,所以顺序不靠平手裁决。)

```python
# tests/test_actio_memory_contract.py
"""记忆端口的共享契约(设计二 4.1 节、M17)。用例只经 MemoryBackend 的五个方法访问实现,
不碰任何实现的内部;计划 B 往 PORT_FACTORIES 加 "starling"(Starling 未装时跳过)。"""
import re
from pathlib import Path

import pytest
import yaml

from actio.memory import TupleMemory
from actio.memory_port import MemoryClosedError, MemoryEvent, MemoryEventConflict, MemoryHit

SAMPLE = yaml.safe_load(Path(__file__).with_name("memory_contract_sample.yaml").read_text(encoding="utf-8"))

PORT_FACTORIES = {
    "tuple": lambda fresh_dir: TupleMemory(),
}


@pytest.fixture(params=sorted(PORT_FACTORIES))
def make_port(request, tmp_path):
    made = []

    def make():
        port = PORT_FACTORIES[request.param](tmp_path / f"port-{len(made)}")
        made.append(port)
        return port

    yield make
    for port in made:
        port.close()  # close 可重复调用


def _event(d):
    return MemoryEvent(d["key"], d["text"], d["interlocutor"])


def test_the_sample_gives_the_same_hit_order_and_digest_curve(make_port):
    port = make_port()
    stored: dict[str, tuple[str, ...]] = {}
    assert port.recall(SAMPLE["empty_recall"], limit=3) == []
    prev = port.digest()
    for step in SAMPLE["steps"]:
        event = _event(step["remember"])
        if step.get("raises") == "MemoryEventConflict":
            with pytest.raises(MemoryEventConflict):
                port.remember(event)
        else:
            out = port.remember(event)
            assert (out.failed, out.category) == (False, None) and out.stored
            if step.get("idempotent"):
                assert out.stored == stored[event.key]  # 同 key 同文本:返回首次那批 id
            stored.setdefault(event.key, out.stored)
        assert port.embed_pending() >= 0
        now = port.digest()
        assert (now != prev) == (step["digest"] == "changed"), step
        prev = now
        for q in step["recall"]:
            hits = port.recall(q["query"], limit=q["limit"])
            assert [h.id for h in hits] == [stored[k][0] for k in q["hits"]], q


def test_empty_ports_share_a_fixed_digest_and_recall_nothing(make_port):
    a, b = make_port(), make_port()
    assert re.fullmatch(r"[0-9a-f]{64}", a.digest())
    assert a.digest() == b.digest()
    assert a.recall("预算", limit=3) == []
    assert a.embed_pending() == 0


def test_the_same_event_gets_the_same_content_ids_in_two_ports(make_port):
    a, b = make_port(), make_port()
    first = SAMPLE["steps"][0]["remember"]
    a.remember(_event(first))
    b.remember(_event(first))
    query = SAMPLE["steps"][0]["recall"][0]["query"]
    ha, hb = a.recall(query, limit=3), b.recall(query, limit=3)
    assert ha and [h.id for h in ha] == [h.id for h in hb]
    assert all(isinstance(h, MemoryHit) and re.fullmatch(r"mem-.+", h.id) for h in ha)
    assert len({h.id for h in ha}) == len(ha)


def test_recall_honours_the_limit(make_port):
    port = make_port()
    for step in SAMPLE["steps"][:3]:
        port.remember(_event(step["remember"]))
    query = SAMPLE["steps"][1]["recall"][0]["query"]
    three = port.recall(query, limit=3)
    assert port.recall(query, limit=0) == []
    assert port.recall(query, limit=1) == three[:1]


def test_a_closed_port_raises_closed_on_every_method_and_close_is_idempotent(make_port):
    port = make_port()
    port.close()
    port.close()
    first = _event(SAMPLE["steps"][0]["remember"])
    for call in (lambda: port.recall("预算", limit=3), lambda: port.remember(first),
                 port.embed_pending, port.digest):
        with pytest.raises(MemoryClosedError):
            call()
```

```python
# tests/test_actio_memory_experience.py(追加;文件头补 import hashlib、json、re,
# 以及 from actio.memory import TupleMemory, content_id 与
# from actio.memory_port import MemoryEvent, MemoryEventConflict, MemoryHit, RememberOutcome)
T0 = "2026-01-01T00:00:00Z"


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_of(entry_dicts, event_rows):
    """4.5 节最后一句:条目 to_dict() 去掉 id 后的排序列表加事件表,整体 sha256。"""
    entries = sorted(({k: v for k, v in d.items() if k != "id"} for d in entry_dicts), key=_canon)
    events = sorted(event_rows, key=lambda r: r["key"])
    return hashlib.sha256(_canon({"entries": entries, "events": events}).encode("utf-8")).hexdigest()


def test_content_id_hashes_only_subject_and_content():
    cid = content_id("pb", "对方担心预算超支")
    raw = json.dumps(["pb", "对方担心预算超支"], ensure_ascii=False, separators=(",", ":"))
    assert cid == "mem-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert content_id("pc", "对方担心预算超支") != cid


def test_tuple_memory_ignores_the_entry_ids_in_hits_and_digest():
    a_entries = [_m("mem-1", 1, T0, content="预算不够"), _m("mem-2", 1, T0, content="人手不够")]
    a = TupleMemory(tuple(a_entries))
    b = TupleMemory((_m("mem-x", 1, T0, content="预算不够"), _m("mem-y", 1, T0, content="人手不够")))
    assert a.recall("预算不够", limit=3) == b.recall("预算不够", limit=3)
    assert a.recall("预算不够", limit=3)[0] == MemoryHit(content_id("pb", "预算不够"), "pb: 预算不够")
    assert a.digest() == b.digest() == _digest_of([e.to_dict() for e in a_entries], [])


def test_tuple_memory_digest_follows_what_recall_sees_not_the_raw_entries():
    # 去掉 id 后多重集相同,但 a 里两条是同一 id 的两个版本(召回只看 v2),b 里是两个 id(都召回)
    a = TupleMemory((_m("mem-1", 1, T0, content="预算不够"), _m("mem-1", 2, T0, content="人手不够")))
    b = TupleMemory((_m("mem-1", 1, T0, content="预算不够"), _m("mem-2", 2, T0, content="人手不够")))
    assert a.recall("预算不够", limit=3) != b.recall("预算不够", limit=3)
    assert a.digest() != b.digest()


def test_tuple_memory_ties_break_by_content_id_and_same_content_merges():
    entries = (_m("mem-z", 1, T0, content="预算甲"), _m("mem-a", 1, T0, content="预算乙"),
               _m("mem-dup", 1, T0, content="预算甲"))
    hits = TupleMemory(entries).recall("预算", limit=3)
    assert [h.id for h in hits] == sorted({content_id("pb", "预算甲"), content_id("pb", "预算乙")})


def test_tuple_memory_validates_entries_at_construction():
    with pytest.raises(ActioDataError):
        TupleMemory(({"id": "mem-1"},))
    with pytest.raises(ActioDataError, match="two different entries"):
        TupleMemory((_m("mem-1", 1, T0), _m("mem-1", 1, T0, content="别的")))


def test_tuple_memory_remember_appends_an_entry_and_keeps_an_event_table():
    m = TupleMemory()
    out = m.remember(MemoryEvent("ep#1", "pb：预算不够", "pb"))
    cid = content_id("pb", "pb：预算不够")
    assert out == RememberOutcome((cid,), False, None)
    assert m.recall("预算", limit=3) == [MemoryHit(cid, "pb: pb：预算不够")]
    assert m.remember(MemoryEvent("ep#1", "pb：预算不够", "pb")) == out  # 幂等,不再追加
    assert TupleMemory().remember(MemoryEvent("other#9", "pb：预算不够", "pb")).stored == (cid,)  # 与 key 无关
    with pytest.raises(MemoryEventConflict):
        m.remember(MemoryEvent("ep#1", "pb：别的", "pb"))
    m.remember(MemoryEvent("ep#3", "pc：好吧", "pc"))
    entry = lambda key, subject, text, ts: {"id": "-", "version": 1, "subject": subject, "content": text,
                                            "keywords": [], "timestamp": ts, "source_episode": key,
                                            "confidence": 1.0}
    row = lambda key, text, who, seq: {"key": key, "text": text, "interlocutor": who, "seq": seq,
                                       "state": "committed", "statement_ids": [content_id(who, text)],
                                       "failure_category": None}
    assert m.digest() == _digest_of(
        [entry("ep#1", "pb", "pb：预算不够", T0), entry("ep#3", "pc", "pc：好吧", "2026-01-01T00:00:01Z")],
        [row("ep#1", "pb：预算不够", "pb", 0), row("ep#3", "pc：好吧", "pc", 1)],
    )
    assert m.embed_pending() == 0
```

`_m` 是该文件既有的条目工厂(subject 缺省 `"pb"`)。这些用例只用公开方法与 `content_id`,不读实现的私有属性。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_memory_contract.py tests/test_actio_memory_experience.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'TupleMemory' from 'actio.memory'`

- [ ] **Step 3: 实现**

`src/actio/memory.py` 新增导入 `hashlib`、`json`、`datetime`(`datetime`、`timedelta`、`timezone`)与 `actio.memory_port` 的类型。

`_Ledger` 是两个替身共用的唯一一份逻辑,`TupleMemory` 把它放内存,Task 4 的 `FileTupleStore` 把它存 JSON,所以同样内容在两者上的命中与摘要逐值相等:

- 字段:`entries: list[MemoryEntry]`、`events: dict[str, dict]`(行的键固定为 `key`、`text`、`interlocutor`、`seq`、`state`、`statement_ids`、`failure_category`)、`next_seq: int`(从 0 起)。
- `recall(query, *, limit)`:`limit` 不是非负 int 抛 `ActioDataError`;取 `snapshot(entries)`,用 `overlap` 打分、留重叠 >= 1 的,按 `(重叠数降序, 置信度降序, content_id 升序)` 排序,按 `content_id` 去重保留第一条,取前 `limit` 条,渲染成 `MemoryHit(content_id(subject, content), f"{subject}: {content}")`。不直接调 `retrieve`:它的平手裁决用条目自带 id,同一内容换个 id 进来顺序就会变,违背 4.1 节"prompt 与轨迹不变";也不去重,而两条同内容条目会让轨迹的 `memory_hits` 出现重复 id。
- `begin(event) -> tuple[str, ...] | None`:已有同 key 行时,`state == "abandoned"` 或文本不同抛 `MemoryEventConflict`;`committed` 返回存下的 `statement_ids`(幂等);`pending` 返回 `None`(交给调用方再抽一次)。新 key 写一行 `state="pending"`,`seq = next_seq`,随后 `next_seq += 1`,返回 `None`。
- `commit(key) -> tuple[str, ...]`:按行里的原文追加 `MemoryEntry(id="mem-" + sha256(key)[:16], version=1, subject=interlocutor, content=text, timestamp=2026-01-01T00:00:00Z 加 seq 秒, confidence=1.0, source_episode=key)`,把行改成 `committed`、`statement_ids=[content_id(interlocutor, text)]`、`failure_category=None`。条目 id 取 key 的哈希而不是内容 id:两个 episode 说同一句话时内容 id 相同,同 id 同版本两条不同条目会让 `snapshot` 抛"two different entries";条目 id 不进命中也不进摘要。
- `fail(key, category)`:行保持 `pending`,记下 `failure_category`。`abandon(key)`:`pending` 改 `abandoned`;`committed` 抛 `MemoryEventConflict`;已 `abandoned` 是空操作;未知 key 抛 `MemoryFailureError(transient=False)`。
- `digest()`:形状就是测试里的 `_digest_of`,但条目取 `snapshot(entries)` 的结果(召回实际看到的那一份),不是原始列表:否则两个库的条目去掉 `id` 后多重集相同、而按 `id` 分组的版本归属不同时,摘要相同、召回却不同;`abandoned` 行进摘要时只保留 `{"key", "state"}` 两列(4.5 节)。
- `to_json()`/`from_json(obj)`:`{"entries": [e.to_dict()...], "events": [行...按 seq], "next_seq": n}`;`from_json` 用 `MemoryEntry.from_dict` 还原并校验键集合,不合法抛 `ActioDataError`。

`TupleMemory.__init__(entries=())`:逐条查 `isinstance(e, MemoryEntry)`(否则 `ActioDataError`),再跑一次 `snapshot(entries)` 让同 id 同版本的冲突在构造时就抛。`remember(event)` = `begin`,返回非 `None` 就原样包成 `RememberOutcome(ids, False, None)`,否则 `commit` 后返回 `RememberOutcome(ids, False, None)`;替身的抽取从不失败。`embed_pending()` 返回 0。`close()` 置关闭标志,重复调用是空操作;关闭后其余四个方法抛 `MemoryClosedError`。

`tests/test_actio_instance.py` 的 `_ALLOWED` 加 `"hashlib"`、`"json"`、`"datetime"`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_memory_contract.py tests/test_actio_memory_experience.py tests/test_actio_memory_port.py tests/test_actio_instance.py tests/test_ma_config_library.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/actio/memory.py tests/memory_contract_sample.yaml tests/test_actio_memory_contract.py
git add tests/test_actio_memory_experience.py tests/test_actio_instance.py
git commit -m "feat(actio): TupleMemory 用内容 id 召回,记忆端口共享契约与规范样例"
```

---

### Task 3: 激活轨迹 v2

**Files:**
- Modify: `src/actio/trace.py`(`TRACE_SCHEMA` 改名为 `TRACE_SCHEMA_V1`,新增 `TRACE_SCHEMA_V2`、`TRACE_KEYS_V2`;`build_trace` 加 `memory`;`validate_trace` 按 `schema` 分两套键集合)
- Test: `tests/test_actio_instance.py`(轨迹模式一节:导入行把 `TRACE_SCHEMA` 换成 `TRACE_SCHEMA_V1, TRACE_SCHEMA_V2`;`test_build_trace_exact_shape_with_a_conflict` 末行断言改成 `TRACE_SCHEMA_V1 == "actio.trace.v1"`,字典里的 `"schema": TRACE_SCHEMA` 改成 `TRACE_SCHEMA_V1`;其余 v1 用例不动,它们证明 v1 仍被接受)

`TRACE_SCHEMA` 只有 `src/actio/trace.py` 与 `tests/test_actio_instance.py` 引用(已 grep),不留别名。`src/mindagent/analysis.py` 经 `validate_trace` 消费轨迹、`src/mindagent/deployment.py` 的 `_reusable` 只读 `library_version`,两者对 v1 与 v2 都不用改。

**Interfaces:**
- Consumes: 无
- Produces:
  - `actio.trace.TRACE_SCHEMA_V1 = "actio.trace.v1"`、`TRACE_SCHEMA_V2 = "actio.trace.v2"`;`TRACE_KEYS` 不变(八个),`TRACE_KEYS_V2 = TRACE_KEYS + ("memory",)`;`MEMORY_TRACE_KEYS = ("digest_before", "remembered", "extraction_failed")`
  - `build_trace(*, signals, candidates, selected, memory_hits, experience, library_version, memory: dict[str, str] | None = None) -> dict`:`memory` 为 `None` 产出 v1,否则产出 v2 并把 `memory` 复制进去
  - `validate_trace(trace)` 接受 v1 与 v2

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_instance.py(轨迹模式一节追加;文件头加 from google.protobuf import json_format, struct_pb2)
MEM = {"digest_before": "ab" * 32, "remembered": "2", "extraction_failed": "0"}


def test_build_trace_with_memory_is_v2_with_nine_keys():
    t = build_trace(signals=[], candidates=[], selected=None, memory_hits=["mem-a"], experience=None,
                    library_version=LV, memory=MEM)
    assert TRACE_SCHEMA_V2 == "actio.trace.v2"
    assert t["schema"] == TRACE_SCHEMA_V2 and len(t) == 9 and t["memory"] == MEM
    assert t["memory"] is not MEM
    assert all(v is None or type(v) in (str, bool) for v in _walk_leaves(t))
    validate_trace(t)


def test_a_v2_trace_survives_a_struct_round_trip():
    t = build_trace(signals=["first_turn"], candidates=["skill-a"], selected="skill-a", memory_hits=[],
                    experience=None, library_version=LV, memory={**MEM, "remembered": "12"})
    s = struct_pb2.Struct()
    s.update(t)
    back = json_format.MessageToDict(s)
    assert back == t and back["memory"]["remembered"] == "12"
    validate_trace(back)


V2_GOOD = dict(signals=[], candidates=[], selected=None, memory_hits=[], experience=None, library_version=LV)


@pytest.mark.parametrize("patch", [
    {"memory": None}, {"memory": {}}, {"memory": {**MEM, "extra": "1"}},
    {"memory": {k: v for k, v in MEM.items() if k != "remembered"}},
    {"memory": {**MEM, "digest_before": ""}}, {"memory": {**MEM, "remembered": 2}},
    {"memory": {**MEM, "remembered": "2.0"}}, {"memory": {**MEM, "extraction_failed": "-1"}},
    {"memory": {**MEM, "remembered": "٣"}},          # 阿拉伯-印度数字:str.isdecimal() 为真,但不是 ASCII
    {"memory": {**MEM, "remembered": "02"}}, {"memory": {**MEM, "remembered": ""}},
])
def test_validate_trace_rejects_a_bad_v2_memory_block(patch):
    good = build_trace(**V2_GOOD, memory=MEM)
    with pytest.raises(ActioDataError, match=r"trace\.memory"):
        validate_trace({**good, **patch})


def test_the_schema_decides_the_key_set():
    v1 = build_trace(**V2_GOOD)
    v2 = build_trace(**V2_GOOD, memory=MEM)
    with pytest.raises(ActioDataError, match="expected exactly keys"):
        validate_trace({**v1, "memory": MEM})                      # v1 带 memory 键
    with pytest.raises(ActioDataError, match="expected exactly keys"):
        validate_trace({k: v for k, v in v2.items() if k != "memory"})  # v2 缺 memory 键
    with pytest.raises(ActioDataError, match=r"trace\.schema"):
        validate_trace({**v2, "schema": "actio.trace.v3"})


def test_build_trace_rejects_a_bad_memory_block():
    with pytest.raises(ActioDataError, match=r"trace\.memory"):
        build_trace(**V2_GOOD, memory={**MEM, "remembered": 3})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'TRACE_SCHEMA_V1' from 'actio.trace'`

- [ ] **Step 3: 实现**

`validate_trace`:先查是 dict;取 `trace.get("schema")`,`TRACE_SCHEMA_V1` 对应 `TRACE_KEYS`、`TRACE_SCHEMA_V2` 对应 `TRACE_KEYS_V2`,都不是抛 `ActioDataError("trace.schema: expected 'actio.trace.v1' or 'actio.trace.v2', got ...")`;键集合不等时沿用现有的 `"trace: expected exactly keys ..."` 文案。八个旧键的校验原样保留。v2 再校验 `memory`:是 dict、键集合恰为 `MEMORY_TRACE_KEYS`、三个值都是非空 `str`;`remembered` 与 `extraction_failed` 必须 `re.fullmatch(r"0|[1-9][0-9]*", v)`。用显式 `[0-9]` 而不是 `str.isdecimal()`,后者接受非 ASCII 数字;不许前导零,因为实例写的是 `str(int)`,同一计数只有一种写法。错误信息都以 `trace.memory` 开头。`re` 已在 `actio` 的导入白名单里。

`build_trace`:`memory` 不为 `None` 时 `schema` 取 `TRACE_SCHEMA_V2` 并加 `"memory": dict(memory)`;最后照旧调 `validate_trace`,所以坏的 `memory` 在构造时就抛。模块 docstring 补一句:v2 的计数也是十进制字符串,理由与 `library_version` 相同(Struct 把数字存成 double)。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_instance.py tests/test_ma_analysis.py tests/test_ma_verify.py tests/test_ma_deployment.py -q -p no:cacheprovider`
Expected: PASS(此时实例还产出 v1,mindagent 测试里用 `build_trace` 造的 v1 轨迹照旧通过)

- [ ] **Step 5: 提交**

```bash
git add src/actio/trace.py tests/test_actio_instance.py
git commit -m "feat(actio): 激活轨迹 v2 带 memory 块,v1 仍被接受"
```

---

### Task 4: `FileTupleStore`:落盘替身与部署管理面

**Files:**
- Modify: `src/actio/memory.py`(新增 `FileTupleStore` 与它发出的端口类 `_FilePort`)
- Modify: `tests/test_actio_memory_contract.py`(`PORT_FACTORIES` 加 `"file"`)
- Create: `tests/test_actio_file_tuple_store.py`

**Interfaces:**
- Consumes: Task 1 的全部类型;Task 2 的 `_Ledger`(`begin`/`commit`/`fail`/`abandon`/`digest`/`to_json`/`from_json`)、`TupleMemory`、`content_id`
- Produces:
  - `actio.memory.FileTupleStore(root: Path, *, extraction_failure: Callable[[MemoryEvent], str | None] | None = None)`,满足 `DeploymentMemoryStore`。`extraction_failure` 是故障注入口:对一条事件返回非 `None` 的字符串,就当作这次抽取以该 `failure_category` 失败;缺省从不失败。第 5 步的部署集成测试(第一次尝试留 `pending` 行)用它
  - 落盘格式:`<root>/<persona_id>.json` = `_Ledger.to_json()`,即 `{"entries": [...], "events": [...], "next_seq": n}`;人格还没有文件时等同空库
  - `open(persona_id)` 返回的端口满足 `MemoryBackend`;`port.digest() == store.digest(persona_id)`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_memory_contract.py(PORT_FACTORIES 加一项;导入补 FileTupleStore)
PORT_FACTORIES = {
    "tuple": lambda fresh_dir: TupleMemory(),
    "file": lambda fresh_dir: FileTupleStore(fresh_dir).open("pa"),
}
```

```python
# tests/test_actio_file_tuple_store.py
import json

import pytest

from actio.errors import ActioDataError
from actio.memory import FileTupleStore, TupleMemory, content_id
from actio.memory_port import (
    DeploymentMemoryStore, MemoryBackend, MemoryEvent, MemoryEventConflict, MemoryFailureError,
    RememberOutcome,
)

EV = MemoryEvent("ep-1#0", "pb：我担心预算超支", "pb")


class Flaky:
    """按 key 注入抽取失败;failing 清空后就恢复。"""

    def __init__(self, *keys):
        self.failing = set(keys)

    def __call__(self, event):
        return "timeout" if event.key in self.failing else None


def _rows(root, persona="pa"):
    return {r["key"]: r for r in json.loads((root / f"{persona}.json").read_text(encoding="utf-8"))["events"]}


def test_store_and_port_satisfy_their_protocols(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("pa")
    assert isinstance(store, DeploymentMemoryStore) and isinstance(port, MemoryBackend)
    assert port.digest() == store.digest("pa") == TupleMemory().digest()  # 没有文件就是空库
    port.close()


def test_the_same_events_give_the_same_hits_and_digest_as_tuple_memory(tmp_path):
    port, mem = FileTupleStore(tmp_path).open("pa"), TupleMemory()
    for ev in (EV, MemoryEvent("ep-1#2", "pb：人手也不够", "pb")):
        assert port.remember(ev) == mem.remember(ev)
    assert port.recall("人手不够预算", limit=3) == mem.recall("人手不够预算", limit=3)
    assert port.digest() == mem.digest()
    port.close()


def test_a_failed_extraction_leaves_a_pending_row_with_its_text_and_a_retry_commits_it(tmp_path):
    flaky = Flaky(EV.key)
    store = FileTupleStore(tmp_path, extraction_failure=flaky)
    assert store.remember_event("pa", EV.key, EV.text, interlocutor="pb") == RememberOutcome((), True, "timeout")
    row = _rows(tmp_path)[EV.key]
    assert (row["state"], row["text"], row["interlocutor"], row["seq"], row["failure_category"]) == (
        "pending", EV.text, "pb", 0, "timeout")
    assert store.pending_events("pa") == [EV.key]
    port = store.open("pa")
    assert port.recall("预算", limit=3) == []
    flaky.failing.clear()
    out = port.remember(EV)  # 同 key 同文本:pending 行被推进,不撞 EventConflict
    assert out == RememberOutcome((content_id("pb", EV.text),), False, None)
    assert _rows(tmp_path)[EV.key]["state"] == "committed" and _rows(tmp_path)[EV.key]["seq"] == 0
    assert store.pending_events("pa") == []
    before = store.digest("pa")
    assert store.remember_event("pa", EV.key, EV.text, interlocutor="pb") == out  # 幂等
    assert store.digest("pa") == before
    port.close()


def test_seq_is_allocated_when_the_row_is_written(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=Flaky("ep-1#0"))
    store.remember_event("pa", "ep-1#0", "pb：一", interlocutor="pb")
    store.remember_event("pa", "ep-1#2", "pb：二", interlocutor="pb")
    assert {k: r["seq"] for k, r in _rows(tmp_path).items()} == {"ep-1#0": 0, "ep-1#2": 1}
    assert json.loads((tmp_path / "pa.json").read_text(encoding="utf-8"))["next_seq"] == 2


def test_conflicts_and_abandon(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=Flaky("ep-1#2"))
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.remember_event("pa", EV.key, "pb：换了一句", interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("pa", EV.key)                     # committed 行不许放弃
    store.remember_event("pa", "ep-1#2", "pb：人手也不够", interlocutor="pb")
    store.abandon_event("pa", "ep-1#2")
    assert _rows(tmp_path)["ep-1#2"]["state"] == "abandoned"
    assert store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):
        store.remember_event("pa", "ep-1#2", "pb：人手也不够", interlocutor="pb")  # 放弃是终态
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", "never-seen#0")
    assert info.value.transient is False


def test_an_abandoned_row_counts_only_its_key_and_state_in_the_digest(tmp_path):
    a = FileTupleStore(tmp_path / "a", extraction_failure=lambda e: "timeout")
    b = FileTupleStore(tmp_path / "b", extraction_failure=lambda e: "schema_violation")
    a.remember_event("pa", "ep-1#0", "pb：一", interlocutor="pb")
    b.remember_event("pa", "ep-1#0", "pc：完全不同", interlocutor="pc")
    assert a.digest("pa") != b.digest("pa")
    a.abandon_event("pa", "ep-1#0")
    b.abandon_event("pa", "ep-1#0")
    assert a.digest("pa") == b.digest("pa")


def test_backup_and_restore_return_logical_digests_and_roll_back(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    start = store.digest("pa")
    assert store.backup_to("pa", tmp_path / "start.json") == start
    store.remember_event("pa", "ep-2#0", "pb：新的一句", interlocutor="pb")
    assert store.digest("pa") != start
    assert store.restore_from("pa", tmp_path / "start.json") == start
    assert store.digest("pa") == start


def test_restore_refuses_while_a_port_is_open_and_leaves_the_library_alone(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.backup_to("pa", tmp_path / "empty.json")
    port = store.open("pa")
    port.remember(EV)
    before = (tmp_path / "lib" / "pa.json").read_bytes()
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", tmp_path / "empty.json")
    assert info.value.transient is False
    assert (tmp_path / "lib" / "pa.json").read_bytes() == before
    port.close()
    port.close()  # 重复关闭不会把计数减成负数
    assert store.restore_from("pa", tmp_path / "empty.json") == TupleMemory().digest()
    other = store.open("pb")  # 别的人格开着端口不挡 pa
    store.restore_from("pa", tmp_path / "empty.json")
    other.close()


def test_a_retry_after_restore_reuses_the_same_key_without_conflict(tmp_path):
    flaky = Flaky(EV.key)
    store = FileTupleStore(tmp_path / "lib", extraction_failure=flaky)
    start = store.backup_to("pa", tmp_path / "start.json")
    port = store.open("pa")
    assert port.remember(EV).failed
    port.close()
    assert store.restore_from("pa", tmp_path / "start.json") == start
    assert store.pending_events("pa") == []
    flaky.failing.clear()
    port = store.open("pa")
    assert not port.remember(EV).failed
    port.close()


def test_the_port_and_the_management_plane_see_the_same_library(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("pa")
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")  # 门② 直接写库
    assert [h.id for h in port.recall("预算", limit=3)] == [content_id("pb", EV.text)]
    assert port.digest() == store.digest("pa")
    port.close()


@pytest.mark.parametrize("bad", ["", "a/b", "../pa", ".hidden", "p a"])
def test_persona_ids_must_be_safe_file_names(tmp_path, bad):
    with pytest.raises(ActioDataError, match="persona_id"):
        FileTupleStore(tmp_path).open(bad)


def test_a_corrupt_library_or_backup_is_a_non_transient_failure(tmp_path):
    (tmp_path / "pa.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MemoryFailureError) as info:
        FileTupleStore(tmp_path).digest("pa")
    assert info.value.transient is False
    (tmp_path / "bad.json").write_text('{"entries": []}', encoding="utf-8")
    with pytest.raises(MemoryFailureError):
        FileTupleStore(tmp_path / "lib").restore_from("pa", tmp_path / "bad.json")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_file_tuple_store.py tests/test_actio_memory_contract.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'FileTupleStore' from 'actio.memory'`

- [ ] **Step 3: 实现**

`FileTupleStore.__init__` 建 `root`(`mkdir(parents=True, exist_ok=True)`),维护 `_open: dict[str, int]`(每个人格打开着的端口数)。`persona_id` 必须 `PERSONA_ID_PATTERN.fullmatch(persona_id)`(Task 1 定义),否则抛 `ActioDataError("persona_id: ...")`:它直接拼进文件名,`/` 与开头的 `.` 都不许。

读写都经两个私有方法:`_load(persona_id) -> _Ledger`(没有文件返回空账本;`json`/`OSError`/`ActioDataError` 一律包成 `MemoryFailureError(type(exc).__name__, transient=False)`,只报类名)与 `_save(persona_id, ledger)`(写 `<persona_id>.json.tmp` 再 `Path.replace` 到正式文件,中途崩溃不会留下半个库)。每个操作都是"读整份、改、写整份";替身只在单线程里用(每个部署 episode 一条线程),不加锁,docstring 写明。

`remember_event` 复刻设计源第 8 节的三段:① `_load`,`ledger.begin(event)`;返回了 id 就是幂等,直接返回 `RememberOutcome(ids, False, None)`,不调抽取;否则 `_save`(`pending` 行与 `seq` 在抽取前就落盘)。② 调 `extraction_failure(event)`(缺省视为 `None`)。③ 返回 `None` 时重新 `_load`、`ledger.commit(key)`、`_save`,返回 `RememberOutcome(ids, False, None)`;返回类别时 `ledger.fail(key, category)`、`_save`,返回 `RememberOutcome((), True, category)`,不抛。替身的语句不会在 ③ 之前就进库(`commit` 才追加条目),所以第 8 节"按 `engram_ref` 补收据"的分支在替身上不存在,`pending` 行重调一律重新抽取。

`abandon_event` = `_load`、`ledger.abandon(key)`、`_save`。`pending_events` 返回 `state == "pending"` 的 key,按 `seq` 升序。`digest(persona_id)` = `_load(...).digest()`。`backup_to(persona_id, path)` 把 `_load(...).to_json()` 规范化写到 `path`(人格还没有文件时写空账本,所以起点副本总是存在),返回 `_Ledger.from_json(读回的 path).digest()`。`restore_from(persona_id, path)`:`_open.get(persona_id, 0) > 0` 抛 `MemoryFailureError("restore_from: persona has an open port", transient=False)`;再读 `path` 并 `from_json` 校验(坏文件按 `_load` 同样包装),用 `_save` 覆盖,返回摘要。

`_FilePort(store, persona_id)`:构造时 `_open[persona_id] += 1`;`recall`/`digest` 走 `_load`;`remember(event)` 就是 `store.remember_event(persona_id, event.key, event.text, interlocutor=event.interlocutor)`;`embed_pending()` 返回 0;`close()` 第一次调用时 `_open[persona_id] -= 1`,之后是空操作;关闭后其余方法抛 `MemoryClosedError`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_file_tuple_store.py tests/test_actio_memory_contract.py tests/test_actio_memory_experience.py tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: PASS(契约用例对 `tuple` 与 `file` 各跑一遍)

- [ ] **Step 5: 提交**

```bash
git add src/actio/memory.py tests/test_actio_memory_contract.py tests/test_actio_file_tuple_store.py
git commit -m "feat(actio): FileTupleStore 落盘替身,三段写入、放弃与起点副本"
```

---

### Task 5: 实例改持端口:读路径、v2 轨迹、`close` 与 `instantiate` 的新参数

**Files:**
- Modify: `src/actio/instance.py`(`render_guidance`、`ActioInstance` 的字段与 `decide`/`respond`/新增 `close`、`instantiate`)
- Test: `tests/test_actio_instance.py`(新增假端口 `RecordingPort`、`LoggingBackend` 与本任务用例;改写 `test_render_guidance_order_and_format` 与 `test_conflicts_and_memory_hits_are_in_the_trace`)
- Modify: `tests/test_actio_realtime_e2e.py`(第 113、117、127 行的 `mem-temper` 换成内容 id)

随签名变化要动的既有调用方(已 grep `instantiate(`):`src/mindagent/rounds.py:245`、`tests/test_ma_deployment.py`、`tests/test_ma_verify.py` 都只用 `memories=`,简写保留且默认只读,所以不用改;`.memories` 属性除 `lib.memories`(`mindagent.library.LibraryState`,不是实例)外无人读。`ActioInstance.decide` 在 `src/actio/instance.py` 之外没有调用方(grep `\.decide(` 只命中 trainplanner 的同名方法);MindAgent 的验证重放 `src/mindagent/verify.py:replay` 调的是 `instance.respond`。

**Interfaces:**
- Consumes: Task 1 的 `MemoryBackend`、`MemoryHit`、`MemoryFailureError`;Task 2 的 `TupleMemory`、`content_id`;Task 3 的 `build_trace(..., memory=)`
- Produces:
  - `render_guidance(selected: Skill | None, experience: ExperienceActivation | None, memory_hits: list[MemoryHit]) -> list[str]`,记忆行 `f"[memory:{h.id}] {h.text}"`
  - `ActioInstance` 字段:`backend`、`skills`、`memory: MemoryBackend`、`experiences`、`library_version`、`profile`、`registry`、`memory_limit`、`replay_read_only: bool`、`episode_id: str | None`、`interlocutors: dict[str, str]`(去掉 `memories`)
  - `ActioInstance.decide(history, speaker_slot) -> tuple[RouteResult, ExperienceActivation | None, list[MemoryHit]]`
  - `ActioInstance.close() -> None`
  - `instantiate(model_backend, *, skills, library_version, profile, memories=None, memory=None, episode_id=None, interlocutors=None, replay_read_only=None, experiences=(), registry=None, memory_limit=3) -> ActioInstance`
  - 每次 `respond` 的轨迹都是 v2;本任务里 `remembered`/`extraction_failed` 恒为 `"0"`(写路径在 Task 6)
  - `ActioInstance._state: _MemoryState`(模块私有的可变计数,字段 `seen_turn`、`retry`、`remembered`、`extraction_failed`;`field(default_factory=_MemoryState, repr=False, compare=False)`),Task 6 的写路径用它

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_instance.py(文件头补:from actio.memory import FileTupleStore, TupleMemory, content_id;
# from actio.memory_port import MemoryEvent, MemoryFailureError, MemoryHit, RememberOutcome)
class RecordingPort:
    """记录调用顺序的假端口。log 可与 LoggingBackend 共用,于是能断言 recall/后端/remember 的先后。
    fail_keys:key -> 还要以 timeout 失败的次数;raise_on:方法名 -> 要抛的异常。"""

    def __init__(self, log=None, *, hits=(), fail_keys=None, raise_on=None):
        self.log = [] if log is None else log
        self.hits = list(hits)
        self.fail_keys = dict(fail_keys or {})
        self.raise_on = dict(raise_on or {})
        self.stored = 0
        self.closed = 0

    def _call(self, entry):
        self.log.append(entry)
        if entry[0] in self.raise_on:
            raise self.raise_on[entry[0]]

    def recall(self, query, *, limit):
        self._call(("recall", query, limit))
        return self.hits[:limit]

    def remember(self, event):
        self._call(("remember", event))
        if self.fail_keys.get(event.key, 0) > 0:
            self.fail_keys[event.key] -= 1
            return RememberOutcome((), True, "timeout")
        self.stored += 1
        return RememberOutcome((f"mem-{self.stored}",), False, None)

    def embed_pending(self):
        self._call(("embed_pending",))
        return 0

    def digest(self):
        self._call(("digest",))
        return f"{self.stored:064x}"

    def close(self):
        self.closed += 1


class LoggingBackend:
    def __init__(self, log):
        self.log = log

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        self.log.append(("backend", list(guidance)))
        return ("回复", None)


def _ported(port, backend=None, **kw):
    kw.setdefault("episode_id", "ep")
    return instantiate(backend or EchoGuidance(), skills=[], memory=port, library_version=LV, profile=None, **kw)


def test_render_guidance_order_and_format():   # 改写:记忆行收 MemoryHit
    ...  # exp/act 的构造原样保留
    hit = MemoryHit("mem-1", "pb: 担心预算")
    assert render_guidance(_calm_skill(), act, [hit]) == [
        "[skill:skill-calm] 先回应对方的情绪",
        "[experience:e1/secondary] 提问",
        "[memory:mem-1] pb: 担心预算",
    ]
    assert render_guidance(None, None, []) == []


def test_conflicts_and_memory_hits_are_in_the_trace():   # 改写:命中是内容 id,轨迹是 v2
    skills = [_calm_skill(), _calm_skill(id="skill-firm", priority=5, policy_instructions=("坚持立场",))]
    mem = MemoryEntry("mem-angry", 1, "pb", "对方容易生气", "2026-01-01T00:00:00Z", 0.9)
    cid = content_id("pb", "对方容易生气")
    echo = EchoGuidance()
    inst = instantiate(echo, skills=skills, memories=[mem], library_version={"skill": 7, "memory": 2}, profile={"name": "x"})
    _, _, trace = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "我很生气")], "a")
    assert trace["candidates"] == ["skill-firm", "skill-calm"]
    assert trace["selected"] == "skill-firm"
    assert trace["conflict"] == {"skills": ["skill-firm", "skill-calm"], "rule": TIE_BREAK_RULE}
    assert trace["memory_hits"] == [cid]
    assert trace["library_version"] == {"skill": "7", "memory": "2"}
    assert trace["schema"] == TRACE_SCHEMA_V2
    assert trace["memory"] == {"digest_before": TupleMemory((mem,)).digest(), "remembered": "0", "extraction_failed": "0"}
    assert echo.calls == [["[skill:skill-firm] 坚持立场", f"[memory:{cid}] pb: 对方容易生气"]]
    validate_trace(trace)


def test_renaming_an_entry_changes_neither_the_prompt_nor_the_trace():
    def run(mid):
        echo = EchoGuidance()
        mem = MemoryEntry(mid, 1, "pb", "对方容易生气", "2026-01-01T00:00:00Z", 0.9)
        inst = instantiate(echo, skills=[], memories=[mem], library_version=LV, profile=None)
        return echo.calls, inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "我很生气")], "a")[2]
    assert run("mem-angry") == run("mem-renamed")


def test_digest_then_recall_then_the_reply():
    log = []
    port = RecordingPort(log, hits=[MemoryHit("mem-a", "pb: 预算"), MemoryHit("mem-b", "pb: 人手")])
    inst = _ported(port, LoggingBackend(log), replay_read_only=True, memory_limit=1)
    history = [TurnRecord(0, "b", "钱不够"), TurnRecord(1, "a", "嗯"), TurnRecord(2, "b", "预算呢")]
    _, _, trace = inst.respond(_persona(), _scenario(), history, "a")
    assert log == [("digest",), ("recall", "预算呢", 1), ("backend", ["[memory:mem-a] pb: 预算"])]
    assert trace["memory_hits"] == ["mem-a"]
    assert trace["memory"]["digest_before"] == "0" * 64


def test_no_recall_before_the_opponent_has_spoken():
    log = []
    inst = _ported(RecordingPort(log), LoggingBackend(log), replay_read_only=True)
    _, _, trace = inst.respond(_persona(), _scenario(), [], "a")
    assert log == [("digest",), ("backend", [])] and trace["memory_hits"] == []


def test_decide_recalls_but_never_writes():
    port = RecordingPort(hits=[MemoryHit("mem-a", "pb: 预算")])
    routed, exp, hits = _ported(port).decide([TurnRecord(0, "b", "预算")], "a")
    assert port.log == [("recall", "预算", 3)] and hits == [MemoryHit("mem-a", "pb: 预算")]


@pytest.mark.parametrize("method", ["digest", "recall"])
def test_read_failures_become_memory_failures_before_the_backend_is_called(method):
    log = []
    port = RecordingPort(log, raise_on={method: RuntimeError("https://endpoint.example/v1 sk-secret")})
    with pytest.raises(MemoryFailureError) as info:
        _ported(port, LoggingBackend(log)).respond(_persona(), _scenario(), [TurnRecord(0, "b", "预算")], "a")
    assert info.value.transient is False and str(info.value) == "RuntimeError"  # 只报类名
    assert not any(entry[0] == "backend" for entry in log)
    original = MemoryFailureError("RecallDegradedError", transient=True)
    port = RecordingPort(raise_on={method: original})
    with pytest.raises(MemoryFailureError) as info:
        _ported(port).respond(_persona(), _scenario(), [TurnRecord(0, "b", "预算")], "a")
    assert info.value is original  # 已是 MemoryFailureError 的原样上抛,transient 不被改写


def test_instantiate_memory_arguments():
    base = dict(skills=[], library_version=LV, profile=None)
    with pytest.raises(ActioDataError, match="exactly one of"):
        instantiate(EchoGuidance(), **base)
    with pytest.raises(ActioDataError, match="exactly one of"):
        instantiate(EchoGuidance(), **base, memories=[], memory=TupleMemory())
    with pytest.raises(ActioDataError, match="episode_id"):
        instantiate(EchoGuidance(), **base, memory=TupleMemory())
    with pytest.raises(ActioDataError, match="episode_id"):
        instantiate(EchoGuidance(), **base, memories=[], replay_read_only=False)
    with pytest.raises(ActioDataError, match="memory"):
        instantiate(EchoGuidance(), **base, memory=object(), episode_id="ep")
    for bad in ({"episode_id": ""}, {"episode_id": 3}, {"interlocutors": {"b": ""}},
                {"interlocutors": [("b", "pb")]}, {"replay_read_only": "yes"}):
        with pytest.raises(ActioDataError):
            instantiate(EchoGuidance(), **base, memory=TupleMemory(), **{"episode_id": "ep", **bad})
    short = instantiate(EchoGuidance(), **base, memories=[])
    assert short.replay_read_only is True and short.episode_id is None and short.interlocutors == {}
    ported = instantiate(EchoGuidance(), **base, memory=TupleMemory(), episode_id="ep", interlocutors={"b": "pb"})
    assert ported.replay_read_only is False and ported.interlocutors == {"b": "pb"}
    assert instantiate(EchoGuidance(), **base, memory=TupleMemory(), replay_read_only=True).episode_id is None


def test_close_forwards_to_the_port():
    port = RecordingPort()
    inst = _ported(port)
    inst.close()
    assert port.closed == 1
```

```python
# tests/test_actio_realtime_e2e.py(只换三处字面量;文件头补 from actio.memory import content_id)
TEMPER = content_id("pb", "对方一急就会生气")
# 第 113 行
    assert first["memory_hits"] == [TEMPER]
# 第 117 行
    assert persisted.turns[1].utterance == f"[skill:skill-calm] 先回应对方的情绪|[memory:{TEMPER}] pb: 对方一急就会生气"
# 第 127 行
    assert persisted.turns[1].utterance == f"[memory:{TEMPER}] pb: 对方一急就会生气"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_instance.py tests/test_actio_realtime_e2e.py -q -p no:cacheprovider`
Expected: FAIL,`TypeError: instantiate() got an unexpected keyword argument 'memory'`;`render_guidance` 用例报 `AttributeError: 'MemoryHit' object has no attribute 'subject'`

- [ ] **Step 3: 实现**

`ActioInstance` 仍是 `@dataclass(frozen=True)`,配置字段照 Interfaces;另加 `_state: _MemoryState = field(default_factory=_MemoryState, repr=False, compare=False)`,`_MemoryState` 是模块私有的可变 dataclass(`seen_turn: int = -1`、`retry: tuple[MemoryEvent, ...] = ()`、`remembered: int = 0`、`extraction_failed: int = 0`),Task 6 的写路径用它;配置不可变、每 episode 的计数可变,分开放。一个带写入的实例只服务一条 episode(`episode_id` 在构造时定),一条线程用,docstring 写明。

`instantiate` 的新校验,按此顺序,都抛 `ActioDataError`:
- `memories` 与 `memory` 恰好一个不是 `None`,否则 `"exactly one of memories= and memory= is required"`。
- `memories` 给了就构造 `TupleMemory(tuple(memories))`(条目类型与同版本冲突在那里查,沿用既有用例 `memories=[{"id": "m"}]` 抛 `ActioDataError`);`memory` 给了就要求 `isinstance(memory, MemoryBackend)`,否则 `"memory: expected a MemoryBackend, got <类名>"`。
- `replay_read_only`:`None` 时,只给 `memories` 取 `True`、给 `memory` 取 `False`;不是 `None` 就必须是 `bool`。
- `episode_id`:`None` 或 `_check.text` 通过的非空字符串;生效的 `replay_read_only` 为假且 `episode_id is None` 抛 `"episode_id: required when the instance writes memory"`。合同只写了 `memory=` 一侧;`memories=` 加显式 `replay_read_only=False` 同样要写库,同样需要 key 前缀,所以按"会写库"而不是按参数名判断。
- `interlocutors`:`None` 取 `{}`;否则必须是 dict 且键与值都是非空 `str`,存一份 `dict(...)` 副本。

`decide`:`route`、`activate` 照旧;查询词 `q = _memory_query(history, speaker_slot)`,`q == ""` 时命中取 `[]`、不调 `recall`,否则 `self.memory.recall(q, limit=self.memory_limit)`,异常经 `_guard` 包装。docstring 改成"路由、经验与记忆召回,不调模型后端、不写库;召回经端口,所以不再是纯函数"。对方没开口时不调 `recall` 是本计划的取舍:替身对空串本来就返回空,Starling 一侧则要嵌入一个空查询词,`strict=True` 下可能抛瞬态失败,让对话开场那一轮无谓地重跑。

`_guard(call)`:模块私有,`MemoryFailureError` 原样上抛;其余 `Exception` 改抛 `MemoryFailureError(type(exc).__name__, transient=False) from None`(第 10 节映射表:Starling 之外的异常一律非瞬态;只报类名,不带可能含端点的原文,与既有 `ActioDataError(...) from None` 的做法一致)。

`respond`:`digest_before = _guard(self.memory.digest)` → `decide` → `render_guidance` → 后端 → (Task 6 在此插入写路径) → `build_trace(..., memory_hits=[h.id for h in hits], memory={"digest_before": digest_before, "remembered": str(state.remembered), "extraction_failed": str(state.extraction_failed)})`。`digest` 与 `recall` 任一失败都在调后端之前抛出。

`close()`:`self.memory.close()`;不放弃任何事件(docstring 写明:放弃由 MindAgent 在 episode 最终结束时经管理面做)。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_instance.py tests/test_actio_realtime_e2e.py tests/test_actio_memory_contract.py tests/test_ma_verify.py tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_e2e.py tests/test_runner_activation_trace.py -q -p no:cacheprovider`
Expected: PASS(实例现在产出 v2;`_reusable` 只比 `library_version`,分析器经 `validate_trace` 两版都收)

- [ ] **Step 5: 提交**

```bash
git add src/actio/instance.py tests/test_actio_instance.py tests/test_actio_realtime_e2e.py
git commit -m "feat(actio): 实例改持记忆端口,召回经端口,轨迹升 v2"
```

---

### Task 6: 实例的写路径:回复后 `remember` 对方新发言、episode 内重试一次、失败计数进轨迹

**Files:**
- Modify: `src/actio/instance.py`(`respond` 在后端回复之后、`build_trace` 之前插入写路径,新增私有方法 `_remember_new`)
- Test: `tests/test_actio_instance.py`(复用 Task 5 的 `RecordingPort`、`LoggingBackend`、`_ported`)

**Interfaces:**
- Consumes: Task 5 的 `ActioInstance`(`memory`、`replay_read_only`、`episode_id`、`interlocutors`、`_state`)与 `_guard`;Task 1 的 `MemoryEvent`、`RememberOutcome`;Task 4 的 `FileTupleStore(root, extraction_failure=...)`
- Produces:事件 key `f"{episode_id}#{turn_index}"`、文本 `f"{persona_id}：{utterance}"`(全角冒号)、`interlocutor = interlocutors.get(slot, slot)`;轨迹 `memory.remembered`/`memory.extraction_failed` 是本实例构造以来的累计值,含本轮的写入。第 2、3、5 步的组合根只需要给 `instantiate` 传 `memory=`、`episode_id=`、`interlocutors=`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_actio_instance.py(追加)
B0, A1, B2, A3, B4 = (TurnRecord(0, "b", "钱不够"), TurnRecord(1, "a", "嗯"), TurnRecord(2, "b", "预算呢"),
                      TurnRecord(3, "a", "好"), TurnRecord(4, "b", "人手呢"))


def _events(log):
    return [entry[1] for entry in log if entry[0] == "remember"]


def test_after_the_reply_each_new_opponent_turn_is_remembered_then_embedded_once():
    log = []
    inst = _ported(RecordingPort(log), LoggingBackend(log), episode_id="run-1-sft-sc-r0",
                   interlocutors={"b": "pb"})
    _, _, first = inst.respond(_persona(), _scenario(), [B0], "a")
    assert [e[0] for e in log] == ["digest", "recall", "backend", "remember", "embed_pending"]
    assert _events(log) == [MemoryEvent("run-1-sft-sc-r0#0", "pb：钱不够", "pb")]
    log.clear()
    _, _, second = inst.respond(_persona(), _scenario(), [B0, A1, B2], "a")
    assert _events(log) == [MemoryEvent("run-1-sft-sc-r0#2", "pb：预算呢", "pb")]  # 自己的话与旧发言都不写
    assert [e[0] for e in log][-1] == "embed_pending" and [e[0] for e in log].count("embed_pending") == 1
    assert (first["memory"]["remembered"], second["memory"]["remembered"]) == ("1", "2")
    assert second["memory"]["digest_before"] == f"{1:064x}"   # 本轮 recall 之前,含上一轮的写入


def test_an_unmapped_slot_speaks_as_itself():
    port = RecordingPort()
    _ported(port, episode_id="ep").respond(_persona(), _scenario(), [B0], "a")
    assert _events(port.log) == [MemoryEvent("ep#0", "b：钱不够", "b")]


def test_the_turn_index_comes_from_history_not_from_a_counter():
    port = RecordingPort()
    _ported(port, episode_id="ep").respond(_persona(), _scenario(), [TurnRecord(7, "b", "钱不够")], "a")
    assert _events(port.log)[0].key == "ep#7"


def test_a_failed_extraction_is_counted_not_raised_and_retried_once_in_the_next_respond():
    port = RecordingPort(fail_keys={"ep#0": 1})
    inst = _ported(port, episode_id="ep")
    _, _, t1 = inst.respond(_persona(), _scenario(), [B0], "a")
    assert (t1["memory"]["remembered"], t1["memory"]["extraction_failed"]) == ("0", "1")
    port.log.clear()
    _, _, t2 = inst.respond(_persona(), _scenario(), [B0, A1, B2], "a")
    assert [e.key for e in _events(port.log)] == ["ep#0", "ep#2"]       # 上次失败的在前,同 key 同文本
    assert (t2["memory"]["remembered"], t2["memory"]["extraction_failed"]) == ("2", "1")


def test_a_second_failure_is_counted_and_not_retried_again():
    port = RecordingPort(fail_keys={"ep#0": 5})
    inst = _ported(port, episode_id="ep")
    inst.respond(_persona(), _scenario(), [B0], "a")
    inst.respond(_persona(), _scenario(), [B0, A1, B2], "a")
    port.log.clear()
    _, _, t3 = inst.respond(_persona(), _scenario(), [B0, A1, B2, A3, B4], "a")
    assert [e.key for e in _events(port.log)] == ["ep#4"]
    assert (t3["memory"]["remembered"], t3["memory"]["extraction_failed"]) == ("2", "2")


def test_a_read_only_instance_never_writes():
    port = RecordingPort()
    inst = _ported(port, replay_read_only=True)
    _, _, t = inst.respond(_persona(), _scenario(), [B0], "a")
    assert [e[0] for e in port.log] == ["digest", "recall"]
    assert (t["memory"]["remembered"], t["memory"]["extraction_failed"]) == ("0", "0")
    short = instantiate(EchoGuidance(), skills=[], memories=[], library_version=LV, profile=None)
    short.respond(_persona(), _scenario(), [B0], "a")  # memories= 简写默认只读,不需要 episode_id


@pytest.mark.parametrize("method", ["remember", "embed_pending"])
def test_write_exceptions_become_memory_failures(method):
    port = RecordingPort(raise_on={method: OSError("/secret/path")})
    with pytest.raises(MemoryFailureError) as info:
        _ported(port).respond(_persona(), _scenario(), [B0], "a")
    assert info.value.transient is False and str(info.value) == "OSError"
    transient = MemoryFailureError("embedding failed", transient=True)
    port = RecordingPort(raise_on={method: transient})
    with pytest.raises(MemoryFailureError) as info:
        _ported(port).respond(_persona(), _scenario(), [B0], "a")
    assert info.value is transient


def test_with_the_file_store_the_round_prefixed_key_lands_and_close_abandons_nothing(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=lambda event: "timeout")
    inst = instantiate(EchoGuidance(), skills=[], memory=store.open("pa"), episode_id="round-7/deploy-m-sc-0",
                       interlocutors={"b": "pb"}, library_version=LV, profile=None)
    _, _, t = inst.respond(_persona(), _scenario(), [B0], "a")
    assert t["memory"]["extraction_failed"] == "1"
    inst.close()
    assert store.pending_events("pa") == ["round-7/deploy-m-sc-0#0"]  # close 只关库,不放弃
    with pytest.raises(MemoryFailureError):
        inst.respond(_persona(), _scenario(), [B0], "a")              # 端口已关:MemoryClosedError
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: FAIL,`assert [...] == ['digest', 'recall', 'backend', 'remember', 'embed_pending']`(Task 5 之后实例还不写库)

- [ ] **Step 3: 实现**

`respond` 在后端回复校验之后、`build_trace` 之前:`replay_read_only` 为假时调 `self._remember_new(history, speaker_slot)`。

`_remember_new(history, speaker_slot) -> None`:
1. `fresh` = `history` 里 `turn_index > self._state.seen_turn` 且 `speaker_slot != 本方` 的轮次,保持历史顺序;每轮转成 `MemoryEvent(f"{episode_id}#{t.turn_index}", f"{pid}：{t.utterance}", pid)`,`pid = self.interlocutors.get(t.speaker_slot, t.speaker_slot)`。随后 `seen_turn` 取 `history` 里最大的 `turn_index`(`history` 为空时不变)。
2. 先逐个 `remember` 上次留下的 `self._state.retry`,再逐个 `remember` `fresh`;每次调用经 `_guard`。结果 `failed` 为假则 `remembered += 1`;为真则 `extraction_failed += 1`,来自 `fresh` 的进新的重试表,来自旧重试表的丢掉(episode 内只重试一次,行留 `pending`,由 MindAgent 在 episode 最终结束时处理)。最后 `self._state.retry = 新的重试表`。
3. 无论本轮有没有事件,都 `_guard(self.memory.embed_pending)` 一次;返回值不用。不调 `tick`(S12)。

`turn_index` 取自 `TurnRecord.turn_index` 而不是实例里的计数器:同一 episode 的重放、断线重连后客户端重建的 `history` 都给出同一批 key,端口的去重才拦得住。对方在本方最后一次回复之后说的话不会被写:写只发生在 `respond` 里,与设计源 4.3"每次 respond 后"一致。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_actio_instance.py tests/test_actio_realtime_e2e.py tests/test_actio_file_tuple_store.py tests/test_actio_memory_contract.py tests/test_actio_memory_experience.py tests/test_actio_memory_port.py tests/test_ma_verify.py tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_e2e.py tests/test_ma_analysis.py -q -p no:cacheprovider`
Expected: PASS

再跑一次全量,确认没有别处依赖实例的旧字段:

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/actio/instance.py tests/test_actio_instance.py
git commit -m "feat(actio): 回复后写入对方新发言,抽取失败只计数并在 episode 内重试一次"
```

---

## 第 2 步:会话资源、终态收尾与准入

### Task 7: `VllmPolicy` 与 `ExternalApiBackend` 的 `respond_guided`

**Files:**
- Modify: `src/factory/opponents.py`(`chat_messages` 加 `guidance` 参数;`ExternalApiBackend` 加 `respond_guided`)
- Modify: `src/factory/vllm_policy.py`(`VllmPolicy` 加 `respond_guided`)
- Test: `tests/test_factory_opponents.py`、`tests/test_factory_vllm_policy.py`

**Interfaces:**
- Consumes: 无(与 `actio.instance.GuidedBackend` 协议同形:`respond_guided(persona, scenario, history, speaker_slot, guidance: list[str]) -> tuple[str, str | None]`,本任务不导入 actio)
- Produces: `chat_messages(persona, scenario, history, speaker_slot, guidance: Sequence[str] = ()) -> list[dict]`;`VllmPolicy.respond_guided(persona, scenario, history, speaker_slot, guidance)`、`ExternalApiBackend.respond_guided(persona, scenario, history, speaker_slot, guidance)`,返回 `(content, None)`。第 3 步的 Actio 包装直接调这两个方法。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_opponents.py
def test_chat_messages_appends_guidance_to_the_only_system_message():
    history = [TurnRecord(0, "a", "你好")]
    plain = chat_messages(_persona(), _scenario(), history, "b")
    guided = chat_messages(_persona(), _scenario(), history, "b",
                           ["[skill:s1] 先复述对方", "[memory:mem-0123456789abcdef] 对方上周迟到"])
    assert [m["role"] for m in guided].count("system") == 1
    assert guided[0]["content"] == (
        plain[0]["content"] + "\n当前要点：\n[skill:s1] 先复述对方\n[memory:mem-0123456789abcdef] 对方上周迟到"
    )
    assert guided[1:] == plain[1:]


def test_empty_guidance_is_byte_identical_to_respond(stub):
    stub.script += [_ok("甲"), _ok("甲")]
    backend = _backend(stub.url)
    history = [TurnRecord(0, "a", "你好")]
    assert backend.respond(_persona(), _scenario(), history, "b") == ("甲", None)
    assert backend.respond_guided(_persona(), _scenario(), history, "b", []) == ("甲", None)
    first, second = stub.requests
    assert first["body"] == second["body"]          # 原始请求体逐字节相同


def test_external_api_respond_guided_posts_one_system_message_with_the_guidance(stub):
    stub.script.append(_ok("乙"))
    backend = _backend(stub.url)
    assert backend.respond_guided(_persona(), _scenario(), [TurnRecord(0, "a", "你好")], "b",
                                  ["[skill:s1] 先复述对方"]) == ("乙", None)
    messages = stub.bodies()[0]["messages"]
    assert [m["role"] for m in messages].count("system") == 1
    assert messages[0]["content"].endswith("\n当前要点：\n[skill:s1] 先复述对方")
```

```python
# tests/test_factory_vllm_policy.py
def test_the_policy_respond_guided_appends_guidance_and_empty_guidance_matches_respond(stub):
    stub.script += [_ok("好吧"), _ok("好吧"), _ok("好吧")]
    with _context(FakeServe(stub.url)) as policy:
        policy.respond(_persona(), _scenario(), HISTORY, "b")
        policy.respond_guided(_persona(), _scenario(), HISTORY, "b", [])
        policy.respond_guided(_persona(), _scenario(), HISTORY, "b", ["[skill:s1] 先复述对方"])
    plain, empty, guided = stub.requests
    assert plain["body"] == empty["body"]
    assert stub.bodies()[2]["messages"] == chat_messages(
        _persona(), _scenario(), HISTORY, "b", ["[skill:s1] 先复述对方"])
```

两个文件的 `_Stub` 都把原始请求体 bytes 存在 `requests[i]["body"]`(opponents 测试第 230 行、vllm 测试第 55 行),逐字节断言直接比它。两个文件都已导入 `chat_messages`。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_opponents.py tests/test_factory_vllm_policy.py -q -p no:cacheprovider -k "guidance or guided"`
Expected: FAIL,`TypeError: chat_messages() takes 4 positional arguments but 5 were given` 与 `AttributeError: 'ExternalApiBackend' object has no attribute 'respond_guided'`

- [ ] **Step 3: 实现**

`chat_messages` 加第五个参数 `guidance: Sequence[str] = ()`:非空时在 system 文本(`"\n".join(lines)`)末尾追加 `"\n当前要点：\n" + "\n".join(guidance)`;空时不碰 system 文本,所以 `respond` 的请求体不变。「当前要点：」用全角冒号,与设计源 7.3 节一致。

`ExternalApiBackend.respond_guided` 与 `VllmPolicy.respond_guided` 各自与 `respond` 同体,只把 `guidance` 传进 `chat_messages`;`respond` 改为调 `respond_guided(..., ())`,保证两者共用一条路径、空指导语逐字节相同。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_opponents.py tests/test_factory_vllm_policy.py tests/test_factory_interact_wire.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/opponents.py src/factory/vllm_policy.py tests/test_factory_opponents.py tests/test_factory_vllm_policy.py
git commit -m "feat(factory): 训练位与外部对手后端支持带指导语的发言"
```

---

### Task 8: `SessionResources` 与 `TimeoutBackend` 登记在途线程

**Files:**
- Create: `src/simulator/realtime/resources.py`
- Modify: `src/simulator/realtime/backend_timeout.py`(新增两个记忆异常基类;`TimeoutBackend.__init__` 与 `respond`)
- Test: `tests/test_realtime_resources.py`(新)、`tests/test_realtime_backend_timeout.py`(既有四个用例不改,`resources` 缺省时行为不变)

**Interfaces:**
- Consumes: 无
- Produces:
  - `SessionInfo(scenario: Scenario, assignment: RoleAssignment, personas: dict[str, Persona], client_slot: str, session_id: str, episode_id: str)`,frozen dataclass。
  - `SessionResources(backends: dict[str, ModelBackend], closers: tuple[Callable[[], None], ...] = ())`;方法 `quiesce() -> None`、`join(timeout_s: float) -> bool`、`close() -> None`、`enter_call() -> None`(已 quiesce 时抛 `SessionClosingError`)、`exit_call() -> None`。
  - `SessionClosingError(Exception)`。
  - `RoleMemoryUnavailableError(Exception)`、`RoleMemoryFailureError(Exception)`(在 `backend_timeout.py`,与三个 `RoleBackend*Error` 互不继承)。
  - `TimeoutBackend(inner: ModelBackend, timeout_s: float, resources: SessionResources | None = None)`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_resources.py
import dataclasses
import shutil
import threading

import pytest

from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.backend_timeout import (
    RoleBackendRejectedError, RoleBackendTimeoutError, RoleBackendUnavailableError,
    RoleMemoryFailureError, RoleMemoryUnavailableError, TimeoutBackend,
)
from simulator.realtime.resources import SessionClosingError, SessionInfo, SessionResources


def _args():
    scenario = Scenario("s1", "bg", [RoleSlot("a", ""), RoleSlot("b", "")], "c", [], "training")
    return Persona("pb", ["t"], "m", "s", ["v"]), scenario, [], "b"


class _Blocking:
    def __init__(self):
        self.release = threading.Event()

    def respond(self, persona, scenario, history, speaker_slot):
        self.release.wait(5)
        return "late", None


def test_session_info_is_frozen_and_carries_both_ids():
    persona, scenario, _, _ = _args()
    assignment = RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")], 4)
    info = SessionInfo(scenario, assignment, {"pb": persona}, "a", "sess-0123456789ab", "ep-sess-0123456789ab")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.episode_id = "other"


def test_after_quiesce_a_wrapped_respond_raises_without_reaching_the_backend():
    calls = []

    class Recording:
        def respond(self, *args):
            calls.append(args)
            return "x", None

    resources = SessionResources(backends={"b": Recording()})
    wrapped = TimeoutBackend(resources.backends["b"], 1.0, resources)
    assert wrapped.respond(*_args()) == ("x", None)
    resources.quiesce()
    with pytest.raises(SessionClosingError):
        wrapped.respond(*_args())
    with pytest.raises(SessionClosingError):
        resources.enter_call()
    assert len(calls) == 1


def test_a_timed_out_call_stays_in_flight_until_its_thread_exits():
    inner = _Blocking()
    resources = SessionResources(backends={"b": inner})
    wrapped = TimeoutBackend(inner, 0.05, resources)
    with pytest.raises(RoleBackendTimeoutError):
        wrapped.respond(*_args())
    resources.quiesce()
    assert resources.join(0.1) is False            # 调用方不等了,线程还在集合里
    inner.release.set()
    assert resources.join(2.0) is True


def test_join_with_nothing_in_flight_returns_true_at_once():
    assert SessionResources(backends={}).join(0) is True


def test_close_runs_every_closer_once_even_when_one_raises(tmp_path):
    copy_dir = tmp_path / "sess-0123456789ab"
    copy_dir.mkdir()
    order = []

    def boom():
        order.append("boom")
        raise OSError("busy")

    def remove():
        order.append("rm")
        shutil.rmtree(copy_dir)

    resources = SessionResources(backends={}, closers=(boom, remove))
    resources.close()
    resources.close()                              # 第二次是空操作
    assert order == ["boom", "rm"]
    assert not copy_dir.exists()


def test_the_memory_error_bases_are_separate_from_the_backend_errors():
    backend_errors = (RoleBackendTimeoutError, RoleBackendRejectedError, RoleBackendUnavailableError)
    for memory_error in (RoleMemoryUnavailableError, RoleMemoryFailureError):
        assert not issubclass(memory_error, backend_errors)
    assert not issubclass(RoleMemoryUnavailableError, RoleMemoryFailureError)
    assert not issubclass(RoleMemoryFailureError, RoleMemoryUnavailableError)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_resources.py -q -p no:cacheprovider`
Expected: FAIL,`ModuleNotFoundError: No module named 'simulator.realtime.resources'`

- [ ] **Step 3: 实现**

`resources.py` 只导入标准库、`simulator.backends.ModelBackend` 与 `simulator.datamodel`(不导入 `backend_timeout`,避免环)。`SessionResources` 用一个 `threading.Condition` 守护三样状态:`_quiescing: bool`、`_in_flight: int`、`_closed: bool`。
- `enter_call`:持锁;`_quiescing` 为真抛 `SessionClosingError("session is closing")`,否则计数加一。
- `exit_call`:持锁减一,归零时 `notify_all`。
- `quiesce`:持锁置位,可重复调用。
- `join(timeout_s)`:`cond.wait_for(lambda: self._in_flight == 0, timeout_s)`;`wait_for` 等待时释放锁,满足设计源 6.2 节"`join` 不持锁等待"。
- `close`:第一次调用按顺序跑 `closers`,每个单独 `try`,异常只记 `logger.warning("session closer failed: %s", type(exc).__name__)` 后继续(设计源第 10 节:副本删除失败不分类、记日志、清扫兜底);之后的调用是空操作。`close` 不调 `quiesce`,也不碰 `backends`:关智能体与删副本目录都由工厂放进 `closers`(设计源 6.2 节"`close()` 直接调它们,包装层不转发")。

`backend_timeout.py`:两个基类各配一段 docstring,写明 T1/T2 与驱动器的终止原因(`role_memory_unavailable` 瞬态、`role_memory_failure` 确定性),以及 `simulator` 不认 `actio.MemoryFailureError`、由组合根的转换后端改抛子类(设计源第 10 节)。

`TimeoutBackend.respond`:`resources` 不为 None 时,在调用方线程里先 `resources.enter_call()`(quiesce 后在这里抛 `SessionClosingError`,不起线程),daemon 线程的 `_call` 用 `try/finally` 在退出时 `resources.exit_call()`。超时只是调用方不再等,线程仍在计数里,直到自然结束。`resources` 为 None 时行为与现状逐行相同。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_resources.py tests/test_realtime_backend_timeout.py tests/test_realtime_driver.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/resources.py src/simulator/realtime/backend_timeout.py tests/test_realtime_resources.py
git commit -m "feat(simulator): 会话资源对象与在途调用登记,记忆异常基类"
```

---

### Task 9: `CreateSession.episode_id` 与客户端 `create_session(episode_id=)`

**Files:**
- Modify: `src/simulator/realtime/proto/realtime_env.proto`(`message CreateSession`,第 51-57 行)
- Regenerate: `src/simulator/realtime/proto/realtime_env_pb2.py`、`realtime_env_pb2.pyi`、`realtime_env_pb2_grpc.py`(跑 `scripts/gen_realtime_proto.sh`,产物入库)
- Modify: `src/simulator/realtime/client.py`(`EnvironmentServiceClient.create_session`)
- Test: `tests/test_realtime_client_transport.py`

**Interfaces:**
- Consumes: 无
- Produces: proto `CreateSession.episode_id`(字段号 6,proto3 `string`,空串即"没给");`EnvironmentServiceClient.create_session(*, scenario_id, role_assignment, personas, purpose, client_driven_slot, episode_id: str | None = None) -> str`。给了 `episode_id` 时客户端本地重建的 `Episode.id` 就是它,没给仍是 `ep-<session_id>`。服务端如何用它在 Task 12。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_client_transport.py
def test_create_session_sends_the_episode_id_and_names_the_local_episode_after_it():
    stub = _FakeStub([_ready("sess-fake")])
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a", episode_id="run-1-grpo_opd-s1-r0",
    )
    assert next(stub.request_gen).create_session.episode_id == "run-1-grpo_opd-s1-r0"
    client.abort()
    assert client.drain_until_end().id == "run-1-grpo_opd-s1-r0"


def test_without_an_episode_id_the_request_leaves_it_empty_and_the_local_id_stays_ep_session():
    stub = _FakeStub([_ready("sess-fake")])
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    assert next(stub.request_gen).create_session.episode_id == ""
    client.abort()
    assert client.drain_until_end().id == "ep-sess-fake"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_client_transport.py -q -p no:cacheprovider -k episode_id`
Expected: FAIL,`TypeError: create_session() got an unexpected keyword argument 'episode_id'`

- [ ] **Step 3: 实现**

proto 的 `CreateSession` 末尾加:

```proto
  // 逻辑 episode id(可选)。给了就是服务端落盘 episode 的 id;空串表示没给,服务端用 ep-<session_id>。
  // 只许 [A-Za-z0-9._-]、1 到 128 字节、不是 "." 或 "..",否则 invalid_session;
  // 同一 episode_id 已有活动会话时以 role_backend_unavailable 拒绝(T1)。
  string episode_id = 6;
```

然后跑 `bash scripts/gen_realtime_proto.sh`。用 `git diff --stat src/simulator/realtime/proto/` 确认只有这三个产物变了;若生成器版本与入库头部(`# Protobuf Python Version: 7.35.1`)不同导致整份重排,停下报告,不要提交一份与 `pyproject.toml` 里 `protobuf>=7.35.1,<8.0.0` 不相容的产物。

`create_session` 加 keyword-only 参数 `episode_id: str | None = None`:不为 None 时写进 `pb2.CreateSession(episode_id=...)`;收到 `session_ready` 后 `self._episode_id = episode_id if episode_id is not None else f"ep-{self._session_id}"`。客户端不校验格式,格式由服务端判 `invalid_session`(一处规则)。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_client_transport.py tests/test_realtime_client.py tests/test_realtime_codec.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/proto/realtime_env.proto src/simulator/realtime/proto/realtime_env_pb2.py
git add src/simulator/realtime/proto/realtime_env_pb2.pyi src/simulator/realtime/proto/realtime_env_pb2_grpc.py
git add src/simulator/realtime/client.py tests/test_realtime_client_transport.py
git commit -m "feat(simulator): CreateSession 可带逻辑 episode id"
```

---

### Task 10: 客户端相邻消息等待上限 `message_timeout_s`

**Files:**
- Modify: `src/simulator/realtime/client.py`(`EnvironmentServiceClient.__init__`、`_next_msg`,类 docstring 的终止原因段)
- Test: `tests/test_realtime_client_transport.py`

**Interfaces:**
- Consumes: 无
- Produces: `EnvironmentServiceClient(stub, *, message_timeout_s: float | None = None)`。相邻两条服务端消息的间隔超过它:客户端在本地合成一条 `EndSession(reason="timeout")` 走既有收尾路径并释放请求流(服务端随之按断连判 `FAILED`,不落盘)。于是会话中途超时表现为 `closed` 为真、`drain_until_end().terminated_reason == "timeout"`,轨迹源与部署驱动器按既有的 `timeout`(T1)重跑,不需要改它们;等 `session_ready` 时超时抛 `SessionRejectedError("timeout", ...)`。第 3 步的 `AgentRuntime.client_factory` 传 90 秒。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_client_transport.py
class _PacedStub:
    """按 (delay_s, msg) 回放;msg 为 _SILENT 时一直不回(最多 5 秒),模拟挂住的服务端。"""
    _SILENT = object()

    def __init__(self, script):
        self._script = list(script)
        self.request_gen = None
        self.cancelled = threading.Event()

    def Interact(self, request_gen):
        self.request_gen = request_gen

        def _replies():
            for delay_s, msg in self._script:
                if msg is _PacedStub._SILENT:
                    self.cancelled.wait(5)
                    return
                time.sleep(delay_s)
                yield msg
        return _replies()


def _paced_client(script, timeout_s):
    stub = _PacedStub(script)
    client = EnvironmentServiceClient(stub, message_timeout_s=timeout_s)
    return client, stub


def test_a_silent_server_mid_session_ends_the_episode_as_timeout_and_releases_the_stream():
    client, stub = _paced_client([(0, _ready()), (0, _PacedStub._SILENT)], timeout_s=0.2)
    client.create_session(scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
                          purpose="training", client_driven_slot="a")
    started = time.monotonic()
    assert client.submit_turn("hi") == []
    assert time.monotonic() - started < 2.0
    assert client.closed
    assert client.drain_until_end().terminated_reason == "timeout"
    _assert_request_stream_released(stub)


def test_the_limit_is_per_gap_not_per_session():
    # 每条间隔 0.15 秒,低于上限 0.3;合计 0.45 秒高于上限,仍不超时
    end = pb2.ServerMessage(end_session=pb2.EndSession(reason="max_turns", episode_id="ep-sess-fake"))
    client, _ = _paced_client([(0, _ready()), (0.15, _turn(0, "a", "hi")), (0.15, _turn(1, "b", "yo")),
                               (0.15, end)], timeout_s=0.3)
    client.create_session(scenario_id="s1", role_assignment=_assignment(max_turns=2), personas=_personas(),
                          purpose="training", client_driven_slot="a")
    client.submit_turn("hi")
    assert client.drain_until_end().terminated_reason == "max_turns"


def test_a_silent_server_before_session_ready_is_a_timeout_rejection():
    client, stub = _paced_client([(0, _PacedStub._SILENT)], timeout_s=0.2)
    with pytest.raises(SessionRejectedError) as info:
        client.create_session(scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
                              purpose="training", client_driven_slot="a")
    assert info.value.reason == "timeout"
    _assert_request_stream_released(stub)
```

(文件头按需补 `import time`、`import pytest`、`from simulator.realtime.client import SessionRejectedError`。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_client_transport.py -q -p no:cacheprovider -k "silent or per_gap"`
Expected: FAIL,`TypeError: __init__() got an unexpected keyword argument 'message_timeout_s'`

- [ ] **Step 3: 实现**

构造参数存为 `self._message_timeout_s`(Task 18 的测试读它)。`message_timeout_s` 为 None 时 `_next_msg` 保持现状(直接 `next(self._resp_iter)`),现有用例行为不变。不为 None 时(须为有限正数,否则 `ValueError`):构造时起一个 daemon 接收线程,把 `self._resp_iter` 的每条消息放进 `self._recv_q`;迭代正常结束放一个结束哨兵,抛异常就把异常对象放进去。`_next_msg` 改为 `self._recv_q.get(timeout=message_timeout_s)`:
- 取到消息:原样返回;
- 取到结束哨兵:走现在的 `stream_ended_without_end_session` 分支;取到异常:走现在的 `client_transport_error` 分支并重抛;
- `queue.Empty`:若 `self._resp_iter` 有 `cancel` 方法就调它(真 gRPC 流;让接收线程尽快退出),然后返回 `pb2.ServerMessage(end_session=pb2.EndSession(reason="timeout", failure_detail=f"no server message within {timeout}s"))`。调用方 `_pump_one` 与 `create_session` 看到的是一条普通 EndSession,经 `_finish` 置 `closed`、记原因、往请求队列放 None 释放请求流。

合成一条 EndSession 而不是抛异常,是为了让既有调用方把它当成服务端报的 `timeout`(T1),而 `_classify_client_error` 会把 `RuntimeError` 判成不可重跑的 `client_protocol_error`。类 docstring 的 T1 列表补一句"客户端相邻消息等待超时也报 timeout"。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_client_transport.py tests/test_realtime_client.py tests/test_realtime_client_order.py tests/test_realtime_client_abort.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/client.py tests/test_realtime_client_transport.py
git commit -m "feat(simulator): 客户端按相邻消息间隔限时,超时按 timeout 收尾"
```

---

### Task 11: 驱动器的 `role_memory_unavailable` 与 `role_memory_failure`,并登记进各处终止原因集合

**Files:**
- Modify: `src/simulator/realtime/driver.py`(导入与 `_run` 的 except 链,第 12-14 行与第 147-163 行;`DriverEvent` docstring 第 22-23 行)
- Modify: `src/simulator/realtime/proto/realtime_env.proto`(`EndSession.reason` 注释,第 82-88 行;只改注释)
- Modify: `src/simulator/realtime/client.py`(类 docstring 的 T1/T2 段,第 50-55 行)
- Modify: `src/mindtrainer/trajectory_source.py`(`_TRANSIENT_REASONS`,第 68-75 行,及第 141-144 行 docstring)
- Modify: `src/mindagent/deployment.py`(`TRANSIENT_REASONS`,第 23-25 行,及第 137-138 行 docstring)
- Modify: `src/mindtrainer/orchestrator.py`(`_DETERMINISTIC_ROLLOUT_REASONS`,第 43-46 行及注释)
- Modify: `src/factory/interact_policy.py`(只改第 29 行与第 340 行注释里列举的确定性原因)
- Test: `tests/test_realtime_driver.py`、`tests/test_mt_trajectory_interact.py`、`tests/test_ma_deployment.py`、`tests/test_mt_orchestrator.py`(`tests/test_interact_parity.py` 不改,它钉住两份瞬态集合相等)

**Interfaces:**
- Consumes: Task 8 的 `RoleMemoryUnavailableError`、`RoleMemoryFailureError`
- Produces: 终止原因字符串 `"role_memory_unavailable"`(T1,在 `mindtrainer.trajectory_source._TRANSIENT_REASONS` 与 `mindagent.deployment.TRANSIENT_REASONS`)与 `"role_memory_failure"`(T2,在 `mindtrainer.orchestrator._DETERMINISTIC_ROLLOUT_REASONS`)。第 3 步的转换后端抛这两个基类的子类,驱动器按 `isinstance` 分支,子类自然落进对应原因。

本任务只登记字符串。创建被拒(`SessionRejectedError`)按原因查瞬态集合是第 3 步(轨迹源)与第 5 步(部署)的事;编排器 `_DETERMINISTIC` 元组加 `RoleMemoryFailureError` 也在第 3 步。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_driver.py
from simulator.realtime.backend_timeout import RoleMemoryFailureError, RoleMemoryUnavailableError


class _TranslatedUnavailable(RoleMemoryUnavailableError):
    """模拟第 3 步组合根的子类:驱动器只认 simulator 的基类。"""


@pytest.mark.parametrize("error,reason", [
    (RoleMemoryUnavailableError("embedding endpoint down"), "role_memory_unavailable"),
    (_TranslatedUnavailable("embedding endpoint down"), "role_memory_unavailable"),
    (RoleMemoryFailureError("store is corrupt"), "role_memory_failure"),
])
def test_driver_maps_memory_errors_to_their_own_reasons(error, reason):
    class MemoryBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise error

    scenario, assignment, personas = _fixture(max_turns=2)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": MemoryBackend()},
        client_driven_slot="a", episode_id="ep-mem", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("candidate turn 1", None)
    assert driver.poll_event(timeout_s=2.0).kind == "turn"
    ev = driver.poll_event(timeout_s=2.0)
    assert (ev.kind, ev.payload) == ("failed", reason)
    assert driver.state == SessionState.FAILED
```

(`tests/test_realtime_driver.py` 文件头补 `import pytest`。)

```python
# tests/test_mt_trajectory_interact.py:在 test_a_transient_failure_is_retried_and_recorded_and_the_half_episode_is_not_kept
# 的 parametrize 里加一项 ({"end_reason": "role_memory_unavailable"}, "role_memory_unavailable"),ids 加 "role_memory_unavailable";
# 在 test_non_transient_failures_are_not_retried 里加 ({"end_reason": "role_memory_failure"}, "role_memory_failure"),ids 加 "memory_failure"。

# tests/test_ma_deployment.py:test_a_transient_failure_is_rerun_whole 加 ({"end_reason": "role_memory_unavailable"}, "role_memory_unavailable");
# test_non_transient_failures_are_not_rerun 加 ({"end_reason": "role_memory_failure"}, "role_memory_failure")。

# tests/test_mt_orchestrator.py:test_a_deterministic_rejection_fails_the_run_and_is_never_retried 的
# @pytest.mark.parametrize("reason", [...]) 加 "role_memory_failure"。
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_driver.py tests/test_mt_trajectory_interact.py tests/test_ma_deployment.py tests/test_mt_orchestrator.py -q -p no:cacheprovider -k "memory or transient or deterministic"`
Expected: FAIL,驱动器用例得到 `("failed", "role_backend_failure")`;轨迹源与部署的 `role_memory_unavailable` 用例未重跑(`RolloutFailedError` / `DeploymentFailedError`);编排器的 `role_memory_failure` 用例训练记录仍是 `pending`

- [ ] **Step 3: 实现**

`driver.py` 在 `except RoleBackendRejectedError` 之后、`except Exception` 之前加两个分支,形状与 `RoleBackendUnavailableError` 分支相同(`assert_transition` 到 `FAILED`,`outbox` 放 `DriverEvent(kind="failed", payload=...)`),注释写明 T1/T2 与"`simulator` 不认 `actio.MemoryFailureError`,组合根改抛这两个基类的子类"。`DriverEvent` 的 docstring 列举补上两个原因。

proto 的 `EndSession.reason` 注释补两行:`"role_memory_unavailable"`(对手位记忆暂时不可用;瞬态,T1)与 `"role_memory_failure"`(对手位记忆确定性失败;T2)。改完跑 `bash scripts/gen_realtime_proto.sh`,`git diff --quiet src/simulator/realtime/proto/*.py src/simulator/realtime/proto/*.pyi` 应为 0(注释不进描述符);不为 0 就把产物一并提交。

两份瞬态集合各加 `"role_memory_unavailable"`,同一次提交里改,`test_interact_parity.py` 保证不漂移;`_DETERMINISTIC_ROLLOUT_REASONS` 加 `"role_memory_failure"`(`factory.interact_policy` 共用这个集合,第 339 行的放行自动覆盖它)。三处注释与 docstring 把新原因并进列举。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_driver.py tests/test_mt_trajectory_interact.py tests/test_ma_deployment.py tests/test_mt_orchestrator.py tests/test_interact_parity.py tests/test_factory_interact_wire.py tests/test_realtime_servicer_limits.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/driver.py src/simulator/realtime/proto/realtime_env.proto src/simulator/realtime/client.py
git add src/mindtrainer/trajectory_source.py src/mindagent/deployment.py src/mindtrainer/orchestrator.py src/factory/interact_policy.py
git add tests/test_realtime_driver.py tests/test_mt_trajectory_interact.py tests/test_ma_deployment.py tests/test_mt_orchestrator.py
git commit -m "feat(simulator): 驱动器分出两个记忆终止原因,并登记进瞬态与确定性集合"
```

---

### Task 12: 后端工厂收 `SessionInfo`、落盘钩子收 `session_id`,服务端采用 `episode_id`

**Files:**
- Modify: `src/simulator/realtime/servicer.py`(`BackendsFactory` 第 27-29 行;`__init__` 的 `on_session_closed`;`_handle_create` 第 218-299 行;`_drain_ready_events` 第 301 行起,钩子调用在第 327-329 行)
- Modify: `src/simulator/realtime/validation.py`(新增 `validate_episode_id`)
- Modify: `src/factory/agent.py`(`AgentRuntime._backends` 第 200-224 行、`_on_session_closed` 第 171-189 行;只改签名形状)
- Create: `tests/realtime_fixtures.py`(测试辅助,不以 `test_` 开头,pytest 不收集;与 `tests/rs_fixtures.py` 同一用法)
- Create: `tests/test_realtime_session_resources.py`(本任务与 Task 13、Task 14 的 loopback 用例都放这里)
- Modify(签名随改,见 Step 3 末尾的清单): `tests/test_realtime_activation_trace.py`、`tests/test_ma_e2e.py`、`tests/test_mt_trajectory_interact.py`、`tests/test_ma_deployment.py`、`tests/test_realtime_persistence.py`、`tests/test_realtime_client_transport.py`、`tests/test_realtime_servicer_limits.py`、`tests/test_realtime_client.py`、`tests/test_realtime_client_order.py`、`tests/test_realtime_client_abort.py`、`tests/test_model_ref_and_isolation_e2e.py`、`tests/test_mt_dispatch.py`、`tests/test_realtime_servicer_e2e.py`、`tests/test_mt_orchestrator.py`、`tests/test_actio_realtime_e2e.py`、`tests/test_mt_dispatch_synthesis.py`、`tests/test_factory_handlers.py`、`tests/test_factory_interact_e2e.py`

**Interfaces:**
- Consumes: Task 8 的 `SessionInfo`、`SessionResources`、`TimeoutBackend(inner, timeout_s, resources)`;Task 9 的 `CreateSession.episode_id` 与 `create_session(episode_id=)`
- Produces:
  - `simulator.realtime.servicer.BackendsFactory = Callable[[SessionInfo], SessionResources]`
  - `simulator.realtime.servicer.SessionClosedHook = Callable[[str, Episode], None]`;`EnvironmentServicer(..., on_session_closed: SessionClosedHook | None = None)`、`create_server(..., on_session_closed=...)` 以 `(session_id, episode)` 调钩子。
  - `simulator.realtime.validation.validate_episode_id(episode_id: str) -> str | None`(合格返回 None,否则返回原因)。
  - `AgentRuntime._backends(self, info: SessionInfo) -> SessionResources`、`AgentRuntime._on_session_closed(self, session_id: str, episode: Episode) -> None`。
  - `tests/realtime_fixtures.py`:
    - `plain_factory(fn: Callable[[Scenario, RoleAssignment, dict[str, Persona], str], dict[str, ModelBackend]]) -> BackendsFactory`:把旧形状的工厂包成新签名,返回 `SessionResources(backends=fn(info.scenario, info.assignment, info.personas, info.client_slot))`。
    - `plain_hook(fn: Callable[[Episode], object]) -> SessionClosedHook`:丢掉 `session_id`,把 episode 交给 `fn`(`closed.append` 这类单参钩子直接包)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_session_resources.py
import shutil
import threading
import time

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.resources import SessionInfo, SessionResources
from simulator.realtime.server import create_server

PERSONAS = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _assignment(max_turns=2):
    return RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"),
                                 RoleBinding("b", "pb", "frozen_reference")], max_turns)


@pytest.fixture
def serve():
    servers, channels = [], []

    def _serve(factory, **kwargs):
        kwargs.setdefault("turn_timeout_s", 2.0)
        server = create_server(factory, port=0, **kwargs)
        server.start()
        servers.append(server)

        def connect():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
        return server, connect

    yield _serve
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


def _open(client, *, episode_id=None, max_turns=2):
    return client.create_session(scenario_id="s1", role_assignment=_assignment(max_turns), personas=PERSONAS,
                                 purpose="training", client_driven_slot="a", episode_id=episode_id)


def _play(client):
    while not client.closed:
        client.submit_turn("嗯")
    return client.drain_until_end()


def _scripted(info):
    return SessionResources(backends={"b": ScriptedBackend({"b": ["一", "二"]})})


def _eventually(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_factory_gets_a_session_info_and_the_hook_gets_the_session_id(serve):
    infos, closed = [], []

    def factory(info):
        infos.append(info)
        return _scripted(info)

    _, connect = serve(factory, on_session_closed=lambda sid, ep: closed.append((sid, ep.id)))
    client = connect()
    session_id = _open(client, episode_id="run-1-grpo_opd-s1-r0")
    episode = _play(client)
    (info,) = infos
    assert isinstance(info, SessionInfo)
    assert (info.session_id, info.episode_id, info.client_slot) == (session_id, "run-1-grpo_opd-s1-r0", "a")
    assert info.scenario.id == "s1" and set(info.personas) == {"pa", "pb"}
    assert episode.id == "run-1-grpo_opd-s1-r0"
    assert closed == [(session_id, "run-1-grpo_opd-s1-r0")]


def test_without_an_episode_id_the_server_keeps_ep_session_id(serve):
    infos = []
    _, connect = serve(lambda info: infos.append(info) or _scripted(info))
    client = connect()
    session_id = _open(client)
    assert _play(client).id == f"ep-{session_id}"
    assert infos[0].episode_id == f"ep-{session_id}"


@pytest.mark.parametrize("bad", ["a/b", ".", "..", "x" * 129, "ep\n", "回合-1", "a b"])
def test_a_malformed_episode_id_is_invalid_session_and_the_factory_is_never_called(serve, bad):
    calls = []
    _, connect = serve(lambda info: calls.append(info) or _scripted(info))
    with pytest.raises(SessionRejectedError) as info:
        _open(connect(), episode_id=bad)
    assert info.value.reason == "invalid_session"
    assert calls == []


@pytest.mark.parametrize("good", ["x" * 128, "a.b", "deploy-zengen-v1-s1-1", "_-.", "..."])
def test_a_well_formed_episode_id_names_the_episode(serve, good):
    _, connect = serve(_scripted)
    client = connect()
    _open(client, episode_id=good)
    assert _play(client).id == good
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_session_resources.py -q -p no:cacheprovider`
Expected: FAIL,服务端仍按四个位置参数调工厂,`TypeError` 被当成工厂故障,客户端得到 `SessionRejectedError("role_backend_failure")`;非法 id 用例里工厂被调用

- [ ] **Step 3: 实现**

`validation.py`:`_EPISODE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")`,`validate_episode_id` 用 `fullmatch`(不要用 `match` 加 `$`:`$` 放行末尾的 `\n`),再排除 `"."` 与 `".."`。字符集只含 ASCII,字符数就是字节数。

`servicer._handle_create`:
1. 现有结构校验之后、解析情境之前:`requested = create_session.episode_id`;非空时 `validate_episode_id(requested)` 不过就 `_invalid_session(f"invalid episode_id: {reason}")`。`episode_id = requested or f"ep-{session_id}"`。
2. 调工厂:`resources = self._backends_factory(SessionInfo(scenario, role_assignment, personas, client_slot, session_id, episode_id))`,异常分支与现在相同(`_server_fault`)。
3. 缺后端检查改查 `resources.backends`;包期限改为 `TimeoutBackend(backend, self._backend_timeout_s, resources)`(第 286-289 行原位)。
4. `SessionDriver(..., episode_id=episode_id, ...)`。
5. 返回值改为 `(driver, resources, msg)`;`Interact` 第 164 行随之解包,把 `resources` 留在局部变量里(收尾在 Task 13,本任务不调 `quiesce`/`join`/`close`)。

`_drain_ready_events(self, driver, session_id)`,钩子改调 `self._on_session_closed(session_id, episode)`。类 docstring 的"调用方约定"一段把工厂与钩子的新形状写进去(工厂每次返回全新的 `SessionResources`,后端不跨会话共享)。

`factory/agent.py`:`_backends(self, info)` 从 `info.scenario`、`info.assignment`、`info.client_slot` 取原来的三个参数,函数体不变,最后 `return SessionResources(backends=backends)`;`_on_session_closed(self, session_id, episode)` 本任务仍按 `episode.scenario_id` 查 `_sessions`(按 `session_id` 路由在 Task 15)。

既有测试的改写(只改签名,不改断言):
- 下面每个文件 `from realtime_fixtures import plain_factory, plain_hook`,把传给 `create_server` 的旧形状工厂包一层 `plain_factory(...)`,`on_session_closed=` 的单参钩子包一层 `plain_hook(...)`:`test_realtime_activation_trace.py:109`、`test_ma_e2e.py:76`、`test_mt_trajectory_interact.py:95`、`test_ma_deployment.py:80`、`test_realtime_persistence.py:28`、`test_realtime_client.py:45`、`test_realtime_client_order.py:44`、`test_realtime_client_abort.py:36`、`test_model_ref_and_isolation_e2e.py:81`、`test_mt_dispatch.py:458`、`test_realtime_servicer_e2e.py:23,101,137,201,263`、`test_mt_orchestrator.py:470`、`test_actio_realtime_e2e.py:64`、`test_mt_dispatch_synthesis.py:453`。
- `test_realtime_servicer_limits.py` 与 `test_realtime_client_transport.py` 各在自己的夹具里包一次(`start_server` 第 71 行、`serve` 第 119 行:`create_server(plain_factory(factory), ...)`),两处 `lambda *_: {...}` 工厂不用动;`test_realtime_servicer_limits.py:175` 的钩子改为 `lambda _sid, ep: closed.append(ep.id)`。
- `test_factory_handlers.py`:加一个本地辅助 `_backends_of(runtime, scenario, assignment, personas, client_slot)`,返回 `runtime._backends(SessionInfo(scenario, assignment, personas, client_slot, "sess-test", "ep-sess-test")).backends`,第 238-368 行直接调 `runtime._backends(...)` 的十处换成它;第 562 行改为 `runtime._on_session_closed(f"sess-{i}", SimpleNamespace(...))`。
- `test_factory_interact_e2e.py:95` 的替身钩子改为 `def hook(self, session_id, episode)`,转手 `real_hook(self, session_id, episode)`。

改完用 `grep -rn "def .*(scenario, assignment, personas, client" tests` 与 `grep -rn "on_session_closed=" tests` 复核:剩下的每一处要么在 `plain_factory`/`plain_hook` 里,要么已是新签名。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_session_resources.py tests/test_realtime_servicer_limits.py tests/test_realtime_servicer_e2e.py tests/test_realtime_client.py tests/test_realtime_client_order.py tests/test_realtime_client_abort.py tests/test_realtime_client_transport.py tests/test_realtime_persistence.py tests/test_realtime_activation_trace.py -q -p no:cacheprovider`
Expected: PASS

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_e2e.py tests/test_ma_deployment.py tests/test_mt_trajectory_interact.py tests/test_mt_orchestrator.py tests/test_mt_dispatch.py tests/test_mt_dispatch_synthesis.py tests/test_model_ref_and_isolation_e2e.py tests/test_actio_realtime_e2e.py tests/test_factory_handlers.py tests/test_factory_interact_e2e.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/servicer.py src/simulator/realtime/validation.py src/factory/agent.py
git add tests/realtime_fixtures.py tests/test_realtime_session_resources.py
git add tests/test_realtime_activation_trace.py tests/test_ma_e2e.py tests/test_mt_trajectory_interact.py tests/test_ma_deployment.py
git add tests/test_realtime_persistence.py tests/test_realtime_client_transport.py tests/test_realtime_servicer_limits.py
git add tests/test_realtime_client.py tests/test_realtime_client_order.py tests/test_realtime_client_abort.py
git add tests/test_model_ref_and_isolation_e2e.py tests/test_mt_dispatch.py tests/test_realtime_servicer_e2e.py
git add tests/test_mt_orchestrator.py tests/test_actio_realtime_e2e.py tests/test_mt_dispatch_synthesis.py
git add tests/test_factory_handlers.py tests/test_factory_interact_e2e.py
git commit -m "refactor(simulator): 后端工厂收 SessionInfo 返回 SessionResources,落盘钩子带 session_id"
```

---

### Task 13: 终态收尾 quiesce → join → close,join 超时进滞留表

**Files:**
- Modify: `src/simulator/realtime/servicer.py`(`__init__`、`Interact` 的 `finally` 第 214-216 行、`_handle_create` 工厂返回之后的失败分支)
- Modify: `src/simulator/realtime/server.py`(`create_server` 透传两个新参数;返回前挂 `server.servicer = servicer`,与 `server.bound_port` 同一写法)
- Test: `tests/test_realtime_session_resources.py`

**Interfaces:**
- Consumes: Task 8 的 `SessionResources.quiesce/join/close`;Task 12 的 `_handle_create` 返回 `(driver, resources, msg)`
- Produces:
  - `EnvironmentServicer(..., resource_join_timeout_s: float = 60.0, linger_retry_s: float = 60.0)`;`create_server(..., resource_join_timeout_s: float = 60.0, linger_retry_s: float = 60.0)` 原样透传。默认值取自设计源 6.2 节(`join(timeout_s=60)`、每 60 秒 `join(0)`);参数只为测试能用小值。
  - `EnvironmentServicer.lingering_count() -> int`(持锁读滞留表长度);`grpc.Server.servicer` 属性指向 servicer。
  - Task 14 的准入读 `self._lingering`(与 `self._lock` 同一把锁)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_session_resources.py(续)
class _Recording(SessionResources):
    def __init__(self, events, copy_dir, backends):
        super().__init__(backends=backends, closers=(lambda: shutil.rmtree(copy_dir),))
        self.events = events

    def quiesce(self):
        self.events.append("quiesce")
        super().quiesce()

    def join(self, timeout_s):
        ok = super().join(timeout_s)
        self.events.append(("join", ok))
        return ok

    def close(self):
        self.events.append("close")
        super().close()


def _recording_factory(tmp_path, events, backend_for=lambda: ScriptedBackend({"b": ["一", "二", "三"]})):
    def factory(info):
        copy_dir = tmp_path / info.session_id
        copy_dir.mkdir()
        return _Recording(events, copy_dir, {"b": backend_for()})
    return factory


@pytest.mark.parametrize("ending", ["max_turns", "turn_timeout", "client_abort"])
def test_every_terminal_state_quiesces_joins_and_closes_and_the_copy_is_removed(serve, tmp_path, ending):
    events = []
    _, connect = serve(_recording_factory(tmp_path, events), turn_timeout_s=0.3)
    client = connect()
    session_id = _open(client, max_turns=4)
    if ending == "max_turns":
        _play(client)
    elif ending == "turn_timeout":
        assert client.drain_until_end().terminated_reason == "timeout"
    else:
        client.submit_turn("嗯")
        client.abort()
    assert _eventually(lambda: "close" in events)
    assert events == ["quiesce", ("join", True), "close"]
    assert not (tmp_path / session_id).exists()


def test_a_failure_after_the_factory_returned_still_closes_the_resources(serve, tmp_path):
    events = []

    def incomplete(info):
        copy_dir = tmp_path / info.session_id
        copy_dir.mkdir()
        return _Recording(events, copy_dir, {})          # 漏了 b 位

    _, connect = serve(incomplete)
    with pytest.raises(SessionRejectedError) as info:
        _open(connect())
    assert info.value.reason == "role_backend_failure"
    assert _eventually(lambda: "close" in events)
    assert events == ["quiesce", ("join", True), "close"]
    assert list(tmp_path.iterdir()) == []


class _Stuck:
    def __init__(self):
        self.release = threading.Event()

    def respond(self, persona, scenario, history, speaker_slot):
        self.release.wait(10)
        return "late", None


def test_a_join_timeout_keeps_the_copy_and_lingers_until_a_retry_succeeds(serve, tmp_path):
    events, stuck = [], _Stuck()
    server, connect = serve(_recording_factory(tmp_path, events, backend_for=lambda: stuck),
                            backend_timeout_s=0.1, resource_join_timeout_s=0.2, linger_retry_s=0.05)
    client = connect()
    session_id = _open(client)
    client.submit_turn("嗯")
    assert client.drain_until_end().terminated_reason == "role_backend_timeout"
    assert _eventually(lambda: server.servicer.lingering_count() == 1)
    assert "close" not in events and (tmp_path / session_id).exists()      # 不关、不删
    stuck.release.set()
    assert _eventually(lambda: server.servicer.lingering_count() == 0)
    assert events[-1] == "close" and not (tmp_path / session_id).exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_session_resources.py -q -p no:cacheprovider -k "terminal or after_the_factory or join_timeout"`
Expected: FAIL,`events == []`(没人调 quiesce/close);滞留用例 `TypeError: create_server() got an unexpected keyword argument 'resource_join_timeout_s'`

- [ ] **Step 3: 实现**

servicer 新增状态,都由 `self._lock = threading.Lock()` 守护:`self._lingering: dict[str, SessionResources]`(session_id → 资源)与 `self._reaper: threading.Thread | None`。

`Interact` 在 `try` 之前把 `session_id`、`resources` 置为 None(与 `driver` 同处),`finally`:现有的 `request_abort` 兜底之后,`resources` 不为 None 时调 `self._wind_down(session_id, resources)`。`_handle_create` 里工厂返回之后的失败(缺后端;`SessionDriver(...)` 或 `driver.start()` 抛)不再自己收尾,只返回 `(None, resources, fault_msg)`,由同一个 `finally` 收尾;`driver.start()` 抛异常时返回 `_server_fault`。工厂自己抛异常时 `resources` 是 None,不收尾(设计源 6.2 节:由工厂保证不留半开的库)。

`_wind_down(session_id, resources)`:
1. `resources.quiesce()`;
2. `ok = resources.join(self._resource_join_timeout_s)`;
3. `ok` 为真:`resources.close()`;为假:持锁把它放进 `self._lingering[session_id]`,`logger.warning("session %s resources did not quiesce within %ss; lingering", ...)`,没有活着的回收线程就起一个(daemon,名字 `"session-reaper"`)。

回收线程循环:`time.sleep(self._linger_retry_s)`;持锁拷一份条目;锁外对每条 `join(0)`,为真的锁外 `close()`,再持锁从表里删掉(先关再删,名额在副本删完后才释放);仍有滞留时 `logger.warning("%d session resources still lingering", n)`(设计源 6.3 节"日志每分钟报一次滞留数",间隔就是 `linger_retry_s`);表空了线程退出,下次有新滞留再起。

`join` 超时之外,`quiesce`、`close` 抛异常只记日志(类名),不让 `finally` 抛出 gRPC 线程。join 在 RPC 的 worker 线程里同步进行:客户端在这之前已经收到 EndSession,不受影响;这段时间该会话仍占 gRPC 的并发名额,与设计源 6.3 节的口径一致。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_session_resources.py tests/test_realtime_servicer_limits.py tests/test_realtime_servicer_e2e.py tests/test_realtime_client_abort.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/servicer.py src/simulator/realtime/server.py tests/test_realtime_session_resources.py
git commit -m "feat(simulator): 会话终态先停新调用、等在途、再关资源,等不到的进滞留表定期重试"
```

---

### Task 14: 准入——一把锁里查名额与同 `episode_id` 活动会话

**Files:**
- Modify: `src/simulator/realtime/servicer.py`(`__init__` 加 `max_workers`;`_handle_create` 在调工厂之前;`_wind_down`)
- Modify: `src/simulator/realtime/server.py`(`create_server` 把已有的 `max_workers` 同时传给 servicer)
- Test: `tests/test_realtime_session_resources.py`

**Interfaces:**
- Consumes: Task 12 的 `episode_id` 取值;Task 13 的 `self._lock`、`self._lingering`、`_wind_down`
- Produces: `EnvironmentServicer(..., max_workers: int = 16)`;拒绝消息 `EndSession(reason="role_backend_unavailable", episode_id="", failure_detail=...)`,客户端得到 `SessionRejectedError("role_backend_unavailable", ...)`。第 3 步(轨迹源)与第 5 步(部署)把创建被拒改为按原因查瞬态集合后,这条拒绝才会按条重跑。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_realtime_session_resources.py(续)
def _try_open(connect, **kw):
    client = connect()
    try:
        _open(client, **kw)
    except SessionRejectedError as exc:
        return exc.reason, client
    except grpc.RpcError as exc:
        # 上一条 RPC 刚收尾、gRPC 的并发计数还没放开时会得到 RESOURCE_EXHAUSTED;与准入拒绝分开报
        return f"rpc_{exc.code().name.lower()}", client
    return "ready", client


def test_a_second_session_with_an_active_episode_id_is_unavailable_until_the_first_winds_down(serve):
    _, connect = serve(_scripted)
    first = connect()
    _open(first, episode_id="deploy-zengen-v1-s1-1", max_turns=4)
    reason, _ = _try_open(connect, episode_id="deploy-zengen-v1-s1-1")
    assert reason == "role_backend_unavailable"
    assert _try_open(connect, episode_id="deploy-zengen-v1-s1-2")[0] == "ready"   # 别的 id 不受影响
    first.abort()
    assert _eventually(lambda: _try_open(connect, episode_id="deploy-zengen-v1-s1-1")[0] == "ready")


def test_lingering_resources_hold_a_slot_until_their_thread_exits(serve, tmp_path):
    events, stuck = [], _Stuck()
    server, connect = serve(_recording_factory(tmp_path, events, backend_for=lambda: stuck), max_workers=1,
                            backend_timeout_s=0.1, resource_join_timeout_s=0.2, linger_retry_s=0.05)
    client = connect()
    _open(client)
    client.submit_turn("嗯")
    client.drain_until_end()
    assert _eventually(lambda: server.servicer.lingering_count() == 1)
    assert _eventually(lambda: _try_open(connect)[0] == "role_backend_unavailable")
    stuck.release.set()
    assert _eventually(lambda: server.servicer.lingering_count() == 0)
    assert _eventually(lambda: _try_open(connect)[0] == "ready")


def test_concurrent_creates_with_one_episode_id_admit_exactly_one(serve):
    gate = threading.Event()

    def slow(info):
        gate.wait(0.5)                     # 工厂在锁外慢跑:登记必须先于它,否则两条都会通过检查
        return _scripted(info)

    _, connect = serve(slow)
    results = []
    workers = [threading.Thread(target=lambda: results.append(_try_open(connect, episode_id="same")))
               for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(5)
    assert sorted(r for r, _ in results) == ["ready", "role_backend_unavailable"]
    for _, client in results:
        client.abort()


def test_concurrent_creates_do_not_exceed_the_cap_with_a_lingering_session(serve, tmp_path):
    stuck, gate, calls = _Stuck(), threading.Event(), []

    def factory(info):
        calls.append(info.session_id)
        if len(calls) == 1:
            return SessionResources(backends={"b": stuck})
        gate.wait(0.5)
        return _scripted(info)

    server, connect = serve(factory, max_workers=2, backend_timeout_s=0.1,
                            resource_join_timeout_s=0.2, linger_retry_s=0.05)
    first = connect()
    _open(first)
    first.submit_turn("嗯")
    first.drain_until_end()
    assert _eventually(lambda: server.servicer.lingering_count() == 1)
    results = []

    def _open_retrying():
        # 上一条 RPC 的 worker 退出 gRPC 并发计数要一点时间;只对 rpc_resource_exhausted 重试,最长 3 秒;固定 sleep 在慢机器上会偶发失败
        deadline = time.monotonic() + 3.0
        while True:
            reason, client = _try_open(connect)
            if reason != "rpc_resource_exhausted" or time.monotonic() > deadline:
                return reason, client
            client.abort()
            time.sleep(0.02)

    workers = [threading.Thread(target=lambda: results.append(_open_retrying())) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(5)
    assert sorted(r for r, _ in results) == ["ready", "role_backend_unavailable"]   # 1 活动 + 1 滞留 = 上限
    for _, client in results:
        client.abort()
    stuck.release.set()


def test_a_factory_that_raises_releases_its_admission(serve):
    calls = []

    def flaky(info):
        calls.append(info)
        if len(calls) == 1:
            raise OSError("first one fails")
        return _scripted(info)

    _, connect = serve(flaky, max_workers=1)
    assert _try_open(connect, episode_id="e1")[0] == "role_backend_failure"
    assert _eventually(lambda: _try_open(connect, episode_id="e1")[0] == "ready")   # 名额与 id 都已放回
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_session_resources.py -q -p no:cacheprovider -k "active_episode_id or hold_a_slot or concurrent or releases_its_admission"`
Expected: FAIL,第二个同 id 会话得到 `"ready"`;滞留时新会话得到 `"ready"`;`max_workers` 传给 servicer 时 `TypeError`

- [ ] **Step 3: 实现**

servicer 加 `self._active: dict[str, str]`(session_id → episode_id),与 `_lingering` 同由 `self._lock` 守护。`_handle_create` 在解析情境之后、调工厂之前:

```python
with self._lock:
    if len(self._active) + len(self._lingering) >= self._max_workers:
        return None, None, self._unavailable(
            f"{len(self._active)} active and {len(self._lingering)} lingering sessions reach the cap {self._max_workers}")
    if episode_id in self._active.values():
        return None, None, self._unavailable(f"episode {episode_id!r} already has an active session")
    self._active[session_id] = episode_id
```

`_unavailable(detail)` 与 `_server_fault` 同形,原因换成 `role_backend_unavailable`。工厂抛异常时在同一把锁里 `self._active.pop(session_id, None)` 后返回 `_server_fault`。`_wind_down`:`close()` 成功之后持锁 `pop`;join 超时时在**同一次**持锁里从 `_active` 删、放进 `_lingering`,名额计数不出现空档。拒绝的会话没有资源,`finally` 不收尾。

校验顺序:结构校验与 `episode_id` 格式(`invalid_session`,T2)在前,准入(`role_backend_unavailable`,T1)在后,所以格式错的请求不会被当成可重跑。`max_workers` 与 gRPC 的 `maximum_concurrent_rpcs` 同值:没有滞留时 gRPC 先挡住第 N+1 条流(既有用例 `test_concurrent_rpc_limit_rejects_extra_session` 不变),servicer 这道检查在有滞留时才起作用。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_realtime_session_resources.py tests/test_realtime_servicer_limits.py tests/test_realtime_servicer_e2e.py tests/test_realtime_client_transport.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/servicer.py src/simulator/realtime/server.py tests/test_realtime_session_resources.py
git commit -m "feat(simulator): 建会话前在一把锁里查名额与同 episode 的活动会话"
```

---

### Task 15: `AgentRuntime` 按 `session_id` 路由落盘

**Files:**
- Modify: `src/factory/agent.py`(`AgentRuntime.__init__` 第 137-139 行、`_backends`、`_on_session_closed`、类 docstring 第 112-122 行)
- Test: `tests/test_factory_handlers.py`

**Interfaces:**
- Consumes: Task 12 的 `AgentRuntime._backends(info) -> SessionResources`、`_on_session_closed(session_id, episode)`;Task 8 的 `SessionResources(closers=...)`
- Produces: `AgentRuntime._session_runs: dict[str, str]`(session_id → run id,私有)。工厂造资源时,情境 id 已登记就记一条,并把"删这条"放进 `closers`;落盘钩子只查这张表。`register_session`/`unregister_session` 的签名与语义不变(仍是情境 id → run id,只用于判定新会话归属,Ruling 24 的检查不动)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_handlers.py
def test_a_session_started_before_unregistering_is_still_written_to_its_run(store):
    """落盘按 session_id 路由:策略上下文注销情境时,已经在跑的会话照样落到自己的 run(设计源 6.1 节)。"""
    from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
    from simulator.store_adapter import write_scenario
    write_scenario(store, Scenario("s-int", "bg", [RoleSlot("b", "b"), RoleSlot("a", "a")], "c", ["n"], "training"))
    runtime = AgentRuntime(store, ("hi",))
    try:
        runtime.register_session("s-int", "run-int")
        client = runtime.client_factory()
        client.create_session(
            scenario_id="s-int", personas={pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")},
            role_assignment=RoleAssignment("s-int", [RoleBinding("a", "pa", "self_play"),
                                                     RoleBinding("b", "pb", "frozen_reference")], 2),
            purpose="training", client_driven_slot="a")
        runtime.unregister_session("s-int")                  # 会话已经开了
        while not client.closed:
            client.submit_turn("嗯")
        episode = client.drain_until_end()
        assert _stored_episode_ids(store, "run-int") == [episode.id]
    finally:
        runtime.close()


def test_the_session_route_is_dropped_when_the_resources_close(store):
    from simulator.realtime.resources import SessionInfo
    runtime = AgentRuntime(store, ("hi",))
    runtime.register_session("s1", "run-1")
    resources = runtime._backends(SessionInfo(_scenario_with(), _assignment(*_two_bindings()), {}, "a",
                                              "sess-route", "ep-sess-route"))
    assert runtime._session_runs == {"sess-route": "run-1"}
    resources.close()
    assert runtime._session_runs == {}
```

(`_two_bindings()` 返回 `RoleBinding("a", "p", "self_play"), RoleBinding("b", "q", "frozen_reference")`;文件里已有同形的绑定写法时直接复用。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py -q -p no:cacheprovider -k "before_unregistering or session_route"`
Expected: FAIL,第一个用例 run 目录为空(钩子按情境 id 查,注销后查不到);第二个 `AttributeError: 'AgentRuntime' object has no attribute '_session_runs'`

- [ ] **Step 3: 实现**

`_backends(info)`:持锁读 `run_id = self._sessions.get(info.scenario.id)`(与现在判 `registered` 同一次持锁),不为 None 时同一次持锁写 `self._session_runs[info.session_id] = run_id`;造后端的循环不变;返回 `SessionResources(backends=backends, closers=(drop,) if run_id is not None else ())`,`drop` 持锁 `self._session_runs.pop(info.session_id, None)`。造后端抛异常时先 `drop()` 再重抛(工厂不留半开状态,设计源 6.2 节)。

`_on_session_closed(session_id, episode)`:持锁 `run_id = self._session_runs.get(session_id)`,其余不变。钩子在 servicer 的 `_drain_ready_events` 里、收尾 `close()` 之前调用(Task 13),所以路由在落盘时一定还在;join 超时滞留的会话,路由随资源留到真正关掉。

既有用例 `test_unregistering_stops_later_episodes_of_that_scenario_from_being_written` 照样成立:第二条会话开时情境已注销,工厂不记路由。Task 12 里 `test_factory_handlers.py:562` 那处直接调钩子的用例,改为先 `runtime._session_runs[f"sess-{i}"] = "run-int"` 再调(或在循环外为每个 i 记一条),断言不变。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_factory_interact_e2e.py tests/test_factory_interact_wire.py tests/test_ma_e2e.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/agent.py tests/test_factory_handlers.py
git commit -m "feat(factory): 会话落盘按 session_id 路由,注销情境不影响已在跑的会话"
```

---

## 第 3 步:训练路换成 Actio 实例

### Task 16: 记忆异常的转换后端 `factory/memory_translate.py`

**Files:**
- Create: `src/factory/memory_translate.py`
- Test: `tests/test_factory_memory_translate.py`(新)

**Interfaces:**
- Consumes:
  - 第 1 步:`actio.memory_port.MemoryFailureError(message: str, *, transient: bool)` 及子类 `MemoryEventConflict`、`MemoryClosedError`(`transient` 恒为假);`actio.memory.TupleMemory(entries=())`;`actio.instance.instantiate(model_backend, *, skills, library_version, profile, memory=..., episode_id=..., interlocutors=...)`,`ActioInstance.respond(...) -> tuple[str, str | None, dict]`,`ActioInstance.close() -> None`。
  - 第 2 步:`simulator.realtime.backend_timeout.RoleMemoryUnavailableError(Exception)`、`RoleMemoryFailureError(Exception)`。
- Produces:
  - `class FactoryMemoryUnavailableError(RoleMemoryUnavailableError)`、`class FactoryMemoryFailureError(RoleMemoryFailureError)`。
  - `class MemoryTranslatingBackend`:`__init__(self, instance: ActioInstance)`;属性 `instance`;`respond(persona, scenario, history, speaker_slot) -> tuple[str, str | None, dict]`;`close() -> None`。
  - `ROLE_LIBRARY_VERSION = {"skill": "0", "memory": "0"}`(训练路的角色位不读技能库,与 `mindagent.library` 空库同一写法)。
  - `memory_role_backend(inner: object, *, memory: MemoryBackend, episode_id: str, interlocutors: Mapping[str, str]) -> MemoryTranslatingBackend`:`instantiate(inner, skills=(), library_version=ROLE_LIBRARY_VERSION, profile=None, memory=memory, episode_id=episode_id, interlocutors=dict(interlocutors))` 外套转换后端。Task 17(对手位)与 Task 21(训练位)都只经它造实例。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_memory_translate.py
from __future__ import annotations

import pytest

from actio.memory import TupleMemory
from actio.memory_port import MemoryClosedError, MemoryEventConflict, MemoryFailureError
from factory.memory_translate import (
    FactoryMemoryFailureError, FactoryMemoryUnavailableError, MemoryTranslatingBackend, memory_role_backend,
)
from factory.opponents import OpponentApiError
from simulator.backends import ScriptedBackend
from simulator.realtime.backend_timeout import RoleMemoryFailureError, RoleMemoryUnavailableError


class _Instance:
    """假的 Actio 实例:respond 抛给定异常或返回三元组;close 记次数,可抛。"""
    def __init__(self, raises=None, close_raises=None):
        self.raises, self.close_raises, self.closed = raises, close_raises, 0

    def respond(self, persona, scenario, history, speaker_slot):
        if self.raises is not None:
            raise self.raises
        return "u", None, {"schema": "actio.trace.v2"}

    def close(self):
        self.closed += 1
        if self.close_raises is not None:
            raise self.close_raises


def test_a_transient_memory_failure_becomes_the_unavailable_error_and_names_only_the_class():
    cause = MemoryFailureError("https://embed.example/v1 timed out", transient=True)
    with pytest.raises(FactoryMemoryUnavailableError) as caught:
        MemoryTranslatingBackend(_Instance(raises=cause)).respond(None, None, [], "b")
    assert isinstance(caught.value, RoleMemoryUnavailableError)
    assert caught.value.__cause__ is cause
    assert "MemoryFailureError" in str(caught.value) and "'b'" in str(caught.value)
    assert "embed.example" not in str(caught.value)


@pytest.mark.parametrize("cause", [
    MemoryFailureError("disk", transient=False), MemoryEventConflict("k#0"), MemoryClosedError("closed"),
])
def test_every_other_memory_failure_becomes_the_failure_error(cause):
    with pytest.raises(FactoryMemoryFailureError) as caught:
        MemoryTranslatingBackend(_Instance(raises=cause)).respond(None, None, [], "b")
    assert isinstance(caught.value, RoleMemoryFailureError)
    assert type(cause).__name__ in str(caught.value)


@pytest.mark.parametrize("cause", [OpponentApiError("429"), ValueError("bad"), RoleMemoryUnavailableError("x")])
def test_non_memory_errors_pass_through_as_the_same_object(cause):
    with pytest.raises(type(cause)) as caught:
        MemoryTranslatingBackend(_Instance(raises=cause)).respond(None, None, [], "b")
    assert caught.value is cause


def test_the_reply_passes_through_and_close_goes_to_the_instance():
    inst = _Instance()
    backend = MemoryTranslatingBackend(inst)
    assert backend.respond(None, None, [], "b") == ("u", None, {"schema": "actio.trace.v2"})
    backend.close()
    assert inst.closed == 1 and backend.instance is inst


def test_a_memory_failure_while_closing_is_translated_too():
    inst = _Instance(close_raises=MemoryClosedError("closed"))
    with pytest.raises(FactoryMemoryFailureError):
        MemoryTranslatingBackend(inst).close()


def test_memory_role_backend_builds_an_actio_instance_that_writes_v2_traces():
    scripted = ScriptedBackend({"b": ["hi"]})
    backend = memory_role_backend(scripted, memory=TupleMemory(), episode_id="ep-x", interlocutors={"a": "pa"})
    utterance, _, trace = backend.respond(None, None, [], "b")
    assert utterance == "hi" and trace["schema"] == "actio.trace.v2"
    assert backend.instance.backend.inner is scripted       # 只有 respond 的底座被套了 IgnoreGuidance
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_translate.py -q -p no:cacheprovider`
Expected: FAIL,`ModuleNotFoundError: No module named 'factory.memory_translate'`

- [ ] **Step 3: 实现**

`src/factory/memory_translate.py`,首条语句 `from __future__ import annotations`。
- `respond` 只包一层 `try/except MemoryFailureError`:`exc.transient` 为真抛 `FactoryMemoryUnavailableError`,否则抛 `FactoryMemoryFailureError`,文字是 `f"memory backend failed for slot {speaker_slot!r}: {type(exc).__name__}"`,`from exc`(文字里不带原异常的文字,设计 10 节"只报异常类名")。只接 `MemoryFailureError` 一个基类,两个子类因此也被映射,不会落进驱动器的 `role_backend_failure`。
- `close()` 同样包一层,文字是 `f"memory backend failed while closing: {type(exc).__name__}"`:训练位副本关不上要按确定性处理(设计 7.2 节)。
- 不实现 `respond_guided`:转换后端是角色位最外层,服务端与轨迹源只调 `respond`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_translate.py tests/test_actio_instance.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_translate.py
git add tests/test_factory_memory_translate.py
git commit -m "feat(factory): 新增记忆异常转换后端,按 transient 改抛 simulator 的两个记忆基类"
```

### Task 17: 对手位一律是带记忆的 Actio 实例;外部对手后端每会话新造

**Files:**
- Modify: `src/factory/agent.py`(`AgentRuntime._backends`,经第 2 步 Task 12、Task 15 改过的版本;类 docstring 里"对手位的后端按各自绑定的驱动分派"一段)
- Modify: `src/factory/interact_policy.py`(`_external_backend` 第 198-217 行、`_dispatching` 第 255-267 行)
- Modify(断言随改): `tests/test_factory_handlers.py`(第 264、337 行两处 `isinstance(backends[...], ScriptedBackend)`)
- Test: `tests/test_factory_handlers.py`、`tests/test_factory_interact_wire.py`

**Interfaces:**
- Consumes:
  - Task 16:`memory_role_backend(inner, *, memory, episode_id, interlocutors) -> MemoryTranslatingBackend`、`MemoryTranslatingBackend.instance`、`.close()`、`FactoryMemoryFailureError`。
  - 第 1 步:`actio.memory.TupleMemory()`(空库;`close()` 之后任何调用抛 `MemoryClosedError`);`ActioInstance` 字段 `memory`、`episode_id`、`interlocutors`、`backend`(只有 `respond` 的底座被套成 `IgnoreGuidance`,其 `.inner` 是原后端)。
  - 第 2 步:`simulator.realtime.resources.SessionInfo(scenario, assignment, personas, client_slot, session_id, episode_id)`、`SessionResources(backends, closers=())`;Task 12 的 `AgentRuntime._backends(self, info: SessionInfo) -> SessionResources`;Task 15 的 `AgentRuntime._session_runs` 与它放进 `closers` 的 `drop`。
- Produces:
  - `AgentRuntime._backends(info)` 返回的 `backends[slot]` 一律是 `MemoryTranslatingBackend`;`closers` 是 `(*每个对手位的 backend.close, drop)`(`drop` 只在会话已登记时有,与 Task 15 相同)。
  - `factory.interact_policy._external_backend(...) -> Callable[[], ExternalApiBackend] | None`(返回造后端的零参函数,不再返回实例)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_handlers.py(接在"对手位按驱动分派"一节之后)
def _info(scenario, assignment, *, session_id="sess-1", episode_id="run-1-grpo0-s1-r0", client_slot="a"):
    from simulator.realtime.resources import SessionInfo
    return SessionInfo(scenario, assignment, {}, client_slot, session_id, episode_id)


def _two_slots():
    from simulator.datamodel import RoleBinding
    return _assignment(RoleBinding("a", "pa", "self_play"),
                       RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1", driver="scripted"))


def test_every_opponent_slot_is_an_actio_instance_with_its_own_empty_memory(store):
    from actio.memory import TupleMemory
    from factory.memory_translate import MemoryTranslatingBackend
    runtime = AgentRuntime(store, ("全局",))
    runtime.register_session("s1", "run-1")
    first = runtime._backends(_info(_scenario_with(), _two_slots()))
    second = runtime._backends(_info(_scenario_with(), _two_slots(), session_id="sess-2"))
    backend = first.backends["b"]
    assert isinstance(backend, MemoryTranslatingBackend)
    inst = backend.instance
    assert inst.episode_id == "run-1-grpo0-s1-r0"
    assert inst.interlocutors == {"a": "pa", "b": "pb"}
    assert isinstance(inst.memory, TupleMemory) and inst.memory.digest() == TupleMemory().digest()
    assert inst.memory is not second.backends["b"].instance.memory
    assert inst.skills == () and inst.library_version == {"skill": "0", "memory": "0"}
    first.close()
    second.close()


def test_the_opponent_lines_are_unchanged_and_each_turn_carries_a_v2_trace(store):
    runtime = AgentRuntime(store, ("全局",))
    resources = runtime._backends(_info(_scenario_with({"b": ["剧本乙"]}), _two_slots()))
    utterance, thought, trace = resources.backends["b"].respond(None, _scenario_with(), [], "b")
    assert (utterance, thought) == ("剧本乙", None)
    assert trace["schema"] == "actio.trace.v2" and trace["memory_hits"] == []
    resources.close()


def test_a_deployment_session_gets_actio_opponents_too_and_no_route(store):
    """部署会话(没登记)的对手位也换成 Actio 实例,底座仍是原来的 ScriptedBackend(设计源 7.4 节)。"""
    from simulator.datamodel import RoleBinding
    from simulator.backends import ScriptedBackend
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    bindings = [RoleBinding("a", "p", "self_play"), RoleBinding("b", "p", "frozen_reference", model_ref="x")]
    resources = runtime._backends(_info(_scenario_with(), _assignment(*bindings), episode_id="deploy-m-s1-0"))
    assert isinstance(resources.backends["b"].instance.backend.inner, ScriptedBackend)
    assert runtime._session_runs == {}
    resources.close()


def test_closing_the_session_closes_every_opponent_instance(store):
    from factory.memory_translate import FactoryMemoryFailureError
    runtime = AgentRuntime(store, ("全局",))
    runtime.register_session("s1", "run-1")
    resources = runtime._backends(_info(_scenario_with(), _two_slots()))
    resources.close()
    with pytest.raises(FactoryMemoryFailureError, match="MemoryClosedError"):
        resources.backends["b"].respond(None, _scenario_with(), [], "b")
    assert runtime._session_runs == {}


def test_a_factory_failure_closes_the_instances_already_built(store, monkeypatch):
    import factory.agent as agent_mod
    from factory.memory_translate import FactoryMemoryFailureError
    from simulator.datamodel import RoleBinding
    made = []
    real = agent_mod.memory_role_backend

    def recording(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    def factory(binding, scenario):
        raise RuntimeError("factory exploded")

    monkeypatch.setattr(agent_mod, "memory_role_backend", recording)
    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=factory)
    runtime.register_session("s1", "run-1")
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "frozen_reference", model_ref="x", driver="scripted"),
                RoleBinding("c", "p", "frozen_reference", model_ref="x")]
    with pytest.raises(RuntimeError, match="factory exploded"):
        runtime._backends(_info(_scenario_with(), _assignment(*bindings)))
    assert len(made) == 1
    with pytest.raises(FactoryMemoryFailureError):
        made[0].respond(None, _scenario_with(), [], "b")
    assert runtime._session_runs == {}
```

```python
# tests/test_factory_interact_wire.py(接在 test_..._opponent_timeout... 即第 750-766 行那个用例之后)
def test_each_session_gets_its_own_external_backend(world, tmp_path, stub):
    """F4:外部对手后端不再跨会话共享,每次工厂调用新造一个,超时取 opponent_timeout_s。"""
    opponent = {"base_url": stub.base_url, "model": "ext-model", "api_key_env": OPP_KEY_ENV}
    _, switch, factory, _ = _unit(world, tmp_path, stub, opponent=opponent, env={OPP_KEY_ENV: OPP_KEY})
    bindings = [RoleBinding("a", "pa", "self_play"),
                _opponent_binding(model_ref="gpt-x", driver="external_api", backend="external_api")]
    scenario = Scenario("t1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")
    with factory(RUN, _cfg(bindings=bindings)):
        first, second = switch(bindings[1], scenario), switch(bindings[1], scenario)
    assert isinstance(first, ExternalApiBackend) and isinstance(second, ExternalApiBackend)
    assert first is not second
```

(`ExternalApiBackend` 若该文件未导入,从 `factory.opponents` 补导入。)

既有断言随改:`tests/test_factory_handlers.py` 第 264 行改为 `assert isinstance(backends[slot].instance.backend.inner, ScriptedBackend)`,第 337 行同样改成看 `.instance.backend.inner`;两处用的 `_says` 只取 `respond(...)[0]`,三元组下不用改。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_factory_interact_wire.py -q -p no:cacheprovider -k "actio_instance or v2_trace or deployment_session_gets or closes_every or already_built or its_own_external"`
Expected: FAIL,`backends["b"]` 是 `ScriptedBackend`,没有 `instance` 属性;外部后端用例 `first is second`

- [ ] **Step 3: 实现**

`AgentRuntime._backends(info)`:原来造后端的分派逻辑不变,得到底座 `base` 后改为 `backends[slot] = memory_role_backend(base, memory=TupleMemory(), episode_id=info.episode_id, interlocutors=interlocutors)`,`interlocutors = {b.slot_id: b.persona_id for b in info.assignment.bindings}`,对每个角色位都一样。训练会话的副本"从起点出发",替身没有种子,所以起点就是空库,与部署会话(每会话一份空 `TupleMemory`,设计源 7.4 节)在计划 A 里是同一行代码;计划 B 第 7 步才把训练会话换成从起点库 `backup_to` 出来的副本。`closers` 依次是每个已造后端的 `close`,最后是 Task 15 的 `drop`。造到一半抛异常时,先关已造好的后端(各自 `try`,关失败只 `logger.warning` 类名),再 `drop()`,再原样重抛。`from actio.memory import TupleMemory` 与 `from factory.memory_translate import memory_role_backend` 放模块顶部(`factory` 是组合根,允许导入 `actio`)。

`_external_backend`:进入时照旧造一个,让凭证与配置错误照旧在进入时抛出(`OpponentCredentialError`、`DirectiveFailedError` 的既有用例不变);返回 `lambda: ExternalApiBackend(同一组参数)`。`_dispatching(vllm_make, make_external)` 在 `external_api` 分支调 `make_external()`。docstring 把"那唯一的外部后端"改为"每个会话新造一个"。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_factory_memory_translate.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/agent.py
git add src/factory/interact_policy.py
git add tests/test_factory_handlers.py
git add tests/test_factory_interact_wire.py
git commit -m "feat(factory): 对手位一律包成带记忆的 Actio 实例,外部对手后端每会话新造"
```

### Task 18: `AgentRuntime.client_factory(*, deadline_s=None)` 与客户端的相邻消息上限

**Files:**
- Modify: `src/factory/agent.py`(`AgentRuntime.__init__` 第 124-142 行、`client_factory` 第 226-247 行、类 docstring 第 113 行)
- Test: `tests/test_factory_handlers.py`(接在第 178-196 行 `DeadlineStub` 用例之后)

**Interfaces:**
- Consumes: 第 2 步 Task 10 的 `EnvironmentServiceClient(stub, *, message_timeout_s: float | None = None)`。
- Produces:
  - `AgentRuntime.__init__(..., message_timeout_s: float | None = None)`:新增 keyword-only 参数,存进 `self._message_timeout`;None 时客户端行为与现在相同。
  - `AgentRuntime.client_factory(self, *, deadline_s: float | None = None) -> EnvironmentServiceClient`:整条流 deadline 取 `deadline_s`,None 时取 `self._deadline`(运行时属性 `interact_deadline_s`,保留给既有调用);客户端以 `message_timeout_s=self._message_timeout` 构造。

`client_factory` 的类型:派发器与 MindAgent 拿到的是绑定方法 `runtime.client_factory`,两边今天都零参调用(`trajectory_source.py:243`、`mindagent/deployment.py`),加一个带默认值的 keyword-only 参数后零参调用照旧成立,所以两处的传法不改;注解放宽到 `Callable[..., EnvironmentServiceClient]`:`make_mindtrainer_dispatcher(client_factory=...)` 的注解由 Task 20 改,`mindagent.rounds.make_start_round_handler` 的 `client_factory: Callable[[], object]`(约 184 行)由 Task 33 改成 `Callable[..., object]`,因为 Task 32 会以 `client_factory(deadline_s=...)` 调它;传 `deadline_s` 由轨迹源在 Task 20 里做(见那里)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_handlers.py
def test_a_caller_given_deadline_overrides_the_runtime_value_for_that_client_only(store):
    calls = []

    class FakeStub:
        def __init__(self, channel):
            pass

        def Interact(self, request_iterator, **kw):
            calls.append(kw)
            return iter(())

    runtime = AgentRuntime(store, ("hi",), stub_factory=FakeStub, interact_deadline_s=2.5)
    try:
        runtime.client_factory(deadline_s=360.0)._stub.Interact(iter(()))
        runtime.client_factory()._stub.Interact(iter(()))
        assert calls == [{"timeout": 360.0}, {"timeout": 2.5}]
        assert runtime.interact_deadline_s == 2.5            # 调用方给的值不回写运行时
    finally:
        runtime.close()


def test_the_message_timeout_reaches_every_client_and_defaults_to_none(store):
    with_limit = AgentRuntime(store, ("hi",), message_timeout_s=90.0)
    plain = AgentRuntime(store, ("hi",))
    try:
        assert with_limit.client_factory()._message_timeout_s == 90.0
        assert plain.client_factory()._message_timeout_s is None
    finally:
        with_limit.close()
        plain.close()
```

(`_message_timeout_s` 是 Task 10 Step 3 钉死的属性名。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py -q -p no:cacheprovider -k "caller_given_deadline or message_timeout_reaches"`
Expected: FAIL,`TypeError: client_factory() got an unexpected keyword argument 'deadline_s'` 与 `TypeError: __init__() got an unexpected keyword argument 'message_timeout_s'`

- [ ] **Step 3: 实现**

`client_factory` 里只改构造客户端那一行:`EnvironmentServiceClient(DeadlineStub(self._stub_factory(channel), self._deadline if deadline_s is None else deadline_s), message_timeout_s=self._message_timeout)`。`deadline_s` 只在持锁的临界区里读一次,不写任何属性,所以部署驱动器与轨迹源同时从一个运行时取客户端时各用各的值(设计源 6.4 节"两开关同开时各用各的",第 11 节对应用例的单元部分)。类 docstring 第 113 行补一句:流 deadline 可由调用方逐个给;`interact_deadline_s` 只是没给时的缺省。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_ma_rounds.py tests/test_factory_interact_wire.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/agent.py
git add tests/test_factory_handlers.py
git commit -m "feat(factory): client_factory 收调用方给的流 deadline,运行时可配客户端相邻消息上限"
```

### Task 19: 编排器把训练位的确定性记忆失败写成 failed

**Files:**
- Modify: `src/mindtrainer/orchestrator.py`(`_DETERMINISTIC` 第 36-41 行与上方注释;导入区)
- Test: `tests/test_mt_orchestrator.py`(接在第 658-672 行 `test_a_deterministic_rejection_fails_the_run_and_is_never_retried` 之后)

**Interfaces:**
- Consumes: 第 2 步的 `simulator.realtime.backend_timeout.RoleMemoryFailureError(Exception)`。`role_memory_failure` 进 `_DETERMINISTIC_ROLLOUT_REASONS` 已在 Task 11 做完,本任务不再动那个集合。
- Produces: `mindtrainer.orchestrator._DETERMINISTIC` 含 `RoleMemoryFailureError`。`mindtrainer.dispatch._DETERMINISTIC` 不加:编排器写完 failed 重抛后,派发器的兜底分支读到 `failed` 记录就当轮判 `DirectiveFailedError`(`dispatch.py:378-393`),与 `invalid_session` 走同一条路。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_mt_orchestrator.py
class _MemoryBrokenSource:
    """替身 Interact 源:collect 时训练位策略抛确定性记忆失败(轨迹源不接它,原样穿出)。"""

    def __init__(self):
        self.collects = 0

    def collect(self):
        from simulator.realtime.backend_timeout import RoleMemoryFailureError
        self.collects += 1
        raise RoleMemoryFailureError("memory backend failed for slot 'a': MemoryClosedError")


def test_a_deterministic_memory_failure_of_the_trained_slot_fails_the_run_instead_of_leaving_it_pending(store):
    from simulator.realtime.backend_timeout import RoleMemoryFailureError
    source = _MemoryBrokenSource()
    with pytest.raises(RoleMemoryFailureError):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["category"], failed.failure["point"]) == (
        "failed", "T2", "step 1 (grpo_opd)",
    )
    assert failed.failure["detail"].startswith("RoleMemoryFailureError: ")
    with pytest.raises(NeedsHumanError, match="failed"):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    assert source.collects == 1
```

(`point` 取值照抄上面那个确定性拒绝用例:同一个替身配置在第 1 步的 grpo_opd 阶段失败。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_orchestrator.py::test_a_deterministic_memory_failure_of_the_trained_slot_fails_the_run_instead_of_leaving_it_pending -q -p no:cacheprovider`
Expected: FAIL,`failed.status == "pending"`(异常落不进任何确定性分支,记录保持 pending,设计源第 10 节说的"被当成可续训而反复延后")

- [ ] **Step 3: 实现**

`orchestrator.py` 加 `from simulator.realtime.backend_timeout import RoleMemoryFailureError`(`mindtrainer` 已经导入 `simulator`,依赖方向不变,不导入 `actio`),`_DETERMINISTIC` 末尾加 `RoleMemoryFailureError`。注释补一句:训练位的记忆端口确定性失败经组合根的转换后端变成它的子类,同样的输入还会再失败。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_orchestrator.py tests/test_mt_dispatch.py tests/test_import_boundaries.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/mindtrainer/orchestrator.py
git add tests/test_mt_orchestrator.py
git commit -m "fix(mindtrainer): 训练位确定性记忆失败进 _DETERMINISTIC,训练记录写 failed 不再滞留 pending"
```

### Task 20: 轨迹源按 rollout 进出策略上下文,用逻辑 episode id,轨迹随提交上行

**Files:**
- Modify: `src/mindtrainer/trajectory_source.py`(导入区;`InteractTrajectorySource` docstring 第 127-156 行、`__init__` 第 158-180 行、`_rollout` 第 215-239 行、`_attempt` 第 241-278 行)
- Modify: `src/mindtrainer/dispatch.py`(`_interact_sources` 第 229-249 行;`make_mindtrainer_dispatcher` 的 `client_factory`、`policy_factory` 注解第 290-292 行与 docstring 第 302-307 行;第 355-360 行进上下文那几行)
- Modify(签名随改): `tests/test_mt_trajectory_interact.py`(第 102、213、312 行三个客户端工厂;`_FakeClient.create_session`、`submit_turn` 第 276、284 行;第 122、140、152、168、186、219、229、322 行八处构造)、`tests/test_mt_orchestrator.py`(第 477 行客户端工厂、第 511 行构造)、`tests/test_mt_dispatch.py`(第 465 行客户端工厂、`_Scope.__enter__` 第 636-640 行)、`tests/test_mt_dispatch_synthesis.py`(第 460 行客户端工厂、第 545 行 `FlakyOnce.__call__`)
- Test: `tests/test_mt_trajectory_interact.py`、`tests/test_mt_dispatch.py`

**Interfaces:**
- Consumes:
  - 第 2 步:`EnvironmentServiceClient.create_session(..., episode_id: str | None = None)`;服务端采用 `episode_id` 当落盘 id(Task 12);同 `episode_id` 有活动会话时创建被拒为 `role_backend_unavailable`(Task 14);等 `session_ready` 超时抛 `SessionRejectedError("timeout", ...)`(Task 10);`RoleMemoryUnavailableError`、`RoleMemoryFailureError`;`"role_memory_unavailable"` 已在 `_TRANSIENT_REASONS` 里(Task 11)。
  - Task 18:`client_factory(*, deadline_s: float | None = None)`。
- Produces:
  - `mindtrainer.trajectory_source.PolicyForRollout = Callable[[str], AbstractContextManager[ModelBackend]]`。
  - `mindtrainer.trajectory_source.PER_TURN_DEADLINE_S = 90.0`(设计源 6.4 节"客户端每轮等待 90 秒")。
  - `InteractTrajectorySource(client_factory: Callable[..., EnvironmentServiceClient], policy_for_rollout: PolicyForRollout, store, config, *, run_id: str, start_step: int = 0)`。`client_factory` 必须收 keyword-only `deadline_s`。
  - `logical_episode_id(run_id: str, step: int, scenario_id: str, k: int) -> str`:返回 `f"{run_id}-grpo{step}-{scenario_id}-r{k}"`。
  - `make_mindtrainer_dispatcher(..., client_factory: Callable[..., object] | None, policy_factory: Callable[[str, TrainingConfig], AbstractContextManager[PolicyForRollout]] | None)`:上下文交出的是 `policy_for_rollout`,不再是后端。`policy=` 这条路照旧收一个固定后端。

逻辑 id 的取法:设计源写的是 `{run}-{stage}-{scenario}-r{k}`。interact 只有 `grpo_opd` 一个阶段,同一情境又会按 `batch_for_step` 在不同 GRPO step 里反复出现,所以 `{stage}` 取 `grpo<step>`(本次 collect 的 `self._next_step`),`k` 是组内 rollout 序号(`_rollout` 的 `rollout`)。这样一个 run 内每条 rollout 的 id 唯一,同一条 rollout 的重试不变。collect 失败后重做同一步、或续训从同一步接着走时,id 也一样,服务端按同名覆盖,符合"重试不变"(F21)。今天轨迹源没有自己的 episode id,只收服务端给的 `ep-<session_id>`;对齐的是部署的 `deploy-{model}-{scenario}-{k}`(`mindagent/deployment.py:66-67`)那种连字符写法,只用白名单 `[A-Za-z0-9._-]` 内的字符。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_mt_trajectory_interact.py
from contextlib import contextmanager, nullcontext

from mindtrainer.trajectory_source import PER_TURN_DEADLINE_S, logical_episode_id
from simulator.realtime.backend_timeout import RoleMemoryFailureError, RoleMemoryUnavailableError


def _fixed(policy):
    return lambda episode_id: nullcontext(policy)


class _Rollouts:
    """policy_for_rollout:记下每次进出与 id,进入时交出 policy。"""
    def __init__(self, policy, events):
        self.policy, self.events = policy, events

    @contextmanager
    def __call__(self, episode_id):
        self.events.append(("enter", episode_id))
        try:
            yield self.policy
        finally:
            self.events.append(("exit", episode_id))


def test_the_logical_episode_id_is_run_stage_scenario_and_rollout():
    assert logical_episode_id("run-7", 3, "s1", 1) == "run-7-grpo3-s1-r1"


def test_each_rollout_gets_its_own_context_and_its_logical_id_reaches_create_session(store):
    events = []
    factory = _FakeFactory({"end_reason": "timeout"})
    source = InteractTrajectorySource(
        factory, _Rollouts(ScriptedBackend({"a": ["x"] * 50}), events), store, _cfg(**_ONE_GROUP),
        run_id="run-1", start_step=2)
    source.collect()
    ids = ["run-1-grpo2-s1-r0", "run-1-grpo2-s1-r0", "run-1-grpo2-s1-r1"]   # 第一条重试一次,id 不变
    assert [c.created["episode_id"] for c in factory.clients] == ids
    assert events == [e for i in ids for e in (("enter", i), ("exit", i))]
    assert factory.deadlines == [_cfg().max_turns_per_episode * PER_TURN_DEADLINE_S] * 3


def test_a_three_tuple_reply_sends_its_trace_and_a_two_tuple_sends_none(store):
    class Traced:
        def __init__(self):
            self.n = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.n += 1
            return ("a", None, {"schema": "actio.trace.v2", "n": str(self.n)}) if self.n % 2 else ("b", None)

    factory = _FakeFactory()
    InteractTrajectorySource(factory, _fixed(Traced()), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    traces = factory.clients[0].traces
    assert traces[0] == {"schema": "actio.trace.v2", "n": "1"} and traces[1] is None


def test_a_reply_of_the_wrong_length_is_a_policy_bug_and_is_not_retried(store):
    class Wrong:
        def respond(self, persona, scenario, history, speaker_slot):
            return ("a", None, {}, "extra")

    factory = _FakeFactory()
    with pytest.raises(TypeError, match="2- or 3-tuple"):
        InteractTrajectorySource(factory, _fixed(Wrong()), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    assert len(factory.clients) == 1 and factory.clients[0].aborted


@pytest.mark.parametrize("reason", ["role_backend_unavailable", "timeout", "role_memory_unavailable"])
def test_a_create_rejection_with_a_transient_reason_is_retried(store, reason):
    factory = _FakeFactory({"create_error": SessionRejectedError(reason, "busy")})
    batch = _source(store, factory, **_ONE_GROUP).collect()
    assert len(batch.episodes) == 2
    assert [a["reason"] for a in batch.metadata["failed_attempts"]] == [reason]


def test_a_transient_memory_failure_of_the_trained_slot_aborts_and_retries_the_rollout(store):
    class Flaky:
        def __init__(self):
            self.calls = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.calls += 1
            if self.calls == 2:
                raise RoleMemoryUnavailableError("memory backend failed for slot 'a': MemoryFailureError")
            return "a", None

    events = []
    factory = _FakeFactory()
    batch = InteractTrajectorySource(
        factory, _Rollouts(Flaky(), events), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    assert len(batch.episodes) == 2 and factory.clients[0].aborted
    assert batch.metadata["failed_attempts"][0]["reason"] == "role_memory_unavailable"
    assert batch.metadata["failed_attempts"][0]["detail"] == "RoleMemoryUnavailableError"
    assert events[:3] == [("enter", "run-1-grpo0-s1-r0"), ("exit", "run-1-grpo0-s1-r0"),
                          ("enter", "run-1-grpo0-s1-r0")]


def test_a_deterministic_memory_failure_of_the_trained_slot_passes_through_unwrapped(store):
    class Broken:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RoleMemoryFailureError("memory backend failed for slot 'a': MemoryClosedError")

    events = []
    factory = _FakeFactory()
    with pytest.raises(RoleMemoryFailureError):
        InteractTrajectorySource(
            factory, _Rollouts(Broken(), events), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    assert len(factory.clients) == 1 and factory.clients[0].aborted
    assert events == [("enter", "run-1-grpo0-s1-r0"), ("exit", "run-1-grpo0-s1-r0")]


def test_the_server_persists_each_rollout_under_its_logical_id(store, live):
    env = live()
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, group_size=2)
    batch = InteractTrajectorySource(
        env.client_factory, _fixed(ScriptedBackend({"a": ["a1", "a2", "a3", "a4"]})), store, cfg,
        run_id="run-1").collect()
    assert batch.metadata["episode_ids"] == ["run-1-grpo0-s1-r0", "run-1-grpo0-s1-r1"]
    assert _wait_until(lambda: len(env.closed) == 2)
    assert sorted(e.id for e in env.closed) == batch.metadata["episode_ids"]
```

```python
# tests/test_mt_dispatch.py(接在第 490-505 行那个 interact 端到端用例之后,复用同一份配置与 live 环境)
def test_an_interact_directive_names_its_episodes_with_the_run_id(store, live):
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})))
    assert _wait_until(lambda: len(env.closed) == 4)
    assert sorted(e.id for e in env.closed) == [
        f"{RUN}-grpo0-s1-r0", f"{RUN}-grpo0-s1-r1", f"{RUN}-grpo1-s2-r0", f"{RUN}-grpo1-s2-r1"]
```

(这个用例的 fixture 名与 `_directive`、`_cfg_dict` 照抄第 490-505 行的那个用例;它前面的 sft 数据准备若在那个用例体里,一并照抄。)

既有用例随改:
- `test_mt_trajectory_interact.py`:`_FakeClient.create_session` 加 `episode_id=None` 并记进 `self.created["episode_id"]`;`submit_turn(self, utterance, inner_thought=None, *, activation_trace=None)` 把 `activation_trace` 追加进 `self.traces`(构造时 `self.traces = []`);`_FakeFactory.__call__(self, *, deadline_s=None)` 把 `deadline_s` 追加进 `self.deadlines`;`live` 里的 `client_factory(*, deadline_s=None)`、第 213 行 `tracked(**kw)` 转给 `env.client_factory(**kw)`。八处构造把策略换成 `_fixed(policy)`,并加 `run_id="run-1"`,`_source` 辅助同样。
- `test_a_transient_failure_is_retried_and_recorded_and_the_half_episode_is_not_kept`:替身的 `drain_until_end` 仍报 `ep-fake-N`,所以 `"ep-fake-0" not in ...` 那句照旧成立,不改。
- `test_mt_orchestrator.py:477`、`test_mt_dispatch.py:465`、`test_mt_dispatch_synthesis.py:460` 的 `client_factory()` 改为 `client_factory(*, deadline_s=None)`(值不用);`test_mt_dispatch_synthesis.py:545` 的 `FlakyOnce.__call__(self, *, deadline_s=None)` 转给 `server.client_factory(deadline_s=deadline_s)`;`test_mt_orchestrator.py:511` 改为 `InteractTrajectorySource(env.client_factory, lambda eid: nullcontext(policy), store, cfg, run_id=RUN, start_step=start)`。
- `test_mt_dispatch.py` 的 `_Scope.__enter__` 改为返回 `lambda episode_id: nullcontext(self.policy)`;七处 `_Scope(...)` 调用(第 666、687、709、725、745、782、887 行)不改。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_trajectory_interact.py tests/test_mt_dispatch.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'PER_TURN_DEADLINE_S' from 'mindtrainer.trajectory_source'`;导入补齐后是 `TypeError: __init__() got an unexpected keyword argument 'run_id'`

- [ ] **Step 3: 实现**

`trajectory_source.py`:
- `_rollout` 按 `logical_episode_id(self._run_id, self._next_step, scenario.id, rollout)` 算出 id,传给 `_attempt(scenario, personas, episode_id)`。
- `_attempt` 结构:最外层 `with self._policy_for_rollout(episode_id) as policy:`,里面才是 `client = self._client_factory(deadline_s=self._cfg.max_turns_per_episode * PER_TURN_DEADLINE_S)` 与现有的 `try/finally`(abort)。这样先进上下文、再造客户端、再 `create_session`;失败时先 abort 客户端,再退出上下文。上下文进入时抛的异常不包装,原样上抛(工厂侧的确定性错误)。
- `create_session(..., episode_id=episode_id)`;`except SessionRejectedError as exc` 改为 `_AttemptFailed(exc.reason, exc.detail, exc.reason in _TRANSIENT_REASONS)`,这样 Task 14 的名额与同 id 拒绝才会按条重跑(设计源第 10 节"现有两个客户端对创建被拒一律判不可重跑,要改")。
- 回合循环:`reply = policy.respond(...)`。只捕获 `RoleMemoryUnavailableError`,改抛 `_AttemptFailed("role_memory_unavailable", type(exc).__name__, True)`(设计源第 10 节"训练位瞬态记忆失败按条重跑";detail 只写类名)。其余策略异常(含 `RoleMemoryFailureError`)照旧不接,原样穿出。`reply` 不是长度 2 或 3 的 tuple/list 时抛 `TypeError(f"policy for slot {slot!r} must return a 2- or 3-tuple, got {reply!r}")`,同样不接。三元组的第三项是 `dict` 时作为 `activation_trace=` 传给 `submit_turn`,二元组传 `None`;第三项不是 `dict` 也抛 `TypeError`(与 `simulator.runner._unpack_reply` 同一判法)。
- 导入 `from contextlib import AbstractContextManager`,并从 `simulator.realtime.backend_timeout` 导入两个记忆基类。`mindtrainer` 仍不导入 `actio`。
- docstring 改三处:"policy 复用 ModelBackend 协议"改成"policy_for_rollout 每条 rollout 进出一次,交出 ModelBackend,二元组或三元组";T1 列举里加 `role_memory_unavailable` 与创建被拒的瞬态原因;再写明逻辑 id 的格式。

`dispatch.py`:`_interact_sources(store, cfg, manifests, client_factory, policy_for_rollout, run_id)` 构造时传 `run_id=run_id`。第 357 行改为 `scope = nullcontext(lambda episode_id: nullcontext(policy)) if policy is not None else policy_factory(run_id, cfg)`,进入后得到的是 `policy_for_rollout`。docstring 写明"上下文进入时交出 `policy_for_rollout(episode_id)`,每条 rollout 进出一次"。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_trajectory_interact.py tests/test_mt_dispatch.py tests/test_mt_orchestrator.py tests/test_mt_dispatch_synthesis.py tests/test_interact_parity.py tests/test_import_boundaries.py -q -p no:cacheprovider`
Expected: PASS(`tests/test_factory_interact_wire.py` 要到 Task 21 才跟上:在那之前,策略工厂交出的仍是后端)

- [ ] **Step 5: 先不提交,接着做 Task 21**

本任务的改动与 Task 21 一起提交(见 Task 21 开头的说明)。这里只确认 Step 4 列出的测试通过;`tests/test_factory_interact_wire.py` 此时预期失败,不要为它改代码。

### Task 21: 策略工厂交出 `policy_for_rollout`,每条 rollout 一个包 `VllmPolicy` 的 Actio 实例

> 注意:Task 20 与 Task 21 合成一次提交。Task 20 做完 Step 4 先不提交,接着做 Task 21;Task 21 的提交步把两任务的文件一起暂存。两者之间 `tests/test_factory_interact_wire.py` 会红,分开提交会违反"每个任务结束时全套仍绿"。

**Files:**
- Modify: `src/factory/interact_policy.py`(`make_policy_factory` 第 270-359 行;模块 docstring 第 3-11 行"交出策略"一句)
- Modify(随改): `tests/test_factory_interact_wire.py`(第 620、630、645、665、676、687、695、697、707、718、728、744、764、774、1070、1082、1175、1188 行的 `with factory(...)`:它们都不用交出的对象,签名变化不影响;只有断言 `interact_deadline_s == cfg.max_turns_per_episode * 30.0` 的第 622 行要改,见 Task 22)
- Test: `tests/test_factory_interact_wire.py`

**Interfaces:**
- Consumes:
  - Task 16:`memory_role_backend(inner, *, memory, episode_id, interlocutors) -> MemoryTranslatingBackend`。
  - Task 20:`mindtrainer.trajectory_source.PolicyForRollout`。
  - 第 2 步 Task 7:`VllmPolicy.respond_guided(persona, scenario, history, speaker_slot, guidance)`(实例因此把它当 `GuidedBackend`,指导语进 prompt)。
  - 第 1 步:`TupleMemory()`、`ActioInstance.close()`。
- Produces: `make_policy_factory(...) -> Callable[[str, TrainingConfig], AbstractContextManager[PolicyForRollout]]`;交出的 `policy_for_rollout(episode_id: str) -> AbstractContextManager[MemoryTranslatingBackend]`。每次进入用 `memory_role_backend(policy, memory=TupleMemory(), episode_id=episode_id, interlocutors={b.slot_id: b.persona_id for b in cfg.bindings})` 新造一个实例,退出时(正常与异常都一样)调它的 `close()`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_interact_wire.py
def test_the_scope_yields_a_per_rollout_factory_of_actio_instances_around_the_vllm_policy(world, tmp_path, stub):
    from actio.memory import TupleMemory
    from factory.memory_translate import MemoryTranslatingBackend
    from factory.vllm_policy import VllmPolicy
    _, _, factory, _ = _unit(world, tmp_path, stub)
    cfg = _cfg()
    with factory(RUN, cfg) as policy_for_rollout:
        with policy_for_rollout(f"{RUN}-grpo0-t1-r0") as first:
            assert isinstance(first, MemoryTranslatingBackend)
            inst = first.instance
            assert isinstance(inst.backend, VllmPolicy)                 # 有 respond_guided,不套 IgnoreGuidance
            assert inst.episode_id == f"{RUN}-grpo0-t1-r0"
            assert inst.interlocutors == {b.slot_id: b.persona_id for b in cfg.bindings}
            assert isinstance(inst.memory, TupleMemory) and inst.memory.digest() == TupleMemory().digest()
        with policy_for_rollout(f"{RUN}-grpo0-t1-r1") as second:
            assert second.instance is not inst and second.instance.backend is inst.backend


def test_leaving_a_rollout_closes_its_instance_even_when_the_rollout_raises(world, tmp_path, stub):
    from factory.memory_translate import FactoryMemoryFailureError
    _, _, factory, _ = _unit(world, tmp_path, stub)
    with factory(RUN, _cfg()) as policy_for_rollout:
        with pytest.raises(RuntimeError, match="rollout broke"):
            with policy_for_rollout(f"{RUN}-grpo0-t1-r0") as backend:
                raise RuntimeError("rollout broke")
        with pytest.raises(FactoryMemoryFailureError, match="MemoryClosedError"):
            backend.respond(None, None, [], "a")


def test_an_interact_directive_records_v2_traces_on_the_trained_slot(world, tmp_path, stub, fake_serve):
    """端到端:装配出的派发器里,训练位每一轮都带 v2 轨迹落盘;对手位同样(设计源第 11 节端到端一行的替身版)。"""
    from simulator.store_adapter import read_episode
    _write_directive(world)
    _run(world, tmp_path)
    assert _read(world, "directive").status == "executed"
    assert _wait_until(lambda: len(_episode_ids(world)) >= 2)
    for path in _episode_ids(world):
        episode = read_episode(world, RUN, path.rsplit("/", 1)[-1].removesuffix(".yaml"))
        assert episode.id.startswith(f"{RUN}-grpo0-")
        assert episode.turns and all(t.activation_trace["schema"] == "actio.trace.v2" for t in episode.turns)
```

(`_episode_ids` 返回 `list_products` 的条目,是 `runs/<run>/episodes/<id>.yaml` 形式的相对路径,所以先取文件名再去后缀。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_interact_wire.py -q -p no:cacheprovider -k "per_rollout_factory or closes_its_instance or v2_traces_on_the_trained_slot"`
Expected: FAIL,`TypeError: 'VllmPolicy' object is not callable`(上下文交出的仍是后端);端到端用例在 Task 20 之后因同一原因判 `failed`

- [ ] **Step 3: 实现**

`make_policy_factory` 里 `yield policy` 改为 `yield policy_for_rollout`。后者是 `scope` 内的闭包,用 `@contextmanager` 定义:先造 `backend = memory_role_backend(...)`,`try: yield backend`,`finally: backend.close()`。`close()` 抛出的 `FactoryMemoryFailureError` 不接:它是 `RoleMemoryFailureError` 的子类,从轨迹源穿出后由编排器写 failed(Task 19),再经派发器的兜底分支判 T2,与设计源 7.2 节"`close` 超时则这条 rollout 判 `role_memory_failure`,不重试"同向。替身的 `close` 不会超时;60 秒上限是计划 B 的 `StarlingMemory` 的事。

记忆用每条 rollout 一份新的空 `TupleMemory`,理由与 Task 17 相同:替身没有种子,"从起点出发"的副本就是空库。计划 B 第 7 步再按 `runs/<run_id>/client/<逻辑 episode id>/<trained_slot>.db` 从起点库复制。`yield` 外面那层 `RolloutFailedError`/`OpponentApiError` 的分类不动:它们照旧从训练体里冒出来,经过的是 `scope` 的 `yield`,不是 `policy_for_rollout` 的。`RoleMemoryFailureError` 不在这两类里,原样穿出 `scope`,由派发器分类。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_mt_dispatch.py tests/test_factory_memory_translate.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/mindtrainer/trajectory_source.py
git add src/mindtrainer/dispatch.py
git add tests/test_mt_trajectory_interact.py
git add tests/test_mt_orchestrator.py
git add tests/test_mt_dispatch.py
git add tests/test_mt_dispatch_synthesis.py
git add src/factory/interact_policy.py
git add tests/test_factory_interact_wire.py
git commit -m "feat(mindtrainer,factory): 轨迹源按 rollout 进出策略上下文、逻辑 episode id 上行;策略工厂交出 policy_for_rollout,每条 rollout 一个带记忆的 Actio 实例"
```

### Task 22: `--interact` 的时间预算:服务端等提交 90 秒、客户端相邻消息 90 秒、策略 HTTP 60 秒

**Files:**
- Modify: `src/factory/wire.py`(第 134 行 `INTERACT_TURN_TIMEOUT_S`;`_shared_runtime` 第 156-159 行)
- Modify: `src/factory/vllm_policy.py`(第 28 行 `POLICY_TIMEOUT_S`)
- Modify: `src/factory/interact_policy.py`(第 70-71 行 `PER_TURN_DEADLINE_S`、`OPPONENT_TIMEOUT_S` 的注释;`scope` 第 328-354 行改写运行时 deadline 的几行;模块 docstring 第 9-10 行"调长流 deadline""还原 deadline")
- Modify(断言随改): `tests/test_factory_interact_wire.py`(第 174 行 `_timeout == 30.0`;第 622 行 `interact_deadline_s == cfg.max_turns_per_episode * 30.0`)
- Test: `tests/test_factory_interact_wire.py`

**Interfaces:**
- Consumes: Task 18 的 `AgentRuntime(..., message_timeout_s=)`;Task 20 的 `mindtrainer.trajectory_source.PER_TURN_DEADLINE_S = 90.0`(轨迹源用它自己算 `deadline_s = max_turns × 90`)。
- Produces: `factory.wire.INTERACT_TURN_TIMEOUT_S = 90.0`、`factory.wire.INTERACT_MESSAGE_TIMEOUT_S = 90.0`、`factory.vllm_policy.POLICY_TIMEOUT_S = 60.0`;`factory.interact_policy.OPPONENT_TIMEOUT_S` 仍是 `25.0`。`factory.interact_policy.PER_TURN_DEADLINE_S` 删掉,流 deadline 只在轨迹源一处算。

取值来自设计源 6.4 节的表:服务端等提交 30 → 90,客户端每轮等待 30 → 90,服务端后端调用上限 60 不变(`create_server` 的缺省 `backend_timeout_s=60.0`,运行时不传),对手 HTTP 25 不变,训练位策略 HTTP 120 → 60。核算:对手一轮最坏 25 + 20 = 45 < 60;训练位一轮最坏 60 + 20 = 80 < 90。`--interact` 与 `--enable agent` 同开时共用一个运行时,`_shared_runtime` 的 interact 分支本来就覆盖同开的情况,服务端等提交只有一份,取两者较大的 90;部署会话只是多等。只开 `--enable agent` 时保持 5 秒与无相邻消息上限。部署路在记忆配置在场时用的 40 秒属于计划 B,本任务不动。

策略上下文不再改写运行时的 `interact_deadline_s`:轨迹源经 `client_factory(deadline_s=...)` 自己给。原来那几行在训练期间把运行时属性改成训练的值,同开时部署会话(替身路径不传 `deadline_s`)就借用了训练的 deadline,与设计源 6.4 节"两开关同开时各用各的"相违。第 619-689 行那几个用例里"退出后 `interact_deadline_s == before`"的断言照旧成立。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_interact_wire.py
def test_interact_alone_builds_one_runtime_and_a_longer_turn_timeout(
        defaults_repo, env, runtimes, dispatcher_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(interact=True), env=env)
    try:
        assert len(runtimes) == 1
        assert dispatcher_kwargs[-1]["client_factory"].__self__ is runtimes[0]
        assert runtimes[0]._timeout == 90.0                        # 原为 30.0
        assert runtimes[0]._message_timeout == 90.0
        assert type(dispatcher_kwargs[-1]["data_producer"]).__name__ == "TemplateDataProducer"
    finally:
        asm.close()


def test_both_switches_share_the_larger_turn_timeout(defaults_repo, env, runtimes):
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent",), interact=True), env=env)
    try:
        assert (runtimes[0]._timeout, runtimes[0]._message_timeout) == (90.0, 90.0)
    finally:
        asm.close()


def test_the_agent_switch_alone_has_no_message_timeout(defaults_repo, runtimes):
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent",)), env={})
    try:
        assert (runtimes[0]._timeout, runtimes[0]._message_timeout) == (5.0, None)
    finally:
        asm.close()


def test_the_time_budget_constants():
    import factory.interact_policy as ip
    import factory.vllm_policy as vp
    import factory.wire as wire
    from mindtrainer.trajectory_source import PER_TURN_DEADLINE_S
    assert (wire.INTERACT_TURN_TIMEOUT_S, wire.INTERACT_MESSAGE_TIMEOUT_S) == (90.0, 90.0)
    assert (vp.POLICY_TIMEOUT_S, ip.OPPONENT_TIMEOUT_S, PER_TURN_DEADLINE_S) == (60.0, 25.0, 90.0)
    assert not hasattr(ip, "PER_TURN_DEADLINE_S")
    assert vp.POLICY_TIMEOUT_S + 20.0 < wire.INTERACT_TURN_TIMEOUT_S       # 训练位 HTTP 加记忆 20 秒,低于服务端等提交
    assert ip.OPPONENT_TIMEOUT_S + 20.0 < 60.0                             # 对手 HTTP 加记忆 20 秒,低于服务端后端上限


def test_a_training_scope_leaves_the_runtime_deadline_to_deployment_sessions(world, tmp_path, stub):
    """同开时部署会话(不传 deadline_s)在训练进行中仍用运行时自己的值。"""
    runtime, _, factory, _ = _unit(world, tmp_path, stub)
    before = runtime.interact_deadline_s
    with factory(RUN, _cfg()):
        assert runtime.interact_deadline_s == before
```

第一个用例就是第 168-177 行的既有用例,只改第 174 行并加一行;第 622 行改为 `assert runtime.interact_deadline_s == before`。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_interact_wire.py -q -p no:cacheprovider -k "longer_turn_timeout or larger_turn_timeout or no_message_timeout or time_budget_constants or leaves_the_runtime_deadline"`
Expected: FAIL,`assert 30.0 == 90.0`、`AttributeError: module 'factory.wire' has no attribute 'INTERACT_MESSAGE_TIMEOUT_S'`、`assert 120.0 == 60.0`,以及训练期间 `interact_deadline_s` 被改成 `4 * 30.0`

- [ ] **Step 3: 实现**

- `wire.py`:`INTERACT_TURN_TIMEOUT_S = 90.0`,注释改为"设计源 6.4 节:训练位策略 HTTP 60 秒加一轮记忆最坏 20 秒";新增 `INTERACT_MESSAGE_TIMEOUT_S = 90.0`,注释"客户端相邻两条服务端消息的等待上限(设计源 6.4 节)"。interact 分支的 `AgentRuntime(...)` 加 `message_timeout_s=INTERACT_MESSAGE_TIMEOUT_S`。
- `vllm_policy.py`:`POLICY_TIMEOUT_S = 60.0`。
- `interact_policy.py`:删 `PER_TURN_DEADLINE_S`;`scope` 里删 `previous` 变量,以及进入时与 `finally` 里两处给 `runtime.interact_deadline_s` 赋值的代码,`registered` 的登记与注销不动;第 330 行那句"不要在持锁时赋值"的注释随之删掉。`OPPONENT_TIMEOUT_S` 的注释改为"低于服务端后端调用上限 60 秒:25 秒加一轮记忆最坏 20 秒"。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_interact_wire.py tests/test_factory_vllm_policy.py tests/test_factory_handlers.py tests/test_factory_interact_e2e.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/wire.py
git add src/factory/vllm_policy.py
git add src/factory/interact_policy.py
git add tests/test_factory_interact_wire.py
git commit -m "feat(factory): --interact 时间预算改为服务端等提交与客户端相邻消息各 90 秒、策略 HTTP 60 秒"
```

### Task 23: `run_daemon(..., on_locked=)`:取到仓库锁之后、第一个 tick 之前调一次

**Files:**
- Modify: `src/controlplane/daemon.py`(`run_daemon` 第 503-529 行)
- Test: `tests/test_repo_lock.py`(接在第 130-150 行 `test_run_daemon_holds_the_lock_while_ticking_and_releases_it_afterwards` 之后)

**Interfaces:**
- Consumes: 无(`controlplane.lock.acquire_repo_lock`、`RepoLockedError` 是现有的)。
- Produces: `run_daemon(store, interval_s, max_ticks=None, *, dispatcher=None, handlers=None, evaluator=None, accept_mock_evidence=False, on_locked: Callable[[], None] | None = None) -> None`。计划 B 第 7 步让 `factory/wire.py` 传入记忆根目录的清扫函数;本步 `wire.py` 两处 `run_daemon(...)`(第 387、389 行)与 `mindtrainer/wire.py:30` 都不传,替身没有根目录要清。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_repo_lock.py
def test_on_locked_runs_once_while_the_lock_is_held_and_before_the_first_tick(store):
    _put_directive(store)
    seen = []

    def on_locked():
        try:
            with acquire_repo_lock(store.repo_root):
                seen.append("free")
        except RepoLockedError:
            seen.append("locked")

    def dispatcher(s, directive):
        seen.append("tick")
        stub_dispatcher(s, directive)

    run_daemon(store, 0, max_ticks=2, dispatcher=dispatcher, on_locked=on_locked)
    assert seen[0] == "locked" and seen.count("locked") == 1
    assert "tick" in seen[1:]


def test_on_locked_is_not_called_when_the_lock_is_taken(store):
    called = []
    with acquire_repo_lock(store.repo_root):
        with pytest.raises(RepoLockedError):
            run_daemon(store, 0, max_ticks=1, on_locked=lambda: called.append(1))
    assert called == []


def test_an_on_locked_failure_stops_the_daemon_before_any_tick_and_releases_the_lock(store):
    _put_directive(store)
    commits = store._git("rev-list", "--count", "HEAD")

    def boom():
        raise OSError("sweep failed")

    with pytest.raises(OSError, match="sweep failed"):
        run_daemon(store, 0, max_ticks=1, on_locked=boom)
    assert store.read_product(DIRECTIVE).status == "pending"
    assert store._git("rev-list", "--count", "HEAD") == commits
    with acquire_repo_lock(store.repo_root):
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_repo_lock.py -q -p no:cacheprovider -k on_locked`
Expected: FAIL,`TypeError: run_daemon() got an unexpected keyword argument 'on_locked'`

- [ ] **Step 3: 实现**

`with acquire_repo_lock(...)` 块的第一句是 `if on_locked is not None: on_locked()`,在 `DeferralBook()` 之前。异常不接:清扫失败时守护进程不该带着半清的记忆根目录开跑,锁由 `with` 释放。docstring 补一句:`on_locked` 在持锁后、第一个 tick 之前调一次,用来清扫只有持锁者才能安全删除的东西(计划 B 的记忆副本目录);清扫要等持锁,是因为另一个守护进程可能正在用那些副本。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_repo_lock.py tests/test_daemon_dispatcher.py tests/test_daemon_deferred.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/daemon.py
git add tests/test_repo_lock.py
git commit -m "feat(controlplane): run_daemon 收 on_locked,取锁后第一个 tick 前调一次"
```

---

## 第 4 步:训练配置的记忆段与起点解析

> 第 4 步的提交顺序按"每个提交全套测试仍绿"排:Task 24 让 `memory` 字段可选地存在;Task 25 让派发器在配置带 `memory` 时解它;Task 26 让组合根在 `--interact` 下注入替身解析器;Task 27 才让调度器把 `memory` 写进新配置(先于 Task 26 会让装配出的派发器拿到带 `memory` 却没有解析器的指令,`tests/test_factory_interact_e2e.py` 中途变红);Task 28 把它变成 interact 必填(F26),并一次补齐所有夹具。

### Task 24: `TrainingConfig.memory` 字段、形状校验与哈希覆盖

**Files:**
- Modify: `src/mindtrainer/training_config.py`(`TrainingConfig` 字段与 `to_dict`/`from_dict`;`_check_types` 里加一个 `_check_memory_types`;`validate_training_config` 的 batch 分支;`_validate_interact`)
- Create: `tests/memory_fixtures.py`(本步与 Task 25 到 Task 28 共用的记忆夹具,放在 `tests/` 顶层,与 `factory_fixtures.py`、`ts_fixtures.py` 同一种 `from memory_fixtures import ...` 用法)
- Test: `tests/test_mt_config.py`

**Interfaces:**
- Consumes: 无(纯 mindtrainer,不导入 actio)。
- Produces:
  - `TrainingConfig.memory: dict | None = None`,形状 `{"seed": str | None, "digests": dict[str, str], "runtime": dict}`;"未解析"是 `digests == {}` 且 `runtime == {}`。
  - `to_dict()` 只在 `memory is not None` 时写 `"memory"` 键(深拷贝);`from_dict` 读 `d.get("memory")` 并深拷贝。batch 配置与既有 interact 配置的 `to_dict` 逐字节不变。
  - 校验原因字面量(后续任务的测试按它们断言):`"memory must be a dict"`、`"memory must have exactly the keys ['digests', 'runtime', 'seed']"`、`"memory.seed must be null or a non-empty str"`、`"memory.digests must be a dict of str to non-empty str"`、`"memory.runtime must be a dict"`、`"memory must be JSON serializable"`、`"memory is only allowed in interact mode"`、`"memory.digests and memory.runtime must be filled together"`、`"memory.digests must name exactly the bound personas"`、`"memory.runtime must have exactly the keys ['backend', 'embedding', 'extraction', 'starling_version', 'trace_schema']"`。
  - `tests/memory_fixtures.py`:`STAND_IN_RUNTIME: dict`(5.2 节替身身份,见下)、`unresolved_memory(seed: str | None = None) -> dict`、`resolved_memory(persona_ids, *, seed: str | None = None, digest: str = "d0") -> dict`、`fake_memory_resolver(seed, persona_ids) -> tuple[dict[str, str], dict]`(返回 `{p: f"digest-{p}"}` 与 `STAND_IN_RUNTIME` 的拷贝,Task 25 起派发器测试注入它)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/memory_fixtures.py
from __future__ import annotations

STAND_IN_RUNTIME = {
    "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
    "trace_schema": "actio.trace.v2",
}

def unresolved_memory(seed=None) -> dict:
    return {"seed": seed, "digests": {}, "runtime": {}}

def resolved_memory(persona_ids, *, seed=None, digest="d0") -> dict:
    return {"seed": seed, "digests": {p: digest for p in persona_ids}, "runtime": dict(STAND_IN_RUNTIME)}

def fake_memory_resolver(seed, persona_ids):
    return {p: f"digest-{p}" for p in persona_ids}, dict(STAND_IN_RUNTIME)
```

```python
# tests/test_mt_config.py
from memory_fixtures import STAND_IN_RUNTIME, resolved_memory, unresolved_memory

def test_memory_is_written_only_when_present_so_batch_configs_stay_byte_identical():
    assert "memory" not in _batch().to_dict()
    assert TrainingConfig.from_dict(_batch().to_dict()).memory is None
    mem = resolved_memory(["pa", "pb"])
    cfg = _interact(memory=mem)
    assert cfg.to_dict()["memory"] == mem
    assert TrainingConfig.from_dict(cfg.to_dict()).memory == mem

def test_to_dict_copies_memory_so_a_caller_cannot_mutate_the_config():
    cfg = _interact(memory=resolved_memory(["pa", "pb"]))
    cfg.to_dict()["memory"]["digests"]["pa"] = "x"
    assert cfg.memory["digests"]["pa"] == "d0"

@pytest.mark.parametrize("memory, needle", [
    ("seed-1", "memory must be a dict"),
    ({"seed": None, "digests": {}}, "memory must have exactly the keys"),
    ({"seed": None, "digests": {}, "runtime": {}, "extra": 1}, "memory must have exactly the keys"),
    ({"seed": "", "digests": {}, "runtime": {}}, "memory.seed must be null or a non-empty str"),
    ({"seed": 3, "digests": {}, "runtime": {}}, "memory.seed must be null or a non-empty str"),
    ({"seed": None, "digests": [], "runtime": {}}, "memory.digests must be a dict of str to non-empty str"),
    ({"seed": None, "digests": {"pa": ""}, "runtime": {}}, "memory.digests must be a dict of str to non-empty str"),
    ({"seed": None, "digests": {}, "runtime": []}, "memory.runtime must be a dict"),
    ({"seed": None, "digests": {}, "runtime": {"x": object()}}, "memory must be JSON serializable"),
])
def test_a_malformed_memory_section_is_named_not_raised(memory, needle):
    reason = validate_training_config(_interact(memory=memory))
    assert reason is not None and needle in reason

def test_an_unserializable_memory_is_named_by_config_hash():
    with pytest.raises(InvalidTrainingConfigError, match="memory must be JSON serializable"):
        config_hash(_interact(memory={"seed": None, "digests": {}, "runtime": {"x": object()}}))

def test_a_batch_config_must_not_carry_memory():
    assert validate_training_config(_batch(memory=unresolved_memory())) == "memory is only allowed in interact mode"

def test_an_unresolved_memory_section_passes_the_strict_and_the_lenient_check():
    # 调度器写进配置时 digests 与 runtime 留空;点名路在构造期走严格口径,所以严格口径也要放行未解析的段
    assert validate_training_config(_interact(memory=unresolved_memory())) is None
    assert validate_pending_interact(_pending(memory=unresolved_memory("seed-1"))) is None

def test_a_resolved_memory_section_that_matches_the_bindings_passes():
    assert validate_training_config(_interact(memory=resolved_memory(["pa", "pb"]))) is None

@pytest.mark.parametrize("memory, needle", [
    ({"seed": None, "digests": {"pa": "d", "pb": "d"}, "runtime": {}},
     "memory.digests and memory.runtime must be filled together"),
    ({"seed": None, "digests": {}, "runtime": dict(STAND_IN_RUNTIME)},
     "memory.digests and memory.runtime must be filled together"),
    ({"seed": None, "digests": {"pa": "d"}, "runtime": dict(STAND_IN_RUNTIME)},
     "memory.digests must name exactly the bound personas"),
    ({"seed": None, "digests": {"pa": "d", "pb": "d", "px": "d"}, "runtime": dict(STAND_IN_RUNTIME)},
     "memory.digests must name exactly the bound personas"),
    ({"seed": None, "digests": {"pa": "d", "pb": "d"}, "runtime": {"backend": "stand-in"}},
     "memory.runtime must have exactly the keys"),
])
def test_a_resolved_memory_section_must_match_the_bindings(memory, needle):
    reason = validate_training_config(_interact(memory=memory))
    assert reason is not None and needle in reason

@pytest.mark.parametrize("change", [
    lambda m: m["digests"].update(pa="d1"),
    lambda m: m["runtime"].update(backend="starling"),
    lambda m: m["runtime"].update(starling_version="0.3.0+abc"),
    lambda m: m["runtime"].update(embedding={"model": "e2"}),
    lambda m: m.update(seed="seed-1"),
], ids=["digest", "backend", "starling_version", "embedding", "seed"])
def test_config_hash_changes_when_the_starting_memory_or_its_runtime_changes(change):
    before = resolved_memory(["pa", "pb"])
    after = copy.deepcopy(before)
    change(after)
    assert config_hash(_interact(memory=before)) != config_hash(_interact(memory=after))
```

(`test_mt_config.py` 若还没 `import copy` 就加上。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_config.py -q -p no:cacheprovider -k "memory"`
Expected: FAIL,`TypeError: TrainingConfig.__init__() got an unexpected keyword argument 'memory'`

- [ ] **Step 3: 实现**

- `TrainingConfig` 在 `max_resume_attempts` 之后加 `memory: dict | None = None`。`to_dict` 末尾 `if self.memory is not None: out["memory"] = copy.deepcopy(self.memory)`(先把字面量字典赋给 `out` 再返回);`from_dict` 加 `memory=copy.deepcopy(d.get("memory"))`。
- `_check_memory_types(memory) -> str | None`:`None` 直接放行;依次查 dict、键集合恰为 `{"seed", "digests", "runtime"}`、`seed`、`digests`(键是 str、值是非空 str)、`runtime` 是 dict,最后 `_canonical_json(memory)` 失败时返回 `f"memory must be JSON serializable: {exc}"`。在 `_check_types` 里 `_check_bindings` 之后、整份 `_canonical_json(cfg.to_dict())` 兜底之前调用,所以 `config_hash` 的异常信息点名 `memory`。
- `validate_training_config`:`_validate_common` 通过后,`mode == "batch"` 且 `memory is not None` 返回 `"memory is only allowed in interact mode"`。
- `_validate_interact` 末尾(绑定检查之后)加语义检查,`memory is None` 在本任务仍放行(Task 28 改成必填):`bool(digests) != bool(runtime)` 报"必须一起填";`digests` 非空时键集合要等于 `{b.persona_id for b in cfg.bindings}`;`runtime` 非空时键集合要等于 5.2 节的五个键。放在 `_validate_interact` 里,所以 `validate_pending_interact` 的占位探针也跑同样的检查;生成路 pending 时人格是空串,`digests` 只能为空,这正是宽口径。
- `is_pending_interact` 不变:它只看情境与人格,与 `memory` 无关。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_config.py tests/test_mt_orchestrator.py tests/test_mt_dispatch.py tests/test_ts_directive.py tests/test_ts_resolve.py -q -p no:cacheprovider`
Expected: PASS(`test_config_hash_keeps_the_formula_existing_records_were_hashed_with` 的 batch 与 interact 两例都仍过:`_interact()` 此时不带 `memory`)

- [ ] **Step 5: 提交**

```bash
git add src/mindtrainer/training_config.py tests/memory_fixtures.py tests/test_mt_config.py
git commit -m "feat(mindtrainer): 训练配置可带 memory 段,形状校验并进 config_hash"
```

---
### Task 25: 派发器注入 `memory_resolver`,进策略上下文前解出起点摘要与运行时身份

**Files:**
- Modify: `src/mindtrainer/dispatch.py`(`make_mindtrainer_dispatcher` 的签名与 `dispatch` 里 interact 分支,约 323-366 行;新增 `_resolve_memory`;`_policy_backend` 加 `memory` 块)
- Test: `tests/test_mt_dispatch.py`
- 随本任务改的既有测试(`policy_backend` 逐字段相等的断言多出 `memory` 键):`tests/test_mt_dispatch.py` 的 `_interact_directive`(约 619 行,字段加 `memory=unresolved_memory()`)、`_run_interact`(约 808 行,派发器加 `memory_resolver=fake_memory_resolver`)、`test_an_interact_run_records_which_backend_produced_its_data`(约 818 行)与 `test_the_backend_has_one_opponent_entry_per_non_trained_slot_and_none_for_a_missing_source`(约 960 行,`TrainingConfig(..., memory=resolved_memory(["pa", "pb"]))`);`tests/test_mt_dispatch_synthesis.py` 里断言整份 `policy_backend` 的用例(约 532 行)同样给指令加 `unresolved_memory()`、给派发器加 `fake_memory_resolver`、期望里加 `memory`。

**Interfaces:**
- Consumes: Task 24 的 `TrainingConfig.memory` 与校验原因;`tests/memory_fixtures.py` 的 `unresolved_memory`、`resolved_memory`、`fake_memory_resolver`、`STAND_IN_RUNTIME`。
- Produces:
  - `make_mindtrainer_dispatcher(*, ..., policy_endpoint=None, memory_resolver: Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]] | None = None) -> Dispatcher`。
  - `_resolve_memory(run_id: str, cfg: TrainingConfig, memory_resolver) -> TrainingConfig`:入参人格是 `tuple(dict.fromkeys(b.persona_id for b in cfg.bindings))`(绑定顺序、去重);返回 `dataclasses.replace(cfg, memory={"seed": cfg.memory["seed"], "digests": dict(digests), "runtime": copy.deepcopy(runtime)})`。
  - 失败原因字面量:没注入解析器 `f"directive {run_id}: interact mode needs a memory_resolver; start factory run or factory reconcile with --interact to run interact directives"`;解析器抛 `DirectiveFailedError` 时 `f"directive {run_id}: {exc.reason}"`;抛其余任何异常时 `f"directive {run_id}: memory resolver failed: {type(exc).__name__}"`。三者都是 T2。
  - `_policy_backend(cfg, endpoint)` 的返回多一个键 `"memory": copy.deepcopy(cfg.memory)`(回写后的那份,含 `digests` 与 `runtime`)。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_mt_dispatch.py
from memory_fixtures import STAND_IN_RUNTIME, fake_memory_resolver, unresolved_memory

def _recording_resolver(trace, digests=None):
    def resolve(seed, persona_ids):
        trace.append(("resolve", seed, persona_ids))
        return (digests if digests is not None else {p: f"digest-{p}" for p in persona_ids}), dict(STAND_IN_RUNTIME)
    return resolve

def test_the_memory_is_resolved_before_the_policy_context_is_entered_and_recorded(store, live):
    _interact_directive(store, memory=unresolved_memory())
    trace, seen = [], []
    def factory(run_id, cfg):
        seen.append(cfg.memory)
        return _Scope(trace, ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8}))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=factory, memory_resolver=_recording_resolver(trace)))
    assert trace[0] == ("resolve", None, ("pa", "pb")) and trace[1] == "enter"
    resolved = {"seed": None, "digests": {"pa": "digest-pa", "pb": "digest-pb"}, "runtime": STAND_IN_RUNTIME}
    assert seen == [resolved]
    assert _record(store).payload["policy_backend"]["memory"] == resolved
    # 解出的值只进这次派发的配置与记录,指令原样不动(续训时重新解)
    directive = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert directive.payload["training_config"]["memory"] == unresolved_memory()

def test_the_resolved_memory_is_part_of_the_config_hash(store, live):
    _interact_directive(store, memory=unresolved_memory())
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
            executor_factory=lambda run_id: MockExecutor(crash_at_step=1), client_factory=live().client_factory,
            policy=ScriptedBackend({"a": ["agreed", "x"] * 16}), memory_resolver=fake_memory_resolver))
    assert _record(store).status == "pending"
    changed = _recording_resolver([], digests={"pa": "digest-pa", "pb": "other"})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({"a": ["agreed", "x"] * 16}),
        memory_resolver=changed))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T3"
    assert "config changed" in env.payload["failure"]["reason"]

def test_an_interact_directive_without_a_memory_resolver_fails_naming_the_interact_switch(store, live):
    _interact_directive(store, memory=unresolved_memory())
    trace = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=lambda run_id, cfg: _Scope(trace, None)))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert "memory_resolver" in env.payload["failure"]["reason"] and "--interact" in env.payload["failure"]["reason"]
    assert trace == [] and not store.exists(f"{run_dir(RUN)}/training_run.yaml")

def test_a_resolver_exception_fails_the_directive_with_its_class_name_only(store, live):
    def broken(seed, persona_ids):
        raise RuntimeError("cannot open https://memory.example/secret")
    _interact_directive(store, memory=unresolved_memory())
    trace = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=lambda run_id, cfg: _Scope(trace, None),
        memory_resolver=broken))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    reason = env.payload["failure"]["reason"]
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert "memory resolver failed: RuntimeError" in reason and "memory.example" not in reason
    assert trace == []

def test_a_resolver_that_refuses_with_a_reason_keeps_that_reason(store, live):
    def refuses(seed, persona_ids):
        raise DirectiveFailedError("memory.seed 'seed-1' needs a memory config")
    _interact_directive(store, memory=unresolved_memory("seed-1"))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({}), memory_resolver=refuses))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and "memory.seed 'seed-1' needs a memory config" in env.payload["failure"]["reason"]

def test_a_resolver_that_misses_a_persona_fails_the_directive(store, live):
    _interact_directive(store, memory=unresolved_memory())
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({}),
        memory_resolver=_recording_resolver([], digests={"pa": "digest-pa"})))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and "memory.digests must name exactly the bound personas" in env.payload["failure"]["reason"]

def test_a_batch_directive_never_calls_the_resolver(store):
    trace = []
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(memory_resolver=_recording_resolver(trace)))
    assert trace == [] and store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
```

(`DirectiveFailedError` 从 `controlplane.daemon` 导入;`_Scope`、`_record`、`live`、`_interact_directive` 是本文件现有的辅助。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_dispatch.py -q -p no:cacheprovider -k "memory or resolver"`
Expected: FAIL,`TypeError: make_mindtrainer_dispatcher() got an unexpected keyword argument 'memory_resolver'`

- [ ] **Step 3: 实现**

- 签名加 `memory_resolver`(keyword-only,放在 `policy_endpoint` 之后),docstring 的 interact 一条补一句"另需 memory_resolver,进策略上下文之前解出起点摘要与运行时身份"。
- `dispatch` 的 interact 分支:"没有 client_factory 或策略"那条检查之后、`_synthesize_missing` 之前,`cfg.memory is not None and memory_resolver is None` 判上面的 T2(本任务带 `is not None` 守卫,让还没带 `memory` 的旧夹具照旧跑;Task 28 让 interact 必带 `memory` 后去掉守卫)。`_batch_manifests` 之后、`try:` 与 `ExitStack` 之前,`cfg.memory is not None` 时 `cfg = _resolve_memory(run_id, cfg, memory_resolver)`。这一位置同时满足三件事:清单缺失与 client_factory 缺失先判(不白跑解析器);解析在策略上下文(起 vLLM、占租约)之前;`resume_or_run` 里的 `config_hash` 在解析之后算,续训时重新解出的摘要或身份一变就是 T3。
- `_resolve_memory`:`try: digests, runtime = memory_resolver(seed, persona_ids)`;`except DirectiveFailedError as exc` 按 `exc.reason` 重抛(组合根用它给出可读原因,例如替身拒绝种子);`except Exception as exc` 只报类名(设计 10 节:起点库打不开、摘要算不出,原因只报类名)。回写后再跑一次 `validate_training_config`,不过判 T2(解析器给的摘要缺人格、身份缺键都在这里挡下)。
- 解出的值**不写回 `directive.yaml`**:它只进本次派发的 `cfg`(于是进 `config_hash`、`policy_factory(run_id, cfg)`、`_interact_sources` 与 `training_run.policy_backend`)。设计要求续训时重新算,写回指令只会多一笔提交,还会在 T3 那一路把被比较的原值盖掉;记录里的 `policy_backend.memory` 是第一次尝试解出的值,读者从记录就能看到记忆从哪来。
- `_policy_backend` 加 `"memory": copy.deepcopy(cfg.memory)`,docstring 补一句。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_dispatch.py tests/test_mt_dispatch_synthesis.py tests/test_mt_orchestrator.py tests/test_mt_training_run.py tests/test_factory_interact_wire.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/mindtrainer/dispatch.py tests/test_mt_dispatch.py tests/test_mt_dispatch_synthesis.py
git commit -m "feat(mindtrainer): 派发器经注入的 memory_resolver 解出起点记忆,进哈希与训练记录"
```

---
### Task 26: 组合根的替身 `memory_resolver`,`--interact` 装配时注入

**Files:**
- Create: `src/factory/memory_runtime.py`(替身运行时身份与替身解析器;计划 B 第 7 步在同一模块按 `RISMFORGE_MEMORY_CONFIG` 加 Starling 那一路)
- Modify: `src/factory/wire.py`(`_interact_wiring` 的返回字典加 `memory_resolver`,并写启动日志;模块级 `_log = logging.getLogger("factory.wire")`)
- Test: `tests/test_factory_memory_runtime.py`(新)、`tests/test_factory_interact_wire.py`

**Interfaces:**
- Consumes: 第 1 步 `actio.memory.TupleMemory`(`TupleMemory().digest()` 是空库摘要,`FileTupleStore` 对没有文件的人格给的也是它);Task 25 的 `make_mindtrainer_dispatcher(..., memory_resolver=...)` 与"解析器抛 `DirectiveFailedError` 时保留其 `reason`"。
- Produces:
  - `factory.memory_runtime.STAND_IN_MEMORY_RUNTIME: dict = {"backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None, "trace_schema": "actio.trace.v2"}`(`trace_schema` 取第 1 步的 `actio.trace.TRACE_SCHEMA_V2`,不另写字面量)。
  - `factory.memory_runtime.stand_in_memory_resolver(seed: str | None, persona_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]`:`seed is None` 时返回 `({p: TupleMemory().digest() for p in persona_ids}, copy.deepcopy(STAND_IN_MEMORY_RUNTIME))`;`seed` 非 null 抛 `DirectiveFailedError(f"memory.seed {seed!r} needs a memory config: set RISMFORGE_MEMORY_CONFIG (the stand-in memory has no seed libraries)")`。
  - `_interact_wiring(...)` 返回 `{"policy_factory": ..., "policy_endpoint": ..., "memory_resolver": stand_in_memory_resolver}`;装配时 `_log.info("memory backend: stand-in")`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_memory_runtime.py
import pytest

from actio.memory import TupleMemory
from controlplane.daemon import DirectiveFailedError
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME, stand_in_memory_resolver
from memory_fixtures import STAND_IN_RUNTIME  # Task 24 的测试夹具先于本模块存在,这里钉住两份相等


def test_the_stand_in_runtime_identity_is_the_one_the_design_fixes():
    assert STAND_IN_MEMORY_RUNTIME == STAND_IN_RUNTIME
    assert STAND_IN_MEMORY_RUNTIME == {
        "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
        "trace_schema": "actio.trace.v2",
    }

def test_every_persona_starts_from_the_empty_stand_in_library():
    digests, runtime = stand_in_memory_resolver(None, ("pa", "pb"))
    assert digests == {"pa": TupleMemory().digest(), "pb": TupleMemory().digest()}
    assert runtime == STAND_IN_MEMORY_RUNTIME

def test_the_returned_identity_is_a_copy():
    _, runtime = stand_in_memory_resolver(None, ("pa",))
    runtime["backend"] = "x"
    assert STAND_IN_MEMORY_RUNTIME["backend"] == "stand-in"

def test_a_seed_needs_a_memory_config():
    with pytest.raises(DirectiveFailedError, match="needs a memory config") as info:
        stand_in_memory_resolver("seed-1", ("pa",))
    assert "RISMFORGE_MEMORY_CONFIG" in info.value.reason and info.value.category == "T2"
```

```python
# tests/test_factory_interact_wire.py
from actio.memory import TupleMemory
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME
from memory_fixtures import unresolved_memory

def test_the_assembled_dispatcher_records_the_stand_in_memory(world, tmp_path, stub, fake_serve):
    _write_directive(world, memory=unresolved_memory())
    _run(world, tmp_path)
    assert _read(world, "directive").status == "executed"
    empty = TupleMemory().digest()
    assert _read(world, "training_run").payload["policy_backend"]["memory"] == {
        "seed": None, "digests": {"pa": empty, "pb": empty}, "runtime": STAND_IN_MEMORY_RUNTIME,
    }

def test_a_seeded_interact_directive_fails_without_a_memory_config(world, tmp_path, fake_serve):
    _write_directive(world, memory=unresolved_memory("seed-1"))
    _run(world, tmp_path)
    env = _read(world, "directive")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert "needs a memory config" in env.payload["failure"]["reason"]
    assert fake_serve.calls == []                    # 解析在策略上下文之前:没起服务
    assert not world.exists(f"{run_dir(RUN)}/training_run.yaml")

def test_the_interact_assembly_logs_the_stand_in_memory_backend(world, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="factory.wire"):
        assemble(world, FactoryOptions(interact=True, endpoint="gpu-2"), env=_env(tmp_path)).close()
    assert "memory backend: stand-in" in caplog.text

def test_an_assembly_without_interact_does_not_log_a_memory_backend(world, caplog):
    with caplog.at_level(logging.INFO, logger="factory.wire"):
        assemble(world, FactoryOptions()).close()
    assert "memory backend" not in caplog.text
```

(`run_dir`、`RUN`、`_read`、`_run`、`_env`、`_write_directive`、`world`、`stub`、`fake_serve` 是该文件现有的辅助与夹具;`logging` 已导入。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py -q -p no:cacheprovider -k "memory"`
Expected: FAIL,`ModuleNotFoundError: No module named 'factory.memory_runtime'`

- [ ] **Step 3: 实现**

- `src/factory/memory_runtime.py`:首条语句 `from __future__ import annotations`;导入 `copy`、`actio.memory.TupleMemory`、`actio.trace.TRACE_SCHEMA_V2`、`controlplane.daemon.DirectiveFailedError`。按 Produces 写两个名字。抛 `DirectiveFailedError` 而不是普通异常,是因为 Task 25 对普通异常只报类名,而 7.1 节要求原因写明"种子需要记忆配置";原因里只有种子 id 与变量名,不含路径。
- `wire.py` 的 `_interact_wiring`:在 `make_policy_factory(...)` 之后惰性导入 `from factory.memory_runtime import stand_in_memory_resolver`(与本函数其余惰性导入同一做法),返回字典加 `"memory_resolver": stand_in_memory_resolver`,返回前 `_log.info("memory backend: stand-in")`。`_dispatcher` 已经把 `**wiring` 原样交给 `make_mindtrainer_dispatcher`,两个装配点(`--interact` 单独用、与 `--enable` 同开)都经过 `_interact_wiring`,不用另改。不走 `--interact` 的三处 `make_mindtrainer_dispatcher(...)`(`wire.py` 约 231、326 行与 `mindtrainer/wire.py:32`)不注入:那里 interact 指令先因缺 `client_factory` 判 T2,原因已点名 `--interact`。
- 启动日志用 `logging` 的 INFO,与 `factory.agent`、`controlplane.daemon` 同一做法。`RISMFORGE_MEMORY_CONFIG` 的判定在计划 B 第 7 步才有;本任务无条件用替身。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_factory_wire.py tests/test_factory_cross_plan.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/factory/wire.py tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py
git commit -m "feat(factory): --interact 装配注入替身记忆解析器,启动日志写明记忆后端"
```

---
### Task 27: 调度器的 interact 段必填 `memory: {seed}`,`resolve` 把种子带进配置

**Files:**
- Modify: `src/trainscheduler/config.py`(`InteractDefaults` 字段与 `to_dict`;`_INTERACT_REQUIRED`;`_parse_interact` 加 `memory` 的解析;`check_perturbable_key` 的锚定字段元组)
- Modify: `src/trainscheduler/resolve.py`(`_interact_config`,约 122-141 行)
- Modify: `src/factory/interact_checks.py`(`check_interact_config` 里"没有 interact 段"的提示,把 `memory` 列进必填键)
- Test: `tests/test_ts_config.py`、`tests/test_ts_resolve.py`
- 随本任务改的既有测试夹具(interact 段从此缺 `memory` 就解析失败,每个都加 `"memory": {"seed": None}`):`tests/test_ts_config.py` 的 `_defaults_with_interact`(约 215 行)与 `test_interact_section_parses_two_sources_and_rejects_halves` 里的 `full`(约 235 行);`tests/test_ts_resolve.py` 的 `_named_section`(约 222 行,`_generated_section` 由它派生);`tests/test_ts_directive.py` 的 `_interact_section`(约 234 行);`tests/test_ts_plan_handler.py` 的 `_allow_interact_with_section`(约 114 行);`tests/test_factory_interact_wire.py` 的 `_interact_defaults`(约 360 行);`tests/test_factory_interact_e2e.py` 约 55 行的 `payload["interact"]`。仓库里没有入库的 `training_defaults.yaml` 样例文件(`default_training_defaults_payload()` 不带 interact 段);`docs/design/70-trainscheduler.md` 约 237 行的 YAML 样例留给第 8 步。

**Interfaces:**
- Consumes: Task 24 的 `TrainingConfig.memory` 与"未解析的段 `{"seed": ..., "digests": {}, "runtime": {}}` 过严格与宽口径校验";Task 26 的组合根注入(装配出的派发器从此会拿到带 `memory` 的新指令)。
- Produces:
  - `InteractDefaults.memory_seed: str | None = None`(放在 `data_spec` 之后);`to_dict()` 恒写 `"memory": {"seed": self.memory_seed}`。
  - `_INTERACT_REQUIRED = ("trained_slot", "slots", "max_turns_per_episode", "memory")`;解析原因字面量:`f"{label}.memory must be a mapping, got {type}"`、`f"{label}.memory: missing key(s) [...], unknown key(s) [...]"`(只许 `seed` 一个键)、`f"{label}.memory.seed must be null or a non-empty str"`。
  - `resolve._interact_config` 两条路都写 `cfg["memory"] = {"seed": section.memory_seed, "digests": {}, "runtime": {}}`。
  - `check_perturbable_key("memory")` 返回 `"perturbable key 'memory': 'memory' anchors the run and is never perturbable"`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ts_config.py
@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s["interact"].pop("memory"), "missing key\\(s\\) \\['memory'\\]"),
    (lambda s: s["interact"].update(memory=None), "memory must be a mapping"),
    (lambda s: s["interact"].update(memory={}), "memory: missing key\\(s\\) \\['seed'\\]"),
    (lambda s: s["interact"].update(memory={"seed": None, "digests": {}}), "memory: missing key\\(s\\) \\[\\], unknown key\\(s\\)"),
    (lambda s: s["interact"].update(memory={"seed": ""}), "memory.seed must be null or a non-empty str"),
    (lambda s: s["interact"].update(memory={"seed": 7}), "memory.seed must be null or a non-empty str"),
])
def test_interact_section_requires_a_memory_seed(mutate, reason):
    raw = _defaults_with_interact()
    mutate(raw)
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)

@pytest.mark.parametrize("seed", [None, "seed-1"])
def test_the_memory_seed_parses_and_round_trips(seed):
    raw = _defaults_with_interact()
    raw["interact"]["memory"] = {"seed": seed}
    d = parse_training_defaults(raw)
    assert d.interact.memory_seed == seed
    assert d.to_dict() == raw and parse_training_defaults(d.to_dict()) == d

def test_memory_anchors_the_run_and_is_never_perturbable():
    assert check_perturbable_key("memory") == "perturbable key 'memory': 'memory' anchors the run and is never perturbable"
```

```python
# tests/test_ts_resolve.py
@pytest.mark.parametrize("make", [_named_section, _generated_section], ids=["named", "generated"])
@pytest.mark.parametrize("seed", [None, "seed-1"])
def test_new_interact_carries_the_memory_seed_and_leaves_the_rest_to_the_dispatcher(store, make, seed):
    section = make()
    section["memory"] = {"seed": seed}
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section))
    assert r.training_config["memory"] == {"seed": seed, "digests": {}, "runtime": {}}

def test_a_new_batch_round_has_no_memory(store):
    r = resolve_action(store, _d(), config=ALLOW, defaults=DEFAULTS)
    assert "memory" not in r.training_config
```

(`test_a_named_path_config_passes_the_strict_validation` 不改,它现在也覆盖"带未解析 `memory` 的点名路配置过严格校验"。)

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_config.py tests/test_ts_resolve.py -q -p no:cacheprovider -k "memory"`
Expected: FAIL,`Failed: DID NOT RAISE`(缺 `memory` 的段仍被接受)与 `AttributeError: 'InteractDefaults' object has no attribute 'memory_seed'`

- [ ] **Step 3: 实现**

- `_parse_interact`:必填键检查通过后、来源二选一之前解析 `memory`:不是 dict 报"must be a mapping";键集合不是恰好 `{"seed"}` 时按 `missing key(s) [...], unknown key(s) [...]` 的现有格式报;`seed` 既不是 `None` 也不是非空 str(`isinstance(seed, str) and seed.strip()`)报"must be null or a non-empty str"。两条 `return InteractDefaults(...)` 都传 `memory_seed=seed`。段里只有 `seed`:`digests` 与 `runtime` 是派发时算出来的,写在默认值里没有意义,出现即 unknown。
- `InteractDefaults.to_dict` 在 `max_turns_per_episode` 之后写 `"memory": {"seed": self.memory_seed}`。字段名用 `memory_seed` 而不是 `memory: dict`,因为 frozen dataclass 里放可变 dict 会破坏 `test_the_interact_dataclass_is_frozen` 与"不别名输入输出"两条既有约束。
- `check_perturbable_key` 的锚定元组加 `"memory"`:`_TOP_FIELDS` 由 `fields(TrainingConfig)` 自动派生,Task 24 加字段后 `memory` 已是合法扰动键;起点库换了就不是同一个实验,`digests` 与 `runtime` 由派发器写,扰动它们只会被覆盖。
- `resolve._interact_config`:在 `cfg["max_turns_per_episode"] = ...` 之后写 `cfg["memory"] = {...}`,两条路共用。`clone`/`replay` 不经过它:配方的 `training_config` 取自当时的指令,本设计之后的 interact 指令本来就带未解析的 `memory`(派发器不回写指令,见 Task 25),本设计之前的配方由 Task 28 的必填校验在 `check_training_config` 处拒绝。
- `interact_checks.check_interact_config` 的提示改为 `"(trained_slot, slots, max_turns_per_episode, memory and either scenario_ids with persona_ids or a data_spec)"`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_config.py tests/test_ts_resolve.py tests/test_ts_directive.py tests/test_ts_plan_handler.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_factory_preflight.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/config.py src/trainscheduler/resolve.py src/factory/interact_checks.py \
  tests/test_ts_config.py tests/test_ts_resolve.py tests/test_ts_directive.py tests/test_ts_plan_handler.py \
  tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py
git commit -m "feat(trainscheduler): interact 段必填 memory.seed,新 interact 配置带未解析的 memory"
```

---
### Task 28: interact 配置必带 `memory`;落地前的 pending interact run 在派发器判 T2(F26)

**Files:**
- Modify: `src/mindtrainer/training_config.py`(新增常量 `MISSING_MEMORY_REASON`;`_validate_interact` 开头加必填检查)
- Modify: `src/mindtrainer/dispatch.py`(`dispatch` 里 `load_config_from_directive` 之后、`_prepare_interact` 之前的早判;去掉 Task 25 给"缺解析器"那条判定加的 `cfg.memory is not None` 守卫与 `_resolve_memory` 前的同名守卫)
- Modify: `tests/memory_fixtures.py`(加 `interact_dispatcher(**kw)`)
- Test: `tests/test_mt_config.py`、`tests/test_mt_dispatch.py`、`tests/test_mt_dispatch_synthesis.py`、`tests/test_ts_resolve.py`、`tests/test_factory_interact_e2e.py`
- 随本任务改的既有测试夹具(interact `TrainingConfig` 从此缺 `memory` 就过不了校验):
  - 构造配置的辅助加 `memory=unresolved_memory()`(字典形状的加 `"memory": unresolved_memory()`):`tests/test_mt_config.py` 的 `_interact`(约 46 行;`_pending` 由它派生,未解析的段对占位人格也成立);`tests/test_mt_trajectory_interact.py` 的 `_cfg`(约 43 行,`InteractTrajectorySource` 构造时 `ensure_valid_config`);`tests/test_mt_orchestrator.py` 的 `_interact_cfg`(约 491 行);`tests/test_mt_dispatch.py` 约 159、491、512、531、737 行的五处 `_cfg_dict(mode="interact", ...)`(619 行的 `_interact_directive` 与 965 行已在 Task 25 改过);`tests/test_mt_dispatch_synthesis.py` 约 330、351 行的两处与 `_interact_cfg`(约 383 行);`tests/test_factory_interact_wire.py` 的 `_cfg_dict`(约 409 行;Task 26 的两条用例里显式传的 `memory=` 保留);`tests/test_ts_directive.py` 的 `_interact_cfg`(约 156 行);`tests/ts_fixtures.py` 的 `recipe_payload` 的 interact 分支(约 123 行,interact 配方被 clone/replay 时要过 `check_training_config`)。
  - 带 `client_factory=` 的派发器构造(`tests/test_mt_dispatch.py` 17 处、`tests/test_mt_dispatch_synthesis.py` 16 处,`grep -n 'client_factory=' ` 列出)改用 `interact_dispatcher(...)`,它在调用方没给时补 `memory_resolver=fake_memory_resolver`。Task 25 里显式传了解析器或故意不传的用例保持 `make_mindtrainer_dispatcher(...)` 不动。
  - 不需要改:`tests/test_mt_data_producer.py`(约 163、240 行)与 `tests/test_mt_data_spec_stages.py`(约 33 行)的 interact 配置只交给生产器与 `check_data_spec_against_config`,两者都不跑 `validate_training_config`;`tests/rs_fixtures.py` 的 interact 配方只过 recipestore 的"键在场"检查;`tests/test_ma_*.py` 的 `client_factory=` 是部署驱动器,不经派发器。
- `src/trainscheduler/resolve.py` 不改:本设计之前写下的 interact 配方被 clone/replay 时,`check_training_config` 自然以同一原因拒绝(见 Step 1 最后一条)。

**Interfaces:**
- Consumes: Task 24 的 `memory` 字段与校验;Task 25 的 `memory_resolver` 与 `_resolve_memory`;Task 27 让调度器写出的每条新 interact 配置都带 `memory`。
- Produces:
  - `mindtrainer.training_config.MISSING_MEMORY_REASON = "interact config has no memory section (written before memory support)"`;`validate_training_config` 与 `validate_pending_interact` 对 `mode == "interact"` 且 `memory is None` 的配置都返回它。
  - 派发器对这种指令抛 `DirectiveFailedError(f"directive {run_id}: {MISSING_MEMORY_REASON}")`(T2),发生在 `_prepare_interact`、解析器与 `config_hash` 之前。
  - `tests/memory_fixtures.py::interact_dispatcher(**kw) -> Dispatcher`:`kw.setdefault("memory_resolver", fake_memory_resolver)` 后转给 `make_mindtrainer_dispatcher`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_mt_config.py
from mindtrainer.training_config import MISSING_MEMORY_REASON

def test_an_interact_config_without_memory_is_refused_on_both_paths():
    assert validate_training_config(_interact(memory=None)) == MISSING_MEMORY_REASON
    assert validate_pending_interact(_pending(memory=None)) == MISSING_MEMORY_REASON
    assert MISSING_MEMORY_REASON == "interact config has no memory section (written before memory support)"

def test_a_batch_config_still_needs_no_memory():
    assert validate_training_config(_batch()) is None
```

```python
# tests/test_mt_dispatch.py
from mindtrainer.training_config import MISSING_MEMORY_REASON

def test_a_pending_interact_run_written_before_memory_support_fails_before_hashing(store, live):
    _interact_directive(store)
    raw = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    del raw.payload["training_config"]["memory"]
    store.write_product(f"{run_dir(RUN)}/directive.yaml", raw, "test: a directive from before memory support")
    calls = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({}),
        memory_resolver=lambda seed, ids: calls.append(ids)))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert env.payload["failure"]["reason"] == f"directive {RUN}: {MISSING_MEMORY_REASON}"
    assert calls == [] and not store.exists(f"{run_dir(RUN)}/training_run.yaml")

def test_a_dispatcher_without_a_resolver_now_refuses_every_interact_directive(store, live):
    _interact_directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({})))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and "memory_resolver" in env.payload["failure"]["reason"]
```

```python
# tests/test_mt_dispatch_synthesis.py
from mindtrainer.training_config import MISSING_MEMORY_REASON

def test_a_generated_interact_directive_from_before_memory_support_writes_nothing_back(store):
    cfg = _interact_cfg()
    del cfg["memory"]
    _directive(store, cfg=cfg)                                     # data_spec 缺省就是本文件的 SPEC
    before = _commit_count(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and MISSING_MEMORY_REASON in env.payload["failure"]["reason"]
    assert env.payload["training_config"]["scenario_ids"] == []      # 判在回写之前:情境与人格没有生成
    assert _commit_count(store) == before + 1                        # 只有判 failed 那一笔
```

(`_directive(store, *, cfg=None, data_spec=SPEC)` 与 `_interact_cfg()`(生成路回写前的形状,约 383 行)是本文件现有辅助;`_commit_count` 本文件没有,照 `tests/test_mt_dispatch.py:799` 的写法在本文件加一个同名辅助。`_interact_cfg` 在 Task 28 补了 `memory`,所以这里先 `del cfg["memory"]`。)

```python
# tests/test_ts_resolve.py
def test_cloning_an_interact_recipe_from_before_memory_support_is_refused(store):
    payload = recipe_payload("run-004", recipe_type="interact")
    del payload["training_config"]["memory"]
    write_recipe(store, "run-004", payload=payload)
    with pytest.raises(ResolveError, match="no memory section"):
        resolve_action(store, _d("replay", source="recipe-run-004"), config=ALLOW, defaults=DEFAULTS)
```

```python
# tests/test_factory_interact_e2e.py,约 157 行 `assert persona_ids == [...]` 之后补两条(第 11 节端到端:记录里有 memory 块;`backend` 在约 143 行已取出)
    assert backend["memory"]["runtime"]["backend"] == "stand-in"
    assert set(backend["memory"]["digests"]) == set(persona_ids)    # persona_ids 是本用例约 156 行从回写后的指令读出的
```



- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_config.py tests/test_mt_dispatch.py tests/test_mt_dispatch_synthesis.py tests/test_ts_resolve.py -q -p no:cacheprovider -k "memory or resolver"`
Expected: FAIL,`ImportError: cannot import name 'MISSING_MEMORY_REASON'`

- [ ] **Step 3: 实现**

- `training_config.py`:模块级 `MISSING_MEMORY_REASON`;`_validate_interact` 第一行 `if cfg.memory is None: return MISSING_MEMORY_REASON`,先于情境检查,所以宽口径的占位探针(保留了 `memory`)给出同一原因,指令构造期(`check_directive`)与派发期结论一致。
- `dispatch.py`:`cfg = load_config_from_directive(directive)` 之后紧接 `if cfg.mode == "interact" and cfg.memory is None: raise DirectiveFailedError(f"directive {run_id}: {MISSING_MEMORY_REASON}")`。放在 `_prepare_interact` 之前,是为了生成路的旧指令不先合成、落树、回写情境与人格再判失败;紧随其后的 `validate_training_config` 也会给出同一原因,早判只是让它不留副作用。缺解析器的判定与 `_resolve_memory` 去掉守卫,对每条 interact 指令生效。
- 夹具按 Files 列表逐个补。补完跑一次 `grep -rn 'mode="interact"\|"mode": "interact"' tests`,逐个确认要么已带 `memory`、要么在"不需要改"的清单里。

- [ ] **Step 4: 跑测试确认通过,并跑全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests -q -p no:cacheprovider`
Expected: PASS(本任务改的是 interact 配置的必填项,牵涉 mindtrainer、trainscheduler、factory 三个包的测试,跑全量)

- [ ] **Step 5: 提交**

```bash
git add src/mindtrainer/training_config.py src/mindtrainer/dispatch.py tests/memory_fixtures.py \
  tests/test_mt_config.py tests/test_mt_trajectory_interact.py tests/test_mt_orchestrator.py \
  tests/test_mt_dispatch.py tests/test_mt_dispatch_synthesis.py tests/test_factory_interact_wire.py \
  tests/test_factory_interact_e2e.py tests/test_ts_directive.py tests/test_ts_resolve.py tests/ts_fixtures.py
git commit -m "feat(mindtrainer): interact 配置必带 memory,落地前的 pending interact run 在派发器判 T2"
```

---

## 第 5 步:MindAgent 部署记忆

### Task 29: 审核类状态机加 `approved → rejected`,只许收录处理器走

**Files:**
- Modify: `src/controlplane/states.py`(`REVIEW_TRANSITIONS`,约 8-16 行)
- Test: `tests/test_states.py`

**Interfaces:**
- Consumes: 无
- Produces: `controlplane.states.can_transition("review", "approved", "rejected") is True`;Task 33 的收录处理器在抽取失败时走这条边

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_states.py 追加
def test_approved_can_be_rejected_only_by_the_admit_handler():
    # 设计二第 9 节"门② 失败"(F23):抽取失败的记忆提案 approved → rejected
    assert can_transition("review", "approved", "rejected")
    assert next_states("review", "approved") == {"effective", "rejected"}


def test_the_cli_table_deliberately_lacks_the_admit_only_edge():
    # CLI 的 cli/src/commands.ts 表是给人用的:人不能把已批准的提案改回拒绝,只有收录处理器能
    import re
    from pathlib import Path
    ts = (Path(__file__).resolve().parents[1] / "cli/src/commands.ts").read_text(encoding="utf-8")
    assert re.search(r'^\s*approved: \["effective"\],$', ts, re.M)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_states.py -q -p no:cacheprovider`
Expected: FAIL,`test_approved_can_be_rejected_only_by_the_admit_handler` 断言失败(`next_states` 只有 `{"effective"}`);第二个用例此时已通过,它钉住的是"CLI 表不跟着改"

- [ ] **Step 3: 实现**

`REVIEW_TRANSITIONS["approved"] = {"effective", "rejected"}`。上方注释在 spec §1.3 那两行后加一行:`approved → rejected 只许 mindagent.admit 在记忆抽取失败时走(设计二第 9 节,F23);cli/src/commands.ts 的 REVIEW_TRANSITIONS 有意不加,人不能把已批准的提案改回拒绝。`。`commands.ts` 第 6 行的"与 Python controlplane/states.py 保持一致"改为"与 Python 一侧相同,只少 approved → rejected 一条(那条只归收录处理器)"——这是注释,不改 TS 行为,所以不跑 vitest。

已核对:controlplane 与 cli 的测试里没有钉住两表逐项相同的用例(`grep -rn 'REVIEW_TRANSITIONS\|commands.ts' tests cli/test` 只命中无关注释),所以没有既有测试要改成"只差这一条边";上面第二个用例就是这条钉子。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_states.py tests/test_ma_admit.py tests/test_awaiting_confirmation.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/states.py cli/src/commands.ts tests/test_states.py
git commit -m "feat(controlplane): 审核类状态机加 approved → rejected,只归收录处理器"
```

---

### Task 30: `load_library` 不再读 `memory_library/`,它非空时装配期退出码 2

**Files:**
- Modify: `src/mindagent/library.py`(`LibraryState` 去掉 `memories`;`load_library` 删掉读 `memory_library/` 的循环;新增 `check_memory_library_empty`)
- Modify: `src/mindagent/rounds.py`(`_run.instance_with` 里 `memories=lib.memories` 暂改为 `memories=()`,Task 33 整段重写)
- Modify: `src/factory/handlers.py`(`_agent` 与 `admit_proposal` 两个构造器各加一次预检)
- Test: `tests/test_ma_config_library.py`、`tests/test_factory_handlers.py`
- 随本任务改的既有测试:`tests/test_ma_config_library.py` 约 83 行 `LibraryState(skills=(), memories=(), ...)` 去掉 `memories`;约 86-98 行 `test_load_library_reads_effective_entries_sorted_and_the_versions` 删掉 `lib.memories` 断言并改成"`memory_library/` 里有 effective 条目,`load_library` 也不读它";约 140-156 行参数化用例删掉 `memory-missing-fields` 一项(坏的记忆文件不再被读)

**Interfaces:**
- Consumes: 无
- Produces:
  - `mindagent.library.LibraryState(skills: tuple[Skill, ...], version: dict)`(没有 `memories`)
  - `mindagent.library.check_memory_library_empty(store: GitStore) -> None`:`store.list_products(f"{CONTROL_PLANE_ROOT}/memory_library")` 非空抛 `MindAgentDataError`,信息 `f"memory_library/ must be empty (design 2 §0, §9): found {n} file(s), e.g. {first}; move them out by hand"`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ma_config_library.py 追加
def test_load_library_no_longer_reads_the_memory_library(store):
    store.write_product(memory_file("mem-a"), _env("mem-a", "memory", "effective", _memory("mem-a")), "m")
    lib = load_library(store)
    assert not hasattr(lib, "memories")
    assert lib.version == {"skill": "0", "memory": "0"}


def test_a_non_empty_memory_library_is_a_data_error_naming_it(store):
    check_memory_library_empty(store)          # 空目录或不存在:通过
    store.write_product(memory_file("mem-a"), _env("mem-a", "memory", "effective", _memory("mem-a")), "m")
    with pytest.raises(MindAgentDataError, match=r"memory_library/ must be empty.*mem-a\.yaml"):
        check_memory_library_empty(store)
```

```python
# tests/test_factory_handlers.py 追加
@pytest.mark.parametrize("switch", ["agent", "admit"])
def test_a_non_empty_memory_library_exits_2_at_assembly(store, capsys, switch):
    from factory.cli import main
    rel = memory_file("mem-a")
    (store.repo_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (store.repo_root / rel).write_text("id: mem-a\n", encoding="utf-8")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", switch], env={}) == 2
    assert "memory_library/ must be empty" in capsys.readouterr().err
```

`tests/test_factory_handlers.py` 的 `store` 夹具是否已提交过初始树、`memory_file` 是否已导入,实施时先 `grep -n 'def store\|^from controlplane.paths' tests/test_factory_handlers.py` 确认;没导入就补。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_config_library.py tests/test_factory_handlers.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'check_memory_library_empty' from 'mindagent.library'`

- [ ] **Step 3: 实现**

`library.py`:删掉 `from actio.memory import MemoryEntry` 与读记忆的循环;docstring 改为只读 `skill_library/`,并写明设计二起记忆在树外的部署库(第 9 节"旧数据")。`check_memory_library_empty` 只列文件不读内容,所以坏文件也会被它点名。

`handlers.py`:`_agent` 开头与 `admit_proposal` 的构造器(由 `lambda ctx: handle_admit_proposal` 改成具名函数 `_admit_proposal(ctx)`,Task 35 再改它的返回值)开头各调 `preflight("memory_library/", lambda: check_memory_library_empty(ctx.store))`;`preflight` 把 `MindAgentDataError` 转成 `FactoryConfigError`,命令行退出码 2,树里什么都不写(第 10 节"`memory_library/` 非空")。在 `admit` 构造器也查,是因为门② 的记忆提案也改走部署库(Task 34),旧条目留在树里会被误当成已收录。

`rounds.py`:只改 `memories=lib.memories` 为 `memories=()`,让这一步能单独跑绿。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_config_library.py tests/test_factory_handlers.py tests/test_ma_rounds.py tests/test_ma_e2e.py tests/test_factory_e2e_loop.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/mindagent/library.py src/mindagent/rounds.py src/factory/handlers.py
git add tests/test_ma_config_library.py tests/test_factory_handlers.py
git commit -m "feat(mindagent): load_library 不再读 memory_library,它非空时装配期退出码 2"
```

---

### Task 31: 部署记忆的目录布局与人格库锁

**Files:**
- Create: `src/mindagent/memory_layout.py`
- Modify: `src/controlplane/lock.py`(新增公开的 `hold_file_lock`,复用模块里已有的 `_try_lock`/`_unlock`/`_BUSY`,`acquire_repo_lock` 不动)
- Test: `tests/test_ma_memory_layout.py`(新)

**Interfaces:**
- Consumes: 第 1 步 `actio.memory_port.PERSONA_ID_PATTERN`(`re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")`)、`actio.errors.ActioDataError`
- Produces:
  - `controlplane.lock.FileLockBusyError(Exception)`:`str(exc)` 是锁文件绝对路径
  - `controlplane.lock.hold_file_lock(path: Path) -> Iterator[None]`(`@contextmanager`):建父目录,`open(path, "a+b")`,非阻塞独占锁,被占抛 `FileLockBusyError`,退出时释放
  - `mindagent.memory_layout.MemoryLayout(root: Path, suffix: str = ".json")`,frozen dataclass;替身 `suffix=".json"`,计划 B 的 Starling 用 `".db"`。方法:
    - `deploy_lock(persona_id) -> Path`:`root/"deploy"/f"{persona_id}.lock"`
    - `snapshot(round_id, episode_id, persona_id) -> Path`:`root/"snapshots"/round_id/f"{episode_id}-{persona_id}{suffix}"`
    - `verify_snapshot(round_id, persona_id) -> Path`:`root/"snapshots"/round_id/f"verify-{persona_id}{suffix}"`
    - `verify_scratch(round_id) -> Path`:`root/"snapshots"/round_id/"verify"`(基线与候选各自的快照副本放它下面,轮次结束连目录删)
    - `round_snapshots(round_id) -> list[Path]`:该轮次目录下以 `deploy-` 起头、以 `suffix` 结尾的文件,按名排序
    - `uncommitted_snapshots(persona_id) -> list[Path]`:`snapshots/*/` 下名字以 `deploy-` 起头、以 `f"-{persona_id}{suffix}"` 结尾的文件
    - `persona_lock(persona_id) -> AbstractContextManager[None]`:`hold_file_lock(self.deploy_lock(persona_id))`
  - 每个收 `persona_id` 的方法先 `PERSONA_ID_PATTERN.fullmatch`,不合法抛 `ActioDataError(f"persona_id: {persona_id!r}")`;`round_id`、`episode_id` 不许含 `/` 且不许是 `.`、`..`,否则抛 `ValueError`(带轮次前缀的 `<round_id>/deploy-...` 只进事件 key,绝不拼路径)

替身下的根目录约定:`MemoryLayout(mem_root)` 与 `FileTupleStore(mem_root / "deploy")` 共用同一个 `mem_root`,于是部署库 `deploy/<persona_id>.json`、锁 `deploy/<persona_id>.lock`、起点副本 `snapshots/<round_id>/<episode_id>-<persona_id>.json`、验证快照 `snapshots/<round_id>/verify-<persona_id>.json`,与设计源 5.3 节的布局逐项对应,只是后缀是 `.json`。`FileTupleStore` 只读写 `<root>/<persona_id>.json`,同目录的 `.lock` 不干扰它。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ma_memory_layout.py
import pytest

from actio.errors import ActioDataError
from controlplane.lock import FileLockBusyError, hold_file_lock
from mindagent.memory_layout import MemoryLayout


def test_paths_follow_the_design_layout(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.deploy_lock("pa") == tmp_path / "deploy" / "pa.lock"
    assert lay.snapshot("agent-round-zengen-v1", "deploy-zengen-v1-s1-1", "pa") == \
        tmp_path / "snapshots" / "agent-round-zengen-v1" / "deploy-zengen-v1-s1-1-pa.json"
    assert lay.verify_snapshot("agent-round-zengen-v1", "pa") == \
        tmp_path / "snapshots" / "agent-round-zengen-v1" / "verify-pa.json"
    assert MemoryLayout(tmp_path, suffix=".db").snapshot("r", "deploy-m-s-1", "pa").name == "deploy-m-s-1-pa.db"


@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "../x", "x" * 129])
def test_persona_ids_are_checked_before_they_reach_a_path(tmp_path, bad):
    with pytest.raises(ActioDataError):
        MemoryLayout(tmp_path).deploy_lock(bad)


@pytest.mark.parametrize("bad", ["agent-round-v1/deploy-m-s-1", "..", "."])
def test_a_round_prefixed_episode_id_never_becomes_a_file_name(tmp_path, bad):
    with pytest.raises(ValueError):
        MemoryLayout(tmp_path).snapshot("r", bad, "pa")


def test_uncommitted_snapshots_ignore_verify_copies_and_other_personas(tmp_path):
    lay = MemoryLayout(tmp_path)
    for p in (lay.snapshot("r1", "deploy-m-s-1", "pa"), lay.snapshot("r2", "deploy-m-s-2", "pb"),
              lay.verify_snapshot("r1", "pa")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
    assert lay.uncommitted_snapshots("pa") == [lay.snapshot("r1", "deploy-m-s-1", "pa")]
    assert lay.round_snapshots("r1") == [lay.snapshot("r1", "deploy-m-s-1", "pa")]


def test_the_persona_lock_is_exclusive_and_released(tmp_path):
    lay = MemoryLayout(tmp_path)
    with lay.persona_lock("pa"):
        with pytest.raises(FileLockBusyError, match="pa.lock"):
            with hold_file_lock(lay.deploy_lock("pa")):
                pass
        with lay.persona_lock("pb"):
            pass
    with lay.persona_lock("pa"):
        pass
```

同一进程里对同一文件再开一个文件描述符取 `flock`,在 macOS 与 Linux 上都会被拒(`flock` 锁属于打开的文件描述,不属于进程),所以上面的同进程用例成立;实施时若在 CI 平台上不成立,改成子进程取锁,不改实现。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_memory_layout.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'FileLockBusyError' from 'controlplane.lock'`

- [ ] **Step 3: 实现**

`hold_file_lock` 与 `acquire_repo_lock` 同一写法(先 `_try_lock`,`_BUSY` 转成 `FileLockBusyError`,`finally` 里 `_unlock` 再 `close`),只是锁文件路径由调用方给、并先 `path.parent.mkdir(parents=True, exist_ok=True)`。锁文件本身不删:删了会让另一个进程在新 inode 上拿到"同一把"锁。

`memory_layout.py` 只导入标准库、`actio.memory_port.PERSONA_ID_PATTERN`、`actio.errors.ActioDataError` 与 `controlplane.lock.hold_file_lock`。`uncommitted_snapshots` 只按文件名匹配:人格 id 里允许 `-`,`deploy-m-s-1-x-pa.json` 对人格 `pa` 与 `x-pa` 都算命中。宁可多判:多判只让门② 多延后一次(Task 34),不会丢记忆。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_memory_layout.py tests/test_repo_lock.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/lock.py src/mindagent/memory_layout.py tests/test_ma_memory_layout.py
git commit -m "feat(mindagent): 部署记忆目录布局与人格库锁"
```

---

### Task 32: 部署驱动器按 episode 新造实例、起点副本即提交点、尝试间回滚

> 注意:Task 32 到 Task 35 合成一次提交(见 Task 35 的 Step 5)。前三个任务做完各自的 Step 4 先不提交。

**Files:**
- Modify: `src/mindagent/deployment.py`(`TRANSIENT_REASONS`,约 23-26 行;`_reusable`,约 70-78 行;`_attempt`,约 81-116 行,创建被拒那一支约 93 行;`run_deployment_episodes`,约 119-183 行)
- Modify: `src/mindagent/errors.py`(新增 `RestoreFailedError`)
- Test: `tests/test_ma_deployment.py`
- 随本任务改的既有测试(都在 `tests/test_ma_deployment.py`):`_instance` 改成实例工厂 `_make`;`_run` 改收 `make_instance` 与 `memory_store`;约 147、218、352 行三处直接 `instantiate(Recorder()/Exploding()/Broken(), memories=[], ...)` 改成各自的 `make_instance`;`_FakeClient.create_session` 加 `episode_id=None` 关键字并记进 `created`;约 179 行 `test_a_persisted_episode_from_another_library_version_is_regenerated` 按新规则拆成下面的 `test_a_memory_admission_alone_reuses_committed_episodes` 与 `test_a_new_skill_version_reruns_under_a_suffixed_id`。`live` 夹具的 `create_server` 工厂签名由第 2 步改,本任务不碰。`tests/test_interact_parity.py:106` 钉住两边瞬态集合相等;两边的 `role_memory_unavailable` 都已在 Task 11 加上,本任务不影响它

**Interfaces:**
- Consumes:
  - 第 1 步:`actio.memory_port.MemoryBackend`、`DeploymentMemoryStore`(`open`、`abandon_event`、`pending_events`、`backup_to`、`restore_from`、`digest`)、`MemoryFailureError`(`transient`)、`MemoryEventConflict`;`actio.memory.FileTupleStore(root: Path, *, extraction_failure: Callable[[MemoryEvent], str | None] | None = None)`;`actio.instance.instantiate(..., memory=, episode_id=, interlocutors=)`、`ActioInstance.close()`(转发给端口,端口 `close()` 幂等);轨迹 v2 的 `trace["memory"]["digest_before"]`
  - 第 2 步:`EnvironmentServiceClient.create_session(..., episode_id: str | None = None)`;`AgentRuntime.client_factory(*, deadline_s: float | None = None)`
  - Task 31:`mindagent.memory_layout.MemoryLayout`(`snapshot`)
- Produces:
  - `mindagent.deployment.TRANSIENT_REASONS` 已含 `"role_memory_unavailable"`(Task 11 加的,本任务不再加)
  - `mindagent.deployment.MakeInstance = Callable[[str, MemoryBackend], object]`;`make_instance(episode_id, memory)` 的 `episode_id` 是带轮次前缀的 `f"{round_id}/{部署 episode id}"`
  - `mindagent.deployment.run_deployment_episodes(store, make_instance, client_factory, *, memory_store: DeploymentMemoryStore, memory_layout: MemoryLayout, run_id: str, round_id: str, library_version: dict, episode_digests: Mapping[str, Mapping[str, str]], on_episode_start: Callable[[str, dict[str, str]], None], scenario_ids, personas, bindings, model_id, max_turns, episodes_per_scenario=1, max_retries_per_episode=1, stream_deadline_s: float | None = None) -> list[Episode]`
  - `mindagent.deployment.rerun_episode_id(base: str, skill_version: str) -> str`:`f"{base}-s{skill_version}"`
  - `mindagent.deployment._reusable(episode: Episode, agent_slot: str, skill_version: str, recorded_digest: str | None) -> bool`
  - `mindagent.errors.RestoreFailedError(AgentRoundFailedError)`:用起点副本覆盖部署库失败(`MemoryFailureError`、`OSError`,或摘要与 `episode_digests` 记的不等);Task 33 接住它时**不**回滚、库与副本都保留

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ma_deployment.py:夹具与辅助改写(其余既有 import 不变)
from actio.memory import FileTupleStore
from actio.memory_port import MemoryEventConflict
from mindagent.errors import RestoreFailedError
from mindagent.memory_layout import MemoryLayout

ROUND = "agent-round-zengen-v1"
EID = "deploy-zengen-v1-s1-1"
KEY0 = f"{ROUND}/{EID}#0"          # 对手开场那句的事件 key:轮次前缀 + 部署 id + 轮次序号
V0 = {"skill": "0", "memory": "0"}


@pytest.fixture
def mem(tmp_path):
    failing: set[str] = set()      # 测试往里放 key,该事件的抽取就以 timeout 失败
    store = FileTupleStore(tmp_path / "mem" / "deploy",
                           extraction_failure=lambda ev: "timeout" if ev.key in failing else None)
    return SimpleNamespace(store=store, layout=MemoryLayout(tmp_path / "mem"), failing=failing)


def _make(skills=(), version=None, seen=None, backend=None):
    def make(episode_id, memory):
        if seen is not None:
            seen.append(episode_id)
        return instantiate(backend or Echo(), skills=list(skills), memory=memory, episode_id=episode_id,
                           interlocutors={"a": "pa", "b": "pb"},
                           library_version=version or V0, profile=None)
    return make


def _run(store, client_factory, mem, *, make=None, digests=None, started=None, **kw):
    digests = {} if digests is None else digests

    def on_start(eid, by_persona):
        digests[eid] = dict(by_persona)
        if started is not None:
            started.append((eid, dict(by_persona)))

    args = dict(run_id=RUN, round_id=ROUND, scenario_ids=["s1"], personas=_personas(), bindings=BINDINGS,
                model_id=MODEL, max_turns=4, library_version=V0)
    args.update(kw)
    return run_deployment_episodes(
        store, make or _make(version=args["library_version"]), client_factory,
        memory_store=mem.store, memory_layout=mem.layout, episode_digests=digests,
        on_episode_start=on_start, **args)
```

`_FakeClient.__init__` 加 `opening="我很生气!!"`,`create_session` 用它作对手开场、签名加 `episode_id=None` 并写进 `self.created["episode_id"]`。

```python
def test_a_retry_restores_the_start_so_the_same_logical_id_never_conflicts(store, mem):
    # 设计源第 11 节:第一次尝试留 pending 后失败,第二次同一逻辑 id 成功,不抛 EventConflict,成功后没有 pending 行
    mem.failing.add(KEY0)
    f = _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"})
    (ep,) = _run(store, f, mem)
    assert ep.id == EID and len(f.clients) == 2
    assert [c.created["episode_id"] for c in f.clients] == [EID, EID]    # 服务端拿不带轮次的 id
    # 不回滚的话 KEY0 的 pending 行带着旧文本,第二次尝试换了开场就会撞 MemoryEventConflict
    assert mem.store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):                            # 成功后被放弃,是终态
        mem.store.remember_event("pa", KEY0, "pb：我换个说法", interlocutor="pb")
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()          # 删起点副本 = 提交


def test_a_second_round_with_the_same_deploy_id_does_not_hit_the_first_rounds_keys(store, mem):
    _run(store, _Factory(), mem)
    _run(store, _Factory({"opening": "今天好点了"}), mem, round_id="agent-round-zengen-v2", run_id="run-004")
    again = mem.store.remember_event("pa", KEY0, "pb：我很生气!!", interlocutor="pb")
    assert not again.failed and again.stored                           # 第一轮的行仍 committed、幂等
    second = mem.store.remember_event("pa", f"agent-round-zengen-v2/{EID}#0", "pb：今天好点了",
                                      interlocutor="pb")
    assert not second.failed and second.stored


def test_each_episode_gets_a_fresh_instance_whose_id_carries_the_round(store, mem):
    seen, started = [], []
    eps = _run(store, _Factory(), mem, make=_make(seen=seen), started=started, episodes_per_scenario=2)
    assert seen == [f"{ROUND}/{EID}", f"{ROUND}/deploy-zengen-v1-s1-2"]
    assert [e.id for e in eps] == [EID, "deploy-zengen-v1-s1-2"]
    for ep, (eid, by_persona) in zip(eps, started):
        first = next(t for t in ep.turns if t.speaker_slot == "a")
        assert eid == ep.id and first.activation_trace["memory"]["digest_before"] == by_persona["pa"]
    assert started[0][1]["pa"] != started[1][1]["pa"]                   # 第一条提交的记忆进了第二条的起点


def test_a_crash_mid_episode_is_rerun_from_the_restored_start(store, mem):
    digests = {}

    class Crash(Exception):
        pass

    class Crashing(_FakeClient):
        def submit_turn(self, *a, **kw):
            raise Crash()

    with pytest.raises(Crash):
        _run(store, lambda **kw: Crashing(), mem, digests=digests)
    assert mem.layout.snapshot(ROUND, EID, "pa").exists()
    assert mem.store.digest("pa") != digests[EID]["pa"]                # 半截记忆已写进部署库
    f = _Factory({"opening": "我换个说法"})
    (ep,) = _run(store, f, mem, digests=digests)                       # 重入:先覆盖、核对摘要、再重跑
    assert len(f.clients) == 1 and ep.id == EID
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()
    assert not mem.store.remember_event("pa", KEY0, "pb：我换个说法", interlocutor="pb").failed


def test_an_episode_written_but_not_committed_is_rerun(store, mem, monkeypatch):
    digests = {}
    real = mem.store.pending_events
    monkeypatch.setattr(mem.store, "pending_events", lambda pid: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        _run(store, _Factory(), mem, digests=digests)                  # episode 已落盘、副本没删
    assert store.exists(episode_file(RUN, EID)) and mem.layout.snapshot(ROUND, EID, "pa").exists()
    monkeypatch.setattr(mem.store, "pending_events", real)
    f = _Factory()
    _run(store, f, mem, digests=digests)
    assert len(f.clients) == 1
    f2 = _Factory()
    _run(store, f2, mem, digests=digests)                              # 已提交的被复用
    assert f2.clients == []


def test_a_restore_whose_digest_disagrees_fails_and_keeps_library_and_copy(store, mem):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    before = mem.store.digest("pa")
    digests[EID] = {"pa": "not-the-recorded-digest"}
    with pytest.raises(RestoreFailedError, match=EID):
        _run(store, _Factory(), mem, digests=digests)
    assert mem.layout.snapshot(ROUND, EID, "pa").exists() and mem.store.digest("pa") == before


def test_a_memory_admission_alone_reuses_committed_episodes(store, mem):
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    f = _Factory()
    (ep,) = _run(store, f, mem, digests=digests, library_version={"skill": "0", "memory": "1"})
    assert f.clients == [] and ep.id == EID


def test_a_new_skill_version_reruns_under_a_suffixed_id(store, mem):
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    seen = []
    v1 = {"skill": "1", "memory": "0"}
    (ep,) = _run(store, _Factory(), mem, digests=digests, library_version=v1, make=_make(version=v1, seen=seen))
    assert ep.id == f"{EID}-s1" and seen == [f"{ROUND}/{EID}-s1"]
    assert store.exists(episode_file(RUN, EID))                        # 原 episode 留着
    (again,) = _run(store, _Factory(), mem, digests=digests, library_version=v1)
    assert again.id == f"{EID}-s1"                                     # 重入复用带后缀的那条


def test_exhausted_transient_retries_leave_the_library_at_the_start(store, mem):
    digests = {}
    with pytest.raises(DeploymentFailedError) as info:
        _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}), mem, digests=digests)
    assert info.value.transient
    assert mem.store.digest("pa") == digests[EID]["pa"]
    assert mem.layout.snapshot(ROUND, EID, "pa").exists()              # 在途:留给重入或轮次回滚


def test_a_rejection_for_unavailable_role_memory_is_transient(store, mem):
    f = _Factory({"create_error": SessionRejectedError("role_memory_unavailable", "")})
    (ep,) = _run(store, f, mem)
    assert len(f.clients) == 2 and ep.id == EID
```

`_Factory.__call__` 改成 `__call__(self, **kw)`,与上面几个 `lambda **kw:` 同形(第 2 步让 `client_factory` 收 `deadline_s` 关键字;替身路径驱动器不传它)。`OSError` 那几条用例里工厂或存储直接抛,驱动器不接 `OSError`,它原样穿出,与"进程崩溃"同效。

既有参数化用例 `test_non_transient_failures_are_not_rerun` 若把某个 `SessionRejectedError` 拒绝列为不可重跑,实施时先 `grep -n 'SessionRejectedError(' tests/test_ma_deployment.py` 确认:原因在新 `TRANSIENT_REASONS` 里的(例如 `role_backend_unavailable`)移到 `test_a_transient_failure_is_rerun_whole` 那组。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_deployment.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'RestoreFailedError' from 'mindagent.errors'`

- [ ] **Step 3: 实现**

`errors.py`:`class RestoreFailedError(AgentRoundFailedError)`,docstring 写"用起点副本覆盖部署库失败(第 9 节'覆盖失败'):轮次 T3,部署库与起点副本都保留,等人处理"。

`deployment.py`:

- `TRANSIENT_REASONS` 已由 Task 11 加了 `"role_memory_unavailable"`,不再改;`_attempt` 里创建被拒那一支(约 93 行)改为 `raise _AttemptFailed(exc.reason, exc.detail, exc.reason in TRANSIENT_REASONS)`(第 10 节:今天对创建被拒一律判不可重跑,改成按终止原因查瞬态集合)。
- `_attempt(instance, client_factory, scenario, assignment, personas, agent_slot, *, episode_id: str, deadline_s: float | None)`:`client = client_factory() if deadline_s is None else client_factory(deadline_s=deadline_s)`;`create_session(..., episode_id=episode_id)`,这里的 `episode_id` 是**不带轮次**的部署 id。`instance.respond` 外面只接 `actio.memory_port.MemoryFailureError`,改抛 `_AttemptFailed("role_memory_unavailable" if exc.transient else "role_memory_failure", type(exc).__name__, exc.transient)`(第 10 节:MindAgent 自己造实例,直接接 `actio.MemoryFailureError`;信息只报类名);其余实例异常照旧原样上抛。
- `_reusable(episode, agent_slot, skill_version, recorded_digest)`:有智能体轮次、`recorded_digest` 不是 `None`、每个智能体轮次的轨迹是 dict 且 `library_version["skill"] == skill_version`、首个智能体轮次的 `trace["memory"]["digest_before"] == recorded_digest`。不比较部署库当前摘要(第 9 节"episode 复用")。
- `_restore(memory_store, persona_id, snap, expected, episode_id) -> str`:`restore_from` 抛 `MemoryFailureError` 或 `OSError` → `RestoreFailedError(f"restore {episode_id} for {persona_id}: {type(exc).__name__}", point="mindagent.deployment.restore")`;`expected` 不为 `None` 且返回的摘要不等 → `RestoreFailedError(f"restore {episode_id} for {persona_id}: digest mismatch", point=...)`。返回覆盖后的摘要。

`run_deployment_episodes` 每条 episode 的算法(`persona` 是智能体位的人格;对手位的记忆在服务端、每会话一份空库,第 7.x 节,不进部署库):

```
base = deployment_episode_id(model_id, scenario_id, k)
eid = base
existing = 已落盘的 base(没有则 None)
if 起点副本(base) 不在 and existing is not None:
    if _reusable(existing, slot, skill, episode_digests.get(base, {}).get(persona)): 复用,下一条
    if existing 首个智能体轮次的 skill 版本 != skill:
        eid = rerun_episode_id(base, skill)       # 对 eid 再走一遍同样的"副本在/已落盘可复用"判断
snap = memory_layout.snapshot(round_id, eid, persona)
if snap.exists():                                  # 在途时崩溃(不论 episode 文件有没有落盘)
    recorded = episode_digests.get(eid, {}).get(persona)
    start = _restore(memory_store, persona, snap, recorded, eid)
    if recorded is None: on_episode_start(eid, {persona: start})   # 崩在 backup 与写回之间
else:
    snap.parent.mkdir(parents=True, exist_ok=True)
    start = memory_store.backup_to(persona, snap)
    on_episode_start(eid, {persona: start})        # 轮次产物先记下起点摘要,再开第一次尝试
for attempt in 1..budget:
    instance = make_instance(f"{round_id}/{eid}", memory_store.open(persona))
    failure = None
    try:   raw = _attempt(instance, ..., episode_id=eid, deadline_s=stream_deadline_s)
    except _AttemptFailed as exc: failure = exc   # 记 failed_attempts
    finally: instance.close()                      # 先关实例与端口:restore_from 要求该人格没有打开的端口
    if failure is None: break
    _restore(memory_store, persona, snap, start, eid)   # 端口已关,回到起点;下一次尝试或最终失败都从这里出发
    if not failure.transient or attempt == budget:
        raise DeploymentFailedError(...)           # 起点副本保留:瞬态用尽时轮次留 pending、重入再覆盖;T3 时由 Task 33 删副本
    write_episode(store, run_id, Episode(id=eid, ...))
    for key in memory_store.pending_events(persona):
        if key.startswith(f"{round_id}/{eid}#"): memory_store.abandon_event(persona, key)
    snap.unlink()                                  # 提交点
```

几处取舍:

- 带后缀的 id 只在"已提交的 base 用的是旧技能版本"时出现,新轮次的第一次运行一律用不带后缀的 base,所以 `tests/test_ma_e2e.py` 断言的 `deploy-...` id 不变。base 已落盘、技能版本相同但 `_reusable` 为假(旧的 v1 轨迹没有 `memory` 块,或 `episode_digests` 里没记)时,按同一 id 重跑:设计二之前的 episode 没写过记忆,不会撞 key;若它其实写过,重跑会撞 `MemoryEventConflict`,经上面的映射成 `role_memory_failure`,轮次 T3,不静默。
- 每一次 `restore_from` 都在该次尝试的 `instance.close()` 之后(`restore_from` 遇到打开的端口会抛错);重试、最终失败与 Task 33 的 T3 回滚都走"先关、再覆盖"这一个顺序。测试 `test_a_failed_attempt_is_rolled_back_after_its_port_is_closed` 用一个记录调用顺序的 `FileTupleStore` 包装,断言每次 `restore_from` 之前该人格的打开端口数为 0。
- `pending` 行在删副本**之前**放弃:崩在两者之间时副本还在,重入先覆盖(被放弃的行随之回滚掉)再重跑,同 key 不会撞终态行;反过来的顺序崩在中间会留下无主的 `pending` 行。设计源 4.3 节写的是"成功提交后",这里按崩溃安全取前者。
- 过滤 `f"{round_id}/{eid}#"` 前缀,门② 的 `proposal-...#k` 行归提案,不碰(4.3 节"谁写的谁处理")。
- `snap.unlink()` 的 `OSError` 原样上抛,不吞:副本留着就是"未提交",轮次留 pending,重入时只有这一条在途(episode 顺序跑),覆盖不会抹掉后面 episode 的记忆。第 10 节"副本删除失败只记日志"说的是会话副本(`runs/` 下,有清扫兜底),不是起点副本。
- `stream_deadline_s` 为 `None`(替身)时不传 `deadline_s`,保持现值(设计源 7.x 节"不设记忆配置时保持现值");计划 B 第 7 步传 `max_turns × 40`。
- docstring 照现有风格改写,点名起点副本、提交点、两种 id 与第 9 节。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_deployment.py tests/test_ma_memory_layout.py tests/test_actio_file_tuple_store.py -q -p no:cacheprovider`
Expected: PASS(`tests/test_ma_rounds.py`、`tests/test_ma_e2e.py` 此时因 `run_deployment_episodes` 签名变了而失败,Task 33 一起改)

- [ ] **Step 5: 先不提交,接着做 Task 33**

本任务的改动与 Task 32 到 Task 35 一起在 Task 35 提交:四个任务改的是同一条调用链(`run_deployment_episodes` → 轮次处理器 → 门② → 组合根),中间任一处单独提交都会让 `factory.handlers` 导入失败或签名不匹配。这里只确认 Step 4 列出的测试通过,其余预期失败的不要为它改代码。

---

### Task 33: 轮次处理器:人格库锁、`episode_digests`、T3 回滚、验证快照

**Files:**
- Modify: `src/mindagent/rounds.py`(`make_start_round_handler` 约 181-200 行的签名;`handle` 约 216-224 行重建信封处;`_binding_payload` 约 141-169 行;`_run` 约 241-280 行)
- Test: `tests/test_ma_rounds.py`
- 随本任务改的既有测试:`tests/test_ma_rounds.py` 的 `_handler`(约 135-143 行)与约 274 行直接调 `make_start_round_handler` 的那处,都补 `memory_store`、`memory_layout`、`memory_runtime`、`scratch_store_factory`;该文件 `_FakeClient.create_session` 加 `episode_id=None`(约 60 行),`_Factory.__call__` 加 `**kw`(约 90 行);`tests/test_ma_e2e.py` 的 `world` 夹具(约 86 行)同样补四个参数。`tests/test_factory_interact_wire.py` 的 `handler_kwargs` 是收 `**kwargs` 的间谍,不用改

**Interfaces:**
- Consumes:Task 31 的 `MemoryLayout`(`persona_lock`、`snapshot`、`round_snapshots`、`verify_snapshot`、`verify_scratch`)与 `FileLockBusyError`;Task 32 的 `run_deployment_episodes(...)`、`RestoreFailedError`、`DeploymentFailedError`;第 1 步的 `DeploymentMemoryStore`、`MemoryFailureError`、`FileTupleStore`、`instantiate(..., memory=, episode_id=, interlocutors=, replay_read_only=)`、`ActioInstance.close()`;第 4 步的 `factory.memory_runtime.STAND_IN_MEMORY_RUNTIME`(只在测试里用作载荷值)
- Produces:
  - `make_start_round_handler(*, config, client_factory, backend_factory, personas_loader, memory_store: DeploymentMemoryStore, memory_layout: MemoryLayout, memory_runtime: dict, scratch_store_factory: Callable[[Path], DeploymentMemoryStore], bridge=None, stream_deadline_s: float | None = None) -> Handler`
  - `agent_round` 载荷新增 `memory_runtime: dict`(装配时给的运行时身份的深拷贝)与 `episode_digests: {部署 episode id: {persona_id: 起点摘要}}`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ma_rounds.py:夹具补记忆(其余 import 不变)
from actio.memory import FileTupleStore
from controlplane.lock import hold_file_lock
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME
from mindagent.memory_layout import MemoryLayout

ROUND = "agent-round-zengen-v1"


@pytest.fixture
def mem(tmp_path):
    root = tmp_path / "mem"
    return SimpleNamespace(store=FileTupleStore(root / "deploy"), layout=MemoryLayout(root))


def _handler(factory=None, bridge=None, backends=None, mem=None, **cfg):
    ...  # backend_factory 照旧
    return make_start_round_handler(
        config=_cfg(**cfg), client_factory=factory or _Factory(), backend_factory=backend_factory,
        personas_loader=_loader, bridge=bridge, memory_store=mem.store, memory_layout=mem.layout,
        memory_runtime=STAND_IN_MEMORY_RUNTIME, scratch_store_factory=FileTupleStore)
```

既有用例都改成带 `mem` 夹具调 `_handler(..., mem=mem)`。新增:

```python
def test_the_round_records_the_memory_runtime_and_each_episodes_start_digest(store, mem):
    env = _start(store, _handler(mem=mem))
    assert env.payload["memory_runtime"] == STAND_IN_MEMORY_RUNTIME
    digests = env.payload["episode_digests"]
    assert sorted(digests) == sorted(env.payload["episode_ids"])
    assert all(set(d) == {"pa"} for d in digests.values())            # 只备份智能体位的人格
    assert mem.layout.round_snapshots(ROUND) == []                    # 全部已提交
    assert not mem.layout.verify_snapshot(ROUND, "pa").exists()       # 验证快照轮次结束即删
    assert not mem.layout.verify_scratch(ROUND).exists()


def test_a_round_that_cannot_lock_a_persona_library_fails(store, mem):
    with hold_file_lock(mem.layout.deploy_lock("pb")):                # 对手人格也要锁(第 9 节"每个绑定人格")
        env = _start(store, _handler(mem=mem))
    _assert_failed(store, "mindagent.rounds.lock", "pb.lock")
    assert env.payload["episode_ids"] == []


def test_a_t3_round_rolls_back_the_in_flight_episode_and_drops_its_copy(store, mem):
    factory = _Factory(plans=[{}, {"create_error": SessionRejectedError("invalid_session", "bad")}])
    env = _start(store, _handler(factory=factory, mem=mem))
    assert env.status == "failed" and env.payload["failure"]["category"] == "T3"
    second = "deploy-zengen-v1-s2-1"
    assert mem.store.digest("pa") == env.payload["episode_digests"][second]["pa"]
    assert mem.layout.round_snapshots(ROUND) == []


def test_a_crash_mid_episode_resumes_from_the_restored_start(store, mem):
    class Crash(Exception):
        pass

    factory = _Factory(plans=[{}, {"crash_on_submit": Crash}])
    with pytest.raises(Crash):
        _start(store, _handler(factory=factory, mem=mem))
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert len(mem.layout.round_snapshots(ROUND)) == 1
    again = _Factory()
    env = _start(store, _handler(factory=again, mem=mem))
    assert env.status == "executed" and again.count == 2               # s2 重跑、s3 新跑,s1 复用
    assert mem.layout.round_snapshots(ROUND) == []


def test_a_failed_restore_fails_the_round_and_keeps_library_and_copy(store, mem):
    class Crash(Exception):
        pass

    with pytest.raises(Crash):
        _start(store, _handler(factory=_Factory(plans=[{"crash_on_submit": Crash}]), mem=mem))
    env = store.read_product(agent_round_file("zengen-v1"))
    env.payload["episode_digests"]["deploy-zengen-v1-s1-1"]["pa"] = "tampered"
    store.write_product(agent_round_file("zengen-v1"), env, "test: tamper")
    _start(store, _handler(mem=mem))
    _assert_failed(store, "mindagent.deployment.restore", "digest mismatch")
    assert len(mem.layout.round_snapshots(ROUND)) == 1                # 副本保留;库停在覆盖后的状态,不再回滚


def test_verification_replays_on_read_only_snapshot_copies(store, mem, monkeypatch):
    import mindagent.rounds as rounds
    calls = []
    real = rounds.instantiate
    monkeypatch.setattr(rounds, "instantiate", lambda *a, **kw: calls.append(kw) or real(*a, **kw))
    _skill(store, GOAL)
    env = _start(store, _handler(mem=mem))
    assert env.payload["proposals"]                                    # 有草案才会验证
    replays = [kw for kw in calls if kw.get("replay_read_only")]
    assert replays and len(replays) % 2 == 0                           # 每份草案基线与候选各一
    assert all(kw["memory"] is not None for kw in replays)
    assert mem.store.pending_events("pa") == []
```

`_FakeClient` 加 `crash_on_submit=None`:非 `None` 时 `submit_turn` 抛它。用 `GOAL` 技能造出草案的写法照 `test_a_library_that_ignores_emotion_produces_a_pending_review_proposal`(约 173 行),实施时先读那条用例确认需要的情境与台词。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_rounds.py -q -p no:cacheprovider`
Expected: FAIL,`TypeError: make_start_round_handler() got an unexpected keyword argument 'memory_store'`

- [ ] **Step 3: 实现**

- `_binding_payload(..., memory_runtime: dict)` 加 `"memory_runtime": copy.deepcopy(memory_runtime)`、`"episode_digests": {}`。
- `handle`:`existing` 是 pending 时,新信封除 `created_at` 外还继承 `existing.payload.get("episode_digests", {})`。不继承的话重入时 `_reusable` 拿不到起点摘要,已提交的 episode 会按同一 id 重跑而撞上自己 committed 的 key(`test_a_pending_round_resumes_without_regenerating_persisted_episodes` 钉住这一点)。
- 锁:`_check_inputs` 之后、`_run` 之前,`ExitStack` 按排序后的绑定人格 id 逐个 `memory_layout.persona_lock(pid)`;`FileLockBusyError` 改抛 `AgentRoundFailedError(f"persona library locked: {exc}", point="mindagent.rounds.lock")`(M20)。锁一直持到 `handle` 返回或抛出,所以下面的回滚也在锁里做。
- T3 回滚:`_run` 外包一层 `try`;接到 `DeploymentFailedError`(`transient` 为假)、`ProposalParseError`、`BridgeError`、`MindAgentDataError`、`ActioDataError`、以及 `AgentRoundFailedError` 但**不是** `RestoreFailedError` 时,先 `_rollback(env.id)` 再重抛给既有的 `except` 链写失败记录。`_rollback` 运行时 `run_deployment_episodes` 已经返回或抛出,它每次尝试的 `finally` 已关掉实例与端口(Task 32),所以 `restore_from` 不会撞上打开的端口;`_rollback` 开头再断言一次该人格的打开端口数为 0,不为 0 就抛 `RestoreFailedError`("port still open"),不去覆盖。`_rollback` 对每个绑定人格,取 `memory_layout.round_snapshots(round_id)` 里以 `f"-{pid}{memory_layout.suffix}"` 结尾的副本,`memory_store.restore_from(pid, snap)` 后 `snap.unlink()`;`restore_from` 抛 `MemoryFailureError`/`OSError` 时改抛 `RestoreFailedError(..., point="mindagent.deployment.restore")`,副本与库都不动(第 9 节"覆盖失败时库与副本都保留")。瞬态的 `DeploymentFailedError` 与其余异常不回滚:轮次留 pending,重入时由 Task 32 的"副本在就先覆盖"处理。
- `_run`:
  - `interlocutors = {b.slot_id: b.persona_id for b in config.bindings}`;`make_instance(episode_id, memory)` = `instantiate(backend_factory(model_id, ckpt), skills=lib.skills, memory=memory, episode_id=episode_id, interlocutors=interlocutors, library_version=lib.version, profile={"model_id": model_id, "round_id": env.id})`。
  - `on_episode_start(eid, by_persona)`:`env.payload["episode_digests"][eid] = dict(by_persona)`,`store.write_product(agent_round_file(model_id), env, f"mindagent: {env.id} start {eid}")`(第 9 节"每条 episode 开始前写回一次")。
  - 调 `run_deployment_episodes(store, make_instance, client_factory, memory_store=..., memory_layout=..., run_id=run_id, round_id=env.id, library_version=lib.version, episode_digests=env.payload["episode_digests"], on_episode_start=on_episode_start, ..., stream_deadline_s=stream_deadline_s)`。
  - 验证快照:部署跑完后,对智能体位人格 `memory_store.backup_to(pid, memory_layout.verify_snapshot(env.id, pid))`。`instance_with(extra)` 每次调用:`scratch = scratch_store_factory(memory_layout.verify_scratch(env.id) / f"{n}")`(`n` 是调用序号),`scratch.restore_from(pid, verify_snapshot)`,`instantiate(..., skills=lib.skills + tuple(extra), memory=scratch.open(pid), episode_id=f"{env.id}/verify", interlocutors=..., replay_read_only=True, ...)`;造出的实例记进列表。验证结束后(`finally`)逐个 `close()`,再删 `verify_snapshot` 与 `shutil.rmtree(verify_scratch, ignore_errors=True)`。`episode_id` 在只读重放下不产生事件 key,给它只是让第 1 步"给 `memory` 时要 `episode_id`"的校验不因显式 `replay_read_only=True` 而分叉。
  - 载荷其余字段的写法不变。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_rounds.py tests/test_ma_e2e.py tests/test_ma_deployment.py tests/test_ma_verify.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 先不提交,接着做 Task 34**

本任务的改动与 Task 32 到 Task 35 一起在 Task 35 提交:四个任务改的是同一条调用链(`run_deployment_episodes` → 轮次处理器 → 门② → 组合根),中间任一处单独提交都会让 `factory.handlers` 导入失败或签名不匹配。这里只确认 Step 4 列出的测试通过,其余预期失败的不要为它改代码。

---

### Task 34: 门② 记忆增量改走部署库,抽取失败转 `rejected`,在途轮次时延后

**Files:**
- Modify: `src/mindagent/admit.py`(`_entries` 约 27-48 行;`handle_admit_proposal` 约 59-107 行改成工厂 `make_admit_proposal_handler`)
- Test: `tests/test_ma_admit.py`
- 随本任务改的既有测试:`tests/test_ma_admit.py` 的 `MEMORY`(约 17 行)改成三键 `{"persona_id": "pa", "text": "对方住在北京", "interlocutor": "pb"}`,`_admit` 改用工厂造的处理器;约 80-88 行 `test_a_handwritten_memory_proposal_is_admitted_to_the_memory_library` 改写为下面的 `test_a_memory_increment_is_remembered_in_the_personas_deploy_library`;约 90-96 行混合提案用例不再断言 `memory_file(...)` 存在,改断言 `library_state.admitted[0]["entries"] == ["skill-agent-round-zengen-v3-1-1", f"{PID}#2"]`;约 141-151 行 `test_a_crash_between_two_entries_converges` 的崩溃点从 `memory_library/` 改成 `skill_library/`(记忆先于技能写,见下);`tests/test_ma_e2e.py` 约 151 行与 `tests/test_factory_handlers.py` 约 41-44 行由 Task 35 改

**Interfaces:**
- Consumes:Task 29 的 `approved → rejected`;Task 31 的 `MemoryLayout`(`persona_lock`、`uncommitted_snapshots`)与 `FileLockBusyError`;第 1 步的 `DeploymentMemoryStore.remember_event`/`abandon_event`、`RememberOutcome(stored, failed, category)`、`MemoryFailureError`、`PERSONA_ID_PATTERN`、`FileTupleStore(..., extraction_failure=)`;`controlplane.deferred.DeferredError(reason, retry_after_seconds=None)`
- Produces:
  - `mindagent.admit.make_admit_proposal_handler(*, memory_store: DeploymentMemoryStore, memory_layout: MemoryLayout) -> Handler`
  - `mindagent.admit.MEMORY_DEFINITION_KEYS = frozenset({"persona_id", "text", "interlocutor"})`
  - 记忆增量的事件 key `f"{proposal_id}#{k}"`(`k` 是增量在提案里的位置,从 1 起);`library_state.admitted[*].entries` 里记忆一项就是这个 key;提案转 `effective` 时载荷加 `admitted_statements: {key: [语句 id, ...]}`(只作证据);转 `rejected` 时载荷加 `reject_reason`(与 `cli/src/commands.ts` 的 `reject` 同一字段)

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ma_admit.py:夹具与辅助(其余 import 不变;删掉 MemoryEntry 导入)
from types import SimpleNamespace

from actio.memory import FileTupleStore
from actio.memory_port import MemoryEventConflict
from controlplane.deferred import DeferredError
from controlplane.lock import hold_file_lock
from mindagent.admit import make_admit_proposal_handler
from mindagent.memory_layout import MemoryLayout

MEMORY = {"persona_id": "pa", "text": "对方住在北京", "interlocutor": "pb"}
KEY = f"{PID}#1"


@pytest.fixture
def mem(tmp_path):
    failing: set[str] = set()
    root = tmp_path / "mem"
    return SimpleNamespace(
        store=FileTupleStore(root / "deploy", extraction_failure=lambda ev: "timeout" if ev.key in failing else None),
        layout=MemoryLayout(root), failing=failing)


def _admit(store, mem, pid=PID):
    make_admit_proposal_handler(memory_store=mem.store, memory_layout=mem.layout)(
        store, Action(kind="admit_proposal", target_id=pid, detail={}))
```

既有用例都改成带 `mem` 调 `_admit(store, mem)`。新增:

```python
def test_a_memory_increment_is_remembered_in_the_personas_deploy_library(store, mem):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "effective"
    again = mem.store.remember_event("pa", KEY, "对方住在北京", interlocutor="pb")
    assert list(again.stored) == proposal.payload["admitted_statements"][KEY]   # 收据是事件行
    assert _state(store)["memory_version"] == "1" and _state(store)["admitted"][0]["entries"] == [KEY]
    assert not (store.repo_root / f"{CONTROL_PLANE_ROOT}/memory_library").exists()


def test_a_failed_extraction_abandons_the_event_and_rejects_the_proposal(store, mem):
    mem.failing.add(KEY)
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "rejected" and "timeout" in proposal.payload["reject_reason"]
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert fr.payload["category"] == "T2"
    assert mem.store.pending_events("pa") == []
    mem.failing.clear()
    with pytest.raises(MemoryEventConflict):                          # abandoned 是终态,任何路径都写不进
        mem.store.remember_event("pa", f"{PID}#2", "对方住在北京", interlocutor="pb")
    assert not store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))     # 记忆先写,技能未落
    assert not store.exists(library_state_file())


def test_a_later_extraction_failure_after_an_earlier_commit_defers_and_then_converges(store, mem):
    # 第 1 条记忆增量 committed、第 2 条抽取失败:不能转 rejected(已提交的放弃不了),也不能放弃第 2 条
    # (放弃后提案留 approved,每个 tick 重入都撞 EventConflict,永不收敛)。所以第 2 条留 pending、动作延后
    second = {"persona_id": "pa", "text": "对方养了一只猫", "interlocutor": "pb"}
    mem.failing.add(f"{PID}#2")
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}, {"kind": "memory", "definition": second}])
    with pytest.raises(DeferredError, match="partial admission"):
        _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert fr.payload["category"] == "T2" and "partial admission" in fr.payload["reason"]
    assert mem.store.pending_events("pa") == [f"{PID}#2"]              # 留 pending,不放弃
    assert not store.exists(library_state_file())
    mem.failing.clear()                                                # 抽取恢复后重入
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "effective"
    assert set(proposal.payload["admitted_statements"]) == {KEY, f"{PID}#2"}
    assert mem.store.pending_events("pa") == []


def test_a_store_error_leaves_the_proposal_approved_and_reentry_completes_the_receipt(store, mem, monkeypatch):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    real = mem.store.remember_event

    def boom(*a, **kw):
        real(*a, **kw)                                                # 语句已写,"③ 写收据"之后崩
        raise OSError("disk")

    monkeypatch.setattr(mem.store, "remember_event", boom)
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert store.exists(failure_record_file("run-003", f"admit-{PID}"))
    monkeypatch.setattr(mem.store, "remember_event", real)
    before = mem.store.digest("pa")
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert mem.store.digest("pa") == before                           # 重入是幂等的,不二次抽取


def test_an_in_flight_round_defers_admission_until_its_copy_is_gone(store, mem):
    # 门② 与在途轮次同一 tick:该人格有未提交起点副本时延后,库不变;副本删掉后下一 tick 收录成功
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    snap = mem.layout.snapshot("agent-round-zengen-v3", "deploy-zengen-v3-s1-1", "pa")
    snap.parent.mkdir(parents=True)
    mem.store.backup_to("pa", snap)
    before, head = mem.store.digest("pa"), store._git("rev-parse", "HEAD")
    with pytest.raises(DeferredError, match="uncommitted"):
        _admit(store, mem)
    assert mem.store.digest("pa") == before and store._git("rev-parse", "HEAD") == head
    snap.unlink()
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"


def test_a_locked_persona_library_defers_admission(store, mem):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    with hold_file_lock(mem.layout.deploy_lock("pa")):
        with pytest.raises(DeferredError, match="pa.lock"):
            _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"


@pytest.mark.parametrize("definition", [
    {"persona_id": "pa", "text": "x"},                                             # 少键
    {"persona_id": "pa", "text": "x", "interlocutor": "pb", "subject": "pb"},      # 多键
    {"persona_id": "a/b", "text": "x", "interlocutor": "pb"},                      # 人格 id 不合法
    {"persona_id": "pa", "text": "", "interlocutor": "pb"},                        # 空文本
    {"persona_id": "pa", "text": "x", "interlocutor": 3},
])
def test_a_malformed_memory_definition_is_t2_and_touches_nothing(store, mem, definition):
    _put(store, increments=[{"kind": "memory", "definition": definition}])
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert store.read_product(failure_record_file("run-003", f"admit-{PID}")).payload["category"] == "T2"
    assert mem.store.digest("pa") == FileTupleStore(mem.layout.root / "empty").digest("pa")
```

`MemoryEventConflict` 从 `actio.memory_port` 导入。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_admit.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'make_admit_proposal_handler' from 'mindagent.admit'`

- [ ] **Step 3: 实现**

`_entries(proposal_id, increments) -> list[tuple[str, str, dict]]` 的 `memory` 分支:`set(definition) != MEMORY_DEFINITION_KEYS` 抛 `ValueError(f"increment {k}: memory definition must have exactly {sorted(MEMORY_DEFINITION_KEYS)}")`;`persona_id` 不匹配 `PERSONA_ID_PATTERN`、`text` 不是非空 `str`、`interlocutor` 不是 `str`(空串合法,表示一般事实)都抛 `ValueError`;产出 `("memory", f"{proposal_id}#{k}", definition)`。不再调 `memory_id_for`、不再写 `memory_file`(`library.memory_id_for` 与 `paths.memory_file` 保留,`tests/test_paths.py` 与 `tests/test_ma_config_library.py` 还在用)。

`make_admit_proposal_handler` 返回的 `handle(store, action)` 顺序(模块 docstring 照此改写):

1. 读提案;不是 `approved` 返回。`_entries` 与 `read_library_versions` 的校验失败照旧 `fail_once(... "T2", point="mindagent.admit")` 后返回。
2. 记忆增量所涉人格(排序去重)逐个进 `ExitStack`:`memory_layout.persona_lock(pid)`,`FileLockBusyError` 改抛 `DeferredError(f"persona library locked: {exc}")`;拿到锁后 `memory_layout.uncommitted_snapshots(pid)` 非空抛 `DeferredError(f"persona {pid} has an uncommitted deployment episode: {names}")`。锁持到处理器返回。
3. 记忆增量按位置逐个 `memory_store.remember_event(pid, key, text, interlocutor=...)`:
   - 抛 `MemoryFailureError` 或 `OSError`:`fail_once(... category="T1" if getattr(exc, "transient", False) else "T2", point="mindagent.admit.memory", reason=f"remember {key}: {type(exc).__name__}")`,返回,不改提案、不放弃事件(第 9 节:留在 `approved` 让下一 tick 重入)。
   - `outcome.failed`:先查本提案在它之前的记忆增量是否已有 `committed` 的(前几条成功、这一条失败)。有就**不**转 `rejected`(已提交的事件放弃不了,转了就是"提案被拒、库里却有它的记忆"),也**不**放弃这一条:让它留在 `pending`。写一条 T2 失败记录(原因 `partial admission: {key} extraction failed after {n} committed`,`fail_once` 只写一次),然后抛 `DeferredError(f"partial admission of {proposal_id}: {key} pending")`:守护进程不写树、按退避重试这一条动作。重入时前几条 `remember_event` 幂等返回原收据,失败的那条按 `pending` 行再抽一次;抽成功就照常收录完、转 `effective`,所以这条路径会收敛。抽取一直失败时动作一直按退避重试,不写树;失败记录与 `pending` 行让人看得到。本计划不加"人工放弃部分收录"的命令(见 Review Focus)。一条都没提交时才走下面的整份拒绝:`memory_store.abandon_event(pid, key)`,`fail_once(... "T2", point="mindagent.admit.memory", reason=f"extraction failed for {key}: {outcome.category}")`,`assert_transition("review", "approved", "rejected")`,载荷写 `reject_reason`(同一句),写回,返回。
   - 成功:记下 `statements[key] = list(outcome.stored)`。
4. 技能条目照旧写 `skill_file`(只写 `kind == "skill"` 的)。
5. `library_state` 照旧递增(`entries` 里记忆是 key)。
6. 提案载荷加 `admitted_statements`,转 `effective`。

记忆先于技能写:抽取失败要整份提案转 `rejected`,技能条目已写成 `effective` 就收不回了。崩在 3 与 6 之间重入时,第 3 步的 `remember_event` 对同 key 同文本返回幂等的同一批 id,不二次抽取。`fail_once` 对同一 `(run, task)` 只写一次,所以先瞬态失败、后抽取失败的提案只留第一条失败记录,原因以提案上的 `reject_reason` 为准。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ma_admit.py tests/test_states.py tests/test_ma_config_library.py -q -p no:cacheprovider`
Expected: PASS(`tests/test_ma_e2e.py`、`tests/test_factory_handlers.py` 仍导入 `handle_admit_proposal`,由 Task 35 改)

- [ ] **Step 5: 先不提交,接着做 Task 35**

本任务的改动与 Task 32 到 Task 35 一起在 Task 35 提交:四个任务改的是同一条调用链(`run_deployment_episodes` → 轮次处理器 → 门② → 组合根),中间任一处单独提交都会让 `factory.handlers` 导入失败或签名不匹配。这里只确认 Step 4 列出的测试通过,其余预期失败的不要为它改代码。

---

### Task 35: 组合根装配替身部署记忆,轮次与门② 共用一份

**Files:**
- Modify: `src/factory/memory_runtime.py`(第 4 步新建的模块;加 `DeploymentMemory` 与 `stand_in_deployment_memory`)
- Modify: `src/factory/wire.py`(`BuildContext` 约 70-91 行加字段;`assemble` 约 342-356 行造一次)
- Modify: `src/factory/handlers.py`(`_agent` 约 80-96 行;Task 30 的 `_admit_proposal`;`DEFAULT_BUILDERS` 约 99-106 行)
- Modify: `src/controlplane/lock.py`(`_git_dir` 改名公开为 `git_dir`,内部调用点跟着改)
- Test: `tests/test_factory_handlers.py`、`tests/test_factory_interact_wire.py`、`tests/test_ma_e2e.py`
- 随本任务改的既有测试:`tests/test_factory_handlers.py` 约 41-44 行 `test_three_keys_are_the_plan_3_and_plan_7_handlers_themselves` 不再断言 `admit_proposal` 是 `handle_admit_proposal`(它现在是工厂造的闭包),改断言两个 recipestore 处理器仍是函数本身、`admit_proposal` 可调用;`tests/test_ma_e2e.py` 约 13、151 行改用 `make_admit_proposal_handler`,与轮次处理器共用 `world` 夹具里的同一个 `FileTupleStore` 与 `MemoryLayout`;`tests/test_factory_e2e_loop.py` 走 `main`,不用改,但要随回归跑

**Interfaces:**
- Consumes:Task 31 `MemoryLayout`;Task 33 `make_start_round_handler(..., memory_store, memory_layout, memory_runtime, scratch_store_factory, stream_deadline_s=None)`;Task 34 `make_admit_proposal_handler(*, memory_store, memory_layout)`;第 1 步 `FileTupleStore`;第 4 步 `factory.memory_runtime.STAND_IN_MEMORY_RUNTIME`
- Produces:
  - `factory.memory_runtime.DeploymentMemory(store: DeploymentMemoryStore, layout: MemoryLayout, runtime: dict, scratch_store_factory: Callable[[Path], DeploymentMemoryStore])`,frozen dataclass
  - `factory.memory_runtime.stand_in_memory_root(repo_root: Path) -> Path`:`controlplane.lock.git_dir(repo_root) / "rismforge-memory"`(Task 31 已动 `lock.py`,本任务把私有的 `_git_dir` 改名公开为 `git_dir`,`acquire_repo_lock` 跟着改调用,`tests/factory_fixtures.py` 若引用 `_git_dir` 一并改——实施时先 `grep -rn '_git_dir' src tests`)
  - `factory.memory_runtime.stand_in_deployment_memory(repo_root: Path) -> DeploymentMemory`:`root = stand_in_memory_root(repo_root)`,`DeploymentMemory(FileTupleStore(root / "deploy"), MemoryLayout(root), copy.deepcopy(STAND_IN_MEMORY_RUNTIME), FileTupleStore)`
  - `BuildContext.deployment_memory: DeploymentMemory | None = None`;`assemble` 在启用的处理器键含 `start_agent_round` 或 `admit_proposal` 时造一次填进去(未设 `RISMFORGE_MEMORY_CONFIG` 走替身;计划 B 第 7 步在这里按配置换成 Starling)

根目录取在 git 目录里(与仓库锁 `rismforge.lock` 同处):不进工作树、不进提交;跨进程重启保留,所以第 9 节"起点副本跨重启保留"与"门② 收录的记忆不随进程退出丢失"在替身上也成立;一棵树一个 git 目录,`.owner` 防混写的问题在替身上不存在。设计源 4.1 节写的是"`control-plane/` 之外的临时目录",按字面做成 `mkdtemp` 会让进程每次重启都换一个空库,崩溃重入与门② 收录都失效;这一点列为待裁定。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_handlers.py 追加
def test_round_and_admit_handlers_share_one_stand_in_deployment_memory(store, monkeypatch):
    import factory.handlers as fh
    from actio.memory import FileTupleStore
    from controlplane.lock import git_dir
    from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    monkeypatch.setattr(fh, "make_admit_proposal_handler", lambda **kw: seen.setdefault("admit", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent", "admit")), env={})
    try:
        r, a = seen["round"], seen["admit"]
        assert isinstance(r["memory_store"], FileTupleStore) and r["memory_store"] is a["memory_store"]
        assert r["memory_layout"] is a["memory_layout"]
        assert r["memory_layout"].root == git_dir(store.repo_root) / "rismforge-memory"
        assert r["memory_runtime"] == STAND_IN_MEMORY_RUNTIME and r["scratch_store_factory"] is FileTupleStore
        assert r.get("stream_deadline_s") is None                       # 替身保持现值
        assert not (store.repo_root / "control-plane" / "rismforge-memory").exists()
    finally:
        asm.close()


def test_the_stand_in_memory_root_survives_a_second_assembly(store):
    from factory.memory_runtime import stand_in_deployment_memory
    first = stand_in_deployment_memory(store.repo_root)
    first.store.remember_event("pa", "k#0", "pb：你好", interlocutor="pb")
    second = stand_in_deployment_memory(store.repo_root)
    assert second.store.digest("pa") == first.store.digest("pa")
```

`tests/test_factory_interact_wire.py` 的 `handler_kwargs` 间谍(约 124-136 行)已收 `**kwargs`;在它断言 `client_factory` 的那条用例(约 185 行)后加一行 `assert handler_kwargs[-1]["memory_runtime"]["backend"] == "stand-in"`,钉住 `--interact` 与 `--enable agent` 同开时轮次处理器也拿到替身身份。

`tests/test_ma_e2e.py` 的 `world` 夹具:Task 33 已经在里面建好 `mem_root`、`FileTupleStore(mem_root / "deploy")` 与 `MemoryLayout(mem_root)` 并传给 `make_start_round_handler`;本任务只把这两个对象随夹具一起 `yield`,让门② 用同一份;约 151 行改成 `make_admit_proposal_handler(memory_store=..., memory_layout=...)(store, Action(...))`。在端到端末尾追加:`assert all(t["memory"]["digest_before"] for _, t in traces)`(每轮带 v2 轨迹)与 `assert memory_layout.round_snapshots("agent-round-zengen-v3") == []`。`traces` 的确切变量名与第三轮的轮次 id,实施时先 `grep -n 'traces =\|r3 =' tests/test_ma_e2e.py` 确认。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_ma_e2e.py -q -p no:cacheprovider`
Expected: FAIL,`ImportError: cannot import name 'git_dir' from 'controlplane.lock'`

- [ ] **Step 3: 实现**

- `handlers.py`:`_agent(ctx)` 在 Task 30 的预检之后取 `mem = ctx.deployment_memory`,给 `make_start_round_handler` 补 `memory_store=mem.store, memory_layout=mem.layout, memory_runtime=mem.runtime, scratch_store_factory=mem.scratch_store_factory`,不传 `stream_deadline_s`;`_admit_proposal(ctx)` 返回 `make_admit_proposal_handler(memory_store=mem.store, memory_layout=mem.layout)`;`DEFAULT_BUILDERS["admit_proposal"] = _admit_proposal`;模块 docstring 的 `admit_proposal` 一行改成"计划 7 的处理器工厂,记忆增量写树外的部署库"。导入改为 `from mindagent.admit import make_admit_proposal_handler`。
- `wire.py`:`assemble` 在构造 `BuildContext` 前,`keys & {"start_agent_round", "admit_proposal"}` 非空时 `deployment_memory = stand_in_deployment_memory(store.repo_root)`,否则 `None`;作为关键字传进 `BuildContext`。只造一次,两个构造器拿到同一个 `FileTupleStore` 实例:它的"打开着的端口数"是实例内状态,共用才能让 `restore_from` 的守卫看见轮次处理器开着的端口。
- `memory_runtime.py`:导入 `actio.memory.FileTupleStore`、`mindagent.memory_layout.MemoryLayout`、`controlplane.lock.git_dir`;启动日志在造替身时写一行 `memory backend: stand-in`(设计源 5.1 节),用模块级 `logging.getLogger(__name__)`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_handlers.py tests/test_factory_interact_wire.py tests/test_factory_e2e_loop.py tests/test_ma_e2e.py tests/test_ma_rounds.py tests/test_ma_admit.py tests/test_ma_deployment.py tests/test_repo_lock.py tests/test_interact_parity.py -q -p no:cacheprovider`
Expected: PASS;再跑一遍全量 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider`,Expected: PASS

- [ ] **Step 5: 提交**

```bash
# Task 32 到 Task 35 一次提交
git add src/mindagent/deployment.py src/mindagent/errors.py tests/test_ma_deployment.py
git add src/mindagent/rounds.py tests/test_ma_rounds.py
git add src/mindagent/admit.py tests/test_ma_admit.py
git add src/factory/memory_runtime.py src/factory/wire.py src/factory/handlers.py src/controlplane/lock.py
git add tests/test_factory_handlers.py tests/test_factory_interact_wire.py tests/test_ma_e2e.py
git commit -m "feat(mindagent,factory): 部署记忆接上替身——episode 按次新造实例、起点副本作提交点、轮次锁人格库与 T3 回滚、门② 写部署库、组合根装配一份共用的替身部署记忆"
```

---
