"""对手位的驱动:选哪种驱动、把 model_ref 解析成适配器导出、外部 API 客户端。

驱动三选一(RoleBinding.driver):
- scripted:按剧本说话,不依赖任何模型(AgentRuntime._backends 里直接用 ScriptedBackend);
- vllm:加载 model_ref 指向的适配器导出,经 vLLM 服务说话(后端由 AgentRuntime 的工厂给出);
- external_api:POST 到 OpenAI 兼容的 /v1/chat/completions(本文件的 ExternalApiBackend)。

本模块不碰凭证的来源:密钥由调用方传入,为空时在构造期抛 OpponentCredentialError,信息里只有环境变量名。
任何异常信息、回溯都不含密钥值(见 ExternalApiBackend)。
"""
from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence

from controlplane.envelope import BigRef, InvalidBigRefError
from controlplane.gitstore import UNREADABLE_PRODUCT_ERRORS, GitStore
from controlplane.models import version_of_id
from controlplane.paths import model_file
from factory.sombench_runner import EXPORT_FORMAT
from simulator.datamodel import OPPONENT_DRIVERS, Persona, RoleBinding, Scenario, TurnRecord
from simulator.realtime.backend_timeout import (
    RoleBackendRejectedError, RoleBackendTimeoutError, RoleBackendUnavailableError,
)

# 回复体上限:一句对手发言用不了这么多,挡住对端发来没完没了的响应
MAX_REPLY_BYTES = 1 << 20


class ModelRefError(Exception):
    """model_ref 解析不到可加载的适配器导出:id 形状不对、模型产物不在或读不出、checkpoint_ref 缺失或不合规、
    或它不是适配器导出。都是确定性的,重试也不会变好。信息点名 model_ref。"""


class OpponentCredentialError(Exception):
    """外部对手 API 的密钥缺失或不可用。信息里只有环境变量名,绝不含密钥值。"""


class OpponentApiError(Exception):
    """外部对手 API 这一次没给出可用的发言:连不上、超时、非 200、回复不是约定的形状。信息里绝不含密钥值。"""


class OpponentTimeoutError(OpponentApiError, RoleBackendTimeoutError):
    """对手 API(外部 API 或站内 vLLM,共用 post_chat)的这一次调用超时。

    同时是两样东西,各有用处:是 RoleBackendTimeoutError,所以服务端驱动把对手位的超时归 role_backend_timeout
    (T1,轨迹源按 max_retries_per_episode 整条重跑),而不是被通用异常分支吞成 role_backend_failure;
    仍是 OpponentApiError,所以训练位策略超时时,策略上下文照旧把它译成延后。
    非 200 与形状不对的回复不是它:那些仍是普通的 OpponentApiError。信息里绝不含密钥值。"""


class OpponentRejectedError(OpponentApiError, RoleBackendRejectedError):
    """对手 API(外部 API 或站内 vLLM,共用 post_chat)明确拒绝了这次请求:4xx,429 除外(400、401、403、404 等)。
    同样的请求再发还是同样的回答,重试无益。

    同时是两样东西,各有用处:是 RoleBackendRejectedError,所以服务端驱动把对手位的拒绝归 role_backend_rejected
    (T2,轨迹源不重跑、编排器把训练记录写 failed),而不是被通用异常分支吞成 role_backend_failure;
    仍是 OpponentApiError,所以训练位策略撞上时,策略上下文照旧把它译成延后(理由见 interact_policy)。
    429、5xx、回复形状不对不是它。信息里绝不含密钥值。"""


class OpponentUnavailableError(OpponentApiError, RoleBackendUnavailableError):
    """对手 API(外部 API 或站内 vLLM,共用 post_chat)暂时不可用:429、5xx、连不上(非超时)、连接中途被断开、
    状态行不合规。过一会儿可能就好,是 T1。

    同时是两样东西,各有用处:是 RoleBackendUnavailableError,所以服务端驱动把对手位的这类失败归
    role_backend_unavailable(轨迹源按 max_retries_per_episode 整条重跑),而不是被通用异常分支吞成
    role_backend_failure(不重跑);仍是 OpponentApiError,所以训练位策略撞上时,策略上下文照旧把它译成延后。
    超时、明确拒绝、回复形状不对不是它。信息里绝不含密钥值。"""


# ---------------- 驱动选择 ----------------

