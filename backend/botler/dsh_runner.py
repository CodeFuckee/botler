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
from importlib.util import find_spec
from typing import Callable

# SDK 安装指引（部署机清华 pip 源未同步 rc 版，需用阿里镜像；
# Docker 部署镜像已内置，无需手动安装）
INSTALL_HINT = (
    "pip install deepseek-harness-sdk==0.1.0rc6 "
    "-i https://mirrors.aliyun.com/pypi/simple/ "
    "（Docker 部署已内置，无需安装；详见 docs/dsh-engine-deployment.md）")


class DshSdkNotInstalledError(Exception):
    """deepseek-harness SDK 未安装（含安装指引）。"""


def _event_line(event: str, extra: dict) -> str:
    """序列化一条 hermes 风格事件行（ensure_ascii=False 保留中文）。"""
    data = {"event": event, **extra}
    return json.dumps(data, ensure_ascii=False)


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
        reason = data.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else None
        return [_event_line("status", {"message": f"回合结束: {kind}"})]
    if etype == "agent/inbox/spliced":
        return []  # SDK 内部簿记，不产行
    return [_event_line("raw", {"type": etype})]


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
                 on_line: Callable[[str], None]):
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
                self.on_line(build_result_line(
                    None, None, self.session_id, error="stopped"))
            else:
                self._error = f"{type(exc).__name__}: {exc}"
                self.on_line(build_result_line(
                    None, None, self.session_id, error=self._error))
            return
        finally:
            self._close_harness()
        self._exit_code = 0
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
            self.on_line(line)
