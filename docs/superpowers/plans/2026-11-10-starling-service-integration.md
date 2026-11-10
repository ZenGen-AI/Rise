# rise 接 Starling 记忆服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rise 的记忆从"进程内按路径开 Starling 库"换成"经纯 Python 客户端 `starling_client` 连 Starling 记忆服务"：世界、快照、分叉都按名字交给服务，rise 不再打开库文件；缺省连用户已在跑的看板服务（外部模式），测试与没有看板的机器由 rise 自己拉起服务（拉起模式）。记忆语义不变，替身路径一行不受影响。

**Architecture:** 端口 `MemoryBackend` 五个方法不变，适配器 `StarlingMemory` 内部对象从 `starling.Memory` 换成客户端的 `RemoteMemory`。部署管理面改签名：放弃事件带主体、`pending_events` 返回 `PendingKey`、快照按 id（`snapshot`/`restore`/`delete_snapshot`）、验证重放从快照分叉可回收世界（`fork_replay`/`release_replay`/只读的 `open_replay`）；快照 id 记在记忆根下的在途记录 `.rec.json`。替身与 Starling 两个实现过同一套契约测试。组合根新增三个模块：世界名与 rise 设置（纯函数）、服务进程的拉起与残留核对、服务会话 ServiceSession（代号、按代号的端口计数、排空窗口、管理传输、重连再核对、致命停机）。训练 rollout 世界改为从种子分叉、只关不删；`factory seed` 从部署世界建种子，`bind` 按起点摘要钉住种子版本。

**Tech Stack:** Python 3.12，pytest（`uv run --offline pytest`），Starling `cff319d952360727823024ff43c71b15055bd8e3`（第 1 至 4 期已合并；可选 extra `memory`：第 1、2 步是 `starling-memory` 一行，第 3 步起是 `starling-client`（`#subdirectory=clients/python`）与 `starling-memory[dashboard]` 两行，钉同一提交），真 `starling-serve` 子进程，本机回环的假抽取与嵌入端点（`tests/starling_fakes.py`），gRPC 回环服务端。

**Spec:** 权威设计在 `docs/design/`（00、20、30、40、50、90 篇里标 [计划中] 的接服务内容；`README.md` 与 `STYLE.md`）。历史规格 `docs/superpowers/specs/2026-11-08-starling-service-integration-design.md` 只在 docs/design 指向它的细节处用。两者冲突以 `docs/design/` 为准；计划与 docs/design 冲突也以 docs/design 为准，执行者发现冲突先停下报告，不自行发明行为（开工前已裁定的几项见文末「已定」）。Starling 侧引用写成 `S:<路径>:<行>`，指 `/Users/jaredguo-mini/develop/memory/starling` 在 `cff319d` 的文件。

## Global Constraints

- 面向用户的文字、注释与提交信息用中文；代码、标识符原样。注释密度与命名照周围代码。提交信息不加 Co-Authored-By 行。逐个文件 `git add`，不用 `git add -A`。
- **测试命令与基线。** 全量：`uv run --offline pytest -q -m 'not slow' -p no:cacheprovider`。基线（`bda664a`，2026-11-10 实测，11 分 47 秒）：`4811 passed, 13 skipped, 56 deselected`。总数会变，不许有失败。每个任务的 Step 4 只跑它列出的文件；全量由控制者在每个任务提交后于后台跑，与评审并行。**每一步（第 0 至 5 步）的最后一个任务结束时全量必须绿**；同一步内的中间任务允许下文列出的红窗口，不为它们加兼容层。
- **装依赖。** 第 3 步改了锁文件之后，本机先跑一次 `uv sync --extra dev --extra memory`（要编译 Starling 的 C++，2026-11-10 在干净 venv 里实测 2 分 38 秒）。`uv run` 不删多余的包，所以不重新 sync 时 `starling_client` 不在，真服务的测试会被 `importorskip` 跳过、看起来是绿的。第 3 步之后的每个任务 Step 4 先确认 `uv run --offline python -c "import starling_client, starling.service"` 成功。
- **红窗口。** Task 2 结束到 Task 3 结束：Starling 部署管理面的签名落后于端口协议，`tests/test_simple_actio_deployment_store_contract.py`、`tests/test_simple_actio_starling_store.py`、`tests/test_ma_deployment_starling.py` 的 Starling 参数会失败。Task 4 结束到 Task 7 结束：管理面的快照接口与布局变了，契约测试与 `tests/test_simple_actio_starling_store.py` 的 Starling 一侧到 Task 5 才恢复，`tests/test_ma_*.py`、`tests/test_factory_handlers.py`、`tests/test_factory_memory_wiring.py`、`tests/test_factory_memory_runtime.py` 里与部署相关的用例到 Task 7 才恢复。Task 13 至 17 在分支上做（见下一条），分支上的中间提交不要求绿。
- **第 4 步是一次提交**（00 篇施工表：回退它就回到进程内 `cff319d`）。Task 13 至 17 在本地分支 `design4-step4` 上逐任务提交；Task 17 的最后一步在全量绿之后把分支 `git merge --squash` 成 main 上的一个提交，**不停下等用户**：合并只在本地 main 上做，分支 `design4-step4` 保留不删，随时能对照或回退。推送仍要先问用户。
- **真服务的测试。** 起真的 `starling-serve` 子进程，照 `S:tests/python/fakes/service_proc.py:35-147` 的写法，夹具在 `tests/starling_service.py`（Task 8 建）：`[sys.executable, "-m", "starling.service", "--data-dir", D, "--host", "127.0.0.1", "--port", "0", "--token-file", F]`，环境去掉 `OPENAI_*` 与 `STARLING_*`，`HOME` 与数据目录都在 pytest 的临时目录下，`OPENAI_API_KEY` 只放占位值 `offline-test-key`；模块级共用一个服务。抽取与嵌入走 `tests/starling_fakes.py` 的假端点。没装客户端或服务端时用 `pytest.importorskip("starling_client")`、`pytest.importorskip("starling.service")` 跳过。
- **不碰用户的东西。** 任何测试与核实步骤都不读写用户的 `~/.starling`，不连正在跑的 8787 看板服务，不读开发者环境里的 `RISE_MEMORY_CONFIG`（测试一律传显式的 `env` 字典）。
- **导入边界。** 第 3 步起，`starling_client` 只由 `src/simple_actio/memory_starling.py` 与 `src/factory/starling_session.py` 导入；第 4 步起 `src/` 下没有任何文件导入 `starling`（拉起模式用子进程 `python -m starling.service`，不是导入；安装来源经 `importlib.metadata` 读）。守卫：`tests/test_factory_cross_plan.py:204-212`（`test_only_simple_actio_memory_starling_imports_starling`，第 3 步扩成两条）、`tests/test_simple_actio_instance.py:655-674`（`simple_actio` 的导入白名单）、`tests/test_factory_boundaries.py`、`tests/test_import_boundaries.py` 等 `tests/test_*_boundaries.py` 的语法树扫描。没设 `RISE_MEMORY_CONFIG` 时不导入 `starling_client`，由装配测试钉住。
- **失败文字**只含错误码、异常类名、id 与世界名，绝不含 token、端点地址与记忆根的绝对路径。客户端异常的文字写成 `starling <code> (<类名>)`。涉及 token 的用例都断言 token 不在 `str(exc)`、`caplog.text` 与 `service.log` 里。
- **替身路径不动。** 没装 extra `memory`、没设 `RISE_MEMORY_CONFIG` 时，`memory_wiring` 直接返回替身，不读配置、不导入客户端、不拉起服务（`src/factory/memory_runtime.py:724-730`）。替身只在第 1、2 步改接口形状，由契约测试钉住与 Starling 同语义；替身摘要的格式不变，替身 run 的 `config_hash` 不变。
- **常量**（单一来源，写成模块常量并在测试里钉住）：

| 常量 | 值 | 位置 |
|---|---|---|
| `SESSION_CLOSE_WAIT_S` | 70.0（已有） | `src/mindagent/deployment.py` |
| 句柄调用超时 | 30.0 秒（`connect` 缺省） | `factory.starling_session.HANDLE_TIMEOUT_S` |
| 管理调用超时 | 300.0 秒 | `factory.starling_session.ADMIN_TIMEOUT_S` |
| 排空窗口 | 60.0 秒 | `factory.starling_session.DRAIN_WINDOW_S` |
| 拉起：等 ready | 60.0 秒 | `factory.starling_service.READY_TIMEOUT_S` |
| 拉起：SIGTERM 后等 / SIGKILL 后等 | 40.0 / 5.0 秒 | `factory.starling_service` |
| 拉起：重启退避与上限 | 1、4、16 秒，至多 3 次 | `factory.starling_service` |
| 分叉 `world_exists` 换后缀 | 至多 3 次 | `factory.memory_runtime` |
| 世界名上限 / 超长替换 | 128 字符 / 中间 id 段换成 sha256 前 24 位 | `factory.starling_names` |
| 随机后缀 | 8 位十六进制，调用之前定 | `factory.starling_names` |
| `root` 标签 | 记忆根绝对路径的 sha256 前 16 位 | `factory.starling_names` |
| `world_prefix` | 缺省 `rise`，`[A-Za-z0-9][A-Za-z0-9_-]{0,15}` | `factory.memory_config` |

- `world_id` 与 `holder` 照旧过 `PERSONA_ID_PATTERN`（`src/simple_actio/memory_port.py:13`，`fullmatch`）。事件 key 的格式不变。
- `docs/design/` 的状态标记在 Task 20 统一从 [计划中] 改 [已落地]。例外是 Task 1：它只把第 0 步的核实结果写进 00 篇风险表第 32、37 行。改 `docs/design/` 的提交前跑 `python3 docs/design/tools/sync_docs.py`、`check_docs.py`、`check_consistency.py`，0 错误。

## Review Focus

- **重连与代号的竞态**：线程 A 在第 1 代会话的端口上调用，会话过期；线程 B 下一次需要服务时换成第 2 代。A 的调用必须抛 `SessionExpired`（译成瞬态），不能撞上已关的传输；第 1 代的 `Client` 要等它的端口计数归零才 `close()`。Task 12 加两线程用例，用可控的假传输让过期发生在调用途中。
- **外部服务重启后的 `handle_closed`**：外部模式下服务停了又在同一端口、同一 token 上起来，旧端口的 `recall` 回 `handle_closed`。rise 没关过的端口要判瞬态并把这一代会话标为丢失，下一次开句柄换新会话；rise 自己关过的端口照旧判 `MemoryClosedError`（永久）。换会话后 `info()` 的提交变了就进致命状态。Task 14 用真服务在同一端口重启来测。
- **分叉超时后按确切世界名加摘要复用**：`fork` 在服务端已建好世界、客户端却超时。rise 按调用前定下的确切世界名查 `list_worlds(with_digest=True)`，摘要等于钉住的摘要才用；摘要不等就换后缀重建；不能按 `(episode_id, attempt)` 查到上一个进程留下、已有写入的世界。Task 15 加三条用例（复用、摘要不等换后缀、上一个进程的同 episode 世界不被认领）。
- **残留服务的误认**：pid 被复用（pid 指向一个无关的活进程）、命令行里的 `--data-dir` 不同、命令行被截断、归属锁被另一个进程持有、`D/.lock` 已经没人持。前四种都拒绝启动且不发任何信号、那个进程还活着；最后一种不发信号直接重试启动。Task 11 注入读命令行与读属主的函数，每种情形一条用例。
- **叶子继承**：看板默认世界的 `source_lane` 子键（如 `k_source`、`tau_source`）与 provider 的 `enable_thinking` 不是默认值时，rise 建的世界与分叉出的世界仍与 rise 设置逐叶相等，`open(settings=rise 设置)` 不报 `world_settings_mismatch`。Task 9 用拉起的真服务、在 `D/starling.json` 里预放非默认值来测。
- **第 2 至 4 步之间的过渡布局**：第 2、3 步里进程内 Starling 后端的 `deploy/*.db`、`held/`、`replay/`、`seeds/`、`runs/` 合法，`snapshots/` 下的 `.db`、`.world` 与不带 `.rec` 的 `deploy-*.json` 判旧布局；第 4 步起 Starling 后端切到最终规则（`deploy/` 下带数据后缀的文件只许 `<已知情境>.created`；`seeds/`、`runs/` 与 `held/`、`replay/` 下的 `.db` 判旧布局）。替身的规则在两段里相同。两种规则都只查 `.json`、`.db`、`.created`、`.rec.json` 四种数据后缀，`.tmp`、`-wal`、`-shm`、`.lock` 不查。Task 6 与 Task 17 各加一组用例，同一个记忆根在两种规则下得到不同结论。
- **设置核对在持仓库锁之后**：第二个 `run` 撞上仓库锁之前不能开过任何句柄，否则在跑守护进程的恢复会收到 `world_held` 判 T3。Task 16 断言装配期不开句柄，核对只在 `on_locked` 里做。
- **恢复的分码**：`wait_closed` 返回真之后，恢复收到 `world_busy` 判瞬态（轮次延后、重入先恢复），`world_held`、`snapshot_not_found`、`restore_digest_mismatch` 判 `RestoreFailedError`（记录与快照都保留）。Task 7（替身与假管理面）与 Task 14（真服务）各钉一次。

---
## 第 0 步：依赖可锁、可装，提交可比

### Task 1: 核实风险 32 与 37（只改依赖与锁文件之外的一行文档）

本步只核实，不改 `pyproject.toml` 的正式内容：钉住版本的升级在第 1 步，两行 extra 在第 3 步。核实在临时副本里做，主干的 `pyproject.toml` 与 `uv.lock` 不动。

**Files:**
- Create: `scripts/check_starling_deps.py`（核实脚本，只用标准库与 `subprocess` 调 `uv`；可重复跑，升级 Starling 时重跑）
- Modify: `docs/design/00-overview.md:920`（风险第 32 行）、`:925`（风险第 37 行）：写核实结果
- Test: 脚本自身（它的输出就是本步的"测试"）

**Interfaces:**
- Produces: `python3 scripts/check_starling_deps.py --commit <40 位提交> --workdir <临时目录>`，在 `<临时目录>` 里放 `pyproject.toml` 与 `uv.lock` 的副本，把 extra `memory` 换成两行后跑 `uv lock`、`uv sync --frozen --extra memory --no-install-project`，起一次 `python -m starling.service`（数据目录、HOME 都在 `<临时目录>` 下，`OPENAI_API_KEY=offline-test-key`），用客户端调 `info()`，再用 `starling_client.Transport(url, token, 300.0)` 发一次 `POST /api/v1/client/admin/list_worlds`。逐项打印 `OK <项>` 或 `FAIL <项>: <原因>`，有 FAIL 时退出码 1。

**闸门：** 第 1 项（uv 锁得住两行）FAIL 时，计划停在这里，不进第 1 步，向用户报告。其余项 FAIL 时按风险表的"影响"一列改设计（例如服务报 `unknown` 就改成拉起模式只比客户端提交），先改 `docs/design/`，再回来改本计划。

- [x] **Step 1: 写核实脚本**

```python
# scripts/check_starling_deps.py(节选:检查项与判据)
CHECKS = (
    "uv lock resolves starling-client (subdirectory=clients/python) and starling-memory[dashboard] at one commit",
    "uv sync installs httpx2 from PyPI",
    "both dist-infos record vcs_info.commit_id == --commit",
    "starling_client.COMMIT == --commit",
    "service info()['commit'] == --commit and protocol == starling_client.PROTOCOL",
    "starling_client.Transport(url, token, timeout_s) and .post(path, json, *, timeout_s) keep their signatures",
    "a session-less admin POST through Transport succeeds",
)
# Transport 的签名按 inspect.signature 比字符串:
EXPECTED_INIT = "(self, url: 'str', token: 'str', timeout_s: 'float' = 30.0, *, _httpx_transport=None)"
EXPECTED_POST = "(self, path: 'str', json: 'dict | None' = None, *, timeout_s: 'float | None' = None) -> 'dict'"
```

脚本在 `<临时目录>/venv` 的解释器里跑探针（`uv run --project <临时目录> python -I -c ...`），不碰仓库的 `.venv`。token 由脚本生成、写进 0600 的文件、探针结束即删，不打印。

- [x] **Step 2: 跑脚本**

Run: `python3 scripts/check_starling_deps.py --commit cff319d952360727823024ff43c71b15055bd8e3 --workdir "$(mktemp -d)"`
Expected: 七行 `OK`，退出码 0。2026-11-10 已在临时副本里手工跑过同一组检查，结果如下，脚本的输出应与之一致：

| 项 | 实测 |
|---|---|
| `uv lock`（uv 0.10.7） | 成功，11 秒，`starling-client v0.1.0 (cff319d9)` 与 `starling-memory v0.0.1 (cff319d9)` 两项，新增 26 个包 |
| `httpx2` | 从 PyPI 装上 `httpx2==2.16.0` |
| `uv sync --extra memory` | 成功，2 分 38 秒（含 C++ 编译） |
| 两个 `direct_url.json` | `vcs_info.commit_id` 都是 `cff319d9…`；客户端那份另有 `"subdirectory": "clients/python"` |
| `starling_client.COMMIT` | `cff319d952360727823024ff43c71b15055bd8e3`（从 git URL 装时不是 `unknown`；只有源码树里未构建的副本是 `unknown`） |
| 服务 `info()` | `commit` 是完整的 `cff319d9…`，`version` `0.0.1`，`protocol` 1；`default_settings` 的顶层键是 `background`、`chat`、`extraction`、`gist_thresholds`、`source_lane`、`vector_backend` |
| `Transport` 签名 | 与上面两行逐字相同；`Transport` 在 `starling_client.__all__` 里 |
| 不带会话的管理调用 | `list_worlds` 成功；拉起到 ready 1.87 秒，SIGTERM 退出码 0，临时 HOME 下没有生成任何文件 |

- [x] **Step 3: 把结果写进风险表**

`docs/design/00-overview.md:920` 第 32 行与 `:925` 第 37 行改成已核实的写法：状态从 [计划中] 改为 [已落地]，"影响"一列改写为核实结论（锁得住、`httpx2` 装得上、git 安装报完整提交，拉起模式比两个提交的设计不用改；`Transport` 在 `cff319d` 的签名如上，第 3 步的测试钉住它），"缓解"一列写重跑办法（升级 Starling 时重跑 `scripts/check_starling_deps.py`）。不改别的行。

Run: `python3 docs/design/tools/sync_docs.py && python3 docs/design/tools/check_docs.py && python3 docs/design/tools/check_consistency.py`
Expected: 三者 0 个错误

- [x] **Step 4: 确认主干没变**

Run: `git diff --stat -- pyproject.toml uv.lock`
Expected: 空输出

- [x] **Step 5: 提交**

```bash
git add scripts/check_starling_deps.py docs/design/00-overview.md
git commit -m "chore(deps): 核实 Starling 客户端与服务两行依赖可锁可装、提交可比(风险 32、37)"
```

## 第 1 步：进程内升到 `cff319d`，放弃事件带主体，写入判据改严

本步结束时 Starling 路径仍是进程内（`import starling`）。从这一步起，记忆后端是 Starling 的 interact run 续训判 T3（30 篇 MT-35 一行、90 篇"对实验的影响"）：库第一次打开就迁到 0039，摘要会变。

### Task 2: 端口协议、替身与三个调用处按主体放弃

**Files:**
- Modify: `src/simple_actio/memory_port.py:96-135`（`DeploymentMemoryStore` 的 `abandon_event`、`pending_events`；在 `RememberOutcome` 之后加冻结数据类 `PendingKey`）
- Modify: `src/simple_actio/memory.py:200-367`（`_Ledger`：事件表的键从 `key` 换成 `(holder, key)`，`begin`、`_row`、`commit`、`fail`、`abandon` 都收 `holder`；`to_json`/`from_json` 格式不变）、`:370-423`（`TupleMemory` 调账本时传自己的 `_holder`）、`:528-564`（`FileTupleStore.remember_event`、`abandon_event`、`pending_events`）
- Modify: `src/mindagent/admit.py:200-210`（两处 `abandon_event(sid, key)` 改 `abandon_event(sid, pid, key)`）
- Modify: `src/mindagent/deployment.py:453-457`（按 `PendingKey.key` 过滤前缀，`abandon_event(world, p.holder, p.key)`）；`:319-322` 的 docstring 同步
- Test: `tests/test_simple_actio_file_tuple_store.py`（`:335-541` 里 9 处 `pending_events`、`abandon_event` 的断言改形状，`:57-179` 里直接读 `led.events[...]` 的改成按 `(holder, key)` 取）、`tests/test_simple_actio_memory_port.py:36-60`、`tests/test_simple_actio_memory_experience.py:336-391`、`tests/test_simple_actio_instance.py:620`、`:630`、`tests/test_factory_memory_translate.py:108`、`tests/test_factory_interact_wire.py:766`、`tests/test_ma_admit.py`（12 处 `pending_events` 断言）、`tests/test_ma_deployment.py`（2 处）、`tests/test_ma_rounds.py`（2 处）、`tests/test_simple_actio_deployment_store_contract.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `simple_actio.memory_port.PendingKey`：`@dataclass(frozen=True)`，字段 `key: str`、`holder: str`。
  - `DeploymentMemoryStore.abandon_event(self, world_id: str, holder: str, key: str) -> None`：只找 `(holder, key)`；找不到抛 `MemoryFailureError(transient=False)`（与 Starling 的 `event_not_found` 同类）；已提交抛 `MemoryEventConflict`；已放弃是空操作。
  - `DeploymentMemoryStore.pending_events(self, world_id: str) -> list[PendingKey]`：全部主体的待处理事件，按 `seq` 升序。`holder` 为空串时抛 `MemoryFailureError(f"pending event {key!r} has no holder", transient=False)`（AC-30：rise 的世界不会有空主体的行，见到就是真问题）。
  - `_Ledger.events: dict[tuple[str, str], dict]`；`_Ledger.begin(event, *, holder)` 的去重键是 `(holder, event.key)`；`_Ledger.abandon(key, *, holder)`、`commit(key, *, holder)`、`fail(key, category, *, holder)`。账本 JSON 里的事件行本来就带 `holder`（`memory.py:271-273`），序列化与摘要不变，读旧账本时没有 `holder` 的行照旧补 `""`。同 `(holder, key)` 重复是 `SimpleActioDataError("memory ledger: duplicate event key ...")`，同 key 不同主体是两行。

- [x] **Step 1: 写失败的测试**（加进 `tests/test_simple_actio_deployment_store_contract.py`，参数化到两个实现；本任务只要求 `file` 参数通过，`starling` 参数由 Task 3 修）

```python
from simple_actio.memory_port import PendingKey