def driver_for(binding: RoleBinding) -> str:
    """对手位用哪种驱动。训练位(self_play)永远不配对手驱动,传进来是调用方的错,抛 ValueError。
    显式的 binding.driver 优先;没写就按 model_backend 取默认:frozen_reference 走 vllm,external_api 走外部 API。"""
    if binding.model_backend == "self_play":
        raise ValueError(
            f"slot {binding.slot_id!r} is self_play (the trained slot), which never gets an opponent driver"
        )
    if binding.driver is not None:
        if binding.driver not in OPPONENT_DRIVERS:
            raise ValueError(
                f"slot {binding.slot_id!r} driver must be one of {list(OPPONENT_DRIVERS)}, got {binding.driver!r}"
            )
        return binding.driver
    if binding.model_backend == "frozen_reference":
        return "vllm"
    if binding.model_backend == "external_api":
        return "external_api"
    raise ValueError(
        f"slot {binding.slot_id!r} has model_backend {binding.model_backend!r} and no driver; "
        f"cannot pick one of {list(OPPONENT_DRIVERS)}"
    )


# ---------------- model_ref 解析 ----------------

def resolve_model_ref(store: GitStore, model_ref: str) -> BigRef:
    """`zengen-vN` -> 该模型产物的 checkpoint_ref,且必须是适配器导出(判法与 SomevalRunner 同口径:
    fetch_hint 的 type 是 roll_ssh、format 是 EXPORT_FORMAT)。其余一律抛 ModelRefError。
    OSError(磁盘、权限)是瞬态,原样上抛。"""
    version = version_of_id(model_ref) if isinstance(model_ref, str) else None
    # 要求规范写法:zengen-v01 会被正则放行成版本 1,却不是那个模型的 id
    if version is None or model_ref != f"zengen-v{version}":
        raise ModelRefError(f"model_ref {model_ref!r} is not a model id of the form zengen-vN")
    rel = model_file(version)
    if not store.exists(rel):
        raise ModelRefError(f"model_ref {model_ref!r} has no model product in the tree ({rel})")
    try:
        envelope = store.read_product(rel)
    except UNREADABLE_PRODUCT_ERRORS as exc:
        raise ModelRefError(
            f"model_ref {model_ref!r}: the model product is unreadable ({type(exc).__name__}: {exc})"
        ) from exc
    raw = envelope.payload.get("checkpoint_ref")
    if not isinstance(raw, dict):
        raise ModelRefError(f"model_ref {model_ref!r}: the model product has no usable checkpoint_ref")
    try:
        ref = BigRef.from_dict(raw)
    except (InvalidBigRefError, KeyError) as exc:
        # 手改进树的坏引用(I8)
        raise ModelRefError(
            f"model_ref {model_ref!r}: its checkpoint_ref is malformed ({type(exc).__name__}: {exc})"
        ) from exc
    hint = ref.fetch_hint or {}
    if hint.get("type") != "roll_ssh" or hint.get("format") != EXPORT_FORMAT:
        raise ModelRefError(
            f"model_ref {model_ref!r}: its checkpoint is not an adapter export "
            f"(needs fetch_hint type 'roll_ssh' and format {EXPORT_FORMAT!r})"
        )
    return ref


# ---------------- 对话消息 ----------------

