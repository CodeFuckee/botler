"""botler hermes 执行器 runner（issue #47：集成 hermes）。

独立脚本，不依赖 botler 包：由部署机上已装好的 hermes-agent venv 的
python 运行（config.yaml 的 hermes.command/hermes.args 配置），进程内
调用 run_agent.AIAgent（quiet_mode）完成一次 issue 任务执行。

协议（与 executor._run_hermes_once 对接）：
- stdin 读单行 JSON 请求：
    {"prompt": str, "history": [消息列表] | null, "session_id": str | null}
  prompt 必填；history/session_id 为断点续跑时上次执行落库的会话数据。
- stdout 输出 NDJSON 流（实时输出功能）：事件行在前（逐行 flush，供
  executor 推送到 SSE 事件流），最后一行结果 JSON 收尾（供 executor
  落库断点续跑与结果判定）：
    事件行：{"event": "thinking"|"tool_start"|"tool_complete"|
             "stream_delta"|"status", ...}
    结果行：{"final_response": str, "messages": [...], "session_id": str,
            "error": str|null}
  回调从未触发（安静执行）时输出只有结果行——与旧单行协议兼容。
- 退出码：0 成功；1 失败（协议错误 / hermes 未安装 / agent 异常）。

事件回调来自 hermes-agent 的 AIAgent 构造参数（thinking_callback /
tool_start_callback(id, name, args) / tool_complete_callback(id, name,
args, result) / stream_delta_callback / status_callback），回调包装器
对参数签名宽容解析并容错：单次回调异常（如不可序列化参数）不阻断
任务执行与结果输出。

hermes 的 terminal 工具通过 TERMINAL_CWD 环境变量（executor 注入）在
botler 的仓库工作区执行命令，git 凭据同样经子进程环境继承
（GIT_ASKPASS），与 claude CLI 引擎的凭据机制一致。
"""

import json
import sys
import traceback
from typing import Any, IO


class RequestError(ValueError):
    """stdin 请求协议错误（缺 prompt / 非法 JSON 等）。"""


def load_request(stdin: IO[str] | None = None) -> dict[str, Any]:
    """解析 stdin 的 JSON 请求；协议错误抛 RequestError。"""
    raw = (stdin or sys.stdin).read()
    if not raw.strip():
        raise RequestError("stdin 为空，缺少任务请求 JSON")
    try:
        request = json.loads(raw)
    except ValueError as e:
        raise RequestError(f"请求 JSON 解析失败: {e}") from e
    if not isinstance(request, dict):
        raise RequestError(f"请求必须是 JSON 对象，收到: {type(request).__name__}")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RequestError("请求缺少 prompt 字段（任务提示词）")
    history = request.get("history")
    if history is not None and not isinstance(history, list):
        raise RequestError("history 必须是消息列表")
    session_id = request.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise RequestError("session_id 必须是字符串")
    return {
        "prompt": prompt,
        "history": history,
        "session_id": session_id or None,
    }


def build_result(final_response: str, messages: list | None,
                 session_id: str, error: str | None = None) -> dict[str, Any]:
    """构造 stdout 结果 JSON（messages 缺省空列表，恢复路径需可迭代）。"""
    return {
        "final_response": final_response or "",
        "messages": messages if isinstance(messages, list) else [],
        "session_id": session_id or "",
        "error": error,
    }


def _emit(stdout: IO[str], result: dict) -> None:
    (stdout or sys.stdout).write(json.dumps(result, ensure_ascii=False) + "\n")
    (stdout or sys.stdout).flush()


def _emit_event(stdout: IO[str], event: dict) -> None:
    """实时事件行：单行 JSON + flush（executor 边读边推 SSE）。"""
    (stdout or sys.stdout).write(json.dumps(event, ensure_ascii=False) + "\n")
    (stdout or sys.stdout).flush()


def _safe_emit(stdout: IO[str], event: dict) -> None:
    """事件行序列化容错：回调异常不阻断任务执行与结果输出。"""
    try:
        _emit_event(stdout, event)
    except (TypeError, ValueError, OSError):
        pass


