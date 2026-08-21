"""hermes 引擎 SDK runner（issue #171：hermes agent SDK 进程内集成）。

与旧 hermes_runner.py（独立子进程 + stdin/stdout NDJSON，经部署机独立
hermes venv 的 python 运行）不同，本模块把 hermes-agent SDK
（run_agent.AIAgent）集成进 botler 自身进程：worker 线程跑
``run_conversation()``，主循环轮询人工停止，``stop()`` 经
``AIAgent.interrupt()`` 请求中断（跨线程安全，conversation loop 多处
检查中断标志提前退出，并通知进行中的工具提前终止——语义等价旧模式的
SIGKILL 进程组）。集成方式与 dsh 引擎（deepseek-harness SDK，issue #84）
对齐。

输出协议与旧 hermes runner 完全一致（事件行 + 结果行 NDJSON），executor
的结果判定（_hermes_result）、SSE 解析（parse_hermes_event_line）、会话
历史落库（hermes_history）与日志落盘设施直接复用，无需新协议解析。

LLM 配置（model/provider/base_url/api_key）从 hermes 侧 ~/.hermes 解析
（经 hermes_cli.config.load_config_readonly / get_env_value，与 hermes
CLI 同源）：hermes-agent 0.20.0 起 AIAgent 直接构造不会自动回退 config
的 model.default，不显式传 model 会以空模型发起请求（DeepSeek 400）；
旧 hermes_runner.py 同样存在该缺口。botler 仍不管理 LLM 配置（无新增
配置项），只是把 hermes 侧已配好的值转发给 AIAgent。解析失败回退空参数
（AIAgent 默认行为），不阻断执行。

进程内模式的环境处理（旧模式注入子进程 env，进程内模式只能操作
进程环境）：
- 工作区：``run_conversation`` 传入 botler 任务 id，经 hermes 的
  ``register_task_env_overrides(task_id, {"cwd": 工作区})`` 注册会话级
  cwd 覆盖（terminal/file 工具按 task_id 解析，优先于进程级 TERMINAL_CWD）；
  同时 worker 线程内临时设置 ``TERMINAL_CWD`` 兜底并在结束后恢复；
- git 凭据 / 引擎环境：``env`` 参数（executor._build_env 产物：
  GIT_ASKPASS / GITLAB_TOKEN / GIT_CONFIG_GLOBAL 等）在 worker 线程内
  临时写入 os.environ 并恢复，供 hermes terminal 工具派生的子进程继承。

SDK 为可选依赖（同 dsh 部署模式）：requirements.txt 不声明；pm2 部署经
deploy/install-hermes-agent.sh editable 安装，Docker 部署由
docker-entrypoint.sh 在容器启动时对挂载源码安装。未安装时 start() 抛
HermesSdkNotInstalledError（含安装指引）。导入全部惰性（仅 worker 线程
import），平台其余功能不受 SDK 缺失影响。
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from importlib.util import find_spec
from typing import Any, Callable

# SDK 安装指引（hermes-agent 以源码分发，只能 editable 安装；pm2 部署
# 用一键脚本，Docker 部署由 entrypoint 启动时安装）
INSTALL_HINT = (
    "deploy/install-hermes-agent.sh "
    "（pm2 部署在主依赖安装后自动调用；Docker 部署由 docker-entrypoint.sh "
    "启动时安装，详见 docs/hermes-engine-deployment.md）")


class HermesSdkNotInstalledError(Exception):
    """hermes-agent SDK 未安装（含安装指引）。"""


def _emit(on_line: Callable[[str], None], line: str) -> None:
    """写一行 NDJSON（事件行或结果行）；on_line 由 executor 单线程调用。"""
    on_line(line)


def _emit_event(on_line: Callable[[str], None], event: dict) -> None:
    """实时事件行：单行 JSON（executor 边读边推 SSE）。"""
    _emit(on_line, json.dumps(event, ensure_ascii=False))


def _safe_emit(on_line: Callable[[str], None], event: dict) -> None:
    """事件行序列化容错：单次回调异常不阻断任务执行与结果输出。"""
    try:
        _emit_event(on_line, event)
    except (TypeError, ValueError, OSError):
        pass


def _stream_callbacks(on_line: Callable[[str], None]) -> dict[str, Any]:
    """构造传给 AIAgent 的事件回调集合（与旧 hermes_runner.py 同协议）。

    参数签名按 hermes-agent 真实调用点宽容解析：
    - tool_start_callback(tool_call_id, name, args)
    - tool_complete_callback(tool_call_id, name, args, result)
    - thinking_callback(text) / stream_delta_callback(text)
    - status_callback(*args)
    """

    def thinking(text: str) -> None:
        if isinstance(text, str) and text.strip():
            _safe_emit(on_line, {"event": "thinking", "text": text})

    def tool_start(*args: Any) -> None:
        name = args[1] if len(args) >= 3 else (args[0] if args else "?")
        tool_input = args[2] if len(args) >= 3 else (args[1] if len(args) > 1 else "")
        _safe_emit(on_line, {"event": "tool_start", "tool": name,
                             "input": tool_input})

    def tool_complete(*args: Any) -> None:
        name = args[1] if len(args) >= 4 else (args[0] if args else "?")
        output = args[3] if len(args) >= 4 else (args[2] if len(args) >= 3 else "")
        is_error = isinstance(output, str) and (
            "Traceback" in output or "Error" in output)
        _safe_emit(on_line, {"event": "tool_complete", "tool": name,
                             "output": output, "is_error": is_error})

    def stream_delta(text: str) -> None:
        if isinstance(text, str) and text.strip():
            _safe_emit(on_line, {"event": "stream_delta", "text": text})

    def status(*args: Any) -> None:
        message = " ".join(str(a) for a in args if a not in (None, ""))
        if message:
            _safe_emit(on_line, {"event": "status", "message": message})

    return {
        "thinking_callback": thinking,
        "tool_start_callback": tool_start,
        "tool_complete_callback": tool_complete,
        "stream_delta_callback": stream_delta,
        "status_callback": status,
    }


class HermesSdkRunner:
    """进程内运行 hermes-agent SDK（run_agent.AIAgent）的封装（一次性实例跑一个任务）。

    线程模型：start() 启动 worker 线程跑 run_conversation()（阻塞直至
    回合结束或中断）；AIAgent 回调在 worker 线程内逐条转 NDJSON 事件行
    回调 on_line（单线程顺序调用，executor 侧写日志/发 SSE 无竞态）；
    stop() 可从任意线程调用（幂等：置停止标志 + 请求 AIAgent.interrupt()）。
    """

    def __init__(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        history: list | None = None,
        task_id: str | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        on_line: Callable[[str], None],
    ):
        self.prompt = prompt
        self.session_id = session_id      # 断点续跑：上次落库的会话 id
        self.history = history            # 断点续跑：上次落库的消息历史
        self.task_id = task_id            # botler 任务 id（会话级 cwd 覆盖键）
        self.cwd = cwd                    # 仓库工作区（terminal/file 工具执行目录）
        self.env = env or {}              # executor._build_env 产物（git 凭据等）
        self.on_line = on_line
        self._stopping = threading.Event()
        self._agent: Any = None
        self._thread: threading.Thread | None = None
        self._exit_code = 1
        self._error: str | None = None
        # issue #235：任务 token 用量——run_conversation 后从 agent 会话级
        # 计数器聚合（含模型名与 SDK 自带费用），供 executor 落库与结果行展示
        self.usage: dict | None = None
        self.model: str = ""
        # 环境还原快照（worker 线程临时写入 os.environ，结束恢复）
        self._env_saved: dict[str, str | None] = {}
        self._cwd_saved: str | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动 worker 线程；SDK 未安装抛 HermesSdkNotInstalledError。"""
        if find_spec("run_agent") is None:
            raise HermesSdkNotInstalledError(
                f"hermes 引擎需要 hermes-agent SDK，未安装：{INSTALL_HINT}")
        self._thread = threading.Thread(
            target=self._worker, name="botler-hermes-sdk-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求停止：置标志并请求 AIAgent.interrupt()（幂等）。

        interrupt() 为跨线程安全的中断请求：conversation loop 多处检查
        中断标志提前退出，并通知进行中的工具提前终止。agent 尚未构造
        （worker 未就绪）时仅置标志，worker 启动后自行检查退出。
        """
        self._stopping.set()
        agent = self._agent
        if agent is not None:
            try:
                agent.interrupt()
            except Exception:  # noqa: BLE001 停止路径中断失败不掩盖停止语义
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

    # ---- 环境（进程内模式的 env 注入与还原）----

    def _apply_env(self) -> None:
        """把 executor 注入的引擎环境临时写入 os.environ（worker 线程内）。"""
        for key, value in self.env.items():
            self._env_saved[key] = os.environ.get(key)
            os.environ[key] = str(value)
        # TERMINAL_CWD 兜底（会话级 cwd 覆盖优先，进程级仅作回退）
        if self.cwd:
            self._cwd_saved = os.environ.get("TERMINAL_CWD")
            os.environ["TERMINAL_CWD"] = str(self.cwd)

    def _restore_env(self) -> None:
        for key, old in self._env_saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._env_saved.clear()
        if self.cwd:
            if self._cwd_saved is None:
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = self._cwd_saved
            self._cwd_saved = None

    # ---- worker ----

    def _worker(self) -> None:
        try:
            self._apply_env()
            self._run_agent()
        except BaseException as e:  # noqa: BLE001 worker 兜底：异常按失败收尾
            self._error = f"runner 未预期异常: {e}\n{traceback.format_exc()[-2000:]}"
            self._emit_result("", [], "", error=self._error)
        finally:
            self._restore_env()

    def _resolve_llm_config(self) -> dict[str, Any]:
        """从 hermes 侧配置（~/.hermes/config.yaml / .env）解析 LLM 参数。

        返回传给 AIAgent 的 model / provider / base_url / api_key 子集：
        - model.default → model；model.provider → provider；
          model.base_url → base_url；
        - api_key 解析链：config 显式 model.api_key > model.key_env 指定
          的环境变量 > 供应商惯例环境变量（如 deepseek → DEEPSEEK_API_KEY，
          经 hermes get_env_value 读 os.environ 或 ~/.hermes/.env）。
        解析失败（hermes 配置不可读）返回空 dict（AIAgent 默认行为），
        不阻断执行——与旧 runner 的宽容语义一致。
        """
        try:
            from hermes_cli.config import (
                get_env_value,
                load_config_readonly,
            )
            cfg = load_config_readonly()
        except Exception:  # noqa: BLE001 配置解析失败回退空参数
            return {}
        model_cfg = cfg.get("model", {}) or {}
        out: dict[str, Any] = {}
        for cfg_key, target in (("default", "model"), ("provider", "provider"),
                                ("base_url", "base_url")):
            val = str(model_cfg.get(cfg_key) or "").strip()
            if val:
                out[target] = val
        api_key = str(model_cfg.get("api_key") or "").strip()
        if not api_key:
            key_env = str(model_cfg.get("key_env") or "").strip()
            if not key_env and out.get("provider"):
                key_env = f"{out['provider'].upper()}_API_KEY"
            if key_env:
                try:
                    api_key = str(get_env_value(key_env) or "").strip()
                except Exception:  # noqa: BLE001 环境变量读取失败按空处理
                    api_key = ""
        if api_key:
            out["api_key"] = api_key
        return out

    def _register_cwd_override(self, task_id: str) -> None:
        """注册会话级 cwd 覆盖（terminal/file 工具按 task_id 解析工作区）。

        注册失败不阻断（回退 TERMINAL_CWD）；hermes SDK 未安装时导入
        路径不可达，注册本身在 SDK 已安装前提下调用（import 兜底 try）。
        """
        try:
            from tools.terminal_tool import register_task_env_overrides
            register_task_env_overrides(task_id, {"cwd": str(self.cwd)})
        except Exception:  # noqa: BLE001 覆盖注册失败不阻断任务执行
            pass

    def _clear_cwd_override(self, task_id: str) -> None:
        try:
            from tools.terminal_tool import clear_task_env_overrides
            clear_task_env_overrides(task_id)
        except Exception:  # noqa: BLE001 清理失败不阻断任务收尾
            pass

    def _emit_result(self, final_response: str, messages: list | None,
                     session_id: str, error: str | None = None,
                     usage: dict | None = None) -> None:
        """输出结果行（executor 落库断点续跑与结果判定）。

        usage（issue #235）：会话累计 token 用量 dict（无用量时省略），
        写进结果行供日志诊断；executor 落库直接读 runner.usage。
        """
        data = {
            "final_response": final_response or "",
            "messages": messages if isinstance(messages, list) else [],
            "session_id": session_id or "",
            "error": error,
        }
        if isinstance(usage, dict):
            data["usage"] = usage
        _emit(self.on_line, json.dumps(data, ensure_ascii=False))

    def _run_agent(self) -> None:
        """worker 主流程：import SDK → 构造 AIAgent → run_conversation → 结果行。"""
        task_id = str(self.task_id or "")
        registered = bool(task_id and self.cwd)
        if registered:
            self._register_cwd_override(task_id)

        try:
            from run_agent import AIAgent
        except Exception as e:  # noqa: BLE001 缺失依赖统一转协议错误
            self._error = f"无法导入 run_agent.AIAgent: {e}"
            self._emit_result("", [], "", error=self._error)
            if registered:
                self._clear_cwd_override(task_id)
            return

        # 停止请求早于 agent 构造（worker 未就绪时的 stop 只置了标志）：
        # 启动后立即检查，不再发起执行（与 dsh runner 的 stop 语义一致）
        if self._stopping.is_set():
            self._error = "任务被用户停止"
            self._emit_result("", [], "", error=self._error)
            if registered:
                self._clear_cwd_override(task_id)
            return

        try:
            llm_cfg = self._resolve_llm_config()
            agent = AIAgent(quiet_mode=True, session_id=self.session_id,
                            **llm_cfg, **_stream_callbacks(self.on_line))
            self._agent = agent
            if self._stopping.is_set():
                # stop 与构造竞态：构造完成后、run 之前又收到停止 → 直接中断
                agent.interrupt()
            result = agent.run_conversation(
                user_message=self.prompt,
                conversation_history=self.history,
                task_id=task_id or None,
            )
        except BaseException as e:  # noqa: BLE001 中断/agent 异常按失败收尾
            if isinstance(e, (KeyboardInterrupt, InterruptedError)):
                self._error = "任务被用户停止"
            else:
                self._error = f"agent 执行异常: {e}\n{traceback.format_exc()[-2000:]}"
            self._emit_result("", [], "", error=self._error)
            if registered:
                self._clear_cwd_override(task_id)
            return

        if registered:
            self._clear_cwd_override(task_id)

        if not isinstance(result, dict):
            self._error = f"run_conversation 返回非 dict: {type(result).__name__}"
            self._emit_result("", [], "", error=self._error)
            return
        if result.get("interrupted") is True:
            # 用户停止中断：不视为失败（executor 按 STOP 收尾），
            # 结果行带中断说明，便于日志与断点续跑数据观察
            self._error = "任务被用户停止"
            self._emit_result("", [], "", error=self._error)
            return
        final_response = result.get("final_response")
        if final_response is None:
            # 异常返回结构：缺 final_response 视为失败（不允许静默成功）
            self._error = f"结果缺少 final_response 字段: {list(result)[:10]}"
            self._emit_result("", [], "", error=self._error)
            return
        messages = result.get("messages")
        if not isinstance(messages, list):
            messages = []
        # 会话 id 优先取 agent 属性（run_conversation 返回结构中通常不含），
        # 供 executor 落库、下次执行接续同一会话
        session_id = str(getattr(agent, "session_id", "") or ""
                         or result.get("session_id") or "")
        # issue #235：从 agent 会话级计数器聚合 token 用量（会话内全部
        # API 调用的累计；conversation_history 消息本身不携带 usage 字段，
        # 计数器是同等语义的权威合计），模型名取 agent.model，费用优先
        # SDK 自带估算（session_estimated_cost_usd，含缓存/推理计价）
        usage = {
            "prompt_tokens": int(getattr(agent, "session_prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(agent, "session_completion_tokens", 0) or 0),
            "total_tokens": int(getattr(agent, "session_total_tokens", 0) or 0),
            "input_tokens": int(getattr(agent, "session_input_tokens", 0) or 0),
            "output_tokens": int(getattr(agent, "session_output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(agent, "session_cache_read_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(agent, "session_cache_write_tokens", 0) or 0),
            "reasoning_tokens": int(getattr(agent, "session_reasoning_tokens", 0) or 0),
            "api_calls": int(getattr(agent, "session_api_calls", 0) or 0),
            "estimated_cost_usd": getattr(agent, "session_estimated_cost_usd", None),
            "cost_source": str(getattr(agent, "session_cost_source", "") or ""),
            "model": str(getattr(agent, "model", "") or ""),
        }
        self.usage = usage
        self.model = usage["model"]
        self._exit_code = 0
        self._error = None
        self._emit_result(str(final_response), messages, session_id, error=None,
                          usage=usage)
