"""事件流：引擎输出行 → 归一化事件 + 进程内事件总线（SSE 实时推送）。

实时输出功能（任务页面逐事件看到 Claude Code / hermes 的执行过程）：

- parse_claude_stream_line：claude `--output-format stream-json` 一行
  NDJSON → 归一化事件列表。system/init → status（含 session_id）、
  assistant 消息块 → thinking/text/tool、user 消息 → tool_result、
  result 行 → result。旧 `--output-format json` 单行输出（一行 result）
  同样可解析（type=result），天然兼容。
- parse_hermes_event_line：hermes runner 流式事件行 → 归一化事件列表；
  结果行（含 final_response 键）返回 None，交由 executor 判定。
- EventBus：executor worker 线程发布 → SSE 订阅者队列消费。订阅前的
  事件不保留（历史回放由日志文件兜底）；队列满丢最旧（慢消费者不阻塞
  executor 读流）。

归一化事件 dict：{"seq", "ts", "kind", ...}——seq/ts 由发布侧填充
（executor._publish_event / API 回放），解析函数只产出 kind 与内容字段。
"""

from __future__ import annotations

import json
import queue
import threading


# ---- claude stream-json 行解析 ----

def parse_claude_stream_line(line: str) -> list[dict]:
    """一行 claude stream-json 输出 → 归一化事件列表（无可展示事件返回空）。"""
    if not line or not line.strip():
        return []
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    etype = data.get("type")
    if etype == "system":
        return _claude_system_events(data)
    if etype == "assistant":
        return _claude_assistant_events(data)
    if etype == "user":
        return _claude_user_events(data)
    if etype == "result":
        return [{"kind": "result",
                 "result": data.get("result") if isinstance(data.get("result"), str) else "",
                 "subtype": data.get("subtype"),
                 "exit_code": data.get("exit_code")}]
    return []


def _claude_system_events(data: dict) -> list[dict]:
    if data.get("subtype") != "init":
        return []  # thinking_tokens / hook 等杂讯不展示
    event = {"kind": "status"}
    for key in ("session_id", "cwd", "model"):
        value = data.get(key)
        if isinstance(value, str) and value:
            event[key] = value
    return [event]


def _claude_assistant_events(data: dict) -> list[dict]:
    """assistant 消息 content 块 → 事件（多块保序，未知块跳过）。"""
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    events: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                events.append({"kind": "text", "text": text})
        elif ptype == "thinking":
            text = part.get("thinking")
            if isinstance(text, str) and text:
                events.append({"kind": "thinking", "text": text})
        elif ptype == "tool_use":
            events.append({"kind": "tool", "tool": part.get("name", "?"),
                           "input": part.get("input")})
    return events


def _claude_user_events(data: dict) -> list[dict]:
    """user 消息（工具结果）→ tool_result 事件。"""
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    events: list[dict] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_result":
            continue
        text = _result_text(part.get("content"))
        events.append({"kind": "tool_result",
                       "tool_use_id": part.get("tool_use_id"),
                       "text": text, "is_error": bool(part.get("is_error"))})
    return events


def _result_text(content) -> str:
    """tool_result 的 content：字符串原样；文本块列表拼接；其他容错为空。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


# ---- hermes runner 事件行解析 ----

_HERMES_KINDS = {"thinking", "tool_start", "tool_complete",
                 "stream_delta", "status"}


def parse_hermes_event_line(line: str) -> list[dict] | None:
    """一行 hermes runner 输出 → 归一化事件列表。

    结果行（含 final_response 键）返回 None（交由 executor 判定）；
    非法 JSON / 未知事件返回空列表。
    """
    if not line or not line.strip():
        return None
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if "final_response" in data:
        return None  # 结果行不是事件
    kind = data.get("event")
    if kind == "thinking":
        return [{"kind": "thinking", "text": data.get("text", "")}]
    if kind == "tool_start":
        return [{"kind": "tool", "tool": data.get("tool", "?"),
                 "input": data.get("input")}]
    if kind == "tool_complete":
        return [{"kind": "tool_result", "tool": data.get("tool"),
                 "text": data.get("output", ""),
                 "is_error": bool(data.get("is_error"))}]
    if kind == "stream_delta":
        return [{"kind": "text", "text": data.get("text", "")}]
    if kind == "status":
        return [{"kind": "status", "message": data.get("message", "")}]
    if kind in _HERMES_KINDS:
        return []
    return []


# ---- 进程内事件总线 ----

class Subscription:
    """单订阅者队列：get 阻塞取事件；close 后不再接收。"""

    def __init__(self, bus: "EventBus", task_id: int, maxsize: int):
        self._bus = bus
        self._task_id = task_id
        self._maxsize = maxsize
        self._q: queue.Queue[dict] = queue.Queue(maxsize=maxsize)
        self._closed = False

    def put(self, event: dict) -> None:
        """队列满丢最旧再入队（慢消费者不阻塞发布者）。"""
        try:
            self._q.put_nowait(event)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(event)
            except queue.Full:  # noqa: BLE001 极端竞态下丢弃新事件
                pass

    def get(self, timeout: float | None = None) -> dict:
        if self._closed:
            raise RuntimeError("订阅已关闭")
        return self._q.get(timeout=timeout)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus.unsubscribe(self._task_id, self)


class EventBus:
    """按 task_id 分组的发布/订阅总线（executor 线程发布，SSE 线程消费）。"""

    def __init__(self) -> None:
        self._subs: dict[int, set[Subscription]] = {}
        self._lock = threading.Lock()

    def publish(self, task_id: int, event: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(task_id, ()))
        for sub in subs:
            if not sub._closed:  # noqa: SLF001 内部状态判断
                sub.put(event)

    def subscribe(self, task_id: int, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, task_id, maxsize)
        with self._lock:
            self._subs.setdefault(task_id, set()).add(sub)
        return sub

    def unsubscribe(self, task_id: int, sub: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(task_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._subs.pop(task_id, None)