def test_the_same_key_under_two_holders_is_two_events(case):
    store = case.store
    case.fail(KEY)                                          # 两行都留 pending,好列出来
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    store.remember_event("s1", "pb", KEY, "pa：另一句", interlocutor="pa")
    assert store.pending_events("s1") == [PendingKey(KEY, "pa"), PendingKey(KEY, "pb")]


def test_abandoning_under_the_wrong_holder_is_a_permanent_failure(case):
    store = case.store
    case.fail(KEY)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pb", KEY)
    _assert_permanent(info)
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]     # 没被碰
    store.abandon_event("s1", "pa", KEY)
    assert store.pending_events("s1") == []
```

现有契约用例里的 `store.abandon_event("s1", KEY)` 改成 `store.abandon_event("s1", "pa", KEY)`，`== [KEY]` 改成 `== [PendingKey(KEY, "pa")]`。`tests/test_ma_admit.py` 的 `== [f"{PID}#2"]` 改成 `== [PendingKey(f"{PID}#2", "pa")]`（`MEMORY["persona_id"]` 是 `pa`）。替身专有的新用例加进 `tests/test_simple_actio_file_tuple_store.py`：

```python
def test_a_ledger_row_without_a_holder_reads_as_the_empty_holder_and_is_refused_by_pending_events(tmp_path):
    store = FileTupleStore(tmp_path)
    (tmp_path / "s1.json").write_text(json.dumps({
        "entries": [], "next_seq": 1, "holders": {},
        "events": [{"key": "k#0", "text": "t", "interlocutor": "pb", "seq": 0, "state": "pending",
                    "statement_ids": [], "failure_category": None}],
    }), encoding="utf-8")
    with pytest.raises(MemoryFailureError, match="has no holder") as info:
        store.pending_events("s1")
    assert info.value.transient is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_file_tuple_store.py -k "file or not starling"`
Expected: FAIL（`abandon_event()` 参数个数不对；`pending_events` 返回的是 `str`）

- [x] **Step 3: 实现**

按 Interfaces 改。`FileTupleStore.pending_events` 在锁外把行转成 `PendingKey`，顺序照旧按 `seq`。`deployment.py` 的提交段改成：

```python
prefix = f"{round_id}/{eid}#"
pending = _memory_op(f"pending events of world {world}", lambda: memory_store.pending_events(world))
for p in pending:
    if p.key.startswith(prefix):
        _memory_op(f"abandon {p.key}", lambda p=p: memory_store.abandon_event(world, p.holder, p.key))
```

`admit.py` 两处传 `pid`（循环里本来就有，`admit.py:198`）。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider -k "not starling" tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_file_tuple_store.py tests/test_simple_actio_memory_port.py tests/test_simple_actio_memory_experience.py tests/test_simple_actio_instance.py tests/test_simple_actio_memory_contract.py tests/test_factory_memory_translate.py tests/test_factory_interact_wire.py tests/test_ma_admit.py tests/test_ma_deployment.py tests/test_ma_rounds.py`
Expected: PASS（`-k "not starling"` 去掉的只有 Starling 参数，它们在 Task 3 修）

- [x] **Step 5: 提交**

```bash
git add src/simple_actio/memory_port.py src/simple_actio/memory.py src/mindagent/admit.py src/mindagent/deployment.py tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_file_tuple_store.py tests/test_simple_actio_memory_port.py tests/test_simple_actio_memory_experience.py tests/test_simple_actio_instance.py tests/test_factory_memory_translate.py tests/test_factory_interact_wire.py tests/test_ma_admit.py tests/test_ma_deployment.py tests/test_ma_rounds.py
git commit -m "feat(simple_actio): 放弃事件带主体,pending_events 返回 PendingKey;替身事件表按 (holder, key)"
```

### Task 3: 钉住版本升到 `cff319d`，Starling 管理面按主体放弃，写入判据改严，召回显式传参

**Files:**
- Modify: `pyproject.toml:15`（`starling-memory @ git+https://github.com/ruijieguo/starling@cff319d952360727823024ff43c71b15055bd8e3`，仍一行）、`uv.lock`（`uv lock` 重新生成）
- Modify: `src/simple_actio/memory_starling.py:110-119`（`_outcome` 加写入结果判据）、`:189`（`recall` 参数已显式，只补注释指向 AC-32 之外的 40 篇"召回显式传参"一行）、`:477-489`（`abandon_event` 以 `agent=holder` 开短连接；`pending_events` 返回 `PendingKey`）
- Modify: `tests/starling_fakes.py:7-13`（模块文档里的"Starling 730e415"改成 `cff319d`，两种故障类别在 `cff319d` 上实测仍是 `transport_failure` 与空串）
- Test: `tests/test_simple_actio_memory_starling.py:175-200`、`tests/test_simple_actio_starling_store.py:140-220`、`:700-780`、`tests/test_ma_deployment_starling.py:56-200`、`tests/test_simple_actio_deployment_store_contract.py`（Task 2 新增的两条的 `starling` 参数）

**Interfaces:**
- Consumes: Task 2 的 `PendingKey` 与协议签名。
- Produces:
  - `StarlingDeploymentStore.abandon_event(world_id, holder, key)`：`_check_name(holder, "holder")`，以 `agent=holder` 开短连接调 `memory.abandon_event(key)`。`cff319d` 的门面只放弃本主体的行，别的主体名下的 key 抛 `KeyError`（`S:python/starling/memory.py:472-497`），照旧译成永久失败。
  - `StarlingDeploymentStore.pending_events(world_id) -> list[PendingKey]`：`[PendingKey(p.key, p.holder) for p in memory.pending_events()]`；`p.holder == ""` 时抛永久失败（同 Task 2）。
  - `_outcome(memory, res)`：`res.extraction_failed` 为真照旧 `RememberOutcome((), True, category)`；否则 `res.outcome` 不在 `("accepted", "idempotent")` 里时抛 `MemoryFailureError(f"remember: outcome {res.outcome}", transient=False)`（AC-32）；其余照旧换内容 id。端口与管理面共用 `_outcome`，所以两处一起改严。
  - `StarlingMemory.recall` 照旧显式传 `strict=True, include_volatile=True`（`memory_starling.py:189` 已经如此），测试钉住。

- [x] **Step 1: 写失败的测试**

`tests/test_simple_actio_memory_starling.py`（单测用 `FakeStarling`，不起真库）：

```python
@pytest.mark.parametrize("outcome", ["no_store", "rejected", "source_missing", ""])
def test_a_write_whose_outcome_is_not_accepted_or_idempotent_is_a_permanent_failure(outcome):
    fake = FakeStarling()
    fake.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=[], outcome=outcome)
    with pytest.raises(MemoryFailureError, match=f"remember: outcome {outcome}$") as info:
        StarlingMemory(fake).remember(EV)
    assert info.value.transient is False


@pytest.mark.parametrize("outcome", ["accepted", "idempotent"])
def test_accepted_and_idempotent_writes_keep_their_statements(outcome):
    fake = FakeStarling()
    fake.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=["u-1"], outcome=outcome)
    fake.ids = {"u-1": "ab" * 32}
    assert StarlingMemory(fake).remember(EV).stored == ("mem-" + "ab" * 8,)


def test_a_failed_extraction_is_still_counted_whatever_the_outcome_says():
    fake = FakeStarling()        # cff319d 上抽取失败时 outcome 是 accepted、extraction_failed 为真(2026-11-10 实测)
    fake.result = SimpleNamespace(extraction_failed=True, failure_category="transport_failure", statement_ids=[],
                                  outcome="accepted")
    assert StarlingMemory(fake).remember(EV) == RememberOutcome((), True, "transport_failure")
```

现有的 `SimpleNamespace(extraction_failed=False, ...)`（`:177`、`:193`）补 `outcome="accepted"`。`tests/test_simple_actio_starling_store.py` 加：

```python
def test_abandon_acts_only_on_the_named_holders_event(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys.add(KEY)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pb", KEY)                  # cff319d:别的主体名下的 key 是 KeyError
    assert info.value.transient is False
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]
    store.abandon_event("s1", "pa", KEY)
    assert store.pending_events("s1") == []
```

已有的 `abandon_event("s1", KEY)` 全部补主体（`:151`、`:156`、`:172`、`:206`、`:710`、`:762`、`:775`），`pending_events` 的断言改成 `PendingKey`。`tests/test_ma_deployment_starling.py:63` 的 `[p.key for p in memory.pending_events()]` 改成 `[(p.key, p.holder) for p in ...]`，`:79`、`:154`、`:187` 的比较同步。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_memory_starling.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py tests/test_ma_deployment_starling.py`
Expected: FAIL（写入结果没判；`abandon_event` 参数个数不对；`pending_events` 返回 `str`）

- [x] **Step 3: 实现**

先升依赖：`pyproject.toml:15` 换提交，跑 `uv lock` 与 `uv sync --extra dev --extra memory`。只升依赖、不改代码时，在 `cff319d` 上有 8 条用例失败（2026-11-10 在临时 venv 里实测，`293 passed, 8 failed`），全是"以 `agent="world"` 放弃别的主体的事件抛 `KeyError`"及其连锁（如 `test_a_deterministic_admission_extraction_failure_rejects_and_abandons_the_event` 停在 approved），正是本任务要修的。然后按 Interfaces 改 `memory_starling.py`。`_WORLD_AGENT`（`:50`）只剩 `pending_events`、`digest`、`backup_to`、`restore_from` 用。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_memory_starling.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_memory_contract.py tests/test_ma_deployment_starling.py tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py tests/test_factory_memory_wiring.py tests/test_starling_fakes.py`
Expected: PASS。这是第 1 步的最后一个任务：控制者在后台跑全量，必须绿。

- [x] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock src/simple_actio/memory_starling.py tests/starling_fakes.py tests/test_simple_actio_memory_starling.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py tests/test_ma_deployment_starling.py
git commit -m "feat(simple_actio): 进程内 Starling 升到 cff319d,按主体放弃;写入结果不是 accepted 或 idempotent 判永久失败"
```

## 第 2 步：管理面按快照 id，在途记录，重放世界

本步结束时两个后端都实现新接口，Starling 一侧仍是进程内的过渡实现（00 篇施工表第 2 行）。

### Task 4: 端口协议换快照接口，替身实现 `held/` 与 `replay/`

**Files:**
- Modify: `src/simple_actio/memory_port.py:96-135`（删 `backup_to`、`restore_from`；加 `snapshot`、`restore`、`delete_snapshot`、`fork_replay`、`release_replay`、`open_replay`；`open` 签名不变；加冻结数据类 `SnapshotRef`）
- Modify: `src/simple_actio/memory.py:461-637`（`FileTupleStore` 换实现、加 `open_replay`；`_FilePort` 加只读；端口计数改按完整世界名；构造参数加 `held_root`、`replay_root`）
- Modify: `src/factory/memory_runtime.py:160-168`（`stand_in_deployment_memory` 按新构造参数造替身）
- Test: `tests/test_simple_actio_deployment_store_contract.py`（整个文件按新接口重写，参数化夹具 `:31-44` 不变）、`tests/test_simple_actio_file_tuple_store.py:243-640`（`backup_to`/`restore_from` 的用例改写成按 id）、`tests/test_simple_actio_memory_port.py:36-60`

**Interfaces:**
- Consumes: Task 2 的协议。
- Produces（40 篇"接服务后管理面的签名"一节，逐字照抄）：

```python
@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    digest: str

def snapshot(self, world_id: str, *, note: str) -> SnapshotRef: ...
def restore(self, world_id: str, snapshot_id: str) -> str: ...
def delete_snapshot(self, world_id: str, snapshot_id: str) -> None: ...
def fork_replay(self, world_id: str, snapshot_id: str, *, round_id: str) -> str: ...
def release_replay(self, replay_world_id: str) -> None: ...
def open_replay(self, replay_world: str, holder: str) -> MemoryBackend: ...  # 永远只读
```

`open(world_id, holder)`、`remember_event`、`digest`、`wait_closed` 的签名不变，四者都只收部署世界 id（40 篇 AC-34）。

- `FileTupleStore(root, *, held_root: Path, replay_root: Path, extraction_failure=None)`：`root` 仍是部署世界目录 `<记忆根>/deploy`；held 快照 `held_root/<world_id>/<snapshot_id>.json`，内容 `{"note": ..., "ledger": <_Ledger.to_json()>}`；重放世界 `replay_root/<重放世界名>.json`。构造仍不碰磁盘。组合根传 `held_root=<记忆根>/held`、`replay_root=<记忆根>/replay`（90 篇第 4 章布局表）。
- 快照 id：`"snap-" + secrets.token_hex(8)`；`snapshot` 在写之前就定 id，世界没有文件时快照是空账本，摘要等于空世界摘要。
- 重放世界名：`f"{STAND_IN_WORLD_PREFIX}.verify.{round_id}.{world_id}.{secrets.token_hex(4)}"`，`STAND_IN_WORLD_PREFIX = "rise"`（`world_prefix` 的缺省值；替身不读本机记忆配置，所以写死），与 Starling 的 `<前缀>.verify.<round_id>.<scenario_id>.<后缀>` 同形（40、50 篇）。
- 两种打开不按名字前缀推断（40 篇 AC-34）：`open_replay(name, holder)` 只读 `replay_root/<name>.json`，文件不在抛永久失败、不建；`open(world_id, holder)` 只读写部署世界 `root/<world_id>.json`，名为 `rise.verify.x` 的情境照旧是部署世界。两者的名字都过 `PERSONA_ID_PATTERN`。
- 端口计数表按完整世界名，两种端口共用一张：部署端口记在 `rise.deploy.<world_id>` 下，重放端口记在重放世界名下。`wait_closed(world_id, ...)` 收部署世界 id，拼成 `rise.deploy.<world_id>` 再查，所以开着的重放端口不挡部署世界的恢复。
- `restore`：该世界有端口开着时抛永久 `MemoryFailureError("restore: world has an open port")`（与 Starling 服务端的 `world_held` 同类）；快照不在时抛永久 `MemoryFailureError("restore: snapshot_not_found")`；快照读不出时抛永久（只报类名）。返回恢复后的摘要。
- `delete_snapshot`：文件不在是空操作。Starling 对已删的快照回 `snapshot_not_found`，rise 当作成功（50 篇"删一个已删的快照"一段），两边都幂等，由契约用例钉住。
- `open_replay` 开出的只读端口，`remember` 抛永久 `MemoryFailureError("world_read_only")`；`embed_pending` 只读时返回 0。

- [x] **Step 1: 写失败的测试**（`tests/test_simple_actio_deployment_store_contract.py`，参数化到两个实现；本任务只要求 `file` 参数通过）

```python
def test_a_snapshot_then_writes_then_restore_gives_back_the_snapshot_digest(case):
    store = case.store
    ref = store.snapshot("s1", note="rise episode e1")
    assert isinstance(ref, SnapshotRef) and ref.digest == store.digest("s1")
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert store.digest("s1") != ref.digest
    assert store.restore("s1", ref.snapshot_id) == ref.digest == store.digest("s1")