def chat_messages(
    persona: Persona,
    scenario: Scenario,
    history: list[TurnRecord],
    speaker_slot: str,
    guidance: Sequence[str] = (),
) -> list[dict]:
    """给聊天补全接口的消息:一条 system(人格与情境),历史里每个回合一条:说话者自己的发言是 assistant
    (vLLM 的聊天模板把模型自己说过的话放在 assistant 轮,不带前缀),别人的发言是 user(前缀说话的角色位)。
    只放话语,不放内心独白与激活轨迹(那是评估材料,不是对手该看见的)。
    历史为空、或第一条就是自己的发言时,先补一条开场的 user:有的聊天模板要求 user 轮打头。

    guidance 非空时(Actio 给的技能、经验与记忆要点,每条一行),并入同一条 system 文本的末尾,
    前面加「当前要点：」(全角冒号);仍只有一条 system。空时不碰 system 文本,请求体与不带指导语时逐字节相同。"""
    role = next((r.description for r in scenario.role_slots if r.slot_id == speaker_slot), "")
    lines = [f"你在一段社会情境对话里扮演角色位「{speaker_slot}」" + (f"({role})。" if role else "。")]
    if scenario.background:
        lines.append(f"情境背景:{scenario.background}")
    if scenario.conflict:
        lines.append(f"核心冲突:{scenario.conflict}")
    if scenario.norms:
        lines.append("需要遵守的规范:" + ";".join(scenario.norms))
    traits = [
        ("特质", "、".join(persona.traits)), ("动机", persona.motivation),
        ("说话风格", persona.speech_style), ("价值观", "、".join(persona.values)),
    ]
    described = [f"{label}:{value}" for label, value in traits if value]
    if described:
        lines.append("你的人格——" + ";".join(described))
    lines.append("只说出你这一轮要说的话,不要加角色名前缀,不要解释。")
    system_text = "\n".join(lines)
    if guidance:
        system_text += "\n当前要点：\n" + "\n".join(guidance)
    messages = [{"role": "system", "content": system_text}]
    if not history or history[0].speaker_slot == speaker_slot:
        messages.append({"role": "user", "content": "对话刚开始,请你先开口。"})
    for turn in history:
        if turn.speaker_slot == speaker_slot:
            messages.append({"role": "assistant", "content": turn.utterance})
        else:
            messages.append({"role": "user", "content": f"{turn.speaker_slot}:{turn.utterance}"})
    return messages


# ---------------- 外部 API 客户端 ----------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向:urllib 会把 Authorization 头带到重定向的目的地,密钥会跑到另一台主机上。
    返回 None 后 3xx 作为 HTTPError 抛出,由 post_chat 报成非 200。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def require_http_url(base_url: object) -> urllib.parse.SplitResult:
    """base_url 必须是带主机的 http(s) 地址。urllib 的默认 opener 还认 file: 与 ftp:,在这里挡掉。"""
    parts = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"base_url must be an http(s) URL, got {base_url!r}")
    return parts


def require_timeout(timeout_s: object) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not (
        math.isfinite(timeout_s) and timeout_s > 0
    ):
        raise ValueError(f"timeout_s must be a finite number > 0, got {timeout_s!r}")
    return float(timeout_s)