def _stream_callbacks(stdout: IO[str]) -> dict[str, Any]:
    """构造传给 AIAgent 的事件回调集合。

    参数签名按 hermes-agent 真实调用点宽容解析：
    - tool_start_callback(tool_call_id, name, args)
    - tool_complete_callback(tool_call_id, name, args, result)
    - thinking_callback(text) / stream_delta_callback(text)
    - status_callback(*args)
    """

    def thinking(text: str) -> None:
        if isinstance(text, str) and text.strip():
            _safe_emit(stdout, {"event": "thinking", "text": text})

    def tool_start(*args: Any) -> None:
        name = args[1] if len(args) >= 3 else (args[0] if args else "?")
        tool_input = args[2] if len(args) >= 3 else (args[1] if len(args) > 1 else "")
        _safe_emit(stdout, {"event": "tool_start", "tool": name,
                            "input": tool_input})

    def tool_complete(*args: Any) -> None:
        name = args[1] if len(args) >= 4 else (args[0] if args else "?")
        output = args[3] if len(args) >= 4 else (args[2] if len(args) >= 3 else "")
        is_error = isinstance(output, str) and (
            "Traceback" in output or "Error" in output)
        _safe_emit(stdout, {"event": "tool_complete", "tool": name,
                            "output": output, "is_error": is_error})

    def stream_delta(text: str) -> None:
        if isinstance(text, str) and text.strip():
            _safe_emit(stdout, {"event": "stream_delta", "text": text})

    def status(*args: Any) -> None:
        message = " ".join(str(a) for a in args if a not in (None, ""))
        if message:
            _safe_emit(stdout, {"event": "status", "message": message})

    return {
        "thinking_callback": thinking,
        "tool_start_callback": tool_start,
        "tool_complete_callback": tool_complete,
        "stream_delta_callback": stream_delta,
        "status_callback": status,
    }


def _run_agent(request: dict, stdout: IO[str] | None = None) -> dict[str, Any]:
    """进程内调用 run_agent.AIAgent 执行一次任务。

    注册事件回调（thinking/tool_start/tool_complete/stream_delta/status），
    执行过程中实时输出事件行到 stdout（实时输出功能）。
    返回 {"final_response", "messages", "session_id", "error"}。
    import 失败（hermes 未安装 / venv 不对）时返回 error 结果。
    """
    try:
        from run_agent import AIAgent
    except Exception as e:  # noqa: BLE001 缺失依赖统一转协议错误
        return {"final_response": "", "messages": [],
                "session_id": "", "error": f"无法导入 run_agent.AIAgent: {e}"}

    try:
        agent = AIAgent(quiet_mode=True, session_id=request.get("session_id"),
                        **_stream_callbacks(stdout))
        result = agent.run_conversation(
            user_message=request["prompt"],
            conversation_history=request.get("history"),
        )
    except Exception as e:  # noqa: BLE001 agent 异常按失败收尾，由 executor 重试
        return {"final_response": "", "messages": [],
                "session_id": "", "error": f"agent 执行异常: {e}\n{traceback.format_exc()[-2000:]}"}

    if not isinstance(result, dict):
        return {"final_response": "", "messages": [],
                "session_id": "", "error": f"run_conversation 返回非 dict: {type(result).__name__}"}
    final_response = result.get("final_response")
    if final_response is None:
        # 异常返回结构：缺 final_response 视为失败（不允许静默成功）
        return {"final_response": "", "messages": [],
                "session_id": "", "error": f"结果缺少 final_response 字段: {list(result)[:10]}"}
    messages = result.get("messages")
    if not isinstance(messages, list):
        messages = []
    # 会话 id 优先取 agent 属性（run_conversation 返回结构中通常不含），
    # 供 executor 落库、下次执行接续同一会话
    session_id = str(getattr(agent, "session_id", "") or ""
                     or result.get("session_id") or "")
    return {"final_response": str(final_response), "messages": messages,
            "session_id": session_id, "error": None}


def main(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """runner 入口：读请求 → 跑 agent → 输出结果 JSON；返回退出码。"""
    try:
        request = load_request(stdin)
    except RequestError as e:
        _emit(stdout, build_result("", [], "", error=str(e)))
        return 1
    result = _run_agent(request, stdout)
    _emit(stdout, build_result(result["final_response"], result["messages"],
                               result["session_id"], error=result["error"]))
    return 0 if result["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
