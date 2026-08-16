"""dsh 引擎 runner（issue #84）：进程内调用 deepseek-harness Python SDK（方案B）。

与 hermes_runner.py（独立子进程 + stdin/stdout NDJSON）不同：SDK 在 botler
进程内运行（stdio JSON-RPC 驱动捆绑运行时），运行中无法从外部 SIGKILL，
因此采用「工作线程跑 harness.run() + 主循环轮询停止/超时 + close() 强制
终止运行时」模型（close 终止运行时子进程后 run 抛 TransportClosedError，
语义等价现有引擎的 SIGKILL 进程组）。

输出协议与 hermes 对齐（事件行 + 结果行），executor 的结果判定
（_dsh_result）、SSE 解析（parse_hermes_event_line）与日志落盘设施
直接复用，无需新协议解析。

SDK 为可选依赖（同 hermes 部署模式）：requirements.txt 不声明；
Docker 部署镜像已内置（issue #112：Dockerfile 构建期自动安装 + import
校验）。开发机 / pm2 部署未安装时 start() 抛 DshSdkNotInstalledError
（含安装指引）。导入全部惰性（仅 worker 线程 import），平台其余
功能不受 SDK 缺失影响。
"""

from __future__ import annotations

import json
import threading
import time
from importlib.util import find_spec
from typing import Callable

# SDK 安装指引（部署机清华 pip 源未同步 rc 版，需用阿里镜像；
# Docker 镜像与 CI pm2 部署已自动安装，手动部署用一键脚本）
INSTALL_HINT = (
    "deploy/install-dsh-sdk.sh "
    "（Docker 镜像与 CI pm2 部署已自动安装；手动部署在项目根目录"
    "执行该脚本，详见 docs/dsh-engine-deployment.md）")


class DshSdkNotInstalledError(Exception):
    """deepseek-harness SDK 未安装（含安装指引）。"""


def _event_line(event: str, extra: dict) -> str:
    """序列化一条 hermes 风格事件行（ensure_ascii=False 保留中文）。"""
    data = {"event": event, **extra}
    return json.dumps(data, ensure_ascii=False)


class _DeltaCoalescer:
    """连续流式增量（文本/思考）合并缓冲（issue #122）。

    dsh SDK 的 assistant/chunk 按 token/字粒度回调 text-delta /
    reasoning-delta，一句话会拆成几十上百个独立通知；若逐条转事件行，
    SSE 事件流、事件总线、日志文件都会被放大到逐字粒度，极其冗长。
    这里把连续同类型增量先拼进缓冲，遇到异类事件、类型切换或超过
    刷新间隔时一次性冲刷为一条事件行——事件数从「每字一条」降到
    「每句/每几百毫秒一条」，UI 仍保持实时感。事件行协议不变
    （下游 parse_hermes_event_line / executor / SSE 回放零改动）。
    """

    FLUSH_INTERVAL = 0.5  # 秒：连续增量超过该间隔强制冲刷（保持实时感）

    def __init__(self, on_line: Callable[[str], None],
                 flush_interval: float = FLUSH_INTERVAL):
        self._on_line = on_line
        self._flush_interval = flush_interval
        self._kind: str | None = None  # "text" / "thinking"
        self._parts: list[str] = []
        self._last_flush = 0.0

    def feed(self, line: str) -> None:
        """喂入一条事件行：增量行合并进缓冲，非增量行先冲刷再直发。"""
        kind, text = self._classify(line)
        if kind is not None:
            if text:
                self._append(kind, text)
            return  # 空文本增量丢弃（上游 format 已过滤，防御兜底）
        self.flush()
        self._on_line(line)

    def flush(self) -> None:
        """冲刷缓冲为一条合并事件行（无缓冲时为空操作）。"""
        if self._kind is None:
            return
        event = "stream_delta" if self._kind == "text" else "thinking"
        self._on_line(_event_line(event, {"text": "".join(self._parts)}))
        self._kind = None
        self._parts = []
        self._last_flush = time.time()

    def _append(self, kind: str, text: str) -> None:
        now = time.time()
        if self._kind != kind:
            # 类型切换：先冲刷旧类型缓冲，再开新缓冲（保序）
            if self._kind is not None:
                self.flush()
            self._kind = kind
            self._parts = []
            self._last_flush = now
        self._parts.append(text)
        if now - self._last_flush >= self._flush_interval:
            self.flush()

    @staticmethod
    def _classify(line: str) -> tuple[str | None, str]:
        """解析事件行：增量行返回 (kind, text)，其余返回 (None, "")。"""
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            return None, ""
        if not isinstance(data, dict):
            return None, ""
        event = data.get("event")
        text = data.get("text")
        if event == "stream_delta" and isinstance(text, str):
            return "text", text
        if event == "thinking" and isinstance(text, str):
            return "thinking", text
        return None, ""


