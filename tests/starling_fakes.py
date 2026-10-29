"""本机回环的 OpenAI 兼容假端点,设计二第 7 步各用例共用。

它只在 127.0.0.1 上监听,不发任何外网请求。Starling 的 OpenAI 适配器把它当成抽取与嵌入服务:
- POST .../embeddings:对 input 逐条用 fake_vector 给向量(余弦只来自共有词面)。
- POST .../chat/completions:按提示词开头判断是哪一遍抽取。信念抽取把「[key] 说话方：原话」
  抽成一条信念;另外两遍(general fact、episodic)一律回 "[]"。

服务端跑在**子进程**里,不在测试进程的线程里。原因:Starling 合并提交 20de8a1 的
SemanticRetriever.vector_recall 绑定没有释放 GIL,Memory.recall 在等查询嵌入的 HTTP 响应时
整个进程的 Python 线程都被卡住,同进程的假端点线程根本处理不了请求,recall 会超时判降级。
子进程没有这个问题。计数与开关经同一端口上的 /__fake__/ 控制路径同步。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from actio.text import tokens

# Starling python/starling/extractor/prompts.py 里信念抽取提示词的开头
_BELIEF_PROMPT_HEAD = "You are an extractor for a Statement-based memory system."
# C++ 侧把段落拼成 "[" + key + "] " + text,text 形如「说话方：原话」
_SEGMENT = re.compile(r"\[([^\]\n]+)\] ([^：\n]+)：([^\n]+)")
_CONTROL = "/__fake__/"


def fake_vector(text: str, *, dim: int, salt: str = "") -> list[float]:
    """词面哈希到 dim 个桶、各加 1、再归一化。空词面集合给全零向量。
    没有共有词面(且不撞桶)时两个向量的余弦恰为 0。"""
    vec = [0.0] * dim
    for tok in tokens(text):
        bucket = int(hashlib.sha256((salt + "\0" + tok).encode()).hexdigest()[:8], 16) % dim
        vec[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


# ---------------- 子进程里的服务端 ----------------

def _serve(dim: int) -> None:
    lock = threading.Lock()
    state = {"chat_calls": 0, "embedding_calls": 0}
    cfg = {"fail_embeddings": False, "fail_keys": frozenset(), "chat_delay_s": 0.0, "salt": ""}

    def bump(name: str) -> None:
        with lock:
            state[name] += 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端已超时断开

        def do_GET(self):
            if self.path == _CONTROL + "state":
                with lock:
                    self._reply(200, dict(state))
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == _CONTROL.rstrip("/") + "/config":
                with lock:
                    for name, value in payload.items():
                        cfg[name] = frozenset(value) if name == "fail_keys" else value
                self._reply(200, {})
            elif path.endswith("/embeddings"):
                self._embeddings(payload)
            elif path.endswith("/chat/completions"):
                self._chat(payload)
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def _embeddings(self, payload: dict) -> None:
            bump("embedding_calls")
            with lock:
                fail, salt = cfg["fail_embeddings"], cfg["salt"]
            if fail:
                self._reply(503, {"error": {"message": "embeddings unavailable"}})
                return
            raw = payload.get("input", [])
            items = [raw] if isinstance(raw, str) else list(raw)
            data = [{"object": "embedding", "index": i, "embedding": fake_vector(str(t), dim=dim, salt=salt)}
                    for i, t in enumerate(items)]
            self._reply(200, {"object": "list", "data": data, "model": payload.get("model", "fake-embed"),
                              "usage": {"prompt_tokens": 0, "total_tokens": 0}})

        def _chat(self, payload: dict) -> None:
            bump("chat_calls")
            with lock:
                fail_keys, delay = cfg["fail_keys"], cfg["chat_delay_s"]
            users = [m for m in payload.get("messages", []) if m.get("role") == "user"]
            content = users[0].get("content", "") if users else ""
            if delay:
                time.sleep(delay)
            answer = "[]"
            if isinstance(content, str) and content.startswith(_BELIEF_PROMPT_HEAD):
                found = _SEGMENT.search(content)
                if found:
                    key, speaker, utterance = found.groups()
                    if key in fail_keys:
                        self._reply(500, {"error": {"message": "extraction failed"}})
                        return
                    answer = json.dumps([{
                        "holder": "self", "holder_perspective": "FIRST_PERSON",
                        "subject": f"cog-{speaker}", "predicate": "responsible_for",
                        "object": utterance, "modality": "BELIEVES", "polarity": "POS",
                        "nesting_depth": 0,
                    }], ensure_ascii=False)
            self._reply(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "model": payload.get("model", "fake-extract"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    server = Server(("127.0.0.1", 0), Handler)

    def exit_when_parent_goes_away() -> None:
        sys.stdin.read()  # 父进程退出或关掉管道时读到 EOF
        os._exit(0)

    threading.Thread(target=exit_when_parent_goes_away, daemon=True).start()
    sys.stdout.write(f"PORT {server.server_port}\n")
    sys.stdout.flush()
    server.serve_forever()


# ---------------- 测试进程里的句柄 ----------------

class _SyncedSet(set):
    """fail_keys:任何改动都同步给子进程。"""

    def __init__(self, push):
        super().__init__()
        self._push = push

    def _sync(self):
        self._push()


def _synced(name: str):
    base = getattr(set, name)

    def method(self, *args, **kwargs):
        result = base(self, *args, **kwargs)
        self._sync()
        return result

    method.__name__ = name
    return method


for _name in ("add", "discard", "remove", "pop", "clear", "update", "difference_update",
              "intersection_update", "symmetric_difference_update",
              "__ior__", "__iand__", "__isub__", "__ixor__"):
    setattr(_SyncedSet, _name, _synced(_name))


class FakeOpenAI:
    """with FakeOpenAI(dim=256) as fake: ... 。开关可在运行中改,立即同步给服务端子进程。"""

    def __init__(self, *, dim: int = 256):
        self.dim = dim
        self._fail_embeddings = False
        self._chat_delay_s = 0.0
        self._salt = ""
        self.fail_keys: set[str] = _SyncedSet(self._push)
        self._proc: subprocess.Popen | None = None
        self._port = 0
        self._last_counts = {"chat_calls": 0, "embedding_calls": 0}

    # -- 地址 --
    @property
    def base_url(self) -> str:
        assert self._proc is not None, "FakeOpenAI 还没启动"
        return f"http://127.0.0.1:{self._port}/v1"

    # -- 开关 --
    @property
    def fail_embeddings(self) -> bool:
        return self._fail_embeddings

    @fail_embeddings.setter
    def fail_embeddings(self, value: bool) -> None:
        self._fail_embeddings = bool(value)
        self._push()

    @property
    def chat_delay_s(self) -> float:
        return self._chat_delay_s

    @chat_delay_s.setter
    def chat_delay_s(self, value: float) -> None:
        self._chat_delay_s = float(value)
        self._push()

    @property
    def salt(self) -> str:
        return self._salt

    @salt.setter
    def salt(self, value: str) -> None:
        self._salt = value
        self._push()

    # -- 计数 --
    @property
    def chat_calls(self) -> int:
        return self._counts()["chat_calls"]

    @property
    def embedding_calls(self) -> int:
        return self._counts()["embedding_calls"]

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body),
                         headers={"Content-Type": "application/json"})
            return json.loads(conn.getresponse().read() or b"{}")
        finally:
            conn.close()

    def _counts(self) -> dict:
        if self._proc is not None:
            self._last_counts = self._request("GET", _CONTROL + "state")
        return self._last_counts

    def _push(self) -> None:
        if self._proc is None:
            return
        self._request("POST", _CONTROL + "config", {
            "fail_embeddings": self._fail_embeddings, "fail_keys": sorted(self.fail_keys),
            "chat_delay_s": self._chat_delay_s, "salt": self._salt,
        })

    # -- 生命周期 --
    def start(self) -> "FakeOpenAI":
        if self._proc is not None:
            return self
        src = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(self.dim)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
        line = proc.stdout.readline() if proc.stdout else ""
        if not line.startswith("PORT "):
            proc.kill()
            raise RuntimeError(f"假端点子进程没有给出端口(退出码 {proc.poll()})")
        self._port = int(line.split()[1])
        self._proc = proc
        self._push()
        return self

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
            try:
                conn.request("GET", _CONTROL + "state")
                self._last_counts = json.loads(conn.getresponse().read() or b"{}")
            finally:
                conn.close()
        except OSError:
            pass
        if proc.stdin:
            proc.stdin.close()  # 子进程读到 EOF 自己退出
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()

    def __enter__(self) -> "FakeOpenAI":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


_shared: FakeOpenAI | None = None
_shared_lock = threading.Lock()


def shared_fake_openai() -> FakeOpenAI:
    """进程内唯一一个假端点:懒启动、不随用例关闭,测试进程退出时子进程随之退出。
    给 PORT_FACTORIES 这类拿不到夹具的地方用;导入本模块不会起服务器。"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = FakeOpenAI(dim=256).start()
        return _shared


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        _serve(int(sys.argv[2]))
