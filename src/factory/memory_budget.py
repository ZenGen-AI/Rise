"""记忆调用的时间预算(设计四补丁;90 篇"客户端超时"表与"时间预算"两行、FC-50、FC-51)。纯函数,不导入 `starling`。

外部模式下抽取与嵌入的超时、重试由 Starling 默认世界的端点决定(看板里配)。rise 不给它封顶,而是按这些端点推出
Starling 一侧的最坏耗时(`memory_worst_case`),再由此推出 rise 自己的各层时限(`memory_time_budget`),每一层都
严格大于它包住的那一层:内层先超时,外层才看得到分类过的失败。

拉起模式与替身沿用固定的各层(`SPAWN_LAYERS`,与接服务之前的常量相同);`spawn_time_budget` 只把按拉起配置算出的
最坏耗时填进去供日志看,不改各层。

`mindagent`、`mindtrainer`、`simulator` 不导入 `factory`:这里算出的值由组合根以参数传给它们。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

# ---- Starling 一侧的事实(提交 cff319d,引用写 S:路径:行) ----

STARLING_DEFAULT_TIMEOUT_MS = 60_000      # 适配器缺省;端点 timeout_ms 为 0 或缺时用它
STARLING_DEFAULT_MAX_RETRIES = 3          # 适配器缺省;端点没写 max_retries 时用它
BACKOFF_FIRST_MS = 1_000                  # 第一次重试之前的退避
BACKOFF_MAX_MS = 30_000                   # 退避封顶
EXTRACTION_CALLS_PER_REMEMBER = 7         # 一次 remember_event 的抽取 LLM 调用数上界:信念 3 + 一般事实 3 + 情节 1
EMBED_BATCH_ROWS = 32                     # 嵌入工作线程一批的行数上限
EMBED_MAX_BATCH_INPUTS = 10               # 一次批量嵌入 HTTP 的输入条数上限
# 一轮里 remember_event 的次数:对方的一句新发言,加上一轮抽取失败留下的那一句重试(两方对话;多一个对方多两次)
REMEMBER_EVENTS_PER_TURN = 2


def endpoint_call_ms(endpoint: Mapping) -> int:
    """Starling 对一个端点发一次 HTTP 调用(含重试与退避)的最坏耗时,毫秒。

    - 单次尝试的上限 `t`:端点 `timeout_ms` 给了正数才设,0 或缺取适配器缺省 60000
      (S:python/starling/dashboard/engine.py:218-220、:238-239;S:include/starling/extractor/openai_adapter.hpp:27、
      S:include/starling/embedding/openai_embedding_adapter.hpp:20)。
    - 重试次数 `r`:端点 `max_retries` 按 is None 判,缺取缺省 3(engine.py:221-222、:240-241;openai_adapter.hpp:28、
      openai_embedding_adapter.hpp:21)。`provider` 为 `anthropic` 时 Python 侧不设这两项,一律取缺省
      (engine.py:202-209;S:include/starling/extractor/anthropic_adapter.hpp:21-22)。
    - 尝试数 `r + 1`(S:src/net/http_post_json.cpp:182-183);第 i 次重试之前退避 `min(1000 × 2^i, 30000)` 毫秒,
      首次 1 秒、翻倍、封顶 30 秒(http_post_json.cpp:148-151、:180)。
    最坏 = `(r + 1) × t + Σ_{i<r} min(1000 × 2^i, 30000)`。"""
    anthropic = str(endpoint.get("provider") or "").lower() == "anthropic"
    timeout_ms = None if anthropic else endpoint.get("timeout_ms")
    max_retries = None if anthropic else endpoint.get("max_retries")
    t = timeout_ms if timeout_ms else STARLING_DEFAULT_TIMEOUT_MS
    r = STARLING_DEFAULT_MAX_RETRIES if max_retries is None else max_retries
    if t < 0 or r < 0:
        raise ValueError("timeout_ms and max_retries must be non-negative")
    backoff = sum(min(BACKOFF_FIRST_MS * 2 ** i, BACKOFF_MAX_MS) for i in range(r))
    return (r + 1) * t + backoff


def embed_pending_calls() -> int:
    """一次 embed_pending 的嵌入 HTTP 次数,按一轮至多一批(32 行)走批量路径:ceil(32 / 10) = 4。

    - 一批至多 32 行(S:include/starling/embedding/embedding_worker.hpp:38),批量调用按每次至多 10 条切成几次 HTTP
      (S:include/starling/embedding/openai_embedding_adapter.hpp:26、S:src/embedding/openai_embedding_adapter.cpp:148)。
    - 不计的两种情形,Starling 没有上界:积压超过一批时循环到某一批一行都没嵌上为止(S:src/memory/memory_ops.cpp:808-814);
      整批以永久错误失败时逐行回退(S:src/embedding/embedding_worker.cpp:294-306)。逐行回退跟在 4xx 或结构错误之后,
      同一个端点对单行通常同样立即回 4xx,不重试。瞬态的整批失败不逐行回退(embedding_worker.cpp:287-293)。"""
    return -(-EMBED_BATCH_ROWS // EMBED_MAX_BATCH_INPUTS)


@dataclass(frozen=True)
class MemoryWorstCase:
    """Starling 一侧的最坏耗时,秒。"""

    extraction_call_s: float      # 抽取端点一次 HTTP(含重试与退避)
    embedding_call_s: float       # 嵌入端点一次 HTTP
    recall_s: float               # 一次 recall:嵌入查询词一次
    remember_s: float             # 一次 remember_event:抽取至多 7 次
    embed_pending_s: float        # 一次 embed_pending:批量嵌入 4 次
    handle_call_s: float          # 一次句柄调用:上面三者的最大
    memory_turn_s: float          # 一轮记忆:recall 1 次、remember_event 2 次、embed_pending 1 次


def memory_worst_case(extraction: Mapping, embedding: Mapping, *,
                      remember_events: int = REMEMBER_EVENTS_PER_TURN) -> MemoryWorstCase:
    """按 rise 设置里的两个端点算 Starling 一侧的最坏耗时。设 `E = endpoint_call_ms(抽取)`、`M = endpoint_call_ms(嵌入)`:

    - recall = `M`:语义召回嵌入查询词一次(S:python/starling/dashboard/engine.py:1134-1142,
      单条嵌入 S:src/embedding/openai_embedding_adapter.cpp:57-66)。
    - remember_event = `7 × E`:抽取段调 `remember_extract_all`(S:src/memory/memory_events.cpp:395-404),信念与一般事实
      各一遍(S:src/memory/memory_ops.cpp:192、:206),每遍在 Extractor 的循环里至多调 3 次 LLM
      (S:src/extractor/extractor.cpp:666,kMaxRetries 在 S:include/starling/extractor/extractor.hpp:145;非截断的失败
      都重试,extractor.cpp:67-72);情节一遍只调一次(memory_ops.cpp:211、S:src/extractor/episodic_extractor.cpp:85)。
      rise 走旧抽取路径:引擎用缺省的 ExtractionConfig(semantic_claim_contract 为假,
      S:python/starling/extractor/config.py:42),一般事实那遍强制为假(memory_ops.cpp:202)。
    - embed_pending = `4 × M`(见 `embed_pending_calls`)。
    - 一次句柄调用 = 三者最大;一轮记忆 = `M + remember_events × 7E + 4M`。一轮的调用次数照
      `simple_actio.instance` 的 `_remember_new`:recall 一次,对方新发言与上一轮失败的重试各一次 remember,embed_pending 一次。"""
    e_ms = endpoint_call_ms(extraction)
    m_ms = endpoint_call_ms(embedding)
    remember_ms = EXTRACTION_CALLS_PER_REMEMBER * e_ms
    embed_ms = embed_pending_calls() * m_ms
    return MemoryWorstCase(
        extraction_call_s=e_ms / 1000, embedding_call_s=m_ms / 1000, recall_s=m_ms / 1000,
        remember_s=remember_ms / 1000, embed_pending_s=embed_ms / 1000,
        handle_call_s=max(m_ms, remember_ms, embed_ms) / 1000,
        memory_turn_s=(m_ms + remember_events * remember_ms + embed_ms) / 1000,
    )


@dataclass(frozen=True)
class MemoryTimeBudget:
    """rise 各层时限,秒(90 篇时间预算表)。`derived` 为真:由 Starling 的端点推出(外部模式);为假:拉起模式与替身的
    固定值。`worst` 是算出的 Starling 一侧最坏耗时,替身没有(None)。

    | 层 | 包住的是 |
    |---|---|
    | `handle_s` 客户端句柄调用超时 | 一次句柄调用 |
    | `drain_window_s` 排空窗口、`port_close_s` 关端口等在途调用 | 一次句柄调用的客户端超时 |
    | `deploy_turn_s` 部署时服务端等一次提交 | 一轮记忆(部署的智能体位后端是罐头) |
    | `backend_s` 服务端单次后端调用 | 对手 HTTP `opponent_s` 加一轮记忆 |
    | `interact_turn_s` 训练时服务端等一次提交 | 训练位策略 HTTP `policy_s` 加一轮记忆 |
    | `message_s` 客户端等相邻两条服务端消息 | 一次后端调用 |
    | `per_turn_deadline_s`、`deploy_per_turn_s` 流 deadline 的每轮 | 一轮里较长的那一层 |
    | `join_s` 服务端收尾等在途后端调用 | 一次后端调用与一次句柄调用 |
    | `close_wait_s` 等世界的端口全关、`admission_wait_s` 准入退避 | 服务端收尾 |"""

    derived: bool
    worst: MemoryWorstCase | None
    handle_s: float
    drain_window_s: float
    port_close_s: float
    deploy_turn_s: float
    deploy_per_turn_s: float
    opponent_s: float
    policy_s: float
    backend_s: float
    interact_turn_s: float
    message_s: float
    per_turn_deadline_s: float
    join_s: float
    close_wait_s: float
    admission_wait_s: float

    def violations(self) -> list[str]:
        """不成立的包含关系,每条写出两层与数值;推出的预算应为空表。"""
        w = self.worst
        pairs: list[tuple[str, float, str, float]] = []
        if w is not None:
            pairs += [
                ("handle_s", self.handle_s, "handle call", w.handle_call_s),
                ("drain_window_s", self.drain_window_s, "handle_s", self.handle_s),
                ("port_close_s", self.port_close_s, "handle_s", self.handle_s),
                ("deploy_turn_s", self.deploy_turn_s, "memory turn", w.memory_turn_s),
                ("backend_s", self.backend_s, "opponent_s + memory turn", self.opponent_s + w.memory_turn_s),
                ("interact_turn_s", self.interact_turn_s, "policy_s + memory turn", self.policy_s + w.memory_turn_s),
            ]
        pairs += [
            ("message_s", self.message_s, "backend_s", self.backend_s),
            ("join_s", self.join_s, "backend_s", self.backend_s),
            ("join_s", self.join_s, "handle_s", self.handle_s),
            ("close_wait_s", self.close_wait_s, "join_s", self.join_s),
            ("admission_wait_s", self.admission_wait_s, "join_s", self.join_s),
        ]
        out = [f"{outer} ({o:g}s) must exceed {inner} ({i:g}s)" for outer, o, inner, i in pairs if not o > i]
        for name, value, floor in (("per_turn_deadline_s", self.per_turn_deadline_s,
                                    max(self.interact_turn_s, self.backend_s)),
                                   ("deploy_per_turn_s", self.deploy_per_turn_s,
                                    max(self.deploy_turn_s, self.backend_s) if self.derived else self.deploy_turn_s)):
            if value < floor:
                out.append(f"{name} ({value:g}s) must be at least {floor:g}s")
        return out

    def describe(self) -> str:
        """装配期日志的一行。"""
        head = "derived from the memory service's endpoints" if self.derived else "fixed spawn-mode layers"
        worst = "" if self.worst is None else (
            f"memory turn worst case {self.worst.memory_turn_s:g}s, handle call {self.worst.handle_call_s:g}s; ")
        return (f"memory time budget ({head}): {worst}handle timeout {self.handle_s:g}s, "
                f"deploy turn {self.deploy_turn_s:g}s, backend {self.backend_s:g}s, interact turn {self.interact_turn_s:g}s, "
                f"message {self.message_s:g}s, per-turn deadline {self.per_turn_deadline_s:g}s, join {self.join_s:g}s, "
                f"close wait {self.close_wait_s:g}s, admission wait {self.admission_wait_s:g}s")


# 拉起模式与替身:接服务之前的常量,不随端点变(90 篇时间预算表的"拉起"一列)
SPAWN_LAYERS = MemoryTimeBudget(
    derived=False, worst=None, handle_s=30.0, drain_window_s=60.0, port_close_s=60.0,
    deploy_turn_s=40.0, deploy_per_turn_s=40.0, opponent_s=60.0, policy_s=120.0, backend_s=90.0,
    interact_turn_s=150.0, message_s=120.0, per_turn_deadline_s=150.0, join_s=60.0, close_wait_s=70.0,
    admission_wait_s=65.0,
)

# 推出各层时在内层之上加的余量,秒
HANDLE_MARGIN_S = 30.0
DRAIN_MARGIN_S = 10.0
DEPLOY_TURN_MARGIN_S = 20.0
TURN_MARGIN_S = 10.0
MESSAGE_MARGIN_S = 30.0
JOIN_MARGIN_S = 10.0
CLOSE_WAIT_MARGIN_S = 10.0
ADMISSION_MARGIN_S = 5.0


def spawn_time_budget(extraction: Mapping, embedding: Mapping) -> MemoryTimeBudget:
    """拉起模式:各层是 `SPAWN_LAYERS`,只把按 rise 配置的端点算出的最坏耗时填进 `worst` 供日志看。"""
    return dataclasses.replace(SPAWN_LAYERS, worst=memory_worst_case(extraction, embedding))


def memory_time_budget(extraction: Mapping, embedding: Mapping, *, policy_s: float = SPAWN_LAYERS.policy_s,
                       opponent_s: float = SPAWN_LAYERS.opponent_s) -> MemoryTimeBudget:
    """外部模式:由 rise 设置里的两个端点推出各层。每层取"内层加余量"与拉起模式常量的较大者,所以只会放宽:

    | 层 | 取值 |
    |---|---|
    | 句柄 | 一次句柄调用 + 30 |
    | 排空窗口、关端口 | 句柄 + 10 |
    | 部署等提交 | 一轮记忆 + 20 |
    | 后端 | 对手 HTTP + 一轮记忆 + 10 |
    | 训练位等提交 | 策略 HTTP + 一轮记忆 + 10 |
    | 相邻消息 | 后端 + 30 |
    | 训练流每轮 | 训练位等提交与后端的较大者 |
    | 部署流每轮 | 部署等提交与后端的较大者(外部模式下部署路的后端调用也取 `backend_s`) |
    | 收尾 join | 后端与句柄的较大者 + 10 |
    | 等端口全关 | join + 10 |
    | 准入退避 | join + 5 |

    返回之前核对包含关系,不成立抛 ValueError(余量都是正数,走不到)。"""
    w = memory_worst_case(extraction, embedding)
    base = SPAWN_LAYERS
    handle_s = max(base.handle_s, w.handle_call_s + HANDLE_MARGIN_S)
    deploy_turn_s = max(base.deploy_turn_s, w.memory_turn_s + DEPLOY_TURN_MARGIN_S)
    backend_s = max(base.backend_s, opponent_s + w.memory_turn_s + TURN_MARGIN_S)
    interact_turn_s = max(base.interact_turn_s, policy_s + w.memory_turn_s + TURN_MARGIN_S)
    join_s = max(base.join_s, max(backend_s, handle_s) + JOIN_MARGIN_S)
    budget = MemoryTimeBudget(
        derived=True, worst=w, handle_s=handle_s,
        drain_window_s=max(base.drain_window_s, handle_s + DRAIN_MARGIN_S),
        port_close_s=max(base.port_close_s, handle_s + DRAIN_MARGIN_S),
        deploy_turn_s=deploy_turn_s, deploy_per_turn_s=max(deploy_turn_s, backend_s),
        opponent_s=opponent_s, policy_s=policy_s, backend_s=backend_s, interact_turn_s=interact_turn_s,
        message_s=max(base.message_s, backend_s + MESSAGE_MARGIN_S),
        per_turn_deadline_s=max(base.per_turn_deadline_s, interact_turn_s, backend_s),
        join_s=join_s, close_wait_s=max(base.close_wait_s, join_s + CLOSE_WAIT_MARGIN_S),
        admission_wait_s=max(base.admission_wait_s, join_s + ADMISSION_MARGIN_S),
    )
    problems = budget.violations()
    if problems:
        raise ValueError("time budget: " + "; ".join(problems))
    return budget
