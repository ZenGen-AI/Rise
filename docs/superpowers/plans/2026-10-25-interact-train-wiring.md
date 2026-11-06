# 场景二接通 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一条 interact 指令在真实入口 `factory reconcile --interact` 从派发走到候选 `pending_review`,会话由真模型经 vLLM 驱动,episode 落进 run 目录。

**Architecture:** 调度器写指令时按 `training_defaults.yaml` 的 `interact` 段构造 interact 配置;派发器在训练前生成或校验情境与人格并回写指令;`AgentRuntime` 按角色位的 `driver` 选三种对手驱动之一;训练位由组合根注入的策略工厂经共用 vLLM 服务说话;训练仍是 mock 执行器。

**Tech Stack:** Python 3.11+、pytest、grpc、远端计算层 `compute`(SSH 到 vLLM)。无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-10-24-interact-train-wiring-design.md`(并已并入 `docs/design/` 的 00、20、30、70、90 五篇)。

## Global Constraints

- 每个源文件首条语句 `from __future__ import annotations`。
- 测试命令:`PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest <path> -q -p no:cacheprovider`。
- 不新增第三方依赖;外部 API 客户端只用 `urllib`。
- 凭证只从环境变量读,异常信息只报变量名,不回显取值。
- 每个任务先写失败的测试,确认它失败,再实现。
- 新增的 `[计划中]` 设计文档改完后必须 `check_docs` 与 `check_consistency` 都是 0 错误 0 告警。
- 依赖方向:`simulator` 不得导入 `mindagent`、`mindtrainer`、`diagnostic`;`mindtrainer` 不得导入 `diagnostic`、`trainscheduler`;`trainscheduler` 不得导入 `simulator`、`simple_actio`、`mindagent`、`factory`。
- 不改生成图(`docs/design/diagrams/*.py` 与 `.svg`)。

## Review Focus

- 生成路指令 `scenario_ids` 与 `bindings` 都为空:`validate_training_config` 必须拒绝,`validate_pending_interact` 必须放行,派发器必须在严格校验**之前**回写——三处顺序错一处,合法指令就写不出来或走不到回写。
- `RoleBinding.persona_id` 是必填,生成路回写前它是空的;任何提前读它的代码都会 `KeyError`。
- `model_ref` 解析失败的落点:必须在起服务之前判 T2,放进运行时后端工厂会变成 `role_backend_failure`(按瞬态重跑到上限也不会好)。
- 点名路 `sft_steps > 0` 时没有可合成数据,会因缺清单判 `failed`;装配期就要拒绝这个组合。
- 共用 `AgentRuntime` 时,部署会话与训练会话的 episode 不能互相写进对方的 run。

---

### Task 1: `Scenario.opponent_script` 与 `ScriptedBackend(loop=...)`

**Files:**
- Modify: `src/simulator/datamodel.py`(`Scenario`)
- Modify: `src/simulator/backends.py`(`ScriptedBackend`)
- Test: `tests/test_sim_datamodel.py`、`tests/test_sim_backends.py`

**Interfaces:**
- Produces: `Scenario.opponent_script: dict[str, list[str]]`(默认空 dict);`ScriptedBackend(scripted, *, loop: bool = False)`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_sim_datamodel.py
def test_opponent_script_is_optional_and_survives_a_round_trip():
    plain = Scenario(id="s", background="", role_slots=[], conflict="", norms=[], purpose="training")
    assert plain.to_dict() == {  # 旧形状逐字节不变
        "id": "s", "background": "", "role_slots": [], "conflict": "", "norms": [],
        "purpose": "training", "source": "auto",
    }
    scripted = Scenario(id="s", background="", role_slots=[], conflict="", norms=[],
                        purpose="training", opponent_script={"b": ["一", "二"]})
    assert scripted.to_dict()["opponent_script"] == {"b": ["一", "二"]}
    assert Scenario.from_dict(scripted.to_dict()).opponent_script == {"b": ["一", "二"]}
    assert Scenario.from_dict(plain.to_dict()).opponent_script == {}
```

```python
# tests/test_sim_backends.py
def test_scripted_backend_keeps_the_last_line_when_loop_is_off():
    backend = ScriptedBackend({"b": ["一"]})
    assert backend.respond(_persona(), _scenario(), [], "b")[0] == "一"
    assert backend.respond(_persona(), _scenario(), [], "b")[0] == "(无更多发言)"

def test_scripted_backend_wraps_around_when_loop_is_on():
    backend = ScriptedBackend({"b": ["一", "二"]}, loop=True)
    said = [backend.respond(_persona(), _scenario(), [], "b")[0] for _ in range(5)]
    assert said == ["一", "二", "一", "二", "一"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_sim_datamodel.py::test_opponent_script_is_optional_and_survives_a_round_trip tests/test_sim_backends.py -q -p no:cacheprovider`
Expected: FAIL,`TypeError: __init__() got an unexpected keyword argument 'opponent_script'` 与 `'loop'`

- [ ] **Step 3: 实现**

`Scenario` 加字段 `opponent_script: dict[str, list[str]] = field(default_factory=dict)`;`to_dict` 只在非空时写该键(先例是 `RoleBinding.model_ref` 的条件写入);`from_dict` 用 `d.get("opponent_script", {})` 并深拷贝成 `{str(k): list(v) for ...}`。

`ScriptedBackend.__init__` 加 `*, loop: bool = False`;`respond` 在游标越界时,`loop` 为真则把游标归零重取,否则维持现在的 `"(无更多发言)"`。

- [ ] **Step 4: 跑测试确认通过,并跑相邻回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_sim_datamodel.py tests/test_sim_backends.py tests/test_sim_dataset.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/datamodel.py src/simulator/backends.py tests/test_sim_datamodel.py tests/test_sim_backends.py
git commit -m "feat(simulator): 情境可带对手剧本,剧本后端可循环"
```

---

### Task 2: `training_defaults.yaml` 的 `interact` 与 `opponent` 段

**Files:**
- Modify: `src/trainscheduler/config.py`(`_DEFAULTS_KEYS`、`TrainingDefaults`、`parse_training_defaults`)
- Modify: `src/trainscheduler/directive.py`(`directive_payload`)
- Test: `tests/test_ts_config.py`、`tests/test_ts_directive.py`(若无此文件则并入 `tests/test_ts_resolve.py`)

**Interfaces:**
- Produces: `TrainingDefaults.interact: InteractDefaults | None`;`InteractDefaults(trained_slot: str, slots: tuple[dict, ...], max_turns_per_episode: int, scenario_ids: tuple[str, ...], persona_ids: dict[str, str], data_spec: DataSpec | None)`;`TrainingDefaults.opponent: dict | None`。
- Consumes: Task 1 的 `Scenario.opponent_script`(仅测试夹具用到)。

- [ ] **Step 1: 写失败的测试**

```python
def test_interact_section_parses_two_sources_and_rejects_halves():
    full = {"training_config": copy.deepcopy(DEFAULT_TRAINING_CONFIG),
            "data_spec": copy.deepcopy(DEFAULT_DATA_SPEC),
            "interact": {"trained_slot": "a",
                         "slots": [{"slot_id": "a", "model_backend": "self_play"},
                                   {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1"}],
                         "max_turns_per_episode": 4,
                         "scenario_ids": ["s1", "s2"], "persona_ids": {"a": "p1", "b": "p2"}}}
    d = parse_training_defaults(full)
    assert d.interact.scenario_ids == ("s1", "s2") and d.interact.data_spec is None

    generated = copy.deepcopy(full)
    del generated["interact"]["scenario_ids"]
    del generated["interact"]["persona_ids"]
    generated["interact"]["data_spec"] = {"scenario_count": 2, "seed": 0, "purpose": "training",
                                          "stages": {"sft": {"group_size": 1}}}
    assert parse_training_defaults(generated).interact.data_spec.scenario_count == 2

@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s["interact"].pop("persona_ids"), "persona_ids"),
    (lambda s: s["interact"].update(persona_ids={"a": "p1"}), "same slots"),
    (lambda s: s["interact"].update(scenario_ids=["s1"]), "at least"),
    (lambda s: s["interact"].update(unknown=1), "unknown"),
])
def test_interact_section_is_strictly_parsed(mutate, reason):
    raw = _defaults_with_interact()
    mutate(raw)
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_config.py -q -p no:cacheprovider -k interact_section`
Expected: FAIL,`unknown key(s) ['interact']`

- [ ] **Step 3: 实现**

`_DEFAULTS_KEYS` 加 `"interact"`、`"opponent"`,两者都可选:缺省时 `TrainingDefaults.interact` 与 `.opponent` 为 `None`,`training_defaults.yaml` 与今天逐字节相同。

`interact` 段严格解析:键集合恰好 `trained_slot`、`slots`、`max_turns_per_episode`、`scenario_ids`、`persona_ids`、`data_spec`;`scenario_ids` 与 `data_spec` 二选一(同时写或都缺都抛),`persona_ids` 必须与 `scenario_ids` 成对,`persona_ids` 的键集合必须等于 `slots` 的 `slot_id` 集合;`scenario_ids` 至少一个元素且唯一;`slot_id` 唯一;`trained_slot` 必须在 `slots` 里。

`opponent` 段严格解析为 `{base_url: str, model: str, api_key_env: str}`,三者必填,`api_key_env` 只存变量名。

解析不读树(解析函数没有 `store` 参数),点名的 id 在不在树里由 Task 8 的装配期检查负责。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_config.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/config.py tests/test_ts_config.py
git commit -m "feat(trainscheduler): training_defaults 支持 interact 与 opponent 段"
```

---

### Task 3: `new` 加 `interact` 的领域校验与动作解析

**Files:**
- Modify: `src/trainscheduler/resolve.py`(`validate_decision`、`resolve_action`)
- Test: `tests/test_ts_resolve.py`

**Interfaces:**
- Consumes: Task 2 的 `TrainingDefaults.interact`。
- Produces: `resolve_action` 对 `new` + `interact` 返回 `ResolvedAction(training_config=<interact 形状>, data_spec=<生成路或 None>)`。

- [ ] **Step 1: 写失败的测试**

```python
def test_new_interact_is_refused_without_the_section(store, defaults_without_interact):
    assert "interact" in validate_decision(store, _decision("new", "interact"),
                                           config=SchedulerConfig(allow_interact=True))

def test_new_interact_resolves_from_the_section(store, defaults_with_interact):
    resolved = resolve_action(store, _decision("new", "interact"),
                              config=SchedulerConfig(allow_interact=True),
                              defaults=defaults_with_interact)
    cfg = resolved.training_config
    assert (cfg["mode"], cfg["trained_slot"], cfg["sft_steps"]) == ("interact", "a", 0)
    assert cfg["scenario_ids"] == ["s1", "s2"]
    assert [b["slot_id"] for b in cfg["bindings"]] == ["a", "b"]

def test_named_path_forces_sft_steps_to_zero(store, defaults_with_interact):
    resolved = resolve_action(store, _decision("new", "interact"), config=SchedulerConfig(allow_interact=True),
                              defaults=defaults_with_interact)
    assert resolved.training_config["sft_steps"] == 0 and resolved.data_spec is None

def test_new_interact_still_needs_allow_interact(store, defaults_with_interact):
    assert "allow_interact" in validate_decision(store, _decision("new", "interact"), config=SchedulerConfig())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_resolve.py -q -p no:cacheprovider -k new_interact`
Expected: FAIL,现在的违规原因是「training_defaults does not provide」

- [ ] **Step 3: 实现**

`validate_decision` 的 `new` + `interact` 分支:`allow_interact` 关时照旧拒;开着但 `defaults` 没有 `interact` 段时拒,原因点名缺段。注意 `validate_decision` 现在只拿 `config`,要加一个可选参数 `defaults: TrainingDefaults | None = None` 并让调用点传入。

`resolve_action` 的 `new` 分支:`interact` 时以默认 batch 配置为底,覆盖 `mode`、`trained_slot`、`max_turns_per_episode`,把 `sft_steps` 强制为 `0`,`scenario_ids` 与 `bindings` 按段填(生成路两者都为空),`data_spec` 取段里的(点名路为 `None`),基座照 batch 的规则取最新已晋级模型。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_resolve.py tests/test_ts_plan_handler.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/resolve.py tests/test_ts_resolve.py
git commit -m "feat(trainscheduler): new 可构造 interact 指令"
```

---

### Task 4: 指令构造的宽口径与 `data_spec` 条件写入

**Files:**
- Modify: `src/trainscheduler/directive.py`(`check_directive`、`directive_payload`)
- Modify: `src/mindtrainer/training_config.py`(新增 `validate_pending_interact`)
- Test: `tests/test_ts_directive.py`、`tests/test_mt_config.py`

**Interfaces:**
- Produces: `validate_pending_interact(cfg: TrainingConfig) -> str | None`,只给 `check_directive` 用。
- Consumes: Task 3 的 `resolve_action`。

- [ ] **Step 1: 写失败的测试**

```python
def test_directive_construction_allows_a_pending_interact_config():
    resolved = ResolvedAction("tom", "new", "", _interact_cfg(scenario_ids=[], bindings=[]), _spec(), None)
    payload = directive_payload(resolved, new_run_id="run-002", planner_decision_id="planner-run-001")
    assert "data_spec" in payload
    assert payload["training_config"]["scenario_ids"] == []   # 回写前为空,放行

def test_named_path_omits_data_spec_instead_of_crashing():
    resolved = ResolvedAction("tom", "new", "", _interact_cfg(scenario_ids=["s1", "s2"],
                                                              bindings=_bindings()), None, None)
    payload = directive_payload(resolved, new_run_id="run-002", planner_decision_id="planner-run-001")
    assert "data_spec" not in payload

def test_strict_validation_still_rejects_an_empty_scenario_list():
    with pytest.raises(InvalidTrainingConfigError):
        validate_training_config(_interact_cfg(scenario_ids=[], bindings=[]))
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL,`AttributeError: 'NoneType' object has no attribute 'to_dict'`

- [ ] **Step 3: 实现**

`directive_payload` 的 `new` 分支:`resolved.data_spec` 为 `None` 时不写 `data_spec` 键。

`validate_pending_interact(cfg)`:`mode == "interact"`、`scenario_ids == []`、`bindings == []` 三者同时成立时,把 `cfg` 用 `dataclasses.replace` 换上占位 `scenario_ids=["pending"] * groups_per_step` 与按 `interact` 段 `slots` 构造的占位 `bindings`(`persona_id="pending"`)后跑 `validate_training_config`,返回其结果;否则直接返回 `validate_training_config(cfg)`。占位值只用于校验,不写进指令。

`check_directive`:指令带 `data_spec` 时用 `validate_pending_interact`,否则照旧 `validate_training_config`。`validate_training_config` 本身一个字不改。

注意 `_check_bindings` 要求 `persona_id` 是 `str`(`src/mindtrainer/training_config.py:231-234`),所以生成路回写前 `bindings` 只能是空列表,不存在"有绑定但缺 `persona_id`"这种中间态。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_ts_directive.py tests/test_mt_config.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/directive.py src/mindtrainer/training_config.py tests/test_ts_directive.py tests/test_mt_config.py
git commit -m "feat: 指令构造对生成路 interact 用宽口径,点名路不写 data_spec"
```

---

### Task 5: 派发器的生成、回写与严格复校

**Files:**
- Modify: `src/mindtrainer/dispatch.py`(新增 `_prepare_interact`,在 `_synthesize_missing` 之前调用)
- Modify: `src/mindtrainer/data_producer.py`(`TemplateDataProducer.produce` 写 `scenarios/` 与 `personas/`)
- Test: `tests/test_mt_dispatch_synthesis.py`

**Interfaces:**
- Consumes: Task 4 的 `validate_pending_interact`、Task 1 的 `Scenario`。
- Produces: `_prepare_interact(store, run_id, cfg, directive, data_producer) -> TrainingConfig`,返回回写后的配置;失败抛 `DirectiveFailedError`(T2)。`TemplateDataProducer.world_for_interact(cfg, data_spec) -> tuple[list[Scenario], list[Persona]]`。

调用顺序是硬约束:`dispatch` 今天一进来就跑严格校验(`src/mindtrainer/dispatch.py:205`),生成路指令在那一步就会被判 failed、走不到回写。所以 `_prepare_interact` 必须插在 `load_config_from_directive` 之后、严格校验之前;它返回的配置再交给严格校验。

- [ ] **Step 1: 写失败的测试**

```python
def test_interact_write_back_fills_scenario_ids_and_personas(store):
    _directive(store, mode="interact", scenario_ids=[], bindings=[], data_spec=SPEC)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=_fake_client_factory, policy=ScriptedBackend({"a": ["x"] * 8}),
        data_producer=TemplateDataProducer()))
    cfg = store.read_product(directive_file(RUN)).payload["training_config"]
    assert len(cfg["scenario_ids"]) == SPEC["scenario_count"]
    assert all(b["persona_id"] for b in cfg["bindings"])
    assert store.exists(scenario_file(cfg["scenario_ids"][0]))

def test_write_back_happens_once_and_stays_stable(store):
    ...  # 跑两次 reconcile,断言 directive 内容不变、提交数不变

def test_a_write_back_that_still_fails_strict_validation_is_t2(store):
    ...  # 生产器只写一个情境,groups_per_step=2,断言指令 failed 且原因点名 groups_per_step
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_dispatch_synthesis.py -q -p no:cacheprovider -k write_back`
Expected: FAIL,`scenario_ids` 仍为空

- [ ] **Step 3: 实现**

`TemplateDataProducer.produce` 第一步就判 `stages_to_synthesize`,缺清单时才干活,所以不能指望它顺带生成情境人格。新增独立方法 `world_for_interact(cfg, data_spec)`:复用 `_world` 的生成逻辑,返回情境与人格,不写 episode。

`_prepare_interact`:`cfg.scenario_ids` 非空时原样返回(回写只做一次,重入幂等);为空时要求指令带 `data_spec` 且注入了生产器(缺任一抛 `DirectiveFailedError`),调 `world_for_interact`,用 `simulator.store_adapter.write_scenario` 与 `write_persona` 落树,按 `interact` 段 `slots` 的顺序组出带 `persona_id` 的 `bindings`,写回 `directive.payload["training_config"]` 后 `store.write_product(directive_file(run_id), env, "mindtrainer: write back interact scenarios")`,返回新的 `TrainingConfig`。之后 `dispatch` 照常跑严格校验,不合法判 T2。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_dispatch_synthesis.py tests/test_mt_dispatch.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/mindtrainer/dispatch.py src/mindtrainer/data_producer.py tests/test_mt_dispatch_synthesis.py
git commit -m "feat(mindtrainer): 派发时生成情境人格并回写指令,回写后严格复校"
```

---

### Task 6: 三种对手驱动

**Files:**
- Create: `src/factory/opponents.py`(驱动选择、`model_ref` 解析、外部 API 客户端)
- Modify: `src/factory/agent.py`(`AgentRuntime._backends`)
- Test: `tests/test_factory_opponents.py`(新建)、`tests/test_factory_handlers.py`

**Interfaces:**
- Produces: `resolve_model_ref(store, model_ref: str) -> BigRef`(T2 失败);`ExternalApiBackend(base_url, model, api_key, timeout_s)` 实现 `respond`;`driver_for(binding) -> str`。
- Consumes: `controlplane.models` 的模型产物读取。

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.parametrize("binding, expected", [
    (RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1"), "vllm"),
    (RoleBinding("b", "p", "external_api", model_ref="gpt-x"), "external_api"),
    (RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted"), "scripted"),
])
def test_driver_defaults_follow_model_backend(binding, expected):
    assert driver_for(binding) == expected

def test_resolve_model_ref_rejects_a_missing_or_non_adapter_model(store):
    with pytest.raises(ComputeError):   # 或专有的 ModelRefError
        resolve_model_ref(store, "zengen-v99")

def test_external_api_backend_reports_only_the_variable_name_on_missing_key():
    backend = ExternalApiBackend("http://x", "m", api_key="", api_key_env="FORGE_OPP_KEY")
    with pytest.raises(OpponentCredentialError) as info:
        backend.respond(_persona(), _scenario(), [], "b")
    assert "FORGE_OPP_KEY" in str(info.value) and "sk-" not in str(info.value)
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL,`ModuleNotFoundError: factory.opponents`

- [ ] **Step 3: 实现**

`RoleBinding` 加可选 `driver` 字段(默认 `None`,不进 `to_dict` 当为空;`from_dict` 用 `d.get`)。校验:`driver` 只能是 `vllm` / `external_api` / `scripted`;`scripted` 允许与任何 `model_backend` 并存;`vllm` 要求 `model_ref` 非空。

`resolve_model_ref` 按 `zengen-vN` 找到模型产物、取 `checkpoint_ref`、核对 `fetch_hint` 是 `roll_ssh` 适配器导出,否则抛确定性错误。

`ExternalApiBackend.respond` 拼 `{"model":…, "messages":[{"role":"user","content": …}]}` POST 到 `{base_url}/v1/chat/completions`,`urllib.request`,超时取构造参数;缺凭证在构造时抛 `OpponentCredentialError`(只带变量名)。

`AgentRuntime._backends` 按 `driver_for(binding)` 分派:`scripted` 给 `ScriptedBackend(loop=True)`,台词优先取 `scenario.opponent_script[slot]`,否则取构造时传入的全局台词;`vllm` 与 `external_api` 由构造时传入的工厂给出后端。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_opponents.py tests/test_factory_handlers.py tests/test_sim_datamodel.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/simulator/datamodel.py src/factory/opponents.py src/factory/agent.py tests/test_factory_opponents.py
git commit -m "feat(factory): 对手位三驱动与 model_ref 解析"
```

---

### Task 7: `VllmServeConfig` 多适配器与策略工厂

**Files:**
- Modify: `src/compute/vllm_service.py`
- Modify: `src/mindtrainer/dispatch.py`(`policy_factory` 参数)
- Create: `src/factory/vllm_policy.py`(vLLM 策略后端与起停上下文)
- Test: `tests/test_factory_vllm_policy.py`(新建)、`tests/test_mt_dispatch.py`

**Interfaces:**
- Produces: `VllmServeConfig.adapters: tuple[tuple[str, str], ...]`;`make_mindtrainer_dispatcher(..., policy_factory: Callable[[str, BigRef], ModelBackend] | None = None)`;`policy_context(client, endpoint, adapters, *, serve=real_serve)` 上下文管理器,产出 `VllmPolicy`。
- Consumes: Task 6 的 `resolve_model_ref`。

- [ ] **Step 1: 写失败的测试**

```python
def test_serve_job_lists_every_adapter_and_sets_max_loras():
    cfg = VllmServeConfig(model_path="/m", adapters=(("trained", "/a"), ("opponent", "/b")), remote_port=8000)
    argv = list(build_serve_job(cfg).argv)
    assert argv[argv.index("--max-loras") + 1] == "2"
    i = argv.index("--lora-modules")
    assert argv[i + 1:i + 3] == ["trained=/a", "opponent=/b"]  # 一个开关后跟多个独立的 name=path 项

def test_serve_job_without_adapters_does_not_enable_lora():
    cfg = VllmServeConfig(model_path="/m", adapters=(), remote_port=8000)
    assert "--enable-lora" not in build_serve_job(cfg).argv

def test_policy_and_policy_factory_together_are_refused():
    with pytest.raises(ValueError, match="policy_factory"):
        make_mindtrainer_dispatcher(policy=ScriptedBackend({}), policy_factory=lambda *a: None)
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL,`TypeError: unexpected keyword argument 'adapters'`

- [ ] **Step 3: 实现**

`VllmServeConfig` 用 `adapters` 列表替换 `adapter_name`/`adapter_path`,`__post_init__` 校验每个名字符合 `_ADAPTER_NAME`、路径合法且不为基座名、名字不重复。`build_serve_job` 按列表生成 `--max-loras <n>` 与 `--lora-modules a=/p b=/q`(vLLM 的 `--lora-modules` 是 `nargs='+'`,一个开关后跟多个独立的 `name=path` 项,不是逗号拼接);空列表时不加 `--enable-lora`、`--max-loras`、`--lora-modules`。现有 `SomevalRunner` 的调用点改成 `adapters=((ADAPTER, path),)`。

`make_mindtrainer_dispatcher` 加 `policy_factory: Callable[[str, TrainingConfig], AbstractContextManager[ModelBackend]]`;两者同时给时 `ValueError`。派发 interact 指令时用 `with (nullcontext(policy) if policy is not None else policy_factory(run_id, cfg)) as pol:` 包住 `_interact_sources(...)` 与 `resume_or_run(...)`,且这个 `with` 在分类异常的 `try` 之内:进入时抛的延后异常照常上抛、指令保持 pending,此时训练记录一个字节都没写;进入时抛的确定性错误照常判 `failed`。返回上下文而不是后端,是因为训练位只在推理服务运行期间可用、会话登记也跨同一段,两件事要在同一次进出里做。旧 `policy=` 路径用 `nullcontext` 包一层,语义不变。

`factory/vllm_policy.py` 提供 `policy_context(client, endpoint, adapters, *, serve=real_serve)`:返回上下文管理器,进入时调 `serve(...)` 起服务并给出 `VllmPolicy`(`respond` 向本地隧道端口 POST `/v1/chat/completions`,`model` 填适配器名或 `BASE_MODEL_NAME`),退出时停服务放租约。`serve` 可注入,照 `src/factory/sombench_runner.py:84` 的先例,测试里传一个本地假服务。

`mindtrainer` 不导入 `compute`:派发器只拿到组合根注入的 `policy_factory`,上下文的进出由组合根包在派发外层。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_vllm_policy.py tests/test_mt_dispatch.py tests/test_factory_sombench_runner.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/compute/vllm_service.py src/mindtrainer/dispatch.py src/factory/vllm_policy.py tests/test_factory_vllm_policy.py
git commit -m "feat: vLLM 可挂多个适配器,派发器支持按 run 构造策略"
```

---

### Task 8: 超时口径、`policy_backend` 记录与 `--interact` 装配

**Files:**
- Modify: `src/factory/agent.py`(超时参数)
- Modify: `src/mindtrainer/datamodel.py`(`TrainingRun.policy_backend`)
- Modify: `src/factory/wire.py`(`FactoryOptions.interact`、装配与十一条拒绝项)
- Modify: `src/factory/cli.py`(`--interact`)
- Test: `tests/test_factory_wire.py`、`tests/test_mt_training_run.py`

**Interfaces:**
- Produces: `FactoryOptions.interact: bool = False`;`TrainingRun.policy_backend: dict | None = None`。
- Consumes: Task 7 的 `policy_context`、Task 6 的驱动选择。

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.parametrize("argv, reason", [
    (["--interact", "--executor", "roll"], "--executor roll"),
    (["--interact", "--endpoint"], ...),
])
def test_interact_assembly_refusals(argv, reason):
    ...  # 每一条拒绝项一条用例,共十一条

def test_interact_records_the_policy_backend(store):
    ...  # 跑一轮后读 training_run,断言 policy_backend 的 trained.kind == "base"
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL,`unrecognized arguments: --interact`

- [ ] **Step 3: 实现**

`TrainingRun` 加 `policy_backend: dict | None = None`(`to_dict` 只在非空时写这个键,保持既有记录的序列化不变;`from_dict` 用 `d.get`)。

写入点:`TrainingRun` 只在 `src/mindtrainer/orchestrator.py:180` 构造,`resume_or_run` 没有能带它的参数。给 `resume_or_run` 与 `run_training` 加关键字参数 `policy_backend: dict | None = None`,在第 180 行构造新记录时填进去,续训时保留已有值。派发器 interact 分支传入:`{"trained": {"ref": <base_checkpoint_ref.uri>, "kind": "base"|"adapter", "endpoint": <端点名>}, "opponents": {<slot>: {"driver": ..., "source": ...}}, "real_inference_with_mock_training": True}`。batch 分支不传。

`AgentRuntime` 的超时:`turn_timeout_s` 默认由装配传 `30.0`,流 deadline 由 `max_turns × 30` 算。

`--interact` 装配:注入 `TemplateDataProducer`;构造 `AgentRuntime` 与策略工厂;`--interact` 允许与 `--endpoint` 同用;十一条拒绝项按 `docs/design/90-factory-and-cli.md` 第 6.1 节的表实现,退出码 2、一行错误、树里不写东西。

落盘:`on_session_closed` 回调只收到 `Episode`(见 `tests/test_realtime_persistence.py:25-26`),不知道它属于哪个 run。`AgentRuntime` 加 `register_session(scenario_id: str, run_id: str)` 与 `unregister_session(scenario_id)`,派发器在开会话前登记、结束后注销;回调按 `episode.scenario_id` 查登记表,查到就 `write_episode(store, run_id, episode)`,查不到(部署会话)不写。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_wire.py tests/test_factory_cli.py tests/test_mt_training_run.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/agent.py src/mindtrainer/datamodel.py src/factory/wire.py src/factory/cli.py tests/test_factory_wire.py tests/test_mt_training_run.py
git commit -m "feat(factory): --interact 装配、十一条拒绝项、policy_backend 记录"
```

---

### Task 9: 端到端与文档收尾

**Files:**
- Test: `tests/test_factory_interact_e2e.py`(新建)
- Modify: `docs/design/00-overview.md`、`20-socialsimulator.md`、`30-mindtrainer.md`、`70-trainscheduler.md`、`90-factory-and-cli.md`(把本次实现的条目从 `[计划中]` 改成 `[已落地]`)

**Interfaces:**
- Consumes: 前八个任务的全部。

- [ ] **Step 1: 写端到端测试**

```python
def test_an_interact_directive_runs_through_the_real_entry(store):
    # 起一个假的 vLLM(本地 HTTP 服务,回固定文本)替代 compute
    # 断言:指令 executed、训练记录 executed、候选 pending_review、
    #      episode 都在 run 目录里、training_run 的 policy_backend 在场
```

- [ ] **Step 2: 跑它确认失败,再实现到通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_interact_e2e.py -q -p no:cacheprovider`
Expected: 先 FAIL,反复到 PASS

- [ ] **Step 3: 跑全量**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider`
Expected: 全通过;若出现与本次无关的偶发失败,重跑确认后再记

- [ ] **Step 4: 改文档状态标记并检查**

```bash
cd docs/design && PYTHONDONTWRITEBYTECODE=1 uv run python tools/check_docs.py && \
  PYTHONDONTWRITEBYTECODE=1 uv run python tools/check_consistency.py
```
Expected: 都是 0 错误 0 告警

- [ ] **Step 5: 提交**

```bash
git add tests/test_factory_interact_e2e.py docs/design
git commit -m "test: interact 在真实入口的端到端用例;文档状态转已落地"
```