def format_dsh_notification(method: str, payload: dict) -> list[str]:
    """SDK 通知 → hermes 风格事件行列表（0..n 行，逐行回调 on_line）。

    - session.event：assistant/message 的 content blocks → text/thinking/
      tool_use 事件行；turn/end → status；SDK 内部簿记（inbox/spliced）
      跳过；未知类型 → raw 行（仅落日志诊断，parse_hermes_event_line
      不发布 SSE）
    - session.status → status 行
    - 其他 method（subagent 等）→ raw 行（日志诊断）
    payload 畸形时容错返回空列表，不抛异常。
    """
    if not isinstance(payload, dict):
        return []
    if method == "session.event":
        return _format_session_event(payload.get("event"))
    if method == "session.status":
        return [_event_line("status",
                            {"message": f"dsh 会话状态: {payload.get('status')}"})]
    if method in ("subagent.started", "subagent.finished"):
        return [_event_line("status",
                            {"message": f"subagent {method.split('.')[1]}: "
                                        f"{payload.get('childSessionId')}"})]
    return [_event_line("raw", {"method": method})]


def _format_session_event(event) -> list[str]:
    if not isinstance(event, dict):
        return []
    etype = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    if etype == "assistant/message":
        return _format_assistant_content(data)
    if etype == "turn/end":
        return [_turn_end_status_line(data.get("reason"))]
    if etype == "assistant/chunk":
        # issue #115：SDK 真实事件结构（本机实测 deepseek-harness 0.1.0rc6）：
        # assistant/chunk 承载流式增量——block-start / reasoning-delta /
        # text-delta / block-end / usage / finish。此前的实现只透传
        # finish/error 块（LLM 调用失败细节），reasoning-delta 与
        # text-delta 全部落 raw：SSE 无实时流式输出、思考过程不可见，
        # 任务执行页在回合结束前一片空白，失败诊断只能靠「回合结束:
        # error」贫瘠摘要。这里补齐增量透传（与 assistant/message
        # 的完整块语义一致），其余簿记块（block-start/block-end/
        # usage/finish 正常结束）仍落 raw 供日志诊断。
        chunk = data.get("chunk")
        if isinstance(chunk, dict):
            ctype = chunk.get("type")
            text = chunk.get("text")
            if ctype == "text-delta" and isinstance(text, str) and text:
                return [_event_line("stream_delta", {"text": text})]
            if ctype == "reasoning-delta" and isinstance(text, str) and text:
                return [_event_line("thinking", {"text": text})]
            if ctype == "finish":
                reason = chunk.get("reason")
                if isinstance(reason, dict) and reason.get("kind") == "error":
                    failure = reason.get("failure") or reason.get("error")
                    message = (failure.get("message")
                               if isinstance(failure, dict) else None)
                    if isinstance(message, str) and message:
                        return [_event_line("status",
                                            {"message": f"模型调用失败: {message}"})]
        return [_event_line("raw", {"type": etype})]
    if etype == "agent/inbox/spliced":
        return []  # SDK 内部簿记，不产行
    return [_event_line("raw", {"type": etype})]


def _turn_end_status_line(reason) -> str:
    """turn/end 的状态行：kind 非 completed 时附带 error/failure message。

    issue #115：401 AUTH 等失败原因在 reason.error.message /
    reason.failure.message 里，只输出 kind 会丢失诊断信息。
    """
    kind = reason.get("kind") if isinstance(reason, dict) else None
    message = f"回合结束: {kind}"
    if isinstance(reason, dict) and kind not in (None, "completed"):
        detail = reason.get("error") or reason.get("failure")
        text = detail.get("message") if isinstance(detail, dict) else None
        if isinstance(text, str) and text:
            message += f"（{text}）"
    return _event_line("status", {"message": message})


def _format_assistant_content(data: dict) -> list[str]:
    """assistant/message 的 content blocks → 事件行（多块保序，未知块跳过）。"""
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                lines.append(_event_line("stream_delta", {"text": text}))
        elif btype == "thinking":
            text = block.get("thinking")
            if isinstance(text, str) and text:
                lines.append(_event_line("thinking", {"text": text}))
        elif btype == "reasoning":
            # issue #115：真实 SDK 的思考块类型是 reasoning（字段 text，
            # 与 thinking 块同语义），此前不识别导致思考内容从未透传
            text = block.get("text")
            if isinstance(text, str) and text:
                lines.append(_event_line("thinking", {"text": text}))
        elif btype == "tool_use":
            lines.append(_event_line("tool_start", {
                "tool": block.get("name", "?"),
                "input": block.get("input")}))
    return lines


def build_result_line(final_response, finish_reason, session_id,
                      error=None) -> str:
    """构造结果行（hermes 协议同款：final_response/finish_reason/session_id）。"""
    data = {"final_response": final_response or "",
            "finish_reason": finish_reason,
            "session_id": session_id or ""}
    if error:
        data["error"] = error
    return json.dumps(data, ensure_ascii=False)