def post_chat(
    base_url: str, model: str, api_key: str, messages: list[dict], *, timeout_s: float, host_label: str,
    extra_body: dict | None = None,
) -> str:
    """向 OpenAI 兼容的 `<base_url>/v1/chat/completions` POST 一次,返回 choices[0].message.content。

    外部 API 与 vLLM 服务共用这一处:不重试(重试与降级是调用方的事),不跟随重定向,回复体有上限。
    失败一律抛 OpponentApiError,信息只含状态码、host_label、异常类型名,不含响应体;密钥若出现在
    信息里会被抹掉,且切断异常链(from None),回溯里没有携带请求头的原异常。子类按失败的性质分:
    超时 OpponentTimeoutError(T1);暂时不可用 OpponentUnavailableError(429、5xx、连不上、中途断开;T1);
    对端明确拒绝 OpponentRejectedError(4xx,429 除外;T2);回复不是约定的形状(及其余非 200)是普通的 OpponentApiError。
    host_label 是写进错误信息的"对端是谁"(外部 API 用 host:port,vLLM 用服务名)。
    extra_body 是并进请求体的额外字段,只给认得它们的对端用(vLLM 的 VLLM_CHAT_EXTRA);外部 API 不传。"""
    require_http_url(base_url)
    timeout = require_timeout(timeout_s)

    def fail_status(code: int) -> OpponentApiError:
        """非 200 的状态码。4xx(429 除外)是对端明确拒绝(T2);429 与 5xx 是暂时不可用(T1);
        其余(1xx、2xx 不是 200、3xx 重定向)我们读不懂,是普通的 OpponentApiError。"""
        if 400 <= code < 500 and code != 429:
            return fail(f"HTTP {code} from {host_label}", OpponentRejectedError)
        if code == 429 or 500 <= code < 600:
            return fail(f"HTTP {code} from {host_label}", OpponentUnavailableError)
        return fail(f"HTTP {code} from {host_label}")

    def fail(message: str, error: type[OpponentApiError] = OpponentApiError) -> OpponentApiError:
        """所有失败走这一处,出口前抹掉密钥(原异常的文本理论上不该含,这里是第二道防线)。"""
        return error(message.replace(api_key, "***") if api_key else message)

    payload = {**(extra_body or {}), "model": model, "messages": messages}
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_REPLY_BYTES + 1)
            declared = response.getheader("Content-Length")
    except urllib.error.HTTPError as exc:
        exc.close()
        raise fail_status(exc.code) from None
    except TimeoutError:
        raise fail(f"request to {host_label} timed out after {timeout:g}s", OpponentTimeoutError) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise fail(f"request to {host_label} timed out after {timeout:g}s", OpponentTimeoutError) from None
        raise fail(
            f"request to {host_label} failed ({type(exc.reason).__name__}: {exc.reason})", OpponentUnavailableError,
        ) from None
    except OSError as exc:  # 连接被重置、对端中途断开
        raise fail(f"request to {host_label} failed ({type(exc).__name__})", OpponentUnavailableError) from None
    except http.client.HTTPException as exc:  # 不是 OSError 的子类:状态行不合规、读到一半断开等,对端的毛病
        raise fail(f"request to {host_label} failed ({type(exc).__name__})", OpponentUnavailableError) from None
    except Exception as exc:  # 认不出来的异常:不冒充暂时不可用(会被按条重跑),归普通的 OpponentApiError
        raise fail(f"request to {host_label} failed ({type(exc).__name__})") from None
    if status != 200:
        raise fail_status(status)
    # isascii():http.client 把头字节按 Latin-1 解码,0xB2/0xB3/0xB9 变成 ²³¹,str.isdigit() 对它们为真而 int() 不认。
    # 位数上限:比回复上限还长的数字串不必转成 int 就知道体被截短了,也避开 int() 的 4300 位上限抛 ValueError
    if (declared is not None and declared.isascii() and declared.isdigit()
            and len(raw) <= MAX_REPLY_BYTES
            and (len(declared) > len(str(MAX_REPLY_BYTES)) or len(raw) < int(declared))):
        # read(amt) 遇到比 Content-Length 短的体不抛 IncompleteRead,只是少给:对端中途断开,不是回复形状不对
        raise fail(f"the reply from {host_label} was cut off before its declared length", OpponentUnavailableError)
    if len(raw) > MAX_REPLY_BYTES:
        raise fail(f"the reply from {host_label} is larger than {MAX_REPLY_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # UnicodeDecodeError 与 JSONDecodeError 都是 ValueError;嵌套过深的体让 json 抛 RecursionError(不是 ValueError)
        raise fail(f"the reply from {host_label} is not valid JSON") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise fail(f"the reply from {host_label} has no choices[0].message.content") from None
    if not isinstance(content, str) or not content.strip():
        raise fail(f"the reply from {host_label} has an empty or non-text choices[0].message.content")
    return content


class ExternalApiBackend:
    """ModelBackend:把对手位的发言交给 OpenAI 兼容的聊天补全接口。一次 respond 一次 POST,不重试
    (重试与降级是调用方的事:服务端用 TimeoutBackend 限时,失败按角色位后端故障处理)。

    密钥只在构造时校验、只放进 Authorization 头。校验不过抛 OpponentCredentialError(信息只含 api_key_env);
    请求与失败处理见 post_chat(与 vLLM 后端共用)。"""

    def __init__(self, base_url: str, model: str, *, api_key: str, api_key_env: str, timeout_s: float) -> None:
        parts = require_http_url(base_url)
        timeout = require_timeout(timeout_s)
        if not isinstance(api_key, str) or not api_key.strip():
            raise OpponentCredentialError(
                f"the opponent API key is empty; set the environment variable {api_key_env}"
            )
        if not (api_key.isascii() and api_key.isprintable()):
            # 换行或非 ASCII 的值放进请求头,http.client 抛的 ValueError 会带上整个头值
            raise OpponentCredentialError(
                f"the opponent API key in {api_key_env} has characters that cannot go into an HTTP header"
            )
        self._base_url = base_url
        self._host = parts.netloc
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        return self.respond_guided(persona, scenario, history, speaker_slot, ())

    def respond_guided(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
        guidance: Sequence[str],
    ) -> tuple[str, str | None]:
        """带指导语的发言:guidance 并入 system 消息(见 chat_messages),其余与 respond 同一条路径。"""
        content = post_chat(
            self._base_url, self._model, self._api_key,
            chat_messages(persona, scenario, history, speaker_slot, guidance),
            timeout_s=self._timeout, host_label=self._host,
        )
        return content, None