def test_restore_is_refused_while_a_port_of_the_world_is_open(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    port = store.open("s1", "pb")
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", ref.snapshot_id)
    _assert_permanent(info)
    port.close()
    store.restore("s1", ref.snapshot_id)


def test_a_deleted_snapshot_deletes_again_quietly_but_cannot_be_restored(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    store.delete_snapshot("s1", ref.snapshot_id)
    store.delete_snapshot("s1", ref.snapshot_id)            # 服务回 snapshot_not_found,rise 当作成功
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", ref.snapshot_id)
    _assert_permanent(info)


def test_a_replay_world_starts_at_the_snapshot_and_takes_many_read_only_ports(case):
    store = case.store
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="rise verify r1 s1")
    store.remember_event("s1", "pa", "later#0@pa", "pb：后来的", interlocutor="pb")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    assert name.split(".")[1:4] == ["verify", "r1", "s1"] and len(name.rsplit(".", 1)[1]) == 8
    base, cand = store.open_replay(name, "pa"), store.open_replay(name, "pa")
    assert base.digest() == cand.digest() == ref.digest
    assert store.wait_closed("s1", 0.05) is True            # 重放端口记在重放世界名下,不挡部署世界
    with pytest.raises(MemoryFailureError) as info:
        base.remember(MemoryEvent("x#0@pa", "pb：写不进去", "pb"))
    _assert_permanent(info)
    base.close(); cand.close()
    store.release_replay(name)


def test_a_scenario_named_like_a_replay_world_is_still_a_deploy_world(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    replay = store.open_replay(name, "pa")
    store.remember_event(name, "pa", KEY, TEXT, interlocutor="pb")   # 情境 id 恰好等于重放世界名:写的是部署世界
    assert store.digest(name) != ref.digest and replay.digest() == ref.digest
    port = store.open(name, "pa")
    assert store.wait_closed(name, 0.05) is False           # 部署端口记在 <前缀>.deploy.<name> 下
    port.close()
    assert store.wait_closed(name, 0.05) is True            # 重放端口还开着,记在另一个键下
    replay.close()
    store.release_replay(name)


def test_open_replay_does_not_build_a_world_that_was_never_forked(case):
    with pytest.raises(MemoryFailureError) as info:
        case.store.open_replay("rise.verify.r1.s1.00000000", "pa")
    _assert_permanent(info)
```

`tests/test_simple_actio_file_tuple_store.py` 加替身专有的布局用例：

```python
def test_held_snapshots_and_replay_worlds_live_outside_deploy(tmp_path):
    store = FileTupleStore(tmp_path / "deploy", held_root=tmp_path / "held", replay_root=tmp_path / "replay")
    with pytest.raises(MemoryFailureError):
        store.open_replay("rise.verify.r1.s1.00000000", "pa")
    assert not (tmp_path / "replay").exists()               # 没分叉过的重放世界不建
    ref = store.snapshot("s1", note="rise episode e1")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    assert json.loads((tmp_path / "held" / "s1" / f"{ref.snapshot_id}.json").read_text("utf-8"))["note"] == "rise episode e1"
    assert (tmp_path / "replay" / f"{name}.json").is_file()
    assert not (tmp_path / "deploy").exists()               # 空世界的快照与重放不建部署世界文件
    store.release_replay(name)
    assert not (tmp_path / "replay" / f"{name}.json").exists()
```

`test_simple_actio_memory_port.py::test_both_protocols_are_checked_by_method_names` 的替身协议类改成新的方法集，断言 `"backup_to" not in dir(DeploymentMemoryStore)`、`"fork_replay" in dir(DeploymentMemoryStore)`、`"open_replay" in dir(DeploymentMemoryStore)`，并断言 `inspect.signature(DeploymentMemoryStore.open)` 的参数只有 `self`、`world_id`、`holder`（没有 `read_only`）。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider -k "not starling" tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_file_tuple_store.py tests/test_simple_actio_memory_port.py`
Expected: FAIL（没有 `snapshot`、`fork_replay`、`open_replay`；构造参数不对）

- [x] **Step 3: 实现**

按 Interfaces。`snapshot`、`restore`、`fork_replay` 的读写都在 store 的锁里；`restore` 的"查端口数、读快照、写世界"在同一次持锁里。快照写用 `_write_ledger` 同一套临时文件加改名。重放世界的读写复用 `_load`/`_save`：`open_replay` 固定读 `replay_root`，`open` 与其余按世界 id 的方法固定读写 `root`，不看名字长什么样。端口计数的键在开端口时就定成完整世界名。`stand_in_deployment_memory` 改成：

```python
return DeploymentMemory(
    FileTupleStore(root / "deploy", held_root=root / "held", replay_root=root / "replay"),
    layout, copy.deepcopy(STAND_IN_MEMORY_RUNTIME), FileTupleStore)   # 第四个字段在 Task 7 删
```

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2，再加 `tests/test_simple_actio_memory_contract.py tests/test_simple_actio_instance.py`
Expected: PASS（`-k "not starling"`；Starling 参数在 Task 5 修）

- [x] **Step 5: 提交**

```bash
git add src/simple_actio/memory_port.py src/simple_actio/memory.py src/factory/memory_runtime.py tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_file_tuple_store.py tests/test_simple_actio_memory_port.py
git commit -m "feat(simple_actio): 部署管理面改按快照 id,加 fork_replay、release_replay 与只读的 open_replay;替身快照与重放世界移出 deploy/"
```

### Task 5: 进程内 Starling 的过渡实现

过渡布局（00 篇施工表第 2 行、90 篇第 18 条）：held 快照是 `<记忆根>/held/<world_id>/<snapshot_id>.db`，重放世界是 `<记忆根>/replay/<重放世界名>.db`，部署世界照旧 `<记忆根>/deploy/<scenario_id>.db`。这一套到第 4 步整体删掉，所以只求与替身过同一套契约，不做新的崩溃安全设计：沿用今天 `backup_to`（`VACUUM INTO`）与 `restore_from`（先删日志、后换文件）的实现。

**Files:**
- Modify: `src/simple_actio/memory_starling.py:358-572`（`StarlingDeploymentStore`：构造参数加 `held_root`、`replay_root`；`backup_to` 与 `restore_from` 改成私有的 `_backup_to`、`_restore_from`，在其上实现 `snapshot`、`restore`、`delete_snapshot`、`fork_replay`、`release_replay`、`open_replay`；端口计数改按完整世界名；`open` 签名不变）、`:149-243`（`StarlingMemory` 加 `read_only`，只给 `open_replay` 用）
- Modify: `src/factory/memory_runtime.py:758-765`（`memory_wiring` 造 `StarlingDeploymentStore` 时传两个新根）
- Test: `tests/test_simple_actio_starling_store.py:299-700`（`backup_to`/`restore_from` 的用例改走私有方法或改写成按 id；崩溃点用例 `:594-623` 保留，改调 `_restore_from`）、`tests/test_simple_actio_deployment_store_contract.py`（Task 4 新增用例的 `starling` 参数）、`tests/test_simple_actio_memory_starling.py`

**Interfaces:**
- Consumes: Task 4 的协议与重放世界名规则（`rise.verify.<round_id>.<world_id>.<8 位十六进制>`；本模块另定一份同值常量，不从 `simple_actio.memory` 导入，免得适配器依赖替身）。
- Produces:
  - `StarlingDeploymentStore(root, *, held_root: Path, replay_root: Path, endpoints, close_timeout_s=60.0)`。
  - `snapshot(world_id, *, note)`：`snapshot_id = "snap-" + secrets.token_hex(8)`，`_backup_to(world_id, held_root / world_id / f"{snapshot_id}.db")`，返回 `SnapshotRef(snapshot_id, 摘要)`。`note` 在过渡实现里不落盘（进程内没有看板）。
  - `restore(world_id, snapshot_id)`：文件不在抛永久 `MemoryFailureError("restore: snapshot_not_found")`；否则 `_restore_from(world_id, 那个文件)`，有端口开着照旧抛永久失败。
  - `delete_snapshot(world_id, snapshot_id)`：删文件与它的 `-wal`、`-shm`，不在是空操作。
  - `fork_replay(world_id, snapshot_id, *, round_id)`：`copy_library(held 文件, replay_root / f"{名字}.db")`，返回名字。
  - `release_replay(name)`：删 `replay_root/<name>.db` 与旁边的 `-wal`、`-shm`。
  - `open_replay(name, holder)`：开 `replay_root/<name>.db` 上的只读端口（`StarlingMemory(..., read_only=True)`）；文件不在抛永久失败，不建库。
  - `open(world_id, holder)`：签名与行为不变，只开部署世界 `root/<world_id>.db`，不看名字前缀。
  - 端口计数表按完整世界名：部署端口记在 `rise.deploy.<world_id>` 下，重放端口记在重放世界名下；`wait_closed(world_id, ...)` 拼成部署世界名再查。
  - `StarlingMemory(memory, *, read_only=False, ...)`：只读时 `remember` 抛永久 `MemoryFailureError("world_read_only")`，`embed_pending` 返回 0 且不调门面。

- [x] **Step 1: 写失败的测试**：Task 4 Step 1 的六条契约用例此时对 `starling` 参数是红的，不另写。`tests/test_simple_actio_starling_store.py` 加过渡布局专有的一条：

```python
def test_the_transitional_layout_keeps_held_and_replay_files_out_of_deploy(tmp_path, fake):
    root = tmp_path / "mem"
    store = StarlingDeploymentStore(root / "deploy", held_root=root / "held", replay_root=root / "replay",
                                    endpoints=fake.endpoints())
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="rise episode e1")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    assert (root / "held" / "s1" / f"{ref.snapshot_id}.db").is_file()
    assert (root / "replay" / f"{name}.db").is_file()
    assert sorted(p.name for p in (root / "deploy").iterdir()) == ["s1.db"]
    store.release_replay(name)
    store.delete_snapshot("s1", ref.snapshot_id)
    assert not (root / "replay" / f"{name}.db").exists() and not (root / "held" / "s1" / f"{ref.snapshot_id}.db").exists()
```

`tests/test_simple_actio_memory_starling.py` 加：

```python
def test_a_read_only_port_refuses_writes_and_skips_embedding():
    fake = FakeStarling()
    port = StarlingMemory(fake, read_only=True)
    with pytest.raises(MemoryFailureError, match="world_read_only") as info:
        port.remember(EV)
    assert info.value.transient is False and fake.remembered == []
    assert port.embed_pending() == 0 and fake.embed_nows == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_memory_starling.py`
Expected: FAIL（`StarlingDeploymentStore` 没有新方法与新构造参数）

- [x] **Step 3: 实现**：按 Interfaces。`_backup_to`、`_restore_from` 是今天 `backup_to`、`restore_from` 原样改名，文档字符串里写明"第 4 步删"。`held_root`、`replay_root` 由第一次需要时建（构造不碰磁盘，照 `memory_starling.py:377-379`）。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_memory_starling.py tests/test_simple_actio_memory_contract.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/simple_actio/memory_starling.py src/factory/memory_runtime.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_memory_starling.py
git commit -m "feat(simple_actio): 进程内 Starling 管理面的过渡实现:held/ 下按 id 的备份,replay/ 下的重放世界与 open_replay"
```

### Task 6: `MemoryLayout` 改读在途记录，旧布局按后端判（过渡规则）

**Files:**
- Modify: `src/mindagent/memory_layout.py:1-159`（整份：模块文档的布局图、`snapshot`、`verify_snapshot` 改返回记录路径；加 `SnapshotRecord`、`write_record`、`read_record`；`world_of`、`round_snapshots`、`any_uncommitted_snapshots`、`uncommitted_snapshots` 改读记录；删 `_tag_path`、`write_world_tag`、`tagged_snapshots`、`verify_scratch`、`suffix`；`legacy_entries` 改按后端）
- Test: `tests/test_ma_memory_layout.py`（整份重写，`_snap` 夹具 `:8-15` 改成写记录）

**Interfaces:**
- Consumes: 无（纯文件布局，不碰管理面）。
- Produces（50 篇"接服务后的在途记录"一段与 MA-41）：

```python
@dataclass(frozen=True)
class SnapshotRecord:
    world_id: str        # 过 PERSONA_ID_PATTERN
    snapshot_id: str     # 非空、不含 "/"
    digest: str

@dataclass(frozen=True)
class MemoryLayout:
    root: Path
    def world_lock(self, scenario_id: str) -> Path: ...                  # 不变
    def hold_world(self, scenario_id: str) -> AbstractContextManager[None]: ...   # 不变
    def snapshot(self, round_id: str, episode_id: str) -> Path: ...      # snapshots/<round>/deploy-<episode>.rec.json
    def verify_snapshot(self, round_id: str, scenario_id: str) -> Path: ...   # snapshots/<round>/verify-<scenario>.rec.json
    def write_record(self, path: Path, record: SnapshotRecord) -> None: ...   # 临时文件加改名;sort_keys、ensure_ascii=False
    def read_record(self, path: Path) -> SnapshotRecord: ...             # 读不出、不是对象、键不对、world_id 过不了白名单:MindAgentDataError
    def world_of(self, path: Path) -> str: ...                           # read_record(path).world_id
    def round_snapshots(self, round_id: str) -> list[Path]: ...          # 该轮次的 deploy-*.rec.json,按名排序
    def any_uncommitted_snapshots(self) -> list[Path]: ...               # 任何轮次的 deploy-*.rec.json,按路径排序
    def uncommitted_snapshots(self, scenario_id: str) -> list[Path]: ... # world_of 等于 scenario_id 的;任何一份读不出都抛
    def legacy_entries(self, known_scenarios: Iterable[str], *, backend: str) -> list[str]: ...
```

- 在途记录的识别：文件名以 `deploy-` 起头、以 `.rec.json` 结尾才是起点快照的记录；`verify-*.rec.json` 不算在途 episode。
- `legacy_entries` 的 `backend` 取三个值之一，规则见下表（90 篇第 18 条与第 4 章布局表；`"starling-inprocess"` 只在第 2、3 步存在，Task 17 删掉它）。返回相对记忆根的 POSIX 路径，排序。读不出的在途记录不在这里报，由 `check_memory_layout` 逐份 `read_record` 报（Task 7）。

| 位置 | `stand-in` | `starling-inprocess`（过渡） | `starling`（第 4 步起） |
|---|---|---|---|
| `deploy/<已知情境>.json` | 合法 | 判旧布局 | 判旧布局 |
| `deploy/<已知情境>.db` | 判旧布局 | 合法 | 判旧布局 |
| `deploy/<已知情境>.created` | 判旧布局 | 判旧布局 | 合法 |
| `deploy/` 下其余带数据后缀（`.json`、`.db`、`.created`、`.rec.json`）的文件，含不是已知情境名的 | 判旧布局 | 判旧布局 | 判旧布局 |
| `deploy/` 下其余文件（`*.lock`、`*.tmp`、`*-wal`、`*-shm` 等） | 不查 | 不查 | 不查 |
| `seeds/`、`runs/` 目录 | 判旧布局（替身根在 git 目录里，从来没有这两个目录） | 合法（训练世界仍在进程内） | 判旧布局 |
| `snapshots/*/` 下的 `.db`、`.world`、不带 `.rec` 的 `deploy-*.json` | 判旧布局 | 判旧布局 | 判旧布局 |
| `snapshots/*/` 下的 `*.rec.json`、`snapshots/*/verify/` 目录 | 合法 | 合法 | 合法 |
| `held/`、`replay/` 下的 `.db` | 不查 | 合法 | 判旧布局，逐个文件列出（第 2、3 步过渡留下的） |
| `held/`、`replay/` 下的其余文件（替身的 `.json` 等） | 合法 | 合法 | 不查 |
| `service/`、`service.log`、`service.pid`、`service.owner.lock`、`service.token`、`.owner` | 不查 | 不查 | 不查 |

`deploy/` 的两行照 90 篇第 18 条：只查四种数据后缀，不在允许名单里的就拒绝；其余一律不查。`.tmp`、`-wal`、`-shm`、`.lock` 是写到一半或运行时的文件，不是世界数据，拒绝它们只会让一次崩溃（`s1.json.tmp`、开着库时的 `s1.db-wal`）变成无法启动。`held/`、`replay/` 下的 `.db` 在最终规则里判旧布局，文字照 `check_memory_layout` 的同一口径列出至多 5 条。`deploy/` 下"已知情境"沿用今天的判法（`agent.yaml` 的 `scenario_ids`，`src/factory/memory_runtime.py:131-135`）：替身与过渡 Starling 的 `<scenario_id>.<后缀>` 里主名不在已知情境里的仍按旧布局报，提示词照 FC-37 不变。替身旧格式的 `deploy/<已知情境>.json` 账本本身不变，照旧合法。

- [x] **Step 1: 写失败的测试**（`tests/test_ma_memory_layout.py`）

```python
def _rec(lay, rid, eid, world, snapshot_id="snap-0"):
    p = lay.snapshot(rid, eid)
    lay.write_record(p, SnapshotRecord(world, snapshot_id, "d" * 64))
    return p


def test_records_follow_the_world_layout(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.snapshot("r1", "deploy-m-s1-1") == tmp_path / "snapshots" / "r1" / "deploy-deploy-m-s1-1.rec.json"
    assert lay.verify_snapshot("r1", "s1") == tmp_path / "snapshots" / "r1" / "verify-s1.rec.json"


def test_a_record_round_trips_and_names_its_world(tmp_path):
    lay = MemoryLayout(tmp_path)
    p = _rec(lay, "r1", "deploy-m-s1-1", "s1", "snap-ab")
    assert lay.read_record(p) == SnapshotRecord("s1", "snap-ab", "d" * 64) and lay.world_of(p) == "s1"
    assert not list(p.parent.glob(".*.tmp"))


@pytest.mark.parametrize("body", ["", "[]", '{"world_id": "../s1", "snapshot_id": "x", "digest": "d"}',
                                  '{"world_id": "s1", "snapshot_id": "x"}'])
def test_an_unreadable_record_is_a_data_error_naming_it_relative_to_the_root(tmp_path, body):
    lay = MemoryLayout(tmp_path)
    p = lay.snapshot("r1", "deploy-m-s1-1"); p.parent.mkdir(parents=True); p.write_text(body)
    with pytest.raises(MindAgentDataError, match=r"snapshots/r1/deploy-deploy-m-s1-1\.rec\.json") as info:
        lay.read_record(p)
    assert str(tmp_path) not in str(info.value)


def test_uncommitted_snapshots_are_found_by_the_world_in_the_record_and_skip_verify_records(tmp_path):
    lay = MemoryLayout(tmp_path)
    a = _rec(lay, "r1", "deploy-m-s1-1", "s1")
    _rec(lay, "r2", "deploy-m-s2-1", "s2")
    lay.write_record(lay.verify_snapshot("r1", "s1"), SnapshotRecord("s1", "snap-v", "d" * 64))
    assert lay.uncommitted_snapshots("s1") == [a] and lay.round_snapshots("r1") == [a]
    assert [p.parent.name for p in lay.any_uncommitted_snapshots()] == ["r1", "r2"]


@pytest.mark.parametrize("backend, legacy", [
    ("stand-in", ["deploy/pa.json", "deploy/s1.db", "deploy/s1.rec.json", "runs", "seeds",
                  "snapshots/r1/deploy-m-1.db", "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world"]),
    ("starling-inprocess", ["deploy/pa.json", "deploy/s1.json", "deploy/s1.rec.json", "snapshots/r1/deploy-m-1.db",
                            "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world"]),
    ("starling", ["deploy/pa.json", "deploy/s1.db", "deploy/s1.json", "deploy/s1.rec.json", "held/s1/snap-1.db",
                  "replay/y.db", "runs", "seeds",
                  "snapshots/r1/deploy-m-1.db", "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world"]),
])
def test_the_same_root_is_judged_by_its_backend(tmp_path, backend, legacy):
    for rel in ("deploy/s1.lock", "deploy/pa.json", "deploy/s1.json", "deploy/s1.db", "deploy/s1.rec.json",
                "snapshots/r1/deploy-m-1.db", "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world",
                "snapshots/r1/deploy-m-4.rec.json", "held/s1/snap-0.json", "replay/x.json",
                "held/s1/snap-1.db", "replay/y.db",                          # 第 2、3 步过渡留下的
                "seeds/k/s1.db", "runs/run-1/e.db", "service.log", ".owner",
                "deploy/s1.db-wal", "deploy/s1.db-shm", "deploy/s1.json.tmp"):   # 不带数据后缀的文件不报
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / rel).write_text("{}")
    assert MemoryLayout(tmp_path).legacy_entries(["s1"], backend=backend) == legacy


def test_a_creation_marker_is_legal_only_for_the_service_backend_and_a_known_scenario(tmp_path):
    (tmp_path / "deploy").mkdir()
    for name in ("s1.created", "s9.created", "s1.created.tmp"):
        (tmp_path / "deploy" / name).write_text("")
    lay = MemoryLayout(tmp_path)
    assert lay.legacy_entries(["s1"], backend="starling") == ["deploy/s9.created"]
    assert lay.legacy_entries(["s1"], backend="stand-in") == ["deploy/s1.created", "deploy/s9.created"]
```

`world_lock`、`hold_world`、`_check_segment`、`_check_world` 的旧用例（`:42-58`、`:145-155`）照旧保留；按旁注的用例（`:27-40`、`:59-143`、`:157-210`）改写成按记录，或在新表里已有覆盖时删掉。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_memory_layout.py`
Expected: FAIL（没有 `SnapshotRecord`、`write_record`；`legacy_entries` 不收 `backend`）

- [x] **Step 3: 实现**：按 Interfaces。`seeds`、`runs` 判旧布局时报目录本身（相对路径，不展开里面的文件）；`held/`、`replay/` 下的 `.db` 逐个文件报。`deploy/` 里先按文件名结尾挑出带数据后缀的文件，再按完整文件名比允许名单（替身 `<已知情境>.json`，过渡 `<已知情境>.db`，最终 `<已知情境>.created`），所以 `s1.rec.json` 不会被当成情境 `s1` 的账本。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: PASS（`tests/test_ma_*` 其余文件与 `tests/test_factory_memory_wiring.py` 此时会失败，由 Task 7 改）

- [x] **Step 5: 提交**

```bash
git add src/mindagent/memory_layout.py tests/test_ma_memory_layout.py
git commit -m "feat(mindagent): 记忆布局改读在途记录 .rec.json,旧布局按后端判(第 2、3 步的过渡规则)"
```

### Task 7: 部署驱动器、轮次回滚与验证改用快照 id 与在途记录，删 `scratch_store_factory`

**Files:**
- Modify: `src/mindagent/deployment.py:35`（去掉 `_tag_path`）、`:166-181`（`_restore` 读记录、按 id 恢复、分码）、`:304-340`（docstring）、`:385-396`（开 episode：`snapshot` → 写记录 → `on_episode_start`）、`:439`、`:446-459`（提交：删记录 → `delete_snapshot`，后者失败只记 warning）
- Modify: `src/mindagent/rounds.py:13-37`（模块文档）、`:63`、`:285-307`（删 `scratch_store_factory`）、`:336-372`（回滚：`restore` → 删记录 → `delete_snapshot`）、`:459-506`（验证：每情境 `snapshot` 加 `verify-` 记录、每情境 `fork_replay` 一次、`open_replay` 开只读端口，finally 里 `release_replay`、删快照与记录）
- Modify: `src/mindagent/admit.py:167-185`（读不出的在途记录：延后文字从"world tag"改成"in-flight record"，仍只报类名）
- Modify: `src/factory/handlers.py:122`（删 `scratch_store_factory=`）
- Modify: `src/factory/memory_runtime.py:70`、`:101-110`（`DeploymentMemory` 删 `scratch_store_factory` 字段）、`:121-157`（`check_memory_layout(layout, repo_root, *, backend)`：`legacy_entries(..., backend=)`，再对每份在途记录 `read_record`，读不出退出码 2、文字写相对路径）、`:160-168`、`:735-765`（`MemoryLayout(config.root)`，`backend="starling-inprocess"`）
- Test: `tests/test_ma_deployment.py`（`_events`、`:953-1080` 的快照与旁注用例改成记录）、`tests/test_ma_rounds.py:138`、`:162`、`:296`、`:544`、`:586`、`:690-910`、`:956-975`（删 `scratch_store_factory`；回滚与验证用例改写）、`tests/test_ma_deployment_starling.py:43`、`tests/test_ma_e2e.py:91-99`、`tests/test_ma_admit.py:160-190` 一带的旁注用例、`tests/test_factory_handlers.py:1145`、`tests/test_factory_memory_wiring.py:165`、`:337-445`（旧布局用例改成按后端）

**Interfaces:**
- Consumes: Task 4、5 的管理面；Task 6 的 `SnapshotRecord`、`write_record`、`read_record`、`legacy_entries(..., backend=)`。
- Produces:
  - 起点快照 `note`：`f"rise episode {episode_id}"`；验证快照 `note`：`f"rise verify {round_id} {scenario_id}"`（50 篇在途记录表）。
  - 恢复分码（50 篇"接服务后 `wait_closed` 保留"一节的表）：`wait_closed` 返回真之后，`restore` 抛**瞬态** `MemoryFailureError`（服务的 `world_busy`）→ `DeferredError`，轮次留 pending、记录与快照保留，重入时先恢复；抛**永久** `MemoryFailureError`（`world_held`、`snapshot_not_found`、`restore_digest_mismatch` 等）或 `OSError` → `RestoreFailedError`（失败点 `mindagent.deployment.restore`，只报类名），记录与快照保留；摘要与记录不等 → `RestoreFailedError`。`_restore` 与 `rounds.py` 的回滚走同一个判法。
  - 提交与回滚收尾的 `delete_snapshot` 抛 `MemoryFailureError` 或 `OSError` 时 `_log.warning("... delete_snapshot failed: %s", 类名)`，不改变结果（50 篇 MA-41、失败表"孤儿 held 快照"一行）。删已删的快照不抛（两个管理面都幂等），驱动器不另判。
  - 验证段：`fork_replay` 每个情境至多一次（第一次有提案要这个情境的实例时做），`instance_with(extra, scenario_id)` 在它上面 `open_replay(replay, agent_pid)`（`replay` 是 `fork_replay` 原样返回的名字，40 篇 AC-34）；`built` 记实例，`replays` 记重放世界名。finally 顺序：关实例 → 每个重放世界 `release_replay` → 每个情境 `delete_snapshot` 并删 `verify-` 记录 → `_rmdir_if_empty(snapshots/<round_id>)`。收尾里的任何失败只记 warning（终审 M-1 的同一口径）。
  - `make_start_round_handler(...)` 不再收 `scratch_store_factory`；`DeploymentMemory` 只剩 `store`、`layout`、`runtime` 三个字段。

- [x] **Step 1: 写失败的测试**

`tests/test_ma_deployment.py`：

```python
def test_an_episode_start_takes_a_held_snapshot_and_records_it_before_announcing_the_digest(store, mem, monkeypatch):
    order = []
    real_snap, real_write = mem.store.snapshot, MemoryLayout.write_record
    monkeypatch.setattr(mem.store, "snapshot",
                        lambda world, *, note: order.append(("snapshot", world, note)) or real_snap(world, note=note))
    monkeypatch.setattr(MemoryLayout, "write_record",
                        lambda self, path, rec: order.append(("record", path.name, rec.world_id)) or real_write(self, path, rec))
    _run(store, _Factory(), mem, started=SimpleNamespace(append=lambda item: order.append(("start", item[0]))))
    assert order[:3] == [("snapshot", WORLD, f"rise episode {EID}"),
                         ("record", f"deploy-{EID}.rec.json", WORLD), ("start", EID)]


def test_a_commit_drops_the_record_before_the_snapshot_and_a_failed_delete_is_only_a_warning(store, mem, monkeypatch, caplog):
    order = []
    monkeypatch.setattr(mem.store, "delete_snapshot",
                        lambda world, sid: order.append(("delete", mem.layout.snapshot(ROUND, EID).exists()))
                        or (_ for _ in ()).throw(MemoryFailureError("boom http://secret", transient=False)))
    (ep,) = _run(store, _Factory(), mem)
    assert ep.id == EID and order == [("delete", False)]                # 记录先删
    assert "delete_snapshot failed: MemoryFailureError" in caplog.text and "secret" not in caplog.text


@pytest.mark.parametrize("transient, raised", [(True, DeferredError), (False, RestoreFailedError)])
def test_a_restore_failure_is_split_by_its_transient_flag_and_keeps_the_record(store, mem, monkeypatch, transient, raised):
    monkeypatch.setattr(mem.store, "restore",
                        lambda world, sid: (_ for _ in ()).throw(MemoryFailureError("world_busy", transient=transient)))
    f = _WorldFactory(mem, {"end_reason": "role_backend_failure"})
    with pytest.raises(raised):
        _run(store, f, mem, **f.hooks())
    assert mem.layout.snapshot(ROUND, EID).exists()                     # 记录与快照都留着,重入先恢复
```

`tests/test_ma_rounds.py`：

```python
def test_verification_forks_one_replay_world_per_scenario_and_releases_it(store, mem, monkeypatch):
    forks, names, opened, released = [], [], [], []
    real_fork, real_open, real_release = mem.store.fork_replay, mem.store.open_replay, mem.store.release_replay

    def fork(w, sid, *, round_id):
        forks.append(w); names.append(real_fork(w, sid, round_id=round_id)); return names[-1]

    monkeypatch.setattr(mem.store, "fork_replay", fork)
    monkeypatch.setattr(mem.store, "open_replay", lambda name, holder: opened.append(name) or real_open(name, holder))
    monkeypatch.setattr(mem.store, "release_replay", lambda name: released.append(name) or real_release(name))
    _skill(store, GOAL); _skill(store, GOAL2)                           # 两个草案:基线与候选不止一对
    _start(store, _handler(mem=mem))
    assert sorted(forks) == sorted(set(forks)) and set(forks) <= {"s1", "s2", "s3"}   # 每个情境至多一次
    assert opened and set(opened) <= set(names)                         # 重放端口只经 open_replay,名字原样传
    assert sorted(released) == sorted(names)
    assert not any(mem.layout.verify_snapshot(ROUND, sid).exists() for sid in ("s1", "s2", "s3"))
    assert not (mem.layout.root / "snapshots" / ROUND).exists()


def test_a_rollback_restores_by_record_then_drops_record_and_snapshot(store, mem, monkeypatch):
    start, rec = _kept_copy(store, mem)                                 # 夹具改成留一份在途记录
    deleted = []
    real = mem.store.delete_snapshot
    monkeypatch.setattr(mem.store, "delete_snapshot", lambda w, sid: deleted.append((w, sid, rec.exists())) or real(w, sid))
    _model(store, 2, "run-002")
    _start(store, _handler(mem=mem))
    assert mem.store.digest("s1") == start and not rec.exists()
    assert [(w, gone) for w, _, gone in deleted] == [("s1", False)]
```

`GOAL2` 是第二个能产出草案的发现（照 `GOAL` 的写法在测试文件里加一个）。`_kept_copy`、`_verify_handler`、`_RecallFailingScratch`、`BrokenScratch` 改成在替身管理面上 monkeypatch `fork_replay`、`open_replay`、`restore`，不再造临时管理面。`tests/test_factory_memory_wiring.py` 的旧布局用例按 Task 6 的表改期望，加一条：

```python
def test_an_unreadable_in_flight_record_exits_2_naming_it_relative_to_the_root(tmp_path, store, monkeypatch, capsys):
    root = _stand_in_root(store)
    rec = root / "snapshots" / "r1" / "deploy-deploy-m-s1-1.rec.json"
    rec.parent.mkdir(parents=True); rec.write_text("not json")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env={}) == 2
    err = capsys.readouterr().err
    assert "snapshots/r1/deploy-deploy-m-s1-1.rec.json" in err and str(root) not in err
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_admit.py tests/test_ma_e2e.py tests/test_factory_memory_wiring.py tests/test_factory_handlers.py`
Expected: FAIL（驱动器还在调 `backup_to`；处理器还收 `scratch_store_factory`）

- [x] **Step 3: 实现**

按 Interfaces。`_restore` 的骨架：

```python
def _restore(memory_store, world_id, rec_path, layout, expected, episode_id, close_wait_s) -> str:
    what = f"restore {episode_id} in world {world_id}"
    point = "mindagent.deployment.restore"
    if not memory_store.wait_closed(world_id, close_wait_s):
        raise RestoreFailedError(f"{what}: memory ports still open after {close_wait_s:g}s", point=point)
    try:
        rec = layout.read_record(rec_path)
        digest = memory_store.restore(world_id, rec.snapshot_id)
    except MemoryFailureError as exc:
        if exc.transient:            # world_busy:服务端还在排空,轮次延后、重入先恢复
            raise DeferredError(f"{what}: {type(exc).__name__}") from exc
        raise RestoreFailedError(f"{what}: {type(exc).__name__}", point=point) from exc
    except (MindAgentDataError, OSError) as exc:
        raise RestoreFailedError(f"{what}: {type(exc).__name__}", point=point) from exc
    if expected is not None and digest != expected:
        raise RestoreFailedError(f"{what}: digest mismatch", point=point)
    return digest
```

`rounds.py` 的回滚用同一个分码（`DeferredError` 原样上抛，`_close_round` 不接，轮次留 pending）。`rounds.py` 不再用 `shutil` 就删掉这个导入。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_admit.py tests/test_ma_e2e.py tests/test_ma_verify.py tests/test_ma_memory_layout.py tests/test_ma_deployment_starling.py tests/test_factory_memory_wiring.py tests/test_factory_memory_runtime.py tests/test_factory_handlers.py tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_starling_store.py`
Expected: PASS。这是第 2 步的最后一个任务：控制者在后台跑全量，必须绿。

- [x] **Step 5: 提交**

```bash
git add src/mindagent/deployment.py src/mindagent/rounds.py src/mindagent/admit.py src/factory/handlers.py src/factory/memory_runtime.py tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_deployment_starling.py tests/test_ma_e2e.py tests/test_ma_admit.py tests/test_factory_handlers.py tests/test_factory_memory_wiring.py
git commit -m "feat(mindagent): 起点与验证快照改按 id 加在途记录,恢复按瞬态分码;验证每情境分叉一个重放世界,删 scratch_store_factory"
```

## 第 3 步：新模块，不接线

本步新增的模块都不被装配导入，Starling 路径仍是进程内。extra 换成两行，带真 `starling-serve` 的测试从这一步起真正跑起来。

### Task 8: extra 换两行，真服务的测试夹具，导入边界

**Files:**
- Modify: `pyproject.toml:15`（两行，钉同一提交）、`uv.lock`
- Create: `tests/starling_service.py`（真服务夹具，照 `S:tests/python/fakes/service_proc.py:35-147`）
- Create: `tests/test_starling_service_fixture.py`
- Modify: `tests/test_factory_cross_plan.py:204-212`（加一条 `starling_client` 的导入守卫）

**Interfaces:**
- Produces:

```toml
memory = [
    "starling-client @ git+https://github.com/ruijieguo/starling@cff319d952360727823024ff43c71b15055bd8e3#subdirectory=clients/python",
    "starling-memory[dashboard] @ git+https://github.com/ruijieguo/starling@cff319d952360727823024ff43c71b15055bd8e3",
]
```

```python
# tests/starling_service.py
class ServiceProc:
    url: str; token: str; data_dir: Path; port: int; stderr_path: Path
    def terminate(self, timeout_s: float = 40.0) -> int: ...
    def kill(self) -> None: ...
    def restart(self) -> None: ...       # 同一端口、同一 token、同一数据目录重起(外部服务重启的用例用)
def clean_env(home: Path) -> dict: ...   # 去掉 OPENAI_*、STARLING_*;HOME=home;OPENAI_API_KEY=offline-test-key
def start_service(tmp_dir: Path, *, starling_json: dict | None = None, port: int = 0) -> ServiceProc: ...
```

`start_service` 的数据目录是 `tmp_dir / "data"`，HOME 是 `tmp_dir / "home"`，token 由 `secrets.token_urlsafe(32)` 生成、写进 `tmp_dir / "token"`（0600）；`starling_json` 给了就先写成 `data/starling.json`（叶子继承的用例用）。等 listening 与 ready 两行各至多 60 秒。stdout 由一条线程读完，stderr 写文件。用它的测试文件各自定义模块级夹具：

```python
@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()
```

- 导入守卫：`src/` 下导入 `starling_client`（任何层级、含函数内惰性导入）的只有 `simple_actio/memory_starling.py` 与 `factory/starling_session.py`。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_starling_service_fixture.py
import pytest

from starling_service import start_service


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()


def test_the_service_reports_the_pinned_commit_and_never_logs_its_token(service):
    import starling_client
    client = starling_client.connect(service.url, token=service.token)
    try:
        info = client.info()
    finally:
        client.close()
    assert info["commit"] == starling_client.COMMIT == "cff319d952360727823024ff43c71b15055bd8e3"
    assert info["protocol"] == starling_client.PROTOCOL
    assert service.token not in service.stderr_path.read_text("utf-8", "replace")


def test_the_service_home_stays_empty(service):
    assert not any((service.data_dir.parent / "home").iterdir())     # 不碰真 HOME,也不往假 HOME 里写
```

```python
# tests/test_factory_cross_plan.py
def test_only_the_adapter_and_the_service_session_import_starling_client():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    allowed = {"simple_actio/memory_starling.py", "factory/starling_session.py"}
    offenders = [f"{p.relative_to(src)}:{n}" for p in sorted(src.rglob("*.py"))
                 for n, top in _top_level_imports(p)
                 if top == "starling_client" and p.relative_to(src).as_posix() not in allowed]
    assert offenders == []
```

`_top_level_imports`（`tests/test_factory_cross_plan.py:175-183`）用 `ast.walk` 扫全树，函数体内的惰性导入也算。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_starling_service_fixture.py tests/test_factory_cross_plan.py`
Expected: FAIL（`starling_service` 模块不在；装了旧锁文件时 `starling_client` 被跳过，先改依赖再跑）

- [x] **Step 3: 实现**

改 `pyproject.toml`，跑 `uv lock`，再跑 `uv sync --extra dev --extra memory`（确认 `uv run --offline python -c "import starling_client, starling.service"` 成功）。写 `tests/starling_service.py`。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_starling_service_fixture.py tests/test_factory_cross_plan.py tests/test_mt_packaging.py tests/test_simple_actio_starling_store.py`
Expected: PASS（`tests/test_simple_actio_starling_store.py` 证明进程内路径在新锁文件上照旧）

- [x] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock tests/starling_service.py tests/test_starling_service_fixture.py tests/test_factory_cross_plan.py
git commit -m "build(deps): extra memory 改成客户端与服务两行,钉同一提交;真 starling-serve 的测试夹具"
```

### Task 9: 世界名、标签与 rise 设置（纯函数），叶子继承用真服务钉住

**Files:**
- Create: `src/factory/starling_names.py`（不导入 `starling_client`、`starling`；只用标准库与 `simple_actio.memory_port.PERSONA_ID_PATTERN`）
- Modify: `src/factory/memory_config.py:44-66`（`OPTIONAL_KEYS` 加 `url`、`token_file`、`world_prefix`；`MemoryConfig` 加三个字段，缺省 `None`、`None`、`"rise"`；`read_memory_config` 校验，见下）
- Test: Create `tests/test_factory_starling_names.py`；Modify `tests/test_factory_memory_config.py`

**Interfaces:**
- Produces（00 篇世界表与世界名规则、90 篇世界设置表）：

```python
WORLD_NAME_MAX = 128
LONG_ID_DIGEST_CHARS = 24
SUFFIX_HEX_CHARS = 8
ROOT_LABEL_CHARS = 16
WORLD_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}")

def root_label(memory_root: Path) -> str: ...      # sha256(str(memory_root.resolve())) 前 16 位
def new_suffix() -> str: ...                       # secrets.token_hex(4)
def deploy_world(prefix: str, scenario_id: str) -> str: ...          # <前缀>.deploy.<scenario_id>
def empty_seed_world(prefix: str) -> str: ...                        # <前缀>.seed-empty
def seed_world(prefix: str, seed_id: str, scenario_id: str, version: int) -> str: ...   # <前缀>.seed.<seed_id>.<scenario_id>.v<n>
def rollout_world(prefix: str, episode_id: str, suffix: str) -> str: ...                # <前缀>.ro.<episode_id>.<后缀>
def replay_world(prefix: str, round_id: str, scenario_id: str, suffix: str) -> str: ... # <前缀>.verify.<round_id>.<scenario_id>.<后缀>
def labels(prefix: str, root: str, kind: str, **extra: str) -> dict[str, str]: ...      # {"repo": 前缀, "root": root, "kind": kind, **extra}
def rise_settings(config: MemoryConfig) -> dict: ...
def settings_digest(settings: dict) -> str: ...    # sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")))
```

- 不提供"按名字判世界种类"的函数：重放世界只经管理面的 `open_replay` 打开，部署世界只经 `open`（40 篇 AC-34），没有调用方需要从名字反推。
- 超长：拼出的名字超过 128 字符时，把**中间的 id 段**（部署世界的 `scenario_id`；种子的 `seed_id` 与 `scenario_id` 按从长到短逐段替换，直到不超长；rollout 的 `episode_id`；重放世界的 `round_id` 再 `scenario_id`）换成它的 sha256 前 24 位；标签里照旧是完整的 id。
- `rise_settings(config)` 返回 90 篇世界设置表的全部叶子，一个不缺：

```python
{
    "extraction": {"provider": "openai", "provider_name": "", "base_url": config.extraction_base_url,
                   "model": config.extraction_model, "dim": 0, "max_tokens": 0,
                   "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
                   "enable_thinking": False, "thinking_type": ""},
    "embedding": {"provider": "openai", "provider_name": "", "base_url": config.embedding_base_url,
                  "model": config.embedding_model, "dim": config.embedding_dim, "max_tokens": 0,
                  "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
                  "enable_thinking": False, "thinking_type": ""},
    "chat": None, "consolidation": None, "gist_thresholds": None,
    "vector_backend": "sqlite",
    "source_lane": {"enabled": False, "render_time": False, "tau_source": 0.56, "model": "text-embedding-v4",
                    "dim": 1024, "k_source": 30, "chunk_byte_budget": 32000},
    "background": {"tick_interval_s": 0, "ingest": False,
                   "auto_snapshot": {"on_session": False, "interval_hours": 0}},
}
```

- `read_memory_config` 新规则（90 篇配置键表、第 20 条）：`url` 与 `token_file` 同给同不给；`url` 过 `normalize_base_url`（不许 userinfo），scheme 是 `http`，主机是回环地址（`127.0.0.0/8`、`::1`、`localhost`），否则 `FactoryConfigError("memory config: url must be an http URL on the loopback interface")`，不回显取值；`token_file` 是绝对路径、仓库外、0600、普通文件（复用 `_check_private`、`_is_inside`）；`world_prefix` 过 `WORLD_PREFIX_PATTERN.fullmatch`。`MemoryConfig.mode` 属性：`url` 给了是 `"external"`，否则 `"spawn"`。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_factory_starling_names.py
import pytest

from factory.starling_names import (deploy_world, labels, replay_world, rise_settings, rollout_world, root_label,
                                    seed_world, settings_digest)


def test_names_follow_the_world_table():
    assert deploy_world("rise", "s1") == "rise.deploy.s1"
    assert seed_world("rise", "k", "s1", 2) == "rise.seed.k.s1.v2"
    assert rollout_world("rise", "run-1-grpo3-s1-r1", "0a1b2c3d") == "rise.ro.run-1-grpo3-s1-r1.0a1b2c3d"
    assert replay_world("rise", "agent-round-m-v1", "s1", "0a1b2c3d") == "rise.verify.agent-round-m-v1.s1.0a1b2c3d"


def test_an_overlong_name_hashes_the_middle_id_and_keeps_the_rest():
    eid = "e" * 128
    name = rollout_world("rise", eid, "0a1b2c3d")
    assert len(name) <= 128 and name.startswith("rise.ro.") and name.endswith(".0a1b2c3d") and eid not in name


def test_the_root_label_is_sixteen_hex_chars_of_the_resolved_path(tmp_path):
    a = root_label(tmp_path)
    assert len(a) == 16 and a == root_label(tmp_path / "." ) and a != root_label(tmp_path / "other")


def test_rise_settings_write_every_leaf_and_never_name_a_provider(memory_config):
    s = rise_settings(memory_config)
    assert set(s) == {"extraction", "embedding", "chat", "consolidation", "gist_thresholds", "vector_backend",
                      "source_lane", "background"}
    for role in ("extraction", "embedding"):
        assert set(s[role]) == {"provider", "provider_name", "base_url", "model", "dim", "max_tokens",
                                "timeout_ms", "max_retries", "enable_thinking", "thinking_type"}
        assert s[role]["provider_name"] == "" and s[role]["enable_thinking"] is False
    assert len(s["source_lane"]) == 7 and s["source_lane"]["enabled"] is False
    assert s["background"] == {"tick_interval_s": 0, "ingest": False,
                               "auto_snapshot": {"on_session": False, "interval_hours": 0}}
    assert settings_digest(s) == settings_digest(dict(reversed(list(s.items()))))     # 与键序无关


def test_labels_always_carry_repo_and_root():
    assert labels("rise", "abcd", "deploy", scenario_id="s1") == {"repo": "rise", "root": "abcd", "kind": "deploy",
                                                                 "scenario_id": "s1"}
```

`memory_config` 夹具在同一文件里造一个 `MemoryConfig`。叶子继承用真服务钉住（同一文件，模块级服务，`starling_json` 里放非默认的 source lane 与 provider 的 `enable_thinking`）：

```python
@pytest.fixture(scope="module")
def tuned_service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_service import start_service
    proc = start_service(tmp_path_factory.mktemp("tuned"), starling_json={
        "providers": {"p": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                            "enable_thinking": True}},
        "roles": {"extraction": "p"},
        "source_lane_enabled": True, "source_lane_k_source": 7, "source_lane_tau_source": 0.4,
    })
    yield proc
    proc.kill()


def test_worlds_built_and_forked_under_a_tuned_default_world_still_match_rise_settings(tuned_service, memory_config):
    import starling_client
    settings = rise_settings(memory_config)
    client = starling_client.connect(tuned_service.url, token=tuned_service.token)
    try:
        client.create_world("rise.deploy.s1", labels={"repo": "rise", "kind": "deploy"}, settings=settings)
        client.open("rise.deploy.s1", holder="pa", settings=settings).close()        # 不报 world_settings_mismatch
        snap = client.snapshot("rise.deploy.s1", note="t")
        client.fork("rise.deploy.s1", "rise.verify.r.s1.00000000", snapshot_id=snap["snapshot_id"],
                    reclaimable=True, retention_group="r", settings=settings)
        client.open("rise.verify.r.s1.00000000", holder="pa", read_only=True, settings=settings).close()
    finally:
        client.close()
```

`tests/test_factory_memory_config.py` 加：`url` 只给一个、`url` 不是回环（`http://10.0.0.1:8787`）、`url` 是 https、`url` 带 userinfo、`token_file` 权限 0644、`token_file` 在仓库里、`world_prefix` 含 `.`，各退出码 2，文字不含取值；两者都给时 `mode == "external"`，都不给时 `mode == "spawn"`。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_starling_names.py tests/test_factory_memory_config.py`
Expected: FAIL（模块不在；配置不认新键）

- [x] **Step 3: 实现**：按 Interfaces。`source_lane` 的七个值与 `S:include/starling/governance/world_settings.hpp:40-49` 逐个对过，在常量旁注出处。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: PASS；叶子继承那条在装了 extra 时真跑（不是 skip）

- [x] **Step 5: 提交**

```bash
git add src/factory/starling_names.py src/factory/memory_config.py tests/test_factory_starling_names.py tests/test_factory_memory_config.py
git commit -m "feat(factory): 世界名、标签与 rise 设置(写出每个叶子);记忆配置加 url、token_file、world_prefix"
```

### Task 10: 客户端异常的错误映射与运行时身份

错误映射放在适配器里（`simple_actio` 不得导入 `factory`，端口的映射又在 `simple_actio` 一侧做），运行时身份放在组合根的会话模块里（它要 `info()`）。

**Files:**
- Modify: `src/simple_actio/memory_starling.py:58-108`（加 `translate_client_failure(exc, *, port_closed_by_rise: bool, in_drain_window: bool) -> MemoryFailureError`；进程内的 `translate_failure` 留到第 4 步删）
- Create: `src/factory/starling_session.py`（本任务只放 `service_runtime(info, settings, config) -> dict` 与常量；会话类在 Task 12 加）
- Modify: `tests/test_simple_actio_instance.py:655-674`（`memory_starling.py` 的导入白名单加 `starling_client`；第 4 步再删 `starling`）
- Test: Create `tests/test_simple_actio_client_errors.py`、`tests/test_factory_starling_session.py`

**Interfaces:**
- Produces:
  - `translate_client_failure`（40 篇第 8 节的码表、AC-33）：消息一律 `f"starling {code} ({type(exc).__name__})"`，不带服务端 `detail`；返回值是新异常，调用方 `raise ... from None`。

| 输入 | 返回 | `transient` |
|---|---|---|
| `EventConflict` | `MemoryEventConflict` | 假 |
| `MemoryClosed`（`handle_closed`），`port_closed_by_rise` 为真 | `MemoryClosedError` | 假 |
| `MemoryClosed`（`handle_closed`），`port_closed_by_rise` 为假 | `MemoryFailureError` | 真（rise 改判） |
| `SessionExpired` | `MemoryFailureError` | 真（rise 改判） |
| `WorldError` 码 `world_held`，`in_drain_window` 为真 | `MemoryFailureError` | 真（rise 改判） |
| `WorldError` 码 `world_held`，其余 | `MemoryFailureError` | 假 |
| 其余 `StarlingClientError`（含 `recall_degraded`、`world_busy`、`worlds_unavailable`、`service_unavailable`、`service_timeout`、`event_not_found`、`world_settings_mismatch`、`world_read_only`、`snapshot_not_found`、`restore_digest_mismatch`、未列出的新码） | `MemoryFailureError` | `exc.transient` |
| `TimeoutError`（`RemoteMemory.close` 超时） | `MemoryFailureError` | 假（与今天 `close` 一致） |
| 其他异常 | `MemoryFailureError(f"starling client failed: {类名}")` | 假 |

  - `Unauthorized`、`ProtocolMismatch` 走"其余"那行（非瞬态）；它们在装配期与重连时由会话模块先接住、转成 `FactoryConfigError` 或致命状态（Task 12），到不了端口。
  - `factory.starling_session.service_runtime(info, settings, config) -> dict`（90 篇运行时身份表）：五个顶层键，`backend` `"starling"`；`starling_version` `f"{info['version']}+git:{info['commit']}+protocol:{info['protocol']}"`；`extraction` 是今天的五项（`base_url`、`model`、`timeout_ms`、`max_retries`，`prompts` 改为 `info["extraction_config"]` 规范化 JSON 的 sha256）加 `world_settings`（`settings_digest(settings)`）；`embedding` 不变；`trace_schema` `TRACE_SCHEMA_V2`。规范化 JSON 一律 `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_simple_actio_client_errors.py
import pytest

errors = pytest.importorskip("starling_client.errors")
from simple_actio.memory_port import MemoryClosedError, MemoryEventConflict, MemoryFailureError
from simple_actio.memory_starling import translate_client_failure


def _t(exc, *, closed=False, drain=False):
    return translate_client_failure(exc, port_closed_by_rise=closed, in_drain_window=drain)


@pytest.mark.parametrize("exc, cls, transient", [
    (errors.EventConflict("event_conflict"), MemoryEventConflict, False),
    (errors.SessionExpired("session_expired"), MemoryFailureError, True),
    (errors.RecallDegradedError("recall_degraded", transient=True), MemoryFailureError, True),
    (errors.ServiceUnavailable("service unreachable"), MemoryFailureError, True),
    (errors.ServiceTimeout("service request timed out"), MemoryFailureError, True),
    (errors.ServiceError("world_busy", code="world_busy", transient=True), MemoryFailureError, True),
    (errors.WorldError("world_settings_mismatch", code="world_settings_mismatch", transient=False), MemoryFailureError, False),
    (errors.EventNotFound("event_not_found"), MemoryFailureError, False),
    (errors.WorldError("brand_new_code", code="brand_new_code", transient=True), MemoryFailureError, True),   # 信 Starling 的标记
])
def test_each_code_maps_to_its_class_and_transient_flag(exc, cls, transient):
    out = _t(exc)
    assert type(out) is cls and out.transient is transient
    assert str(out).startswith(f"starling {exc.code} (")


def test_handle_closed_is_permanent_only_when_rise_closed_the_port():
    exc = errors.MemoryClosed("handle_closed")
    assert type(_t(exc, closed=True)) is MemoryClosedError
    assert type(_t(exc, closed=False)) is MemoryFailureError and _t(exc, closed=False).transient is True


def test_world_held_is_transient_only_inside_the_drain_window():
    exc = errors.WorldError("world_held", code="world_held", transient=False)
    assert _t(exc, drain=True).transient is True and _t(exc, drain=False).transient is False


def test_the_message_carries_no_detail_url_or_token():
    exc = errors.WorldError("world_held", code="world_held")
    exc.args = ("world_held http://127.0.0.1:8787 Bearer s3cr3t",)
    assert "8787" not in str(_t(exc)) and "s3cr3t" not in str(_t(exc))
```

```python
# tests/test_factory_starling_session.py(本任务的部分)
from factory.starling_session import service_runtime

INFO = {"version": "0.0.1", "commit": "c" * 40, "protocol": 1, "extraction_config": {"b": 1, "a": [2]}}


def test_the_runtime_identity_keeps_five_top_level_keys_and_names_the_service(memory_config):
    rt = service_runtime(INFO, {"x": 1}, memory_config)
    assert set(rt) == {"backend", "starling_version", "extraction", "embedding", "trace_schema"}
    assert rt["starling_version"] == f"0.0.1+git:{'c' * 40}+protocol:1"
    assert rt["extraction"]["world_settings"] and len(rt["extraction"]["prompts"]) == 64


def test_the_identity_changes_with_the_extraction_config_and_the_settings(memory_config):
    a = service_runtime(INFO, {"x": 1}, memory_config)
    assert a != service_runtime({**INFO, "extraction_config": {"b": 2}}, {"x": 1}, memory_config)
    assert a != service_runtime(INFO, {"x": 2}, memory_config)
    assert a == service_runtime({**INFO, "extraction_config": {"a": [2], "b": 1}}, {"x": 1}, memory_config)
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_client_errors.py tests/test_factory_starling_session.py tests/test_simple_actio_instance.py`
Expected: FAIL（函数不在；白名单不放 `starling_client`）

- [x] **Step 3: 实现**：按 Interfaces。`translate_client_failure` 在 `memory_starling.py` 里惰性导入 `starling_client.errors`（模块层已导入 `starling`；第 4 步换成模块层导入 `starling_client`）。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_client_errors.py tests/test_factory_starling_session.py tests/test_simple_actio_instance.py tests/test_simple_actio_memory_starling.py tests/test_factory_cross_plan.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/simple_actio/memory_starling.py src/factory/starling_session.py tests/test_simple_actio_client_errors.py tests/test_factory_starling_session.py tests/test_simple_actio_instance.py
git commit -m "feat: 客户端异常按 code 与 transient 映射(三处改判),运行时身份取服务 info()"
```

### Task 11: 拉起模式：拉起与停止、残留服务六项核对、归属锁

**Files:**
- Create: `src/factory/starling_service.py`（不导入 `starling` 与 `starling_client`：服务是子进程 `python -m starling.service`，就绪靠读 stdout）
- Test: Create `tests/test_factory_starling_service.py`

**Interfaces:**
- Consumes: `controlplane.lock.hold_file_lock`、`FileLockBusyError`（`src/controlplane/lock.py:81-97`，`flock`，与服务的 `D/.lock` 同一取法，`S:python/starling/service/serve.py:61-74`）。
- Produces（90 篇拉起模式两张表、FC-41）：

```python
READY_TIMEOUT_S = 60.0
TERM_WAIT_S = 40.0
KILL_WAIT_S = 5.0
RESTART_BACKOFF_S = (1.0, 4.0, 16.0)

@dataclass(frozen=True)
class ProcessProbe:
    """读别的进程的属主与命令行;测试注入假实现。"""
    alive: Callable[[int], bool]                    # os.kill(pid, 0),ProcessLookupError 为假
    uid_of: Callable[[int], int | None]             # macOS: ps -o uid= -p;Linux: os.stat(f"/proc/{pid}").st_uid
    command_of: Callable[[int], str | list[str] | None]   # macOS: ps -ww -o command= -p(字符串);Linux: /proc/<pid>/cmdline 按 NUL 拆(列表)
    signal: Callable[[int, int], None]              # os.kill

SYSTEM_PROBE: ProcessProbe

class ServiceRestartsExhausted(Exception): ...

class SpawnedService:
    url: str            # http://127.0.0.1:<端口>
    token: str          # 只在内存里;不进 repr、日志与异常
    pid: int
    def alive(self) -> bool: ...
    def restart(self) -> None: ...      # 进程已退出时按退避重拉;第 4 次起抛 ServiceRestartsExhausted
    def close(self) -> None: ...        # SIGTERM,至多 40 秒,再 SIGKILL 至多 5 秒;删 service.pid;最后释放归属锁

def spawn_service(root: Path, env: Mapping[str, str], *, python: str = sys.executable,
                  probe: ProcessProbe = SYSTEM_PROBE, ready_timeout_s: float = READY_TIMEOUT_S,
                  sleep: Callable[[float], None] = time.sleep, command: Sequence[str] | None = None) -> SpawnedService: ...
```

- 顺序：取 `<root>/service.owner.lock`（被占 → `FactoryConfigError("another rise process is using the spawned memory service")`，什么都不拉起）→ 数据目录 `D = <root>/service`，D 的字符串含空格就拒绝 → 删遗留的 `<root>/service.token`，写临时文件（0600）再改名 → `subprocess.Popen([python, "-m", "starling.service", "--data-dir", D, "--host", "127.0.0.1", "--port", "0", "--token-file", <root>/service.token], env=子进程环境, stdout=PIPE, stderr=追加打开的 <root>/service.log, close_fds=True)` → 读线程读 stdout：`starling-serve listening on http://127.0.0.1:(\d+)` 取端口 → 此时才写 `<root>/service.pid`（JSON：`pid` 与完整 `argv`，临时文件加改名，0600）：服务先取 `D/.lock` 再绑端口（`S:python/starling/service/serve.py:103-108`），看到 listening 就说明新进程持锁；在这之前写会把残留服务的 pid 文件盖掉，残留核对就读不到它 → `starling-serve ready` 判就绪 → 删 token 文件。
- 子进程环境：父进程环境去掉 `STARLING_*`，`HOME=D/home`，`OPENAI_API_KEY` 照传。
- 出口（全部 `FactoryConfigError`，文字写相对名 `service.log`，不写 token 与绝对路径）：60 秒没有 ready（先杀子进程）；子进程退出码 1；退出码 3 且残留核对不过；停掉残留后重试仍是退出码 3。
- 残留核对（退出码 3 时按序，任一项不过就拒绝、写出那一项与 pid，不发任何信号）：①归属锁已在手（上面已取）；②`service.pid` 在且是 `{"pid": int, "argv": [str, ...]}`；③`probe.alive(pid)`；④`probe.uid_of(pid) == os.getuid()`；⑤命令行：`command_of` 给列表时逐项比 `argv[1:]`、第一项的文件名以 `python` 开头（不分大小写）；给字符串时要以 `" " + " ".join(argv[1:])` 结尾，且前面那段的文件名以 `python` 开头；记下的 `argv` 必须含 `-m starling.service` 与 `--data-dir <本次的 D>`；⑥对 `D/.lock` 试 `hold_file_lock`：拿得到就立即释放、不发信号、直接重试启动。全部通过才 `SIGTERM`，每 0.2 秒试一次 `D/.lock` 至多 40 秒，没释放就 `SIGKILL` 再等至多 5 秒，删 `service.pid`，重试启动一次。每步记一条 warning（pid 与核对结果）。
- `restart()`：`alive()` 为真时什么都不做；否则按 `RESTART_BACKOFF_S` 的第 n 项 `sleep` 后重新走一遍拉起（新端口、新 token，归属锁不放）；进程生命周期内第 4 次调用抛 `ServiceRestartsExhausted`。

- [x] **Step 1: 写失败的测试**（`tests/test_factory_starling_service.py`；真拉起的用例 `importorskip("starling.service")`）

```python
import json, os, signal, subprocess, sys, time
from dataclasses import replace

import pytest

from controlplane.lock import hold_file_lock
from factory.errors import FactoryConfigError
from factory.starling_service import SYSTEM_PROBE, ServiceRestartsExhausted, spawn_service


def _env(tmp_path):
    return {"PATH": os.environ["PATH"], "OPENAI_API_KEY": "offline-test-key", "STARLING_CONFIG": "/nope"}


def test_a_spawned_service_is_ready_answers_and_stops_cleanly(tmp_path):
    pytest.importorskip("starling.service"); client = pytest.importorskip("starling_client")
    svc = spawn_service(tmp_path, _env(tmp_path))
    try:
        c = client.connect(svc.url, token=svc.token)
        assert c.info()["protocol"] == client.PROTOCOL
        c.close()
        assert not (tmp_path / "service.token").exists()                  # ready 之后就删
        assert json.loads((tmp_path / "service.pid").read_text())["pid"] == svc.pid
        assert oct((tmp_path / "service.pid").stat().st_mode & 0o777) == "0o600"
    finally:
        svc.close()
    assert not (tmp_path / "service.pid").exists() and not svc.alive()
    with hold_file_lock(tmp_path / "service.owner.lock"):               # 归属锁已释放
        pass
    assert svc.token not in (tmp_path / "service.log").read_text("utf-8", "replace")


def test_a_held_ownership_lock_refuses_before_anything_is_spawned(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, sys, time; f = open(sys.argv[1], 'a+b'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('held', flush=True); time.sleep(60)"), str(tmp_path / "service.owner.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        with pytest.raises(FactoryConfigError, match="another rise process"):
            spawn_service(tmp_path, _env(tmp_path))
        assert not (tmp_path / "service.pid").exists() and not (tmp_path / "service").exists()
    finally:
        other.kill(); other.wait()


def _residual(tmp_path, argv_tail):
    """一个持着 D/.lock 的无关进程,service.pid 记着它;返回 (进程, 记下的 argv)。"""
    d = tmp_path / "service"; d.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, sys, time; f = open(sys.argv[1], 'a+b'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('held', flush=True); time.sleep(120)"), str(d / ".lock")], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    argv = [sys.executable, "-m", "starling.service", "--data-dir", str(d), *argv_tail]
    (tmp_path / "service.pid").write_text(json.dumps({"pid": proc.pid, "argv": argv}))
    return proc, argv


@pytest.mark.parametrize("seen, item", [
    (lambda argv: "/usr/bin/python3 -m starling.service --data-dir /elsewhere --port 0", "command line"),   # 别的 D
    (lambda argv: "/usr/bin/python3 -m starling.service --data-dir", "command line"),                     # 被截断
    (lambda argv: "/bin/sleep 120", "command line"),                                                       # pid 被复用
    (lambda argv: None, "command line"),                                                                   # 读不到
])
def test_a_residual_that_does_not_match_is_refused_and_left_running(tmp_path, seen, item):
    proc, argv = _residual(tmp_path, ["--host", "127.0.0.1", "--port", "0", "--token-file", "t"])
    probe = replace(SYSTEM_PROBE, command_of=lambda pid: seen(argv),
                    signal=lambda pid, sig: pytest.fail("must not signal"))
    try:
        with pytest.raises(FactoryConfigError, match=item) as info:
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert str(proc.pid) in str(info.value) and "service.log" in str(info.value)
        assert proc.poll() is None                                       # 没被杀
    finally:
        proc.kill(); proc.wait()


def test_a_residual_owned_by_another_user_is_refused(tmp_path):
    proc, argv = _residual(tmp_path, [])
    probe = replace(SYSTEM_PROBE, uid_of=lambda pid: os.getuid() + 1, signal=lambda pid, sig: pytest.fail("no"))
    try:
        with pytest.raises(FactoryConfigError, match="owner"):
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
    finally:
        proc.kill(); proc.wait()


def test_a_residual_that_matches_every_check_is_stopped_and_the_start_is_retried(tmp_path):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, [])
    probe = replace(SYSTEM_PROBE, command_of=lambda pid: argv if pid == proc.pid else SYSTEM_PROBE.command_of(pid))
    svc = spawn_service(tmp_path, _env(tmp_path), probe=probe)
    try:
        assert proc.wait(timeout=45) is not None and svc.alive()        # 残留被 SIGTERM 停掉,新服务起来
    finally:
        svc.close()


def test_a_data_dir_lock_that_is_no_longer_held_means_the_residual_already_left(tmp_path):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, [])
    signals = []

    def uid_then_leave(pid):
        proc.kill(); proc.wait()                                         # 核对途中残留自己退了
        return os.getuid()

    probe = replace(SYSTEM_PROBE, command_of=lambda pid: argv, uid_of=uid_then_leave,
                    alive=lambda pid: True, signal=lambda pid, sig: signals.append(sig))
    svc = spawn_service(tmp_path, _env(tmp_path), probe=probe)
    svc.close()
    assert signals == []                                                 # 不发信号,直接重试启动


def test_no_ready_line_within_the_limit_kills_the_child_and_exits_2(tmp_path):
    hang = [sys.executable, "-c", "print('starling-serve listening on http://127.0.0.1:1', flush=True); import time; time.sleep(60)"]
    with pytest.raises(FactoryConfigError, match="not ready") as info:
        spawn_service(tmp_path, _env(tmp_path), command=hang, ready_timeout_s=0.5)
    assert "service.log" in str(info.value) and str(tmp_path) not in str(info.value)


def test_restarts_back_off_and_stop_after_three(tmp_path):
    pytest.importorskip("starling.service")
    slept = []
    svc = spawn_service(tmp_path, _env(tmp_path), sleep=slept.append)
    try:
        for _ in range(3):
            os.kill(svc.pid, signal.SIGKILL); time.sleep(0.2)
            svc.restart()
            assert svc.alive()
        os.kill(svc.pid, signal.SIGKILL); time.sleep(0.2)
        with pytest.raises(ServiceRestartsExhausted):
            svc.restart()
        assert slept == [1.0, 4.0, 16.0]
    finally:
        svc.close()
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_starling_service.py`
Expected: FAIL（模块不在）

- [x] **Step 3: 实现**：按 Interfaces。`SYSTEM_PROBE` 的 `uid_of`、`command_of` 按 `sys.platform` 选 macOS 与 Linux 两套；`subprocess.run([...], capture_output=True, timeout=5)` 失败或非零退出返回 `None`。`SpawnedService.__repr__` 不含 token。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: PASS（装了 extra 时真拉起的用例真跑；整份文件约 1 分钟）

- [x] **Step 5: 提交**

```bash
git add src/factory/starling_service.py tests/test_factory_starling_service.py
git commit -m "feat(factory): 拉起模式的服务进程:归属锁、pid 文件、残留服务六项核对、停止与重启退避"
```

### Task 12: 服务会话 ServiceSession：代号、端口计数、排空窗口、管理传输、重连再核对、致命状态

**Files:**
- Modify: `src/factory/starling_session.py`（Task 10 建的文件里加会话类）
- Test: `tests/test_factory_starling_session.py`

**Interfaces:**
- Consumes: Task 10 的 `service_runtime`、`translate_client_failure`；Task 11 的 `SpawnedService`（拉起模式）。
- Produces（90 篇"服务会话 ServiceSession"一节、"客户端超时"一节、FC-42、FC-46、FC-47）：

```python
HANDLE_TIMEOUT_S = 30.0
ADMIN_TIMEOUT_S = 300.0
DRAIN_WINDOW_S = 60.0

@dataclass(frozen=True)
class ServiceIdentity:
    commit: str
    protocol: int
    extraction_digest: str      # info()["extraction_config"] 规范化 JSON 的 sha256

class ServiceFatal(Exception):
    """会话已进致命状态;reason 只含码、提交号与 service.log 的相对名。"""

class ServiceSession:
    def __init__(self, *, url_of: Callable[[], str], token_of: Callable[[], str],
                 pinned_commit: str, connect: Callable[..., object] | None = None,
                 transport: Callable[..., object] | None = None,
                 restart: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic) -> None: ...
    generation: int                                   # 当前代号,从 1 起
    identity: ServiceIdentity                         # 第 1 代连上时记下,之后每次换会话都与它比
    info: dict                                        # 第 1 代的 info(),给 service_runtime
    def open_port(self, world: str, holder: str, *, read_only: bool, settings: dict,
                  wrap: Callable[[object, Callable[[], None], int], object]) -> object: ...
    def admin(self, op: str, body: dict) -> dict: ...  # 经管理传输 POST /api/v1/client/admin/<op>,超时 300 秒
    def mark_lost(self, generation: int, *, writable_worlds: Iterable[str]) -> None: ...
    def in_drain_window(self, world: str) -> bool: ...
    def owns_generation(self, generation: int) -> bool: ...   # 端口记的代号还是当前代号
    @property
    def fatal(self) -> str | None: ...
    def close(self) -> None: ...
```

- 构造：立刻连接（代号 1），调 `info()`。`commit != pinned_commit` 或 `protocol != starling_client.PROTOCOL`：抛 `FactoryConfigError`，文字写两个提交，提示同步看板服务的源码树或 rise 的钉住版本（90 篇第 21 条）；`Unauthorized`、`ProtocolMismatch`、连不上也抛 `FactoryConfigError`，只写码。通过后记下 `identity`（`extraction_digest` 取这次 `info()`）与 `info`。
- 端口计数：`open_port` 在当前代号的计数上加一，再调 `client.open(world, holder=holder, read_only=read_only, settings=settings)`；失败时退回计数并按 `translate_client_failure(..., in_drain_window=self.in_drain_window(world))` 抛。成功后调 `wrap(remote, release, generation)` 造端口（`release` 在端口关闭或"当作已关"时调一次，减计数；计数归零且这一代已丢失时 `client.close()`）。代号记在端口里，`owns_generation` 给适配器判"代号过期，当作已关"（40 篇适配器测试项）。
- 丢失：适配器在调用收到 `SessionExpired`、或 rise 没关的端口收到 `handle_closed` 时调 `mark_lost(gen, writable_worlds=...)`；`mark_lost` 记下"代号 gen 在哪些世界上有可写句柄、丢失时刻"，只对当前代号生效（旧代号重复报告是空操作）。
- 换会话：下一次 `open_port` 或 `admin` 发现当前代号已丢失时，新建客户端（`connect(url_of(), token=token_of(), timeout_s=HANDLE_TIMEOUT_S)`，拉起模式下先 `restart()`），代号加一，`_log.warning("memory service session %d lost; switched to %d", old, new)`。换完立刻调 `info()` 与 `identity` 比 `commit`、`protocol`、`extraction_digest`；不符，或 `connect` 抛 `Unauthorized`、`ProtocolMismatch`，或 `restart()` 抛 `ServiceRestartsExhausted`：记下致命原因（`fatal` 非空），本次调用与之后的每次 `open_port`、`admin` 都抛**瞬态** `MemoryFailureError("starling session is stopping")`，不落到单条指令或轮次上（90 篇"停守护进程分两半"）。
- 排空窗口：`in_drain_window(world)` 为真当且仅当有一个已丢失的代号在丢失时于 `world` 上有可写句柄，且距丢失不到 `DRAIN_WINDOW_S`。`world` 是完整世界名，记录与比较都按它（90 篇"排空窗口的比较键"一段）：rollout 世界每次重试都换后缀，重连后开的是新世界，窗口只对名字不变的部署世界起作用。
- 管理传输：`transport(url_of(), token_of(), ADMIN_TIMEOUT_S)`，缺省 `starling_client.Transport`；请求体不带 `session_id`，所以会话过期后照常可用。管理传输随换会话重建（拉起模式下端口与 token 会变）。客户端异常照 `translate_client_failure`（`port_closed_by_rise=False`、`in_drain_window=False`）。
- 关闭：`close()` 关当前与尚未关掉的旧客户端（过期的客户端 `close()` 不发 disconnect，客户端自己处理），再关管理传输。拉起服务的停止不在这里，由组合根另登记（Task 16）。

- [x] **Step 1: 写失败的测试**（假客户端与假传输，不起服务；最后两条用真服务）

```python
import threading

import pytest

errors = pytest.importorskip("starling_client.errors")
from factory.starling_session import ADMIN_TIMEOUT_S, HANDLE_TIMEOUT_S, ServiceIdentity, ServiceSession
from simple_actio.memory_port import MemoryFailureError

class FakeClient:
    def __init__(self, info, *, open_error=None, gate=None):
        self._info, self.open_error, self.gate, self.closed = info, open_error, gate, False
    def info(self): return self._info
    def open(self, world, *, holder, read_only, settings):
        if self.gate is not None:
            self.gate.wait(5)
        if self.open_error is not None:
            raise self.open_error
        return object()
    def close(self): self.closed = True


def _session(clients, **kw):
    made = []

    def connect(url, *, token, timeout_s):
        assert timeout_s == HANDLE_TIMEOUT_S
        c = clients.pop(0); made.append(c); return c

    s = ServiceSession(url_of=lambda: "http://127.0.0.1:1", token_of=lambda: "t", pinned_commit="c" * 40,
                       connect=connect, transport=lambda url, token, timeout_s: FakeTransport(timeout_s), **kw)
    return s, made


INFO = {"commit": "c" * 40, "protocol": 1, "extraction_config": {}, "version": "0.0.1"}


def test_an_old_generation_client_is_closed_only_after_its_last_port_closes():
    s, made = _session([FakeClient(INFO), FakeClient(INFO)])
    releases = []
    s.open_port("rise.deploy.s1", "pa", read_only=False, settings={},
                wrap=lambda remote, release, gen: releases.append((release, gen)))
    s.mark_lost(1, writable_worlds=["rise.deploy.s1"])
    s.open_port("rise.deploy.s2", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    assert s.generation == 2 and made[0].closed is False                 # 旧代号还有一个端口开着
    releases[0][0]()
    assert made[0].closed is True and made[1].closed is False


def test_a_call_racing_a_reconnect_gets_session_expired_not_a_closed_transport():
    gate = threading.Event()
    first = FakeClient(INFO, gate=gate, open_error=errors.SessionExpired("session_expired"))
    s, made = _session([first, FakeClient(INFO)])
    seen = []
    t = threading.Thread(target=lambda: seen.append(_capture(lambda: s.open_port(
        "rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None))))
    t.start()
    s.mark_lost(1, writable_worlds=[])                                    # 另一条线程发现过期
    s.open_port("rise.deploy.s2", "pb", read_only=False, settings={}, wrap=lambda r, rel, g: None)   # 换成第 2 代
    gate.set(); t.join(5)
    (exc,) = seen
    assert isinstance(exc, MemoryFailureError) and exc.transient and "session_expired" in str(exc)
    assert first.closed is True                                           # 线程 A 的调用退出后才关


def test_world_held_is_transient_only_within_sixty_seconds_of_losing_a_writer_there():
    now = [100.0]
    held = errors.WorldError("world_held", code="world_held", transient=False)
    s, _ = _session([FakeClient(INFO), FakeClient(INFO, open_error=held)], clock=lambda: now[0])
    s.open_port("rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    s.mark_lost(1, writable_worlds=["rise.deploy.s1"])
    with pytest.raises(MemoryFailureError) as inside:
        s.open_port("rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    now[0] += 61
    with pytest.raises(MemoryFailureError) as outside:
        s.open_port("rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    with pytest.raises(MemoryFailureError) as other_world:
        now[0] -= 61
        s.open_port("rise.deploy.s9", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    assert inside.value.transient is True and outside.value.transient is False and other_world.value.transient is False


@pytest.mark.parametrize("second, why", [
    (lambda: FakeClient({**INFO, "commit": "d" * 40}), "commit"),
    (lambda: (_ for _ in ()).throw(errors.Unauthorized("unauthorized")), "unauthorized"),
    (lambda: (_ for _ in ()).throw(errors.ProtocolMismatch("protocol_mismatch")), "protocol_mismatch"),
])
def test_a_reconnect_to_a_different_service_turns_fatal_and_every_later_call_defers(second, why):
    s, _ = _session([FakeClient(INFO), second])                         # connect 返回 FakeClient 或抛
    s.open_port("rise.deploy.s1", "pa", read_only=True, settings={}, wrap=lambda r, rel, g: None)
    s.mark_lost(1, writable_worlds=[])
    for _ in range(2):
        with pytest.raises(MemoryFailureError) as info:
            s.open_port("rise.deploy.s1", "pa", read_only=True, settings={}, wrap=lambda r, rel, g: None)
        assert info.value.transient is True
    assert why in s.fatal and "t" * 3 not in s.fatal


def test_admin_calls_go_through_a_sessionless_transport_with_the_long_timeout():
    s, _ = _session([FakeClient(INFO)])
    s.admin("list_worlds", {"labels": None, "page": 1, "page_size": 50, "with_digest": False})
    t = s._admin_transport
    assert t.timeout_s == ADMIN_TIMEOUT_S and t.posts == [("/api/v1/client/admin/list_worlds", "session_id")]
    assert s.identity == ServiceIdentity("c" * 40, 1, _sha256_of_canonical({}))


def test_a_service_on_another_commit_is_refused_at_construction_naming_both_commits():
    with pytest.raises(FactoryConfigError, match=f"{'d' * 40}.*{'c' * 40}|{'c' * 40}.*{'d' * 40}"):
        _session([FakeClient({**INFO, "commit": "d" * 40})])


def test_the_private_transport_still_has_the_signature_the_admin_path_relies_on():
    import inspect
    from starling_client._transport import Transport
    assert str(inspect.signature(Transport.__init__)) == \
        "(self, url: 'str', token: 'str', timeout_s: 'float' = 30.0, *, _httpx_transport=None)"
    assert str(inspect.signature(Transport.post)) == \
        "(self, path: 'str', json: 'dict | None' = None, *, timeout_s: 'float | None' = None) -> 'dict'"


def test_admin_calls_still_work_after_the_session_expired_against_a_real_service(service):
    from starling_client import connect
    import starling_client
    s = ServiceSession(url_of=lambda: service.url, token_of=lambda: service.token,
                       pinned_commit=starling_client.COMMIT)
    s.admin("create_world", {"name": "rise.deploy.t1", "labels": {"repo": "rise"}, "settings": None, "seed": False})
    s.mark_lost(1, writable_worlds=[])
    assert s.admin("digest", {"world": "rise.deploy.t1"})["digest"]
    s.close()
```

`FakeTransport(timeout_s)` 记下 `posts`（路径与"请求体里没有 `session_id`"的标记），`_capture(fn)` 返回 `fn` 抛的异常，`_sha256_of_canonical(obj)` 是规范化 JSON 的 sha256；三者写在同一测试文件里，另导入 `from factory.errors import FactoryConfigError`。`service` 是模块级真服务夹具（Task 8）。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_starling_session.py`
Expected: FAIL（`ServiceSession` 不在）

- [x] **Step 3: 实现**：按 Interfaces。一把 `threading.Lock` 护代号、各代号的计数、丢失表与致命原因；网络调用（`connect`、`info`、`open`、管理 POST）都在锁外。`extraction_digest` 是 `info()["extraction_config"]` 规范化 JSON 的 sha256，与 `service_runtime` 里的 `prompts` 同一个函数算。`fatal` 的文字只写码或"commit <期望> != <实际>"这样的提交号。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_starling_session.py tests/test_factory_starling_service.py tests/test_factory_starling_names.py tests/test_simple_actio_client_errors.py tests/test_factory_cross_plan.py`
Expected: PASS。这是第 3 步的最后一个任务：控制者在后台跑全量，必须绿；新模块没接线，全量里 Starling 路径仍是进程内。

- [x] **Step 5: 提交**

```bash
git add src/factory/starling_session.py tests/test_factory_starling_session.py
git commit -m "feat(factory): 服务会话:代号与按代号的端口计数、排空窗口、不带会话的管理传输、换会话后再核对与致命状态"
```

## 第 4 步：接线（一次提交）

开工前：`git switch -c design4-step4`。Task 13 至 17 在这个分支上逐任务提交，中间提交不要求全量绿（红窗口见 Global Constraints）。Task 17 的最后一步在全量绿之后把分支压成 main 上的一个提交，不停下等用户；分支保留不删，随时能对照或回退。

### Task 13: 端口改包 `RemoteMemory`，派发器让解析器的瞬态失败原样延后

**Files:**
- Modify: `src/simple_actio/memory_starling.py:149-243`（`StarlingMemory`：构造参数从 `starling.Memory` 换成客户端的 `RemoteMemory`，另收 `generation`、`session`；`open` 类方法删掉；`close` 的三种"当作已关"；异常改走 `translate_client_failure`）
- Modify: `src/mindtrainer/dispatch.py:274-295`（`_resolve_memory`：`except DeferredError: raise`，在 `except DirectiveFailedError` 之前）
- Test: `tests/test_simple_actio_memory_starling.py:21-63`（`FakeStarling` 换成 `FakeRemote`，方法签名照 `S:clients/python/starling_client/remote_memory.py:73-112`）、`:84-400`、`tests/test_mt_dispatch.py:1121-1180`

**Interfaces:**
- Consumes: Task 10 的 `translate_client_failure`；Task 12 的 `ServiceSession.owns_generation`、`mark_lost`。
- Produces:
  - `StarlingMemory(remote, *, generation: int, session: SessionView, release: Callable[[], None], read_only: bool = False, close_timeout_s: float = 60.0)`。`read_only=True` 只由管理面的 `open_replay` 传（Task 14）。`SessionView` 是适配器里按结构声明的协议（`owns_generation(gen) -> bool`、`mark_lost(gen, *, writable_worlds) -> None`），`simple_actio` 不导入 `factory`。
  - 方法逐一对应（40 篇"接服务后"的 Starling 一行）：`recall` → `remote.recall(q, k=limit, mode="semantic", strict=True, include_volatile=True)`，命中是 `{"content_id", "score", "row"}`，`row` 是客户端的 `StatementRow`，`_render` 用的五个字段都在；`remember` → `remote.remember_event(key, text, interlocutor=, epoch=EPOCH)`，结果照 Task 3 的 `_outcome`（内容 id 换算改调 `remote.content_ids`）；`embed_pending` → `remote.next_seq()` 加 `remote.embed_pending(now)`；`digest` → `remote.digest()`。
  - 调用收到 `SessionExpired`，或 rise 没关的端口收到 `handle_closed`：先 `session.mark_lost(generation, writable_worlds=[] if read_only else [remote.world])`，再抛 `translate_client_failure(...)`（瞬态）。
  - `close()`：已关返回；`session.owns_generation(generation)` 为假时不发请求，直接当作已关（置位、调 `release`）；否则 `remote.close(timeout=close_timeout_s)`，收到 `SessionExpired` 或 `MemoryClosed` 也当作已关；`TimeoutError` 照旧抛非瞬态 `MemoryFailureError`、端口保持打开（今天的语义，`memory_starling.py:228-243`）。
  - `_resolve_memory`：解析器抛 `DeferredError` 原样上抛，指令留 pending（30 篇 MT-32）。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_simple_actio_memory_starling.py
errors = pytest.importorskip("starling_client.errors")


class Session:
    def __init__(self, current=1):
        self.current, self.lost = current, []
    def owns_generation(self, gen): return gen == self.current
    def mark_lost(self, gen, *, writable_worlds): self.lost.append((gen, list(writable_worlds)))


def _port(remote, session=None, **kw):
    released = []
    port = StarlingMemory(remote, generation=1, session=session or Session(), release=lambda: released.append(1), **kw)
    return port, released


def test_recall_asks_the_service_for_strict_volatile_semantic_hits():
    remote = FakeRemote(); port, _ = _port(remote)
    port.recall("预算", limit=3)
    assert remote.recall_kwargs == {"k": 3, "mode": "semantic", "strict": True, "include_volatile": True}


def test_a_session_expiry_marks_the_generation_lost_and_is_transient():
    session = Session(); remote = FakeRemote(raise_on={"recall": errors.SessionExpired("session_expired")})
    port, _ = _port(remote, session)
    with pytest.raises(MemoryFailureError) as info:
        port.recall("q", limit=1)
    assert info.value.transient is True and session.lost == [(1, [remote.world])]


def test_handle_closed_on_a_port_rise_did_not_close_is_a_lost_session_not_a_closed_port():
    session = Session(); remote = FakeRemote(raise_on={"digest": errors.MemoryClosed("handle_closed")})
    port, _ = _port(remote, session, read_only=True)
    with pytest.raises(MemoryFailureError) as info:
        port.digest()
    assert info.value.transient is True and session.lost == [(1, [])]          # 只读端口不记可写世界


@pytest.mark.parametrize("why", ["expired", "closed", "stale"])
def test_close_treats_an_expired_closed_or_stale_generation_port_as_closed(why):
    raise_on = {"expired": {"close": errors.SessionExpired("session_expired")},
                "closed": {"close": errors.MemoryClosed("handle_closed")}, "stale": {}}[why]
    remote = FakeRemote(raise_on=raise_on)
    port, released = _port(remote, Session(current=2 if why == "stale" else 1))
    port.close()
    assert released == [1] and (remote.close_calls == ([] if why == "stale" else [60.0]))
    with pytest.raises(MemoryClosedError):
        port.recall("q", limit=1)
```

```python
# tests/test_mt_dispatch.py
def test_a_resolver_that_defers_leaves_the_directive_pending(store, live):
    def busy(seed, scenario_ids):
        raise DeferredError("memory service unreachable")

    _interact_directive(store, memory=unresolved_memory())
    trace = []
    with pytest.raises(DeferredError):
        make_mindtrainer_dispatcher(client_factory=live().client_factory,
                                    policy_factory=lambda run_id, cfg: _Scope(trace, None),
                                    memory_resolver=busy)(store, store.read_product(f"{run_dir(RUN)}/directive.yaml"))
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending" and trace == []
```

`tests/test_mt_dispatch.py` 顶部补 `from controlplane.deferred import DeferredError`（若还没有）。派发器的调用形式照同文件里直接调派发器的现有用例。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_memory_starling.py tests/test_mt_dispatch.py`
Expected: FAIL（`StarlingMemory` 不收 `generation`；`DeferredError` 被译成 T2）

- [x] **Step 3: 实现**：按 Interfaces。`memory_starling.py` 模块层的 `import starling` 换成 `import starling_client`（`StarlingDeploymentStore` 在 Task 14 改，本任务里它暂时坏着，属于分支上的红窗口）。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: PASS

- [x] **Step 5: 提交**（分支 `design4-step4`）

```bash
git add src/simple_actio/memory_starling.py src/mindtrainer/dispatch.py tests/test_simple_actio_memory_starling.py tests/test_mt_dispatch.py
git commit -m "feat: Starling 端口改包 RemoteMemory,会话过期与 rise 没关的 handle_closed 判瞬态;解析器的延后原样上抛"
```

### Task 14: 部署管理面走服务：`ensure_world`、创建标记、按快照 id、重放世界；外部服务重启

**Files:**
- Modify: `src/simple_actio/memory_starling.py:248-572`（删 `_sidecar`、`_require_library_file`、`copy_library`、`_short_connection`、`library_digest`、`empty_digest`、`starling_version`、`extraction_prompts_digest`、`translate_failure`、`_translated` 与进程内的 `StarlingDeploymentStore`；重写为经会话的 `StarlingDeploymentStore`，含 `open` 与 `open_replay` 两个打开方法）
- Modify: `src/simple_actio/memory_starling.py:54-77`（`StarlingEndpoints` 只装端点字段，删 `make_llm`、`make_embedder`）
- Test: `tests/test_simple_actio_starling_store.py`（整份重写为真服务用例）、`tests/test_simple_actio_deployment_store_contract.py:31-44`（`starling` 参数改用真服务，世界前缀每条用例唯一）、`tests/test_ma_deployment_starling.py:30-60`（夹具改用真服务）

**Interfaces:**
- Consumes: Task 9 的世界名与标签；Task 12 的 `ServiceSession`（经 `SessionView` 加 `open_port`、`admin` 两个方法的结构协议，适配器不导入 `factory`）；Task 13 的 `StarlingMemory`。
- Produces（40 篇"接服务后管理面的签名"表、00 篇 `ensure_world` 三步、90 篇 FC-49）：

```python
class StarlingDeploymentStore:
    def __init__(self, session: SessionView, *, prefix: str, root_label: str, settings: dict,
                 marker_dir: Path, empty_seed_digest: str, close_timeout_s: float = 60.0) -> None: ...
```

| 方法 | 实现 |
|---|---|
| `open(world_id, holder)` | 只收部署世界 id：`ensure_world(world_id)` 再开 `<前缀>.deploy.<world_id>`，`session.open_port(name, holder, read_only=False, settings=, wrap=...)`。不看 `world_id` 长什么样，名为 `<前缀>.verify.x` 的情境照旧是部署世界 |
| `open_replay(replay_world, holder)` | 参数是 `fork_replay` 返回的完整世界名，原样开，不走 `ensure_world`：`session.open_port(replay_world, holder, read_only=True, settings=, wrap=...)`，`wrap` 造 `StarlingMemory(..., read_only=True)`。世界不在时服务回 `world_not_found`，判永久 |
| `remember_event(world_id, holder, key, text, *, interlocutor)` | `ensure_world`；holder 的可写句柄上 `remember_event` 加 `embed_pending`，再关（今天短连接的同一口径） |
| `abandon_event(world_id, holder, key)` | 世界不存在（`world_not_found`）判永久失败；否则 holder 的句柄上 `abandon_event` |
| `pending_events(world_id)` | 管理调用 `pending_events`；`world_not_found` 返回 `[]`；空 holder 判永久 |
| `digest(world_id)` | 管理调用 `digest`；`world_not_found` 返回 `empty_seed_digest`，不建世界（40 篇：两者相等已实测） |
| `snapshot(world_id, *, note)` | `ensure_world`；管理调用 `snapshot`，返回 `SnapshotRef(out["snapshot_id"], out["digest"])` |
| `restore(world_id, snapshot_id)` | 管理调用 `restore`，返回摘要；码照 Task 10 映射（`world_busy` 瞬态，`world_held`、`snapshot_not_found`、`restore_digest_mismatch` 永久） |
| `delete_snapshot(world_id, snapshot_id)` | 管理调用 `delete_snapshot`；快照已删时服务回 `snapshot_not_found`（`S:src/governance/world_store.cpp:1131-1133`），这里当作成功返回，与替身的幂等一致（50 篇"删一个已删的快照"一段）。其余码照 Task 10 映射 |
| `fork_replay(world_id, snapshot_id, *, round_id)` | 世界名 `replay_world(prefix, round_id, world_id, new_suffix())`（调用前定），管理调用 `fork(source=<部署世界名>, name=, snapshot_id=, labels=labels(prefix, root, "verify", round_id=, scenario_id=), retention_group=round_id, reclaimable=True, settings=)`，返回完整世界名 |
| `release_replay(name)` | 管理调用 `close_world` |
| `wait_closed(world_id, timeout_s)` | 进程内计数，按完整世界名，部署与重放两种端口共用一张表；参数是部署世界 id，先拼成 `<前缀>.deploy.<id>` 再查 |

- `ensure_world(world_id)`：①有创建标记 `marker_dir/<world_id>.created` 而 `list_worlds` 里没有这个世界 → 永久失败 `"deploy world <名字> was deleted"`（装配期另报退出码 2，Task 16）；②`create_world(name, labels=labels(prefix, root, "deploy", scenario_id=world_id), settings=settings, seed=False)`，成功就写创建标记（临时文件加改名）；③收到 `world_exists`：`list_worlds(labels={"repo": prefix, "kind": "deploy", "scenario_id": world_id})` 翻完所有页找这个名字，找不到、`seed` 或 `read_only` 为真、`kind`、`repo` 不对 → 永久 `"world <名字> was not created by rise"`；`root` 标签不等于本记忆根 → 永久 `"world <名字> belongs to another memory root"`；对得上且标记不在就补写标记。同一进程内对每个世界只做一次（记一个集合）。
- 空种子 `ensure_empty_seed()`（同一类里，装配期调）：同样三步，世界名 `<前缀>.seed-empty`，`seed=True`，标签 `kind=seed-empty`；返回它的摘要（`list_worlds(with_digest=True)` 里的 `digest`）。

- [ ] **Step 1: 写失败的测试**（`tests/test_simple_actio_starling_store.py`，模块级真服务；每条用例用唯一前缀 `t<n>`，彼此不干扰）

```python
pytest.importorskip("starling_client"); pytest.importorskip("starling.service")


def test_a_deploy_world_is_created_once_with_rise_labels_and_a_creation_marker(store_factory):
    store, marker_dir, admin = store_factory()
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    (item,) = admin("list_worlds", {"labels": {"repo": store.prefix, "kind": "deploy"}})["worlds"]
    assert item["name"] == f"{store.prefix}.deploy.s1" and item["labels"]["scenario_id"] == "s1"
    assert item["labels"]["root"] == store.root_label and (marker_dir / "s1.created").is_file()


def test_a_same_named_world_from_another_root_is_refused(store_factory):
    a, _, _ = store_factory(prefix="tx")
    a.snapshot("s1", note="n")
    b, _, _ = store_factory(prefix="tx", root_label="0" * 16)          # 同前缀、别的记忆根
    with pytest.raises(MemoryFailureError, match="belongs to another memory root") as info:
        b.snapshot("s1", note="n")
    assert info.value.transient is False


def test_a_deleted_deploy_world_is_not_rebuilt_silently(store_factory):
    store, marker_dir, admin = store_factory()
    store.snapshot("s1", note="n")
    admin("close_world", {"world": f"{store.prefix}.deploy.s1"})
    _delete_world_through_the_dashboard_route(f"{store.prefix}.deploy.s1")
    fresh, _, _ = store_factory(prefix=store.prefix, marker_dir=marker_dir)
    with pytest.raises(MemoryFailureError, match="was deleted"):
        fresh.open("s1", "pa")


def test_deleting_a_deleted_snapshot_is_a_success_against_the_real_service(store_factory):
    store, _, admin = store_factory()
    ref = store.snapshot("s1", note="n")
    store.delete_snapshot("s1", ref.snapshot_id)
    with pytest.raises(Exception) as raw:                                # 服务本身回 snapshot_not_found
        admin("delete_snapshot", {"world": f"{store.prefix}.deploy.s1", "snapshot_id": ref.snapshot_id})
    assert "snapshot_not_found" in str(raw.value)
    store.delete_snapshot("s1", ref.snapshot_id)                         # 管理面当作成功,不抛


def test_open_replay_opens_the_forked_name_as_is_and_open_never_treats_a_name_as_a_replay(store_factory):
    store, marker_dir, admin = store_factory()
    ref = store.snapshot("s1", note="n")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    port = store.open_replay(name, "pa")
    assert port.digest() == ref.digest
    port.close()
    store.open(name, "pa").close()                                       # 情境 id 恰好等于重放世界名:建的是部署世界
    deploys = admin("list_worlds", {"labels": {"repo": store.prefix, "kind": "deploy"}})["worlds"]
    assert f"{store.prefix}.deploy.{name}" in {w["name"] for w in deploys} and (marker_dir / f"{name}.created").is_file()


def test_reading_an_unknown_world_builds_nothing_and_gives_the_empty_seed_digest(store_factory):
    store, marker_dir, admin = store_factory()
    assert store.digest("s9") == store.empty_seed_digest and store.pending_events("s9") == []
    assert admin("list_worlds", {"labels": {"repo": store.prefix, "kind": "deploy"}})["worlds"] == []
    assert not (marker_dir / "s9.created").exists()


def test_after_an_external_restart_an_old_port_reads_handle_closed_as_a_lost_session(restartable):
    store, session, service = restartable()                              # 外部模式:同一端口、同一 token
    port = store.open("s1", "pa")
    service.restart()
    with pytest.raises(MemoryFailureError) as info:
        port.recall("预算", limit=3)
    assert info.value.transient is True and session.generation == 1
    again = store.open("s1", "pa")                                       # 下一次需要服务时换会话
    assert session.generation == 2
    again.close(); port.close()                                          # 旧代号的端口当作已关
    assert store.wait_closed("s1", 0.1) is True
```

`store_factory(prefix=None, root_label=None, marker_dir=None)` 在模块级真服务上造 `ServiceSession` 与 `StarlingDeploymentStore`（前缀缺省取递增的 `t<n>`），返回 `(store, marker_dir, admin)`；`admin(op, body)` 直接调 `session.admin`，不经管理面；`restartable()` 另起一个本条用例独占的真服务（`start_service(..., port=<固定空闲端口>)`），`service.restart()` 在同一端口重起。`_delete_world_through_the_dashboard_route(name)` 用 httpx（`trust_env=False`、带 bearer token）发 `DELETE /api/worlds/<name>?confirm=<name>`（`S:python/starling/dashboard/routes/worlds.py:396-404`；路由前缀以 `S:python/starling/dashboard/app.py` 的挂载为准，实现时核对；客户端没有删世界的接口，只有测试这样做）。契约文件 `tests/test_simple_actio_deployment_store_contract.py` 的 `starling` 参数改为同一套夹具，Task 2、4 写下的契约用例对两个后端都跑。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py`
Expected: FAIL（`StarlingDeploymentStore` 还是进程内的签名）

- [ ] **Step 3: 实现**：按 Interfaces。管理调用的异常都经 `translate_client_failure(..., port_closed_by_rise=False, in_drain_window=False)`；会话致命时 `session.admin` 已经抛瞬态失败，这里不另判。删掉的进程内函数若还有调用方（`src/factory/memory_runtime.py` 的 `StarlingTrainingWorlds`、`starling_memory_resolver`），它们在 Task 15、16 改，分支上暂时坏着。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py tests/test_simple_actio_memory_starling.py tests/test_ma_deployment_starling.py`
Expected: PASS（`tests/test_ma_deployment_starling.py` 的恢复分码用例在真服务上钉住 Review Focus 最后一条）

- [ ] **Step 5: 提交**（分支 `design4-step4`）

```bash
git add src/simple_actio/memory_starling.py tests/test_simple_actio_starling_store.py tests/test_simple_actio_deployment_store_contract.py tests/test_ma_deployment_starling.py
git commit -m "feat(simple_actio): 部署管理面改走记忆服务:ensure_world 三步核对与创建标记、held 快照、重放世界分叉与 open_replay"
```

### Task 15: 训练世界改为分叉、只关不删；解析器走服务；分叉超时后按确切世界名复用

本步训练世界只接受 `seed` 为空（00 篇施工表第 4 行）：`bind(run_id, seed)` 仍是两参，`seed` 非空时 `open_rollout` 抛永久失败；按种子分叉在第 5 步。

**Files:**
- Modify: `src/factory/memory_runtime.py:196-294`（删 `_segment`、`StarlingLayout` 的 `seed_dir`、`seed`、`rollout_world`、`runs_dir`；`starling_memory_resolver` 改走服务）、`:590-673`（删 `_LIBRARY_SIDECARS`、`_remove_library`；`StarlingTrainingWorlds` 的 `_create`、`_port`、`_discard` 改实现，删 `sweep`）
- Test: `tests/test_factory_training_worlds.py:28-60`（`case` 夹具的 `starling` 参数改用真服务、`seed=None`）、`:244-395`（按路径与清扫的 Starling 专有用例删掉，换成下面几条）、`tests/test_factory_memory_runtime.py`（解析器用例）

**Interfaces:**
- Consumes: Task 9 的 `rollout_world`、`new_suffix`、`labels`；Task 12 的会话；Task 14 的 `ensure_empty_seed` 摘要。
- Produces（90 篇 `StarlingTrainingWorlds` 的变化表、分叉的命名与重试表、FC-43）：
  - `StarlingTrainingWorlds(session, *, prefix, root_label, settings, empty_seed: tuple[str, str], close_timeout_s=60.0, held_wait_s=SESSION_CLOSE_WAIT_S, admin_list=None)`；`empty_seed` 是 `(世界名, 摘要)`。
  - `_create(run_id, scenario_id, episode_id, seed)`：`seed` 非空抛永久 `MemoryFailureError("seeded rollouts arrive in step 5")`。否则最多 3 次：`name = rollout_world(prefix, episode_id, new_suffix())`（调用前定），`attempt` 按 episode id 在进程内递增；`admin("fork", {"source": empty_seed[0], "name": name, "snapshot_id": None, "labels": labels(prefix, root, "rollout", run_id=, scenario_id=, episode_id=, seed_id="", attempt=str(n)), "retention_group": run_id, "reclaimable": True, "seed": False, "settings": settings})`。
    - 成功：`out["digest"] != empty_seed[1]` → 永久 `"fork digest does not match the pinned start"`；否则返回 `name`。
    - `world_exists`：换后缀重试。
    - `service_timeout`：`list_worlds(labels={"repo", "root", "kind": "rollout", "episode_id"}, with_digest=True)` 翻完所有页，**只认名字等于 `name`** 的项；它在且 `digest` 等于钉住的摘要就用它，否则换后缀重试（不按 `attempt` 认）。
    - 3 次用完：永久失败。其余码照 Task 10 映射。
  - `_port(name, holder, release)`：`session.open_port(name, holder, read_only=False, settings=settings, wrap=lambda remote, rel, gen: StarlingMemory(remote, generation=gen, session=session, release=lambda: (rel(), release()), close_timeout_s=...))`。
  - `_discard(name)`：`admin("close_world", {"world": name})`；客户端异常译成 `MemoryFailureError` 并吸收，只记 warning（只报码与世界名），不抛（`close_rollout` 只接 `OSError`）。
  - 没有 `sweep`；`MemoryWiring.sweep` 为 `None`（Task 16）。
  - `starling_memory_resolver(session, *, prefix, root_label, empty_seed, runtime)`：`seed` 为 None 时每个情境取 `empty_seed[1]`；`seed` 非空本步抛 `DirectiveFailedError(f"memory.seed {seed!r}: seeds arrive in step 5")`（第 5 步换成真查）。服务瞬态失败抛 `DeferredError`（30 篇 MT-32、Task 13）；其余照 `DirectiveFailedError` 只报类名。

- [ ] **Step 1: 写失败的测试**（`tests/test_factory_training_worlds.py`）

```python
class TimeoutOnceAfterCreating:
    """fork 在服务端照常做完,第一次却对客户端抛 service_timeout(超时发生在响应回来之前)。"""
    def __init__(self, session):
        self.session, self.fired, self.names = session, False, []
    def __call__(self, op, body):
        out = self.session.admin(op, body)
        if op == "fork":
            self.names.append(body["name"])
            if not self.fired:
                self.fired = True
                raise errors.ServiceTimeout("service request timed out")
        return out


def test_a_fork_that_timed_out_after_creating_its_world_is_reused_by_exact_name(starling_worlds):
    worlds, session, admin = starling_worlds(admin_wrap=TimeoutOnceAfterCreating)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert admin.names == [admin.names[0]] and port._remote.world == admin.names[0]     # 只分叉一次
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_a_world_found_by_name_with_another_digest_is_not_reused(starling_worlds, monkeypatch):
    worlds, session, admin = starling_worlds(admin_wrap=TimeoutOnceAfterCreating)
    real = session.admin

    def written_meanwhile(op, body):
        out = real(op, body)
        if op == "list_worlds":
            for item in out["worlds"]:
                item["digest"] = "f" * 64                                 # 这个名字的世界已有别的内容
        return out

    monkeypatch.setattr(session, "admin", written_meanwhile)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert len(admin.names) == 2 and port._remote.world == admin.names[1]               # 换后缀重建
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_a_previous_processs_world_for_the_same_episode_is_never_claimed(starling_worlds):
    worlds, session, admin = starling_worlds(admin_wrap=TimeoutOnceAfterCreating)
    old, _, _ = starling_worlds(prefix=worlds.prefix)                    # 上一个进程:同 episode,attempt=1
    left = old.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    left.remember(MemoryEvent("run-1-grpo1-s1-r1#0@pa", "pb：上一个进程写的", "pb")); left.close()
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert port._remote.world != left._remote.world and port.digest() == worlds.empty_seed[1]
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_closing_a_rollout_only_closes_its_world_and_a_close_failure_is_a_warning(starling_worlds, caplog):
    worlds, session, admin = starling_worlds()
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    name = port._remote.world
    assert worlds.close_rollout("run-1-grpo1-s1-r1", [port]) is True
    (item,) = [w for w in session.admin("list_worlds", {"labels": {"repo": worlds.prefix, "kind": "rollout"},
                                                        "page": 1, "page_size": 50, "with_digest": False})["worlds"]
               if w["name"] == name]
    assert item["reclaimable"] is True and item["labels"]["attempt"] == "1"   # 世界还在,只是关了


def test_a_seeded_run_is_refused_until_step_5(starling_worlds):
    worlds, _, _ = starling_worlds()
    worlds.bind("run-2", "k")
    with pytest.raises(MemoryFailureError) as info:
        worlds.open_rollout("run-2", "s1", "run-2-grpo1-s1-r1", "pa")
    assert info.value.transient is False
```

`starling_worlds(prefix=None, admin_wrap=None)` 夹具在模块级真服务上造会话、`ensure_empty_seed`，再造 `StarlingTrainingWorlds` 并 `bind("run-1", None)`；`admin_wrap` 给了就包住传给世界的管理调用。`tests/test_factory_memory_runtime.py` 加：解析器 `seed=None` 时每个情境都是空种子摘要；会话的管理调用抛瞬态失败时解析器抛 `DeferredError`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py`
Expected: FAIL（`StarlingTrainingWorlds` 还在按路径复制）

- [ ] **Step 3: 实现**：按 Interfaces。端口对象暴露 `_remote` 只为测试读物理世界名；物理世界名另写进 `close_rollout` 的 warning（90 篇分叉表"成功"一行）。`_RolloutWorlds` 的表逻辑（`memory_runtime.py:351-542`）不改。

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2，再加 `tests/test_factory_interact_wire.py tests/test_mt_dispatch.py`
Expected: PASS（替身参数的用例照旧全过）

- [ ] **Step 5: 提交**（分支 `design4-step4`）

```bash
git add src/factory/memory_runtime.py tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py
git commit -m "feat(factory): rollout 世界改为从空种子分叉、只关不删;分叉超时按确切世界名加摘要复用;解析器走服务"
```

### Task 16: 装配：配置新键、装配顺序、持锁后的设置核对、致命停机、收尾顺序

**Files:**
- Modify: `src/factory/memory_config.py:267-321`（`starling_install_source` 改成 `client_install_commit(dist_name)`：只认 `vcs_info.commit_id`，不再接受 `record:`）
- Modify: `src/factory/memory_runtime.py:76-193`（删 `starling_memory_runtime`，身份改用 Task 10 的 `service_runtime`）、`:676-767`（`MemoryWiring` 加 `on_locked`、`closers`、`session`；`memory_wiring` 按 90 篇 4c 前一行的新顺序；删 `_check_runs_dir`）
- Modify: `src/factory/wire.py:116-135`（`Assembly.sweep` 换成 `on_locked`）、`:372-491`（服务收尾最先登记；派发器与处理器套致命检查）、`:493-543`（`run` 传 `on_locked=a.on_locked`；`reconcile_one_tick` 锁内调它）
- Modify: `src/factory/cli.py:160-164`（不改；致命停机靠它把 `FactoryConfigError` 译成退出码 2，测试钉住）
- Test: `tests/test_factory_memory_wiring.py`、`tests/test_factory_memory_config.py`（安装来源）、`tests/test_factory_handlers.py`、`tests/test_factory_interact_wire.py`（装配用例）

**Interfaces:**
- Consumes: Task 9 至 15 的全部新对象。
- Produces:
  - `memory_wiring(repo_root, env, *, deployment)` 的顺序（90 篇 4c 前一行、第 12 至 23 条）：①读配置；②安装来源：`client_install_commit("starling-client")` 非 editable 且有提交，拉起模式下 `client_install_commit("starling-memory")` 也要且两者相同；③拉起模式下 `check_api_key(env)`；④认领记忆根，`check_memory_layout(layout, repo_root, backend="starling")`（最终规则，Task 17）；⑤拉起（`spawn_service(root, env)`）或连接（`token_file` 读出 token，`url`）；造 `ServiceSession(pinned_commit=②的提交)`，协议、token、提交不一致在这里退出码 2；⑥`settings = rise_settings(config)`，`runtime = service_runtime(session.info, settings, config)`；⑦`ensure_empty_seed()`。**不**开任何部署世界的句柄。没设 `RISE_MEMORY_CONFIG` 时照旧在第一行返回替身，不导入 `starling_client`。
  - `MemoryWiring(backend, deployment, memory_resolver, training_worlds, on_locked, closers, session)`：`on_locked` 是持仓库锁之后才跑的检查（90 篇"世界设置"一段、FC-48、第 23 条）：对 `agent.yaml` 里每个情境，部署世界不在而创建标记在 → `FactoryConfigError("deploy world <名字> was deleted ...")`；在 → 带 rise 设置开一次只读句柄再关（直接 `session.open_port(<前缀>.deploy.<sid>, ..., read_only=True, ...)`；管理面的 `open` 只开可写句柄，`open_replay` 只开重放世界，两者都不用），`world_settings_mismatch` → `FactoryConfigError`，文字写世界名；`ensure_world` 核对不过同样退出码 2。替身与不需要记忆的路径 `on_locked=None`。`closers` 依次是：关会话（`session.close`）、拉起模式下停服务（`SpawnedService.close`）。
  - 收尾顺序（90 篇"停止"一段）：`assemble` 在造运行时之前先登记 `memory.closers`（逆序执行，所以最后跑）：先关运行时与端口，再关客户端，最后停服务、删 `service.pid`、释放归属锁。
  - 致命停机（90 篇"停守护进程分两半"、FC-47）：`_guard(fn, session)` 包住派发器与每个处理器：调用返回或抛出之后查 `session.fatal`，非空就抛 `FactoryConfigError(f"memory service: {session.fatal}; see service.log")`（外部模式不写 `service.log`）。调和循环不接它，命令行译成退出码 2。
  - `deploy_turn_timeout_s` 不变（40 秒按服务端每次至多 4 秒折算，客户端超时 30 秒只在服务卡住时生效，90 篇"客户端超时"表）。

- [ ] **Step 1: 写失败的测试**（`tests/test_factory_memory_wiring.py`；真服务用例用拉起模式，`root` 在 `tmp_path` 下）

```python
def _service_env(tmp_path, **extra):
    return _memory_env(tmp_path, extraction_base_url="http://127.0.0.1:9/v1", **extra)


def test_without_the_variable_no_client_is_imported(store, monkeypatch):
    monkeypatch.delitem(sys.modules, "starling_client", raising=False)
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    asm.close()
    assert "starling_client" not in sys.modules


def test_assembly_opens_no_deploy_world_handle_and_the_check_runs_under_the_lock(store, tmp_path, monkeypatch):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    opened = []
    import factory.starling_session as ss
    real = ss.ServiceSession.open_port
    monkeypatch.setattr(ss.ServiceSession, "open_port",
                        lambda self, world, holder, **kw: opened.append(world) or real(self, world, holder, **kw))
    _agent_yaml(store, scenario_ids=["s1"])
    env = _service_env(tmp_path)
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=env)
    try:
        assert opened == []                                              # 第二个 run 撞锁之前不开句柄
        asm.on_locked()                                                  # 世界还没建:没有句柄可开
        _first_run_creates_the_deploy_world(store, env)
        asm.on_locked()
        assert opened and all(w.endswith(".deploy.s1") for w in opened)
    finally:
        asm.close()


def test_a_deploy_world_with_other_settings_exits_2_only_after_the_lock(store, tmp_path, capsys):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    _agent_yaml(store, scenario_ids=["s1"])
    env = _service_env(tmp_path)
    _first_run_creates_the_deploy_world(store, env)                      # 用 rise 设置建好 rise.deploy.s1
    env2 = _service_env(tmp_path, embedding_model="m-other")              # 改了嵌入配置
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env2) == 2
    err = capsys.readouterr().err
    assert "world_settings_mismatch" in err and "rise.deploy.s1" in err and "127.0.0.1" not in err


def test_a_record_install_source_is_refused(tmp_path, store, monkeypatch, capsys):
    monkeypatch.setattr("factory.memory_config.importlib.metadata.distribution",
                        lambda name: _FakeDist(direct_url=None, record="starling_client/__init__.py,sha256=x,1\n"))
    assert main(["reconcile", str(store.repo_root), "--once", "--interact"], env=_memory_env(tmp_path)) == 2
    assert "git commit" in capsys.readouterr().err


def test_a_fatal_session_stops_the_daemon_with_exit_code_2(store, tmp_path, monkeypatch, capsys):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    import factory.starling_session as ss
    monkeypatch.setattr(ss.ServiceSession, "fatal", property(lambda self: "commit cff319d != 0000000"))
    _agent_yaml(store, scenario_ids=["s1"])
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=_service_env(tmp_path)) == 2
    assert "memory service: commit" in capsys.readouterr().err


def test_closing_stops_the_spawned_service_last_and_releases_the_ownership_lock(store, tmp_path):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=_service_env(tmp_path))
    pid = json.loads((tmp_path / "mem" / "service.pid").read_text())["pid"]
    asm.close()
    assert not (tmp_path / "mem" / "service.pid").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with hold_file_lock(tmp_path / "mem" / "service.owner.lock"):
        pass
```

`_agent_yaml`、`_first_run_creates_the_deploy_world`、`_FakeDist` 写在同一测试文件里。`tests/test_factory_memory_wiring.py:177-232` 关于 `sweep` 的用例改写成 `on_locked`：`run` 把 `a.on_locked` 传给 `run_daemon(on_locked=)`、`assemble` 不调它、`reconcile` 锁内第一个动作之前调它、它抛错时关掉装配且不跑 tick。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_memory_wiring.py tests/test_factory_memory_config.py`
Expected: FAIL

- [ ] **Step 3: 实现**：按 Interfaces。`assemble` 里 `memory_wiring` 抛错时，已拉起的服务要停掉：`memory_wiring` 内部在第 5 步之后的任何失败先调已造好的 `closers` 再上抛。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_memory_wiring.py tests/test_factory_memory_config.py tests/test_factory_handlers.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_factory_cli.py`
Expected: PASS

- [ ] **Step 5: 提交**（分支 `design4-step4`）

```bash
git add src/factory/memory_config.py src/factory/memory_runtime.py src/factory/wire.py tests/test_factory_memory_wiring.py tests/test_factory_memory_config.py tests/test_factory_handlers.py tests/test_factory_interact_wire.py
git commit -m "feat(factory): 记忆装配改连服务:新顺序、持锁后的设置核对、致命状态停守护进程、服务最后收尾"
```

### Task 17: 旧布局切到最终规则，删进程内代码与 `starling` 导入，压成一个提交

**Files:**
- Modify: `src/mindagent/memory_layout.py`（`legacy_entries` 删 `"starling-inprocess"` 分支，只剩 `"stand-in"` 与 `"starling"`；`"starling"` 照 Task 6 表的最后一列，`held/`、`replay/` 下的 `.db` 判旧布局）
- Modify: `src/factory/memory_runtime.py`（删 `StarlingLayout` 余下部分与一切按路径的 Starling 代码；`check_memory_layout` 的 `backend` 只收两个值）
- Modify: `tests/test_factory_cross_plan.py:204-212`（`test_only_simple_actio_memory_starling_imports_starling` 改成"`src/` 下没有文件导入 `starling`"）
- Modify: `tests/test_simple_actio_instance.py:655-674`（`_ALLOWED_MEMORY_STARLING` 删 `starling`、`shutil`、`tempfile`、`functools` 中不再用到的项，加 `starling_client`；以实际导入为准）
- Modify: `tests/test_memory_live.py:1-30`（`live_port` 改成经拉起模式的服务开端口；仍只在 `RISE_MEMORY_LIVE=1` 时跑）
- Test: `tests/test_ma_memory_layout.py`（Task 6 的参数化用例删 `starling-inprocess` 一行）、`tests/test_factory_memory_wiring.py:337-445`（旧布局用例：Starling 下 `deploy/s1.db`、`seeds/`、`runs/` 与 `held/`、`replay/` 下的 `.db` 判旧布局退出码 2；同一个记忆根里替身的 `held/*/*.json`、`replay/*.json` 合法，`deploy/s1.db-wal`、`deploy/s1.json.tmp` 两个后端都不查）

**Interfaces:**
- Consumes: Task 13 至 16。
- Produces: `src/` 下没有 `import starling`；`starling_client` 只在两个文件里；`MemoryLayout.legacy_entries(known, *, backend: Literal["stand-in", "starling"])`。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_factory_cross_plan.py
def test_nothing_under_src_imports_starling():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [f"{p.relative_to(src)}:{n}" for p in sorted(src.rglob("*.py"))
                 for n, top in _top_level_imports(p) if top == "starling"]
    assert offenders == []


# tests/test_factory_memory_wiring.py
@pytest.mark.parametrize("rel", ["deploy/s1.db", "seeds/k/s1.db", "runs/run-1/e.db", "snapshots/r1/deploy-m-1.world",
                                 "held/s1/snap-0.db", "replay/rise.verify.r1.s1.00000000.db"])
def test_a_starling_root_left_from_the_in_process_era_exits_2_listing_it(store, tmp_path, monkeypatch, capsys, rel):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    _agent_yaml(store, scenario_ids=["s1"])
    env = _service_env(tmp_path)
    (tmp_path / "mem" / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / "mem" / rel).write_bytes(b"")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert rel.split("/")[0] in err and str(tmp_path) not in err
    assert not (tmp_path / "mem" / "service.pid").exists()               # 旧布局在拉起服务之前就拒绝


def test_half_written_and_runtime_files_never_block_a_stand_in_start(store):
    root = _stand_in_root(store)
    for rel in ("deploy/s1.json.tmp", "deploy/s1.db-wal", "deploy/s1.db-shm", "held/s1/snap-0.json",
                "replay/rise.verify.r1.s1.00000000.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True); (root / rel).write_text("{}")
    memory_wiring(store.repo_root, {}, deployment=True)                  # 替身在造部署记忆时查,不抛
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_cross_plan.py tests/test_factory_memory_wiring.py tests/test_ma_memory_layout.py`
Expected: FAIL（还有文件导入 `starling`；过渡规则仍在）

- [ ] **Step 3: 实现**：删代码、改白名单、改 `legacy_entries`。`grep -rn "import starling\b\|from starling\b" src` 应为空；`grep -rn "starling_client" src` 只列出两个文件。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -m 'not slow' -p no:cacheprovider`
Expected: 0 failed（第 4 步的最后一个任务，在分支上跑全量；真服务的用例都真跑，不是 skip：`uv run --offline pytest -q -rs -m 'not slow' -p no:cacheprovider tests/test_simple_actio_starling_store.py tests/test_factory_starling_service.py tests/test_factory_training_worlds.py` 的跳过原因里不应有 `starling_client`）

- [ ] **Step 5: 在分支上提交，全量绿就压成 main 上的一个提交，保留分支，继续第 5 步**

```bash
git add src/mindagent/memory_layout.py src/factory/memory_runtime.py tests/test_factory_cross_plan.py tests/test_simple_actio_instance.py tests/test_memory_live.py tests/test_ma_memory_layout.py tests/test_factory_memory_wiring.py
git commit -m "refactor: 旧布局切到最终规则,删进程内 Starling 代码,src/ 不再导入 starling"
```

Step 4 的全量绿了就合并，不停下等用户。压缩合并只在本地 main 上做，分支 `design4-step4` 保留不删，随时能对照或回退；不推送（推送前先问用户）。全量不绿就不合并，按失败修到绿再合。

```bash
git switch main
git merge --squash design4-step4
git commit -m "feat: rise 接 Starling 记忆服务(第 4 步):端口、管理面、训练世界、解析器改走客户端,装配连服务,删进程内代码"
git branch --list design4-step4        # 期望仍列出这个分支:不删
```

合并后在 main 上接着做第 5 步（Task 18）。给控制者的报告写明分支上的五个提交、main 上的压缩提交号与全量结果。

## 第 5 步：建种子与按摘要钉住种子版本

### Task 18: 子命令 `factory seed`

**Files:**
- Create: `src/factory/seed.py`
- Modify: `src/factory/cli.py:1-12`（模块文档加一行）、`:16-17`（加 `import os`）、`:58-88`（`build_parser` 加 `seed` 子命令）、`:116-165`（`main` 加分支，惰性导入，照 `import-sombench` `:125-139` 的写法）
- Modify: `src/factory/memory_runtime.py`（抽出 `open_memory_service(repo_root, env) -> ServiceHandle`：Task 16 装配顺序的第 1 至 7 步，供 `memory_wiring` 与 `factory seed` 共用；`ServiceHandle` 带 `session`、`config`、`settings`、`prefix`、`root_label`、`layout`、`close()`）
- Test: Create `tests/test_factory_seed.py`

**Interfaces:**
- Consumes: Task 9、12、14、16；`MemoryLayout.hold_world`、`uncommitted_snapshots`（Task 6）；`read_agent_settings`（`src/factory/agent.py:75`）。
- Produces（90 篇 `factory seed` 的规则表、FC-44）：

```
factory seed <repo_root> --seed-id <id> --scenario <id> [--scenario ...] [--snapshot <id>] [--new-version]
```

```python
@dataclass(frozen=True)
class SeedResult:
    world: str
    digest: str

def build_seeds(repo_root: Path, env: Mapping[str, str], *, seed_id: str, scenario_ids: Sequence[str],
                snapshot_id: str | None, new_version: bool) -> list[SeedResult]: ...
```

- 规则：`RISE_MEMORY_CONFIG` 没设（替身）→ 退出码 2 "the stand-in memory has no seeds"；`seed_id` 过 `PERSONA_ID_PATTERN`；每个 `--scenario` 在 `agent.yaml` 的 `scenario_ids` 里；`--snapshot` 只能配一个 `--scenario`；按 id 排序逐个处理。每个情境：取世界锁（`FileLockBusyError` → 退出码 2 "a deployment round or gate 2 is using world <id>"）→ 锁内 `uncommitted_snapshots(sid)` 非空 → 退出码 2 → 部署世界不在 → 退出码 2 → 同组种子（`list_worlds(labels={repo, root, kind=seed, seed_id, scenario_id})`，翻完所有页，只取 `seed` 为真）已有且没给 `--new-version` → 退出码 2，打印已有的世界名与版本 → `version` = 同组最大版本加一（首份 1）→ `fork(部署世界名, seed_world(prefix, seed_id, sid, version), snapshot_id=, seed=True, labels=labels(prefix, root, "seed", seed_id=, scenario_id=, version=str(version)), settings=)` → `ServiceTimeout` 时按这个世界名查，查到当作成功，查不到退出码 2 提示重跑。输出每个情境一行 `<世界名> <摘要>`。
- 不取仓库锁、不写控制面的树；不造部署管理面与训练世界；用完 `ServiceHandle.close()`（拉起模式下停服务）。拉起模式下守护进程在跑时归属锁被占，退出码 2（Task 11 的文字）。

- [ ] **Step 1: 写失败的测试**（`tests/test_factory_seed.py`，拉起模式的真服务，`root` 在 `tmp_path` 下）

```python
pytest.importorskip("starling_client"); pytest.importorskip("starling.service")


def test_a_seed_is_forked_from_the_deploy_world_with_rise_labels(seeded_repo, capsys):
    store, env, deploy_digest = seeded_repo(scenarios=["s1"])        # 夹具已往 rise.deploy.s1 写过一条事件
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert line == f"rise.seed.k.s1.v1 {deploy_digest}"


def test_a_second_seed_needs_new_version_and_then_becomes_v2(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"])
    args = ["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"]
    assert main(args, env=env) == 0 and main(args, env=env) == 2
    assert "rise.seed.k.s1.v1" in capsys.readouterr().err
    assert main([*args, "--new-version"], env=env) == 0
    assert capsys.readouterr().out.split()[0] == "rise.seed.k.s1.v2"


def test_a_world_with_an_in_flight_episode_is_refused(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"], in_flight="s1")      # 夹具留一份 deploy-*.rec.json
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 2


def test_a_held_world_lock_is_refused(seeded_repo, tmp_path):
    store, env, _ = seeded_repo(scenarios=["s1"])
    with hold_file_lock(tmp_path / "mem" / "deploy" / "s1.lock"):
        assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 2


@pytest.mark.parametrize("args, why", [
    (["--scenario", "s9"], "agent.yaml"),
    (["--scenario", "s1", "--scenario", "s2", "--snapshot", "snap-x"], "--snapshot"),
    (["--scenario", "s2"], "does not exist"),                            # 部署世界还没建
])
def test_bad_requests_exit_2(seeded_repo, capsys, args, why):
    store, env, _ = seeded_repo(scenarios=["s1", "s2"], built=["s1"])
    assert main(["seed", str(store.repo_root), "--seed-id", "k", *args], env=env) == 2
    assert why in capsys.readouterr().err


def test_the_stand_in_has_no_seeds(store, capsys):
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env={}) == 2
    assert "stand-in" in capsys.readouterr().err
```

`seeded_repo(scenarios, built=None, in_flight=None)` 夹具：建临时仓库与 `agent.yaml`，写拉起模式的记忆配置，经 `open_memory_service` 在 `built`（缺省同 `scenarios`）的部署世界里各写一条事件，返回 `(store, env, 第一个世界的摘要)`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_seed.py`
Expected: FAIL（没有 `seed` 子命令）

- [ ] **Step 3: 实现**：按 Interfaces。`main` 的分支：

```python
if args.command == "seed":
    from factory.seed import build_seeds          # 惰性:拖进 mindagent 与客户端

    try:
        results = build_seeds(Path(args.repo_root), os.environ if env is None else env, seed_id=args.seed_id,
                              scenario_ids=args.scenario, snapshot_id=args.snapshot, new_version=args.new_version)
    except FactoryConfigError as exc:
        print(f"factory: error: {exc}", file=sys.stderr)
        return 2
    for r in results:
        print(f"{r.world} {r.digest}")
    return 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_seed.py tests/test_factory_cli.py tests/test_factory_memory_wiring.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/factory/seed.py src/factory/cli.py src/factory/memory_runtime.py tests/test_factory_seed.py
git commit -m "feat(factory): factory seed 从部署世界分叉种子,世界锁内查在途 episode,--new-version 才建新版本"
```

### Task 19: `bind` 改三参、按起点摘要钉住种子版本；解析器按标签找种子

**Files:**
- Modify: `src/factory/memory_runtime.py`（`TrainingWorlds` 协议与两个实现的 `bind(run_id, seed, digests)`；`StarlingTrainingWorlds._create` 从钉住的种子分叉；`starling_memory_resolver` 按标签找种子）
- Modify: `src/factory/interact_policy.py:393`（`training_worlds.bind(run_id, cfg.memory["seed"], cfg.memory["digests"])`）
- Test: `tests/test_factory_training_worlds.py:36`、`:51`、`:212-240`（`bind` 调用补第三参）、`tests/test_factory_memory_runtime.py:161`、`tests/test_factory_interact_wire.py`（`bind` 收到解出的摘要）、Create `tests/test_factory_seed_pinning.py`

**Interfaces:**
- Consumes: Task 15 的分叉逻辑；Task 18 建出的种子世界与标签。
- Produces（90 篇 `StarlingTrainingWorlds` 变化表的 `bind` 行、00 篇"按标签找种子"、30 篇失败表两行）：
  - `TrainingWorlds.bind(self, run_id: str, seed: str | None, digests: Mapping[str, str]) -> None`。替身：`seed` 非空照旧拒绝，`digests` 只存不用。
  - `find_seeds(session, *, prefix, root_label, seed_id, scenario_id) -> list[dict]`：`list_worlds(labels={"repo", "root", "kind": "seed", "seed_id", "scenario_id"}, with_digest=True)` 翻完所有页（每页 500），只留 `seed` 为真的项，按 `(int(labels["version"]), created_at, name)` 降序。
  - 解析器：`seed` 为 None → 每个情境空种子摘要；否则每个情境取 `find_seeds(...)[0]["digest"]`，该情境没有种子 → 空种子摘要；**这个 `seed_id` 一个情境都没有** → `DirectiveFailedError(f"memory.seed {seed!r}: no seed world under this id")`（30 篇失败表）。
  - `StarlingTrainingWorlds.bind`：`seed` 为 None 时每个情境钉空种子；否则每个情境在 `find_seeds` 里取**摘要等于 `digests[sid]` 的版本最大的那份**，钉住它的 `(世界名, 摘要)`；该情境没有种子而 `digests[sid]` 等于空种子摘要时钉空种子；一份都对不上 → 永久 `MemoryFailureError(f"no seed world of {seed!r} for scenario {sid} has the resolved start digest")`（policy 工厂把它译成 T2）。`_create` 从钉住的那份分叉，返回摘要不等于钉住的摘要判永久（第二道）。`fork` 回 `world_schema_behind` → 永久，原因写"需要在看板里升级种子"。

- [ ] **Step 1: 写失败的测试**（`tests/test_factory_seed_pinning.py`，拉起模式的真服务）

```python
pytest.importorskip("starling_client"); pytest.importorskip("starling.service")


def test_bind_pins_the_version_whose_digest_was_resolved_even_after_a_newer_one(seed_env):
    worlds, resolver, make_seed = seed_env()
    v1 = make_seed("k", "s1", text="pb：第一版")
    digests, _ = resolver("k", ("s1",))
    assert digests == {"s1": v1.digest}
    make_seed("k", "s1", text="pb：第二版", new_version=True)           # 解析之后有人建了 v2
    worlds.bind("run-1", "k", digests)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert port.digest() == v1.digest                                    # 在跑的 run 不换起点
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_no_seed_with_the_resolved_digest_is_a_permanent_failure(seed_env):
    worlds, resolver, make_seed = seed_env()
    make_seed("k", "s1", text="pb：一")
    with pytest.raises(MemoryFailureError) as info:
        worlds.bind("run-1", "k", {"s1": "0" * 64})
    assert info.value.transient is False and "s1" in str(info.value)


def test_a_scenario_without_a_seed_starts_from_the_empty_seed(seed_env):
    worlds, resolver, make_seed = seed_env(scenarios=("s1", "s2"))
    make_seed("k", "s1", text="pb：一")
    digests, _ = resolver("k", ("s1", "s2"))
    assert digests["s2"] == worlds.empty_seed[1]
    worlds.bind("run-1", "k", digests)


def test_a_seed_id_with_no_world_at_all_fails_the_directive(seed_env):
    _, resolver, _ = seed_env()
    with pytest.raises(DirectiveFailedError, match="no seed world under this id"):
        resolver("nope", ("s1",))
```

`seed_env(scenarios=("s1",))` 夹具：拉起服务，造会话、空种子、`StarlingTrainingWorlds` 与解析器；`make_seed(seed_id, sid, text, new_version=False)` 往部署世界写一条事件后调 `build_seeds`，返回 `SeedResult`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_seed_pinning.py tests/test_factory_training_worlds.py`
Expected: FAIL（`bind` 只收两参；种子还被拒绝）

- [ ] **Step 3: 实现**：按 Interfaces。删 Task 15 的"seeds arrive in step 5"两处。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_seed_pinning.py tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_mt_dispatch.py tests/test_factory_seed.py`
Expected: PASS。这是第 5 步的最后一个功能任务：控制者在后台跑全量，必须绿。

- [ ] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/factory/interact_policy.py tests/test_factory_seed_pinning.py tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py
git commit -m "feat(factory): bind 收解出的起点摘要并按它钉住种子版本;解析器按标签找种子"
```

### Task 20: 文档状态与全量验证

**Files:**
- Modify: `docs/design/00-overview.md`、`20-socialsimulator.md`、`30-mindtrainer.md`、`40-simple-actio.md`、`50-mindagent.md`、`90-factory-and-cli.md`、`docs/design/_shared/context-block.md`（若有接服务的内容）
- Modify: 本计划（勾选；「已定」一节里的哪条在施工中有偏差就照实改写）

- [ ] **Step 1: 跑全量**

Run: `uv run --offline pytest -q -m 'not slow' -p no:cacheprovider`
Expected: 0 failed；另跑 `uv run --offline pytest -q -rs -m 'not slow' -p no:cacheprovider 2>&1 | grep -i "starling_client\|starling.service"`，期望空输出（没有因为没装客户端或服务端而跳过的用例）

- [ ] **Step 2: 改文档状态**：Task 1 至 19 落地的每一行，[计划中] 改 [已落地]；"今天……[已落地]；接服务后……[计划中]"的成对写法改成只写现在的行为（`STYLE.md`：现在时、不写评审过程）。被取代的旧决策（SYS-27、FC-27、FC-35、MA-16 等里"按路径打开库""启动清扫 `runs/`""`record:` 安装来源"）改成"已由 X 取代"。00 篇施工表六行标 [已落地]；风险 32、37 已在 Task 1 改过，其余风险（28 至 31、33 至 36）仍 [计划中]，不动。90 篇第 9 章测试表"记忆服务（接服务后）"一行填实测的文件与用例数（`uv run --offline pytest --collect-only -q <文件> | tail -1`），"新增测试文件，施工计划定"改成实际文件名：`tests/test_factory_starling_names.py`、`tests/test_factory_starling_service.py`、`tests/test_factory_starling_session.py`、`tests/test_simple_actio_client_errors.py`、`tests/test_factory_seed.py`、`tests/test_factory_seed_pinning.py`、`tests/test_starling_service_fixture.py`。90 篇模块表"记忆服务的新模块（接服务后）"一行换成三行：`factory.starling_names`、`factory.starling_service`、`factory.starling_session`，各写导出与依赖（照本计划 Task 9、11、12 的 Interfaces）。

- [ ] **Step 3: 跑文档检查**

Run: `python3 docs/design/tools/sync_docs.py && python3 docs/design/tools/check_docs.py && python3 docs/design/tools/check_consistency.py`
Expected: 三者都是 0 个错误；`ServiceSession`、`fork_replay`、`open_replay`、`PendingKey`、`SnapshotRef` 的符号告警消失

- [ ] **Step 4: 提交**

```bash
git add docs/design docs/superpowers/plans/2026-11-10-starling-service-integration.md
git commit -m "docs(design): rise 接 Starling 记忆服务标为已落地"
```

---

## 已定

开工前的七条待定项已于 2026-11-10 裁定，先并入 `docs/design/`，再改了上面的任务。下表只指出每条现在写在哪里，内容以 docs/design 为准。

| 项 | 裁定 | docs/design 里的位置 | 改到的任务 |
|---|---|---|---|
| 1 `deploy/` 下"只许"的范围 | 只查 `.json`、`.db`、`.created`、`.rec.json` 四种数据后缀，不在允许名单里的拒绝；`.tmp`、`-wal`、`-shm`、`.lock` 等不查 | 90 篇第 6.1 节拒绝项第 18 条 | Task 6、17 |
| 2 重放世界与部署世界的名字空间 | 不按名字前缀推断；重放世界只经 `open_replay(replay_world, holder)`，永远只读，不走 `ensure_world`；`open` 只收部署世界 id，去掉 `read_only`；`wait_closed` 按完整世界名计，两种端口共用一张计数表 | 40 篇第 5.1 节"接服务后管理面的签名"与方法表、第 5.4 节调用表、AC-34；50 篇第 5 章管理面规则表"重放世界"一行与第 6 章重放世界一段；00 篇第 3.2 节管理面签名一段 | Task 4、5、7、9、13、14 |
| 3 第 2、3 步留下的过渡文件 | 第 4 步起 Starling 后端下 `held/`、`replay/` 里的 `.db` 判旧布局，与其他旧布局同一口径列出至多 5 条；替身在这两处的 `.json` 合法 | 90 篇第 18 条与第 4 章记忆根布局表；00 篇施工表第 4 行 | Task 6、17 |
| 4 种子标签的 `root` | 照文档按 `root` 过滤，跨记忆根不共用种子 | 00 篇第 2.2 节非目标表"跨记忆根共用种子"一行、第 3.2 节"按标签找种子"一段 | Task 19 不变 |
| 5 `delete_snapshot` 删已删的快照 | Starling 回 `snapshot_not_found`，rise 当作成功，与替身的幂等一致 | 50 篇第 6 章"删一个已删的快照"一段与第 8 章失败表；40 篇第 5.1 节方法表 `delete_snapshot` 一行 | Task 4、7、14 |
| 6 排空窗口的比较键 | 按完整世界名；rollout 世界每次重试换名字，窗口只对部署世界起作用 | 90 篇第 5 章"排空窗口的比较键"一段与服务会话情形表；00 篇、90 篇术语表"排空窗口"一行 | Task 12 |
| 7 句柄与管理调用的超时 | 照 90 篇 FC-46：句柄调用 30 秒、管理调用 300 秒 | 90 篇 FC-46 与"客户端超时"表 | 不改 |

另一处约定改了：第 4 步压成一个提交不再停下等用户（Global Constraints 与 Task 17 Step 5）。