class DshRunner:
    """进程内运行 deepseek-harness SDK 的封装（一次性实例跑一个任务）。

    线程模型：start() 启动 worker 线程跑 harness.run()（阻塞直至会话
    idle 或运行时关闭）；SDK 通知在 worker 线程内逐条转事件行回调
    on_line（单线程顺序调用，executor 侧写日志/发 SSE 无竞态）；
    stop() 可从任意线程调用（置停止标志 + close 运行时，幂等）。
    """

    def __init__(self, *, prompt: str, session_id: str | None,
                 provider: str = "deepseek-official",
                 model: str = "deepseek-v4-flash",
                 max_tokens: int | None = None,
                 cwd: str | None = None,
                 session_root: str | None = None,
                 cordis: str | None = None,
                 runtime_bin: str | None = None,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 env: dict | None = None,
                 on_line: Callable[[str], None],
                 stream_flush_interval: float = _DeltaCoalescer.FLUSH_INTERVAL):
        self.prompt = prompt
        self.session_id = session_id  # 断点续跑：上次落库的会话 id
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.cwd = cwd
        self.session_root = session_root
        self.cordis = cordis
        self.runtime_bin = runtime_bin
        self.base_url = base_url
        self.api_key = api_key
        self.env = env or {}
        self.on_line = on_line
        # issue #122：流式增量合并缓冲（一句话拆成逐字事件行的冗长优化）
        self._coalescer = _DeltaCoalescer(
            self.on_line, flush_interval=stream_flush_interval)
        self._stopping = threading.Event()
        self._harness = None
        self._thread: threading.Thread | None = None
        self._exit_code = 1
        self._error: str | None = None

    def start(self) -> None:
        """启动 worker 线程；SDK 未安装抛 DshSdkNotInstalledError。"""
        if find_spec("deepseek_harness") is None:
            raise DshSdkNotInstalledError(
                f"dsh 引擎需要 deepseek-harness-sdk，未安装：{INSTALL_HINT}")
        self._thread = threading.Thread(
            target=self._worker, name="botler-dsh-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求停止：置标志并关闭运行时（worker 的 run 收到传输关闭异常退出）。

        幂等；harness 尚未创建（worker 未就绪）时仅置标志，worker 启动
        后自行检查退出。
        """
        self._stopping.set()
        harness = self._harness
        if harness is not None:
            try:
                harness.close()
            except Exception:  # noqa: BLE001 停止路径关闭失败不掩盖停止语义
                pass

    def done(self) -> bool:
        """worker 线程是否已结束（供调用方轮询循环检查）。"""
        thread = self._thread
        return thread is None or not thread.is_alive()

    def finish(self, join_timeout: float = 60.0) -> int:
        """join worker 线程并返回退出码（0 成功 / 1 失败）。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout)
        return self._exit_code if self._error is None else 1

    # ---- worker 线程 ----

    def _worker(self) -> None:
        try:
            from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
        except ImportError as exc:  # start 探测后仍不可导入（罕见竞态）
            self._error = f"deepseek_harness 导入失败: {exc}"
            return
        # 停止请求先于运行时创建到达：直接产 stopped 结果行退出
        if self._stopping.is_set():
            self._coalescer.flush()
            self.on_line(build_result_line(
                None, None, self.session_id, error="stopped"))
            return
        try:
            config = DeepSeekHarnessConfig(
                provider=self.provider,
                model=self.model,
                max_tokens=self.max_tokens,
                cwd=self.cwd,
                session_root=self.session_root,
                cordis=self.cordis,
                runtime_bin=self.runtime_bin,
                base_url=self.base_url,
                api_key=self.api_key,
                env=dict(self.env),
            )
            self._harness = DeepSeekHarness(config)
        except Exception as exc:
            self._error = f"dsh 运行时初始化失败: {exc}"
            return
        try:
            result = self._harness.run(
                self.prompt, session_id=self.session_id,
                on_notification=self._on_notification)
        except Exception as exc:  # noqa: BLE001 运行失败统一产 error 结果行
            if self._stopping.is_set():
                # stop() 关闭运行时触发（语义等价 SIGKILL）：executor 按
                # 停止/超时收尾（退出码 125/124），这里仅落 stopped 行
                self._coalescer.flush()
                self.on_line(build_result_line(
                    None, None, self.session_id, error="stopped"))
            else:
                self._error = f"{type(exc).__name__}: {exc}"
                self._coalescer.flush()
                self.on_line(build_result_line(
                    None, None, self.session_id, error=self._error))
            return
        finally:
            self._close_harness()
        self._exit_code = 0
        self._coalescer.flush()
        self.on_line(build_result_line(
            result.final_response, result.finish_reason,
            result.session_id or self.session_id))

    def _close_harness(self) -> None:
        harness = self._harness
        if harness is None:
            return
        try:
            harness.close()
        except Exception:  # noqa: BLE001 运行时已死时 close 失败无害
            pass

    def _on_notification(self, notification) -> None:
        """SDK 通知回调（worker 线程）：转事件行逐条 on_line。"""
        try:
            lines = format_dsh_notification(
                notification.method, notification.payload)
        except Exception:  # noqa: BLE001 通知序列化失败不影响任务执行
            lines = []
        for line in lines:
            self._coalescer.feed(line)
