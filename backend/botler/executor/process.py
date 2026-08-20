"""引擎子进程执行 / 结果判定 / CI 流水线等待（issue #192 拆分）。

从原 executor.py 拆出的进程职责：claude CLI 子进程生命周期（启动/停止/
超时/输出 drain）、hermes/dsh SDK 进程内执行、停止请求管理、引擎输出
JSON 解析与结果判定、CI 流水线等待。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path

from ..dsh_sessions import effective_session_root, normalize_session_root_encoding
from ..events import parse_claude_stream_line, parse_hermes_event_line
from ..gitlab_client import PIPELINE_TERMINAL_STATES, GitLabError
from ..log_redact import redact
from ..plugins import PluginKind, has_plugin
from ..usage import finalize_usage, parse_claude_result_usage
from .common import ExecutorError, STOP_EXIT_CODE, _load_json_output, _row_get, logger
from .prompt import PROGRESS_REPORT_INSTRUCTION, _decode_escapes
from .session import _TRANSCRIPT_MAX_MESSAGES, _TRANSCRIPT_MAX_TEXT, _truncate_text

# 明确「无法解决」的表述（模版要求 Claude 如实汇报），命中则不再重试
UNRESOLVABLE_PATTERNS = [
    r"无法解决", r"无法修复", r"无法完成", r"不能解决", r"不能修复", r"未能解决",
    r"无法复现", r"cannot (?:be )?(?:fix|solve|resolve)", r"can'?t (?:fix|solve|resolve)",
    r"not able to (?:fix|solve|resolve)", r"could not (?:fix|solve|resolve)",
    r"out of scope", r"unable to (?:fix|solve|resolve)",
]
_UNRESOLVABLE_RE = re.compile("|".join(UNRESOLVABLE_PATTERNS), re.IGNORECASE)

# 「等待用户决策」提问信号（issue #67）：无人值守执行中 Claude 停在
# 需要用户选择/回答的节点时，最终回复的结尾会出现选项型提问
# （「请选择 A 或 B」「请回复 1 或 2」「请问……？」等）。任务完成汇报的
# 礼貌收尾（「请确认后关闭本 issue」「如有问题请回复我」）不在此列。
# 命中的结尾再结合「无任务提交」双重确认才判定为等待用户决策。
DECISION_QUESTION_RE = re.compile(
    r"(请选择\s*[A-Za-zＡ-Ｚａ-ｚ][^。\n]{0,60}或|"
    r"请选择\s*[A-Za-zＡ-Ｚａ-ｚ]\s*[/、]\s*[A-Za-zＡ-Ｚａ-ｚ]|"
    r"请回复\s*[0-9１-９][^。\n]{0,60}(?:或|和)|"
    r"请回复\s*[0-9１-９]\s*[/、]\s*[0-9１-９]|"
    r"请决定[^。\n]{0,80}[?？]|"
    r"请确认(?:是否|要|需)[^。\n]{0,80}[?？]|"
    r"请问[^。\n]{0,120}[?？])"
)


class ProcessMixin:
    """引擎进程执行与结果判定（依赖 ClaudeExecutor 实例状态）。"""

    def _kill_process_group(self, proc) -> None:
        """向进程组发 SIGKILL（超时与手动停止共用，issue #35 抽取）。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    def request_stop(self, task_id: int) -> None:
        """登记停止请求并终止任务进程组（issue #35 一键停止所有任务）。

        登记先行：进程尚未创建（worker 还在准备阶段）时，_run_once 在
        Popen 后立即检查登记表自行终止；进程已存在则直接 SIGKILL 进程组
        （readline 读到 EOF 后 _run_once 自然退出）。
        """
        with self._proc_lock:
            self._stop_requests.add(task_id)
            proc = self._procs.get(task_id)
        if proc is not None and proc.poll() is None:
            self._kill_process_group(proc)
    def _stop_requested(self, task_id: int) -> bool:
        with self._proc_lock:
            return task_id in self._stop_requests
    def clear_stop_request(self, task_id: int) -> None:
        """清除停止请求登记（issue #69 手动重试时调用，幂等）。

        一键停止登记的停止请求若不清除会永久残留：任务被停止后用户手动
        重试，worker 领取任务时 run_task 开头的 _stop_requested 检查命中
        旧请求，任务被 _finish_stopped 立即打回 interrupted（表现为「每次
        手动重试过几秒就变成中断状态」，只有平台重启内存集合清空才能
        逃脱）。手动重试即用户明确恢复执行，历史停止请求必须清除。
        """
        with self._proc_lock:
            self._stop_requests.discard(task_id)
    def _engine(self, cfg) -> str:
        """任务执行引擎（issue #47/#84，插件化 issue #140）：claude（默认）/
        hermes / dsh；未注册的引擎名回退 claude。引擎插件见 botler.plugins.executors。"""
        engine = str(getattr(cfg, "engine", "") or "claude").strip().lower()
        return engine if has_plugin(PluginKind.EXECUTOR, engine) else "claude"
    def _drain_process_output(self, proc, task_id: int, log_path: Path,
                              deadline: float, on_chunk=None) -> tuple[bool, bool, list[str]]:
        """边读子进程 stdout 边写日志文件，返回 (timed_out, stopped, chunks)。

        issue #47 从 _run_once 抽取，claude 与 hermes 两引擎共用：
        - 每轮检查停止请求（readline 阻塞时外部 request_stop 已 SIGKILL
          进程组 → readline 返回 EOF 自然退出，此处兜底长期无输出时感知）
        - 超时由调用方 kill 进程组后收尾（返回 timed_out=True）
        - on_chunk：每读到一个 chunk 回调（claude 引擎用于运行中实时落
          session_id，issue #20；hermes 引擎无此需求传 None）
        """
        chunks: list[str] = []
        timed_out = False
        stopped = False
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            while not stopped:
                if self._stop_requested(task_id) and proc.poll() is None:
                    stopped = True
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    chunk = proc.stdout.readline() if proc.stdout else b""
                except Exception:
                    chunk = ""
                if chunk == "" and proc.poll() is not None:
                    break
                if chunk:
                    # 统一日志脱敏（issue #259）：子进程输出落盘前打码，
                    # 原始 chunk 仍保留在 chunks 供结果解析/会话落库
                    f.write(redact(chunk))
                    chunks.append(chunk)
                    if len(chunks) > 20000:  # 约 20MB 上限
                        chunks.pop(0)
                    if on_chunk is not None:
                        on_chunk(chunk)
                if time.time() >= deadline and proc.poll() is None:
                    timed_out = True
                    break
                time.sleep(0.05)
        return timed_out, stopped, chunks
    def _capture_env_snapshot(self, task_id: int, workdir: Path) -> None:
        """任务首次执行开始时采集环境快照（issue #276）落库 tasks.environment。

        只采一次：重试/断点续跑不覆盖首次快照（起始 commit 基线以首次
        执行的工作区 HEAD 为准）。采集全程尽力而为——任何失败不影响任务
        执行：整体异常时落库 {"error": "环境快照获取失败"} 标记，前端
        「元信息」区据此显示「环境快照获取失败」。
        """

        from botler.executor import (  # 动态取包级符号：测试 monkeypatch botler.executor.<名> 才能生效
            collect_env_snapshot, error_snapshot, serialize_snapshot,
        )
        task = self.db.get_task(task_id)
        if task is not None and _row_get(task, "environment"):
            return  # 已采集过（重试/续跑），保持首次快照
        try:
            snapshot = collect_env_snapshot(
                # issue #237：环境快照记录实际生效引擎/配置（仓库级覆盖 > 全局）
                engine=self._engine(self._effective_cfg(task_id)),
                workdir=workdir,
                cfg=self._effective_cfg(task_id),
            )
        except Exception as e:  # noqa: BLE001 采集失败不阻塞任务执行
            logger.warning("任务 %s 环境快照采集失败: %s", task_id, e)
            snapshot = error_snapshot()
        try:
            self.db.set_task_status(task_id, None,
                                    environment=serialize_snapshot(snapshot))
            self.db.add_log(task_id, "info",
                            "已采集任务执行环境快照（引擎/模型/起始提交/平台版本/配置哈希）")
        except Exception as e:  # noqa: BLE001 落库失败也不阻塞任务执行
            logger.warning("任务 %s 环境快照落库失败: %s", task_id, e)
    def _capture_base_sha(self, task_id: int, workdir: Path,
                         git_env: dict | None = None) -> None:
        """任务首次执行开始时记录工作区基线提交（issue #252）。

        prepare_workspace 已把工作区重置到远端默认主分支最新提交，此时
        HEAD 即「任务开始前 main 基线」；收尾时用 git diff base_sha..HEAD
        采集任务改动（相对 main 的改动文件与行数）。只采一次：重试/断点
        续跑不覆盖首次基线（同 issue #276 环境快照的首次语义），保证
        diff 边界稳定。采集失败不阻塞任务执行——无基线时评论隐藏改动
        段落（report.collect_diff_data 返回空，验收标准 3 不报错）。
        """
        task = self.db.get_task(task_id)
        if task is not None and _row_get(task, "base_sha"):
            return
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", "HEAD"],
                env=git_env, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                self.db.set_task_status(task_id, None,
                                        base_sha=result.stdout.strip())
                self.db.add_log(task_id, "info",
                                "已记录任务改动基线提交（结构化报告 diff 采集用）")
        except Exception as e:  # noqa: BLE001 采集失败不阻塞任务执行
            logger.warning("任务 %s 基线提交采集失败: %s", task_id, e)
    def _persist_engine_usage(self, task_id: int, engine: str,
                             usage: dict | None,
                             model: str | None = None) -> None:
        """把引擎采集的 token 用量落库 task_usage（issue #235）。

        usage 为 None（引擎无用量数据）→ 不写库（任务详情显示「无数据」）；
        费用估算：优先引擎自带费用（claude total_cost_usd / hermes
        session_estimated_cost_usd），否则按 config usage.pricing 单价
        估算；无单价 → estimated_cost 为 None（前端只展示 token 数）。
        落库失败仅记日志，绝不阻塞任务收尾。
        """
        try:
            record = finalize_usage(
                engine, usage, model=model,
                pricing=self.config.get().usage_pricing,
                currency=self.config.get().usage_currency)
        except Exception as e:  # noqa: BLE001 估算失败按无用量处理
            logger.warning("任务 %s 用量归一化失败: %s", task_id, e)
            record = None
        if record is None:
            return
        try:
            self.db.save_task_usage(
                task_id, engine=engine,
                model=record["model"],
                prompt_tokens=record["prompt_tokens"],
                completion_tokens=record["completion_tokens"],
                total_tokens=record["total_tokens"],
                estimated_cost=record["estimated_cost"],
                currency=record["currency"],
                raw_usage=json.dumps(record["raw_usage"], ensure_ascii=False)
                if record["raw_usage"] is not None else None)
            cost_text = (f"，估算费用 {record['estimated_cost']} "
                         f"{record['currency']}") if record["estimated_cost"] is not None else ""
            self.db.add_log(
                task_id, "info",
                f"token 用量已记录：{record['prompt_tokens']} 输入 / "
                f"{record['completion_tokens']} 输出"
                f"（模型 {record['model'] or engine}）{cost_text}")
        except Exception as e:  # noqa: BLE001 落库失败不影响任务收尾
            self.db.add_log(task_id, "warn", f"token 用量落库失败: {e}")
    def _persist_claude_usage(self, task_id: int, output: str) -> None:
        """从 claude 输出解析用量并落库（执行结束/停止/超时路径共用）。

        结果行（type=result）含 usage 字段（stream-json 与单行
        --output-format json 同构），modelUsage 提供模型名；解析失败
        （异常中断无结果行）不落库，任务详情显示「无数据」。
        """
        data = self._last_json_object(output)
        usage = parse_claude_result_usage(data)
        if usage is None:
            return
        self._persist_engine_usage(task_id, "claude", usage)
    def _run_claude_once(self, task_id: int, repo: dict, issue: dict,
                         resume_session: str | None = None) -> tuple[int, str]:
        """执行一次 claude 引擎（Claude Code CLI 无头模式）。

        resume_session 非空时为断点续跑（claude --resume 接续上次会话，
        工作区保留）；执行后解析 JSON 输出中的 session_id 落库。本方法由
        ClaudeEnginePlugin（botler.plugins.executors）委托调用。
        """
        # issue #237：仓库级任务参数覆盖——超时等取任务生效配置
        # （仓库级 > 全局，run_task 解析暂存，无覆盖时等价全局）
        cfg = self._effective_cfg(task_id)
        workdir, git_env = self.prepare_workspace(repo, resume=bool(resume_session))
        # MCP 工具注入（issue #172）：任务执行前把启用中的工具写入工作区
        # .mcp.json（Claude Code 项目级 MCP 配置），供 agent 直接调用；
        # 注入失败只记日志不阻塞任务（工具配置问题不应拖垮任务执行）
        self._inject_mcp_tools(task_id, workdir)
        self._capture_env_snapshot(task_id, workdir)
        self._capture_base_sha(task_id, workdir, git_env)
        if resume_session:
            prompt = self._resume_prompt(repo, issue, task_id)
            self.db.add_log(
                task_id, "info",
                f"恢复上次会话 {resume_session[:8]}… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 claude -p（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        env = self._build_env(repo, issue)

        log_path = self._log_file(task_id)

        cmd = [cfg.claude_command, *cfg.claude_args]
        # stream-json 输出在 claude 2.1.x 强制要求 --verbose，缺失直接报错；
        # 用户配置可能只写 --output-format stream-json，这里自动补齐
        if ("--output-format" in cmd
                and cmd[cmd.index("--output-format") + 1] == "stream-json"
                and "--verbose" not in cmd):
            cmd.append("--verbose")
        # 无人值守（-p）下跳过权限确认：GIT_ASKPASS/GITLAB_TOKEN 只解决
        # 凭据，Bash/curl/Read/MCP 等操作仍会被权限系统拦截（task_7/8/9
        # 的 permission_denials），且无人值守无法交互授权，任务必然失败。
        cmd.append("--dangerously-skip-permissions")
        if resume_session:
            cmd.extend(["--resume", resume_session])
        cmd.append(prompt)
        try:
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
        except FileNotFoundError:
            raise ExecutorError(f"找不到 claude 命令: {cfg.claude_command}（请先 npm install -g @anthropic-ai/claude-code）")

        # 注册运行中进程（issue #35：一键停止可定位并终止 claude 进程组）
        with self._proc_lock:
            self._procs[task_id] = proc
        try:
            deadline = time.time() + cfg.task_timeout_seconds
            session_known = False

            # 停止请求先于进程创建到达（worker 准备阶段收到 stop）→ 立即终止
            stopped = self._stop_requested(task_id)

            def _on_chunk(chunk: str) -> None:
                nonlocal session_known
                # 运行中即把 session_id 落库（issue #20 实时查看），
                # 只落首次：进程未结束时 API 就能定位当前会话文件。
                # stream-json 下 init 行首条即带 session_id，比 result 更早
                if not session_known and self._persist_session_from_chunk(task_id, chunk):
                    session_known = True
                # 实时事件流（SSE）：逐行解析 stream-json 输出发布到总线
                self._publish_stream_line(task_id, chunk, parse_claude_stream_line)

            if not stopped:
                timed_out, stopped, chunks = self._drain_process_output(
                    proc, task_id, log_path, deadline, _on_chunk)
            else:
                timed_out, chunks = False, []

            if stopped:
                self._kill_process_group(proc)
                proc.wait(timeout=10)
                output = "".join(chunks)
                self.db.add_log(task_id, "warn",
                                "任务被用户停止，已强制终止 claude 进程组")
                self._persist_session_id(task_id, output)
                self._persist_claude_usage(task_id, output)  # issue #235
                return STOP_EXIT_CODE, output

            if timed_out:
                self._kill_process_group(proc)
                proc.wait(timeout=10)
                output = "".join(chunks)
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止进程组")
                self._persist_session_id(task_id, output)
                self._persist_claude_usage(task_id, output)  # issue #235
                return 124, output  # 124 = timeout 约定退出码

            exit_code = proc.wait(timeout=30)
            output = "".join(chunks)
            self.db.add_log(task_id, "info", f"claude 退出码: {exit_code}")
            self._persist_session_id(task_id, output)
            self._persist_claude_usage(task_id, output)  # issue #235
            return exit_code, output
        finally:
            with self._proc_lock:
                self._procs.pop(task_id, None)

    def _inject_mcp_tools(self, task_id: int, workdir) -> None:
        """把启用中的 MCP 工具写入工作区 .mcp.json（issue #172）。

        Claude Code 启动时自动读取项目根目录 .mcp.json 注册 MCP server，
        agent 即可调用「工具页面」启用/下载/自定义的全部工具（全局生效，
        issue #172 Q4）。工具由 executor 写入并追加 .git/info/exclude
        本地忽略，不会被 agent 提交进仓库；无启用工具时清理上次注入的
        残留文件。异常仅记日志，不阻塞任务执行。
        """
        try:
            from ..tools import write_workspace_mcp_config
            path = write_workspace_mcp_config(self.db, workdir)
            if path is None:
                self.db.add_log(task_id, "info",
                                "MCP 工具：无启用中的工具，跳过注入")
            else:
                self.db.add_log(task_id, "info",
                                f"MCP 工具已注入工作区 {path.name}（"
                                f"{self._enabled_tool_count()} 个）")
        except Exception as exc:  # 工具注入失败不影响任务主体
            self.db.add_log(task_id, "warn",
                            f"MCP 工具注入失败（任务继续执行）: {str(exc)[:200]}")

    def _enabled_tool_count(self) -> int:
        """启用中的工具数量（日志展示用）。"""
        try:
            from ..tools import mcp_servers_json
            return len(mcp_servers_json(self.db)["mcpServers"])
        except Exception:
            return 0

    # ---- hermes 引擎（issue #47）----
    def _last_json_object(self, output: str) -> dict | None:
        """取输出中最后一个完整 JSON 对象（流式协议的结果行在最后）。

        claude stream-json 多行输出：最后一行是 result 事件；hermes runner
        流式输出：事件行在前，结果 JSON 收尾。旧单行协议（唯一行）同样适用。
        逐行从尾部扫描，容忍行间/行内噪音（每行内 raw_decode 取首个对象）。
        """
        if not output:
            return None
        for line in reversed(output.splitlines()):
            data = _load_json_output(line)
            if data is not None:
                return data
        return None
    def _result_line(self, output: str) -> dict | None:
        """claude 结果行或 None（异常中断时无结果行）。

        run_task 成功判定依据：stream-json 多行输出下 _load_json_output
        取首个 JSON 对象（init 行）会误判成功，必须找最后的结果行。
        判定宽松兼容两种格式：旧 --output-format json 单行结果（可能有
        type=result）与 stream-json 尾部 result 事件行——两者都带字符串
        result 字段；init/assistant/user 事件行均无该字段，不会误判。
        """
        data = self._last_json_object(output)
        if data is not None and isinstance(data.get("result"), str):
            return data
        return None
    def _hermes_history_from_output(self, output: str) -> list:
        """从 hermes runner 输出解析会话消息历史（messages 缺失/非列表 → 空列表）。"""
        data = self._last_json_object(output)
        messages = data.get("messages") if data else None
        return messages if isinstance(messages, list) else []
    def _hermes_result(self, output: str) -> str:
        """判定 hermes runner 输出：success / unresolvable / failed。

        - success：JSON 合法、error 为空、final_response 非空、未自认无法解决
        - unresolvable：final_response 命中「无法解决」表述（不重试）
        - failed：非 JSON / error 非空 / 缺 final_response（按失败重试）
        """
        data = self._last_json_object(output)
        if data is None or data.get("error"):
            return "failed"
        final_response = data.get("final_response")
        if not isinstance(final_response, str) or not final_response.strip():
            return "failed"
        if self._is_unresolvable(final_response):
            return "unresolvable"
        return "success"
    def _hermes_resume_data(self, raw: str | None) -> tuple[list | None, str | None]:
        """解析任务落库的 hermes_history（{"session_id", "messages"} JSON）。

        解析失败 / 为空 / messages 非列表 → (None, None)（降级全新会话）。
        """
        if not raw or not raw.strip():
            return None, None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        messages = data.get("messages")
        session_id = data.get("session_id")
        return (messages if isinstance(messages, list) and messages else None,
                session_id if isinstance(session_id, str) and session_id else None)
    def _persist_hermes_history(self, task_id: int, output: str) -> None:
        """执行结束后把 hermes 会话数据（session_id + messages）落库（Q3-B）。

        messages 为空时不落库（无从恢复，保持上次值）；落库失败不影响任务收尾。
        """
        data = self._last_json_object(output)
        messages = self._hermes_history_from_output(output)
        if data is None or not messages:
            return
        session_id = data.get("session_id")
        raw = json.dumps(
            {"session_id": session_id if isinstance(session_id, str) else "",
             "messages": messages},
            ensure_ascii=False)
        try:
            self.db.set_task_status(task_id, None, hermes_history=raw)
        except Exception as e:  # noqa: BLE001 历史落库失败不阻塞任务收尾
            self.db.add_log(task_id, "warn", f"hermes 会话历史落库失败: {e}")
    def _run_hermes_once(self, task_id: int, repo: dict, issue: dict,
                         resume_history: list | None,
                         resume_session_id: str | None = None) -> tuple[int, str]:
        """执行一次 hermes 引擎（hermes agent SDK 进程内调用）。返回 (exit_code, output)。

        issue #171：集成方式从「子进程 + 部署机独立 hermes venv
        （hermes.command/hermes.args）」改为「hermes agent SDK 进程内
        集成」（对齐 dsh 引擎的 SDK 方式，issue #84）：worker 线程跑
        HermesSdkRunner（run_agent.AIAgent），主循环轮询停止请求与超时；
        停止/超时通过 runner.stop()（AIAgent.interrupt()，跨线程安全）
        请求中断并通知进行中的工具提前终止，语义等价旧模式的 SIGKILL
        进程组。输出协议不变（事件行 + 结果行），SSE 解析
        （parse_hermes_event_line）与结果判定（_hermes_result）、会话
        落库（hermes_history）直接复用。
        """

        from botler.executor import (  # 动态取包级符号：测试 monkeypatch botler.executor.<名> 才能生效
            HermesSdkRunner, HermesSdkNotInstalledError,
        )
        # issue #237：仓库级任务参数覆盖——超时等取任务生效配置
        cfg = self._effective_cfg(task_id)
        workdir, _git_env = self.prepare_workspace(repo, resume=bool(resume_history))
        self._capture_env_snapshot(task_id, workdir)
        self._capture_base_sha(task_id, workdir, _git_env)
        if resume_history:
            prompt = self._resume_prompt(repo, issue, task_id)
            self.db.add_log(
                task_id, "info",
                f"恢复上次 hermes 会话（{len(resume_history)} 条历史）… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 hermes 引擎（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        env = self._build_env(repo, issue)

        log_path = self._log_file(task_id)
        lines: list[str] = []
        log_f = open(log_path, "w", encoding="utf-8", errors="replace")

        def _on_line(line: str) -> None:
            """worker 线程回调：写日志 + 收行 + 发布 SSE（单线程顺序调用）。"""
            # 统一日志脱敏（issue #259）：事件行落盘前打码
            log_f.write(redact(line) + "\n")
            log_f.flush()
            lines.append(line)
            self._publish_stream_line(task_id, line, parse_hermes_event_line)

        try:
            try:
                runner = HermesSdkRunner(
                    prompt=prompt,
                    session_id=resume_session_id,
                    history=resume_history,
                    task_id=str(task_id),
                    cwd=str(workdir),
                    env=env,
                    on_line=_on_line,
                )
                runner.start()
            except HermesSdkNotInstalledError as e:
                raise ExecutorError(str(e))

            deadline = time.time() + cfg.task_timeout_seconds
            timed_out = False
            stopped = False
            while not runner.done():
                if self._stop_requested(task_id):
                    stopped = True
                    runner.stop()
                    break
                if time.time() >= deadline:
                    timed_out = True
                    runner.stop()
                    break
                time.sleep(0.05)
            exit_code = runner.finish()
            # 事件行拼接必须保留换行分隔（与日志落盘 line + "\n" 一致）：
            # _last_json_object 按行扫描解析结果行，缺换行会误判 failed
            # （issue #119 dsh 同类问题）
            output = "\n".join(lines)

            if stopped:
                self.db.add_log(task_id, "warn",
                                "任务被用户停止，已请求 hermes 中断")
                # getattr 防御：旧 runner / 测试假 runner 可能无 usage/model
                self._persist_engine_usage(  # issue #235
                    task_id, "hermes", getattr(runner, "usage", None),
                    model=getattr(runner, "model", ""))
                return STOP_EXIT_CODE, output

            if timed_out:
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已请求 hermes 中断")
                self._persist_engine_usage(  # issue #235
                    task_id, "hermes", getattr(runner, "usage", None),
                    model=getattr(runner, "model", ""))
                return 124, output  # 124 = timeout 约定退出码

            self.db.add_log(task_id, "info", f"hermes 引擎退出码: {exit_code}")
            self._persist_engine_usage(  # issue #235
                task_id, "hermes", getattr(runner, "usage", None),
                model=getattr(runner, "model", ""))
            return exit_code, output
        finally:
            log_f.close()

    # dsh 引擎凭据回退的 OpenAI 兼容 provider 白名单（issue #395）：设置页
    # 「AI 供应商」中配第三方中转站时 provider 通常选 openai / custom 等
    # OpenAI 兼容类型，此前仅回退 provider=deepseek 导致中转站 key 未被
    # 消费（任务 #569 no API key）。deepseek 仍优先（issue #115），其余
    # 按 ai_providers 列表顺序取第一个启用项作中转站回退源。
    _DSH_OPENAI_COMPAT_PROVIDERS = frozenset({
        "deepseek", "openai", "custom", "siliconflow", "openrouter",
        "moonshot", "qwen", "zhipu", "ollama",
    })

    # ---- dsh 引擎（issue #84）----
    def _dsh_credentials(self, cfg) -> tuple[str | None, str | None, str | None]:
        """dsh 引擎 API Key / Base URL / 模型解析链，返回 (api_key, base_url, model)。

        优先级：dsh 段显式配置 > 设置页「AI 供应商」中 provider=deepseek
        且 enabled 的项（issue #115）> 设置页「AI 供应商」中其他 OpenAI
        兼容 provider（openai / custom 等，issue #395）> 环境变量
        DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL（SDK 默认读取，botler 不
        覆盖，返回 None 即由 SDK 兜底）。

        model 语义（issue #397）：dsh 段未显式配置 model 且凭据回退到
        某个 AI 供应商时，返回该供应商配置的 model（此前模型固定取
        dsh_model 默认 deepseek-v4-flash，ai_providers 里配的模型从未
        传给 dsh 引擎——任务 #581/#397「配置了 deepseek-v4-pro 最终却
        调用 deepseek-v4-flash」根因）；其余情况返回 None，调用方直接
        透传 cfg.dsh_model（dsh 段显式模型优先）。

        issue #115 根因：任务 #194 #195 切 dsh 引擎后全部失败——用户
        只在设置页「AI 供应商」配过 DeepSeek key，dsh 段未配、部署机
        环境无 DEEPSEEK_API_KEY，SDK 无 key 可用 → DeepSeek API 401
        AUTH → finish_reason=error → 判失败。key 已在平台上配过却不
        消费，属于配置链路断裂，在此补齐回退。

        issue #395 根因：任务 #569 在设置页配置第三方中转站（base_url /
        api_key）后 dsh 引擎仍报 `no API key for provider route
        "deepseek-official"`——第三方中转站通常选 OpenAI 兼容类型
        （provider=openai / custom），此前只回退 provider=deepseek 的项，
        中转站配置匹配不到，key 未被消费；中转站本身可用（其他 agent 里
        使用完全没问题），纯属 dsh 凭据回退范围过窄。这里把回退扩大到
        所有 OpenAI 兼容 provider（deepseek 仍优先，保持 issue #115 语义）。
        """
        api_key = cfg.dsh_api_key or None
        base_url = cfg.dsh_base_url or None
        model = None  # issue #397：跟随供应商的模型（dsh 段显式模型由调用方透传）
        if api_key is None:
            # OpenAI 兼容 provider 白名单（issue #395）：deepseek 优先，
            # 其余（openai / custom / siliconflow 等）作为中转站回退源
            candidates = [
                p for p in (getattr(cfg, "ai_providers", None) or [])
                if isinstance(p, dict)
                and str(p.get("provider", "")).strip()
                in self._DSH_OPENAI_COMPAT_PROVIDERS
                and bool(p.get("enabled", True))
                and str(p.get("api_key", "") or "").strip()
            ]
            # 优先 provider=deepseek（issue #115），否则取列表第一个
            # 启用的 OpenAI 兼容中转站项（issue #395）
            provider = next(
                (p for p in candidates
                 if str(p.get("provider", "")).strip() == "deepseek"),
                None) or (candidates[0] if candidates else None)
            if provider is not None:
                api_key = str(provider.get("api_key", "")).strip() or None
                base_url = base_url or (
                    str(provider.get("base_url", "") or "").strip() or None)
                # issue #397：dsh 段未显式配置模型时，模型名跟随选中
                # 供应商（供应商未配 model 则保持 None，调用方回退默认）
                if not getattr(cfg, "dsh_model_explicit", False):
                    model = (str(provider.get("model", "") or "").strip()
                             or None)
        return api_key, base_url, model
    def _run_dsh_once(self, task_id: int, repo: dict, issue: dict,
                      resume_session: str | None = None) -> tuple[int, str]:
        """执行一次 dsh 引擎（deepseek-harness SDK 进程内调用）。返回 (exit_code, output)。

        与 claude/hermes 不同，SDK 在 botler 进程内运行：worker 线程跑
        harness.run()（DshRunner），本循环轮询停止请求与超时；停止/超时
        通过 runner.stop() 关闭运行时强制终止（语义等价 SIGKILL 进程组）。
        输出协议与 hermes 对齐（事件行 + 结果行），SSE 解析
        （parse_hermes_event_line）与结果判定（_dsh_result）复用。
        会话 id 执行后落库 dsh_session_id（断点续跑，含停止/超时路径）。
        """

        from botler.executor import (  # 动态取包级符号：测试 monkeypatch botler.executor.<名> 才能生效
            DshRunner, DshSdkNotInstalledError,
        )
        # issue #237：仓库级任务参数覆盖——超时等取任务生效配置
        cfg = self._effective_cfg(task_id)
        workdir, _git_env = self.prepare_workspace(repo, resume=bool(resume_session))
        self._capture_env_snapshot(task_id, workdir)
        self._capture_base_sha(task_id, workdir, _git_env)
        # issue #281 §4.7：resume 前校验会话可恢复性——session_root 目录
        # 已配置但不存在 = 会话必然丢失，如实降级为全新会话（不假装「对话
        # 已保留」）；未配置 session_root 时无法校验，按可恢复处理（现状）。
        if resume_session and not self._dsh_session_available(cfg, resume_session):
            self.db.set_task_status(task_id, None, dsh_session_id=None)
            self.db.add_log(
                task_id, "warn",
                f"上次 dsh 会话 {resume_session[:8]}… 的会话目录已不存在，"
                f"降级为全新会话（issue #281 诚实降级）")
            resume_session = None
        # issue #281 §4.7：会话 id 任务开始即落库（先落 id 再开跑）——
        # 新建任务预生成 id 并原子写库，任何时刻被强杀/重启 id 都已落库
        # 可恢复；恢复场景直接复用已落库 id。写入失败 = 任务失败（不静默
        # 降级，避免「以为能恢复、实际不能」）。
        if resume_session:
            dsh_sid = resume_session
        else:
            dsh_sid = self._new_dsh_session_id(task_id)
            try:
                self.db.set_task_status(task_id, None, dsh_session_id=dsh_sid)
            except Exception as e:  # noqa: BLE001 前置落库失败 = 任务失败
                raise ExecutorError(f"dsh 会话 id 前置落库失败: {e}") from e
            self.db.add_log(task_id, "info",
                            f"已预生成 dsh 会话 id {dsh_sid[:8]}… 并落库"
                            f"（任务开始即落库，issue #281）")
        if resume_session:
            prompt = self._resume_prompt(repo, issue, task_id)
            self.db.add_log(
                task_id, "info",
                f"恢复上次 dsh 会话 {resume_session[:8]}… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 dsh 引擎（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        # issue #281 §4.1：dsh 提示词追加「进度上报约定」节（Phase 1 仅
        # dsh 引擎解析落库 [PROGRESS] 里程碑，claude/hermes 不受影响）。
        prompt += PROGRESS_REPORT_INSTRUCTION
        env = self._build_env(repo, issue)

        # issue #146：dsh 引擎提示词持久化 + 聊天记录落库（dsh_transcript）。
        # claude 引擎的提示词/聊天记录来自会话 jsonl（首条 user 消息 +
        # user/assistant/tool 行）；dsh SDK 会话文件是 runtime 内部格式，
        # 无法像 jsonl 那样解析。这里在 executor 侧把 prompt 与事件行累积
        # 出的消息落库，execution 接口读取返回——dsh 任务「查看提示词」
        # 与聊天记录不再显示「提示词未持久化 / 暂无聊天记录」。
        def _dsh_utc_ts() -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # 断点续跑：resume 时保留上次会话历史，追加本次恢复引导语为新
        # user 消息（与 SDK 会话内的真实输入一致）；fresh 从提示词开始。
        dsh_messages: list[dict] = (
            self._dsh_resume_messages(task_id) if resume_session else [])
        dsh_messages.append({"role": "user", "text": prompt,
                             "ts": _dsh_utc_ts(), "truncated": False})
        dsh_pending: list[str] = []  # 当前 assistant 回复的流式文本片段

        def _dsh_flush_pending() -> None:
            """把累积的 assistant 流式文本收口为一条聊天消息（空则跳过）。"""
            if not dsh_pending:
                return
            text = "".join(dsh_pending)
            dsh_pending.clear()
            if not text:
                return
            cut_text, cut = _truncate_text(text, _TRANSCRIPT_MAX_TEXT)
            dsh_messages.append({"role": "assistant", "text": cut_text,
                                 "ts": _dsh_utc_ts(), "truncated": cut})
            # issue #281 §4.1：assistant 文本收口时扫描 [PROGRESS] 里程碑
            # 落库 task_progress（增量落库，中断/强杀时已收口部分不丢）
            self._persist_progress_markers(task_id, text)

        def _dsh_accumulate(line: str) -> None:
            """从事件行累积聊天消息（issue #146）。

            stream_delta → assistant 文本片段；tool_start → 工具调用；
            thinking/status/raw 与结果行不展示（思考/状态/簿记已在事件流
            SSE 实时呈现，与 claude transcript 只保留 text 对齐），但会
            先收口当前回复片段保序。
            """
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                return
            if not isinstance(data, dict):
                return
            event = data.get("event")
            if event == "stream_delta":
                text = data.get("text")
                if isinstance(text, str) and text:
                    dsh_pending.append(text)
            elif event == "tool_start":
                _dsh_flush_pending()
                dsh_messages.append({
                    "role": "tool",
                    "tool": data.get("tool", "?"),
                    "input": data.get("input"),
                    "ts": _dsh_utc_ts()})
            elif event in ("status", "raw") or "final_response" in data:
                _dsh_flush_pending()  # 回合收口：先收口当前回复片段

        def _dsh_persist() -> None:
            """落库当前消息列表（每次事件行后调用，运行中实时可见）。

            进行中的流式回复片段（dsh_pending）不强制收口——半截文本不进
            聊天记录（与 claude 会话 jsonl 写完完整行才落盘一致），实时
            增量由事件流 SSE 呈现；收口只发生在 tool_start / status /
            raw / 结果行与执行结束时。
            """
            self._persist_dsh_transcript(task_id, prompt, dsh_messages)

        # 提示词先落库：runner 启动前（执行中）「查看提示词」/聊天记录
        # 首条 user 消息即可用，不等执行结束
        self._persist_dsh_transcript(task_id, prompt, dsh_messages)

        log_path = self._log_file(task_id)
        lines: list[str] = []
        log_f = open(log_path, "w", encoding="utf-8", errors="replace")

        def _on_line(line: str) -> None:
            """worker 线程回调：写日志 + 收行 + 发布 SSE + 累积聊天记录（单线程顺序调用）。"""
            # 统一日志脱敏（issue #259）：事件行落盘前打码
            log_f.write(redact(line) + "\n")
            log_f.flush()
            lines.append(line)
            events = parse_hermes_event_line(line)
            if events:
                for event in events:
                    self._publish_event(task_id, event)
            _dsh_accumulate(line)
            _dsh_persist()

        # issue #235：捕获本轮 runner 供执行结束后读 token 用量（runner
        # 内部在 worker 完成后聚合 usage；碰撞重跑后指向最新一轮 runner）
        last_runner: DshRunner | None = None

        # issue #115/#395/#397：dsh 段未配 key 时回退设置页「AI 供应商」
        # （deepseek 项优先，其余 OpenAI 兼容中转站兜底），模型跟随选中
        # 供应商（issue #397：dsh 段未显式配置模型时不再固定默认 flash）
        dsh_api_key, dsh_base_url, dsh_model = self._dsh_credentials(cfg)
        runner_model = dsh_model or cfg.dsh_model

        def _run_round(session_id: str, round_prompt: str) -> tuple[int, bool, bool]:
            """跑一轮 dsh：构造 runner、启动、等待完成（停止/超时强制终止）。

            返回 (exit_code, stopped, timed_out)；SDK 未安装抛
            ExecutorError（run_task 捕获重试）。
            """
            nonlocal last_runner
            try:
                runner = DshRunner(
                    prompt=round_prompt, session_id=session_id,
                    provider=cfg.dsh_provider, model=runner_model,
                    max_tokens=cfg.dsh_max_tokens,
                    # 推理等级（issue #123）：dsh.reasoning_effort 经
                    # DshRunner 派生 Cordis 注入 SDK，空串 = 不设置
                    reasoning_effort=cfg.dsh_reasoning_effort,
                    cwd=str(workdir),
                    session_root=cfg.dsh_session_root or None,
                    cordis=cfg.dsh_cordis or None,
                    runtime_bin=cfg.dsh_runtime_bin or None,
                    base_url=dsh_base_url,
                    api_key=dsh_api_key,
                    env=env, on_line=_on_line)
                runner.start()
                last_runner = runner
            except DshSdkNotInstalledError as e:
                raise ExecutorError(str(e))

            deadline = time.time() + cfg.task_timeout_seconds
            round_timed_out = False
            round_stopped = False
            while not runner.done():
                if self._stop_requested(task_id):
                    round_stopped = True
                    runner.stop()
                    break
                if time.time() >= deadline:
                    round_timed_out = True
                    runner.stop()
                    break
                time.sleep(0.05)
            return runner.finish(), round_stopped, round_timed_out

        # issue #302：dsh runtime 会话持久化默认 zstd 压缩，且启动时会做
        # 根级编码检查——会话根目录残留旧版部署遗留的明文 session.jsonl
        # 会让整个 runtime 拒绝启动（encodingMismatch，任务 #415 反复失败
        # 的根因）。每次执行前把会话根目录归一化到 zstd（转换并删除明文
        # 遗留文件），保证新会话与断点续跑都能正常启动。
        try:
            session_root = effective_session_root(cfg.dsh_session_root, workdir)
            fixed = normalize_session_root_encoding(session_root)
            if fixed:
                self.db.add_log(
                    task_id, "info",
                    f"dsh 会话根目录 {session_root} 归一化到 zstd 压缩："
                    f"转换/清理 {fixed} 个遗留明文 session.jsonl（issue #302）")
        except Exception as e:  # noqa: BLE001 归一化失败不阻塞执行（runtime 会自报错）
            self.db.add_log(
                task_id, "warn",
                f"dsh 会话根目录编码归一化失败（继续执行）: {e}")

        try:
            exit_code, stopped, timed_out = _run_round(dsh_sid, prompt)
            # issue #291：SDK 会话 id collision（磁盘残留与 live 会话不
            # 匹配）→ 会话实际不可恢复，如实降级为全新会话重跑一次。
            # 背景：dsh SDK 0.1.0rc6 的 runtime 要求跨进程 resume 的输入
            # 与磁盘已持久化事件前缀逐事件一致（seq-aligned 重放），
            # botler 恢复引导语必然不匹配 → 每次 resume 必 collision，
            # 旧逻辑交给重试循环后仍复用同一落库 id 反复撞，重试耗尽
            # 任务失败（任务 #388/#390/#391）。降级不无限递归：新 id 无
            # 磁盘残留，重跑再撞则如实失败（防死循环）。
            output = "\n".join(lines)
            # issue #291：SDK 会话 id collision → 降级全新会话；
            # issue #401：会话文件损坏（tool 消息缺 callId）续跑重放报
            # 「message must have tool source」→ 同样不可恢复，一并降级。
            # 二者都不应交给重试循环反复 resume 同一坏会话（每次必报同样
            # 错误，重试耗尽任务失败——任务 #388/#390/#391/#581/#582）。
            collision = self._dsh_collision(output)
            corrupted = self._dsh_corrupted_session(output)
            if (not stopped and not timed_out
                    and (collision or corrupted)):
                old_sid = dsh_sid
                dsh_sid = self._new_dsh_session_id(task_id)
                self.db.set_task_status(
                    task_id, None, dsh_session_id=dsh_sid)
                if corrupted:
                    degrade_reason = (
                        "会话文件损坏（tool 消息缺少 callId，续跑重放报 "
                        "message must have tool source）")
                    self.db.add_log(
                        task_id, "warn",
                        f"SDK 报告会话 {old_sid[:8]}… 无法恢复（tool source "
                        f"缺失，会话文件损坏），降级为全新会话 "
                        f"{dsh_sid[:8]}… 重跑（issue #401 诚实降级）")
                else:
                    degrade_reason = "id collision"
                    self.db.add_log(
                        task_id, "warn",
                        f"SDK 报告会话 {old_sid[:8]}… 无法恢复（id collision，"
                        f"磁盘残留与 live 会话不匹配），降级为全新会话 "
                        f"{dsh_sid[:8]}… 重跑（issue #291 诚实降级）")
                prompt = (self._dsh_downgrade_prompt(
                              repo, issue, task_id, reason=degrade_reason)
                          + PROGRESS_REPORT_INSTRUCTION)
                # 聊天记录重置为全新会话视角（首条 user 消息 = 新提示词）；
                # 流式回复缓冲一并清空（碰撞轮无文本，防御性收口）
                _dsh_flush_pending()
                dsh_messages = [{
                    "role": "user", "text": prompt,
                    "ts": _dsh_utc_ts(), "truncated": False}]
                self._persist_dsh_transcript(task_id, prompt, dsh_messages)
                exit_code, stopped, timed_out = _run_round(dsh_sid, prompt)

            # issue #119：事件行拼接必须保留换行分隔（与日志落盘 line + "\n"
            # 一致）。DshRunner 的 on_line 回调行尾无换行，若用 ''.join 拼接，
            # output 整串无换行 → _last_json_object 按行扫描只解析到首个事件
            # 对象（finish_reason 缺失）→ _dsh_result 误判 failed → 触发重试；
            # _persist_dsh_session_id 同样解析不到 session_id → 断点续跑失效
            # → 每次重试都是全新会话（重复开发任务），重试耗尽后任务显示失败
            # （任务 #198 #199 日志：引擎 exit 0、结果行 completed 仍失败）。
            output = "\n".join(lines)
            _dsh_flush_pending()  # 收口最后一段回复
            _dsh_persist()  # 最终落库（停止/超时/正常共用）

            if stopped:
                self.db.add_log(task_id, "warn",
                                "任务被用户停止，已强制终止 dsh 运行时")
                self._persist_dsh_session_id(task_id, output)
                self._persist_engine_usage(  # issue #235
                    task_id, "dsh",
                    getattr(last_runner, "usage", None) if last_runner else None,
                    model=runner_model)
                return STOP_EXIT_CODE, output

            if timed_out:
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止 dsh 运行时")
                self._persist_dsh_session_id(task_id, output)
                self._persist_engine_usage(  # issue #235
                    task_id, "dsh",
                    getattr(last_runner, "usage", None) if last_runner else None,
                    model=runner_model)
                return 124, output  # 124 = timeout 约定退出码

            self.db.add_log(task_id, "info", f"dsh 引擎退出码: {exit_code}")
            self._persist_dsh_session_id(task_id, output)
            self._persist_engine_usage(  # issue #235
                task_id, "dsh",
                getattr(last_runner, "usage", None) if last_runner else None,
                model=runner_model)
            return exit_code, output
        finally:
            log_f.close()
    def _dsh_result(self, output: str) -> str:
        """判定 dsh runner 输出：success / unresolvable / failed。

        - success：结果行合法、无 error、finish_reason=completed、
          final_response 非空、未自认无法解决
        - unresolvable：final_response 命中「无法解决」表述（不重试）
        - failed：error 非空 / finish_reason 非 completed（max-tokens 截断、
          error、无回合结束、未知 reason 一律不静默成功）/ 非 JSON /
          final_response 空 → 按失败重试
        """
        data = self._last_json_object(output)
        if data is None or data.get("error"):
            return "failed"
        if data.get("finish_reason") != "completed":
            return "failed"
        final_response = data.get("final_response")
        if not isinstance(final_response, str) or not final_response.strip():
            return "failed"
        if self._is_unresolvable(final_response):
            return "unresolvable"
        return "success"
    def _dsh_collision(self, output: str) -> bool:
        """识别 SDK 会话 id collision（issue #291）：结果行 error 且输出含
        「id collision」特征（runtime 报「already has a persisted log on
        disk that does not match this live session (id collision)」等变体）。

        跨进程 resume 在 dsh SDK 0.1.0rc6 下必撞该错误（seed 必须与磁盘
        已持久化事件前缀逐事件一致），命中即会话不可恢复，应降级全新会话，
        不应交给重试循环反复撞同一 id。
        """
        if "id collision" not in output:
            return False
        data = self._last_json_object(output)
        return bool(data and not data.get("error")
                    and data.get("finish_reason") == "error")
    def _dsh_corrupted_session(self, output: str) -> bool:
        """识别会话文件损坏导致续跑失败（issue #401）：结果行 error 且
        输出含「message must have tool source」特征（runtime 重放会话时
        发现 tool 消息缺 callId——任务 #581/#582 会话文件里持久化了空
        callId 的 tool 消息）。

        命中即会话不可恢复（与 id collision 同性质），应降级全新会话，
        不应交给重试循环反复 resume 同一损坏会话（每次必报同样错误，
        重试耗尽任务失败）。
        """
        if "must have tool source" not in output:
            return False
        data = self._last_json_object(output)
        return bool(data and not data.get("error")
                    and data.get("finish_reason") == "error")
    def _dsh_downgrade_prompt(self, repo: dict, issue: dict,
                              task_id: int,
                              reason: str = "id collision") -> str:
        """会话不可恢复降级后的全新会话提示词（issue #291 补充，
        issue #401 扩展 reason 描述）：基础任务提示词 + 进度账本交接单。

        降级丢的只是对话历史（SDK id collision / 会话文件损坏无法
        恢复），task_progress 账本（运行中增量落库，跨会话持久化）与
        保留的工作区是可靠的——如实说明后引导新会话按账本接续，禁止
        重做已标记 done 的步骤，避免全新对话从头重复实现（issue #281
        用户抱怨的原始痛点）。reason 用于向新会话说明上次失败原因。
        """
        handoff = self._render_progress_handoff(task_id)
        return (self._build_prompt(repo, issue)
                + "\n\n【会话恢复失败，全新会话接续】上次 dsh 会话因 SDK "
                f"限制无法恢复（{reason}），对话历史已丢失；但平台进度"
                "账本与保留的工作区是可靠的，按以下记录直接接续：\n"
                + handoff)
    def _new_dsh_session_id(self, task_id: int) -> str:
        """预生成 dsh 会话 id（issue #281 §4.7）：botler-<task_id>-<ts>-<rand>。

        SDK `run(session_id=<id>)` 支持以指定 id 创建全新会话（用户确认），
        任务开始前即生成并落库，强杀/重启后凭已落库 id 经 SDK resume 续跑。
        """
        return (f"botler-{task_id}-"
                f"{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-"
                f"{secrets.token_hex(4)}")
    def _dsh_session_available(self, cfg, session_id: str) -> bool:
        """dsh 会话可恢复性校验（§4.7）：session_root 目录存在才可恢复。

        SDK 以 DSH_SESSION_ROOT 为会话持久化根目录；已配置但目录不存在 =
        会话必然已丢失（重部署重建目录/未挂载），如实降级为全新会话；
        未配置 session_root（无法校验，按可恢复处理，保持现状）或目录存在
        （信任 SDK 按 id 定位会话）时返回 True。
        """
        root = (getattr(cfg, "dsh_session_root", "") or "").strip()
        if not root:
            return True
        return Path(root).is_dir()
    def _persist_dsh_session_id(self, task_id: int, output: str) -> None:
        """执行结束后把 dsh 会话 id 落库（停止/超时/失败均落，供断点续跑）。

        结果行缺 session_id 或输出非法时保持旧值；落库失败不影响任务收尾。
        """
        data = self._last_json_object(output)
        sid = data.get("session_id") if data else None
        if not isinstance(sid, str) or not sid:
            return
        try:
            self.db.set_task_status(task_id, None, dsh_session_id=sid)
        except Exception as e:  # noqa: BLE001 会话 id 落库失败不阻塞任务收尾
            self.db.add_log(task_id, "warn", f"dsh 会话 id 落库失败: {e}")
    def _dsh_resume_messages(self, task_id: int) -> list[dict]:
        """断点续跑：读取上次落库的 dsh 聊天记录消息列表（issue #146）。

        解析失败 / 无记录返回空列表（降级全新会话，与 _hermes_resume_data
        的容错语义一致）。
        """
        task = self.db.get_task(task_id)
        raw = _row_get(task, "dsh_transcript") if task is not None else None
        if not raw or not str(raw).strip():
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []
    def _persist_dsh_transcript(self, task_id: int, prompt: str,
                                messages: list[dict]) -> None:
        """把 dsh 聊天记录（prompt + messages）落库（issue #146）。

        messages 超上限时截断：保留首条（提示词 user 消息）与最后
        _TRANSCRIPT_MAX_MESSAGES-1 条并置 truncated，与 claude
        parse_transcript 截断语义一致；落库失败不影响任务收尾。
        """
        truncated = False
        if len(messages) > _TRANSCRIPT_MAX_MESSAGES:
            head = messages[:1]
            keep = _TRANSCRIPT_MAX_MESSAGES - 1
            messages = head + (messages[-keep:] if keep > 0 else [])
            truncated = True
        raw = json.dumps(
            {"prompt": prompt or "", "messages": messages,
             "truncated": bool(truncated)},
            ensure_ascii=False)
        try:
            self.db.set_task_status(task_id, None, dsh_transcript=raw)
        except Exception as e:  # noqa: BLE001 聊天记录落库失败不阻塞任务收尾
            self.db.add_log(task_id, "warn", f"dsh 聊天记录落库失败: {e}")
    def _publish_event(self, task_id: int, event: dict) -> None:
        """归一化事件补 seq/ts 后发布到总线（SSE 实时推送）。"""
        seq = self._seq.get(task_id, 0) + 1
        self._seq[task_id] = seq
        event["seq"] = seq
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.event_bus.publish(task_id, event)
    def _publish_stream_line(self, task_id: int, chunk: str, parser) -> None:
        """把一行引擎输出解析为归一化事件并发布（解析失败/无事件静默跳过）。"""
        events = parser(chunk.strip())
        if not events:
            return
        for event in events:
            self._publish_event(task_id, event)
    def _persist_session_id(self, task_id: int, output: str) -> None:
        """执行结束后把 claude 会话 id 落库（供下次重试 / 平台重启断点续跑）。"""
        session_id = self._extract_session_id(output)
        if session_id:
            self.db.set_task_status(task_id, None, claude_session_id=session_id)
    def _persist_session_from_chunk(self, task_id: int, chunk: str) -> bool:
        """运行中首次发现 session_id 即落库（issue #20 实时查看聊天记录）。

        此前 session_id 只在执行完全结束后才落库，任务 running 期间 API
        拿不到当前会话，无法实时读聊天记录；这里在读循环里每行检测，
        首次解析到即落库（幂等，与结束后落库同一值）。
        """
        session_id = self._extract_session_id(chunk)
        if session_id:
            self.db.set_task_status(task_id, None, claude_session_id=session_id)
            return True
        return False
    def _is_unresolvable(self, output: str) -> bool:
        return bool(_UNRESOLVABLE_RE.search(output))
    def _result_text(self, output: str) -> str:
        """提取引擎最终回复文本（claude result 行 / hermes final_response）。

        输出非 JSON 时原样返回（与 _extract_error 的容错一致）。
        """
        data = self._last_json_object(output)
        if data is None:
            return output
        if isinstance(data.get("result"), str):
            return _decode_escapes(data["result"])
        if isinstance(data.get("final_response"), str):
            return data["final_response"]
        return output
    def _output_ends_with_question(self, output: str, window: int = 400) -> bool:
        """最终回复结尾是否停在「等待用户决策」的提问上（issue #67）。

        只看结尾 window 字符：提问信号出现在回复末尾才代表 Claude 停在
        提问节点；中间提到过「请选择」但结尾是完成汇报的不算。
        """
        text = self._result_text(output)
        if not text:
            return False
        return bool(DECISION_QUESTION_RE.search(text[-window:]))
    def _extract_question(self, output: str, max_chars: int = 1000) -> str:
        """提取最终回复中的提问段落（issue #67 反馈到 issue 评论用）。

        从最后一个提问信号的所在行开始截到结尾（提问连同其上下文一起
        反馈，用户才能理解要决策什么）；无提问信号时取回复尾部。
        """
        text = self._result_text(output)
        if not text:
            return ""
        match = None
        for match in DECISION_QUESTION_RE.finditer(text):
            pass
        start = 0
        if match is not None:
            start = text.rfind("\n", 0, match.start()) + 1
        return text[start:][:max_chars]
    def _extract_error(self, output: str, max_chars: int = 3000) -> str:
        """从一次执行的输出中提取错误信息（trace 优先，否则取尾部）。

        claude -p --output-format json 时输出为 JSON，核心内容在 result 字段；
        result 内若含 Python Traceback 则从其起始处截取（异常堆栈对调试最有价值）。
        result 里嵌套序列化的转义（\\n 等字面量）先解码，保证展示可读（issue #16）。
        """
        if not output:
            return ""
        text = output
        data = self._last_json_object(output)
        if data is not None and isinstance(data.get("result"), str):
            text = _decode_escapes(data["result"])
        idx = text.rfind("Traceback (most recent call last)")
        if idx != -1:
            text = text[idx:]
        return text[-max_chars:]
    def _issue_state(self, project_id: int, iid: int,
                     repo: dict | None = None) -> str:
        try:
            issue, _ = self._call_with_fallback(
                repo, lambda c: c.get_issue(project_id, iid))
            return issue.get("state", "unknown")
        except GitLabError as e:
            return f"error: {e}"
    def _wait_pipeline_for_commit(self, task_id: int, project_id: int,
                                  commit_sha: str,
                                  repo: dict | None = None,
                                  detect_timeout: float | None = None,
                                  wait_timeout: float | None = None) -> str:
        """等待 commit_sha 触发的 CI 流水线到终态（issue #40）。

        任务 #63 缺陷：claude push 代码后退出，平台立即把任务判 succeeded，
        此时流水线还在运行（#63 于 13:31:45 收尾，流水线到 13:48:34 才结束）。
        现在成功收尾前先等流水线终态：

        - 探测窗口（默认 ci_wait_detect_seconds）：GitLab 收到 push 即创建
          流水线记录，窗口内找到 sha 匹配的最新流水线就进入终态等待；
          窗口内始终无匹配 → "no_pipeline"（仓库无 CI），调用方不等待；
        - 等待阶段（默认 ci_wait_timeout_seconds 总上限）：轮询到
          success/failed/canceled/skipped 任一终态即返回该状态；
          超上限仍非终态 → "timeout"；
        - 任一阶段收到用户停止请求 → "stopped"。

        detect_timeout / wait_timeout 仅测试注入用（None 时用配置默认值）。
        """
        cfg = self.config.get()
        detect = detect_timeout if detect_timeout is not None else cfg.ci_wait_detect_seconds
        total = wait_timeout if wait_timeout is not None else cfg.ci_wait_timeout_seconds
        deadline = time.time() + total
        detect_deadline = time.time() + min(detect, total)

        pipeline: dict | None = None
        while time.time() < detect_deadline:
            if self._stop_requested(task_id):
                return "stopped"
            try:
                latest, _ = self._call_with_fallback(
                    repo, lambda c: c.get_latest_pipeline(project_id))
            except GitLabError as e:
                self.db.add_log(task_id, "warn", f"查询最新流水线失败: {e}")
                return "no_pipeline"
            if latest is not None and latest.get("sha") == commit_sha:
                pipeline = latest
                break
            time.sleep(cfg.ci_wait_interval_seconds)
        if pipeline is None:
            return "no_pipeline"

        self.db.add_log(task_id, "info",
                        f"发现任务提交触发的流水线 #{pipeline['id']}，等待其到达终态…")
        while time.time() < deadline:
            if self._stop_requested(task_id):
                return "stopped"
            status = pipeline.get("status")
            if status in PIPELINE_TERMINAL_STATES:
                self.db.add_log(task_id, "info", f"CI 流水线 #{pipeline['id']} 终态: {status}")
                return status
            time.sleep(cfg.ci_wait_interval_seconds)
            try:
                pipeline, _ = self._call_with_fallback(
                    repo, lambda c: c.get_pipeline(project_id, pipeline["id"]))
            except GitLabError as e:
                self.db.add_log(task_id, "warn", f"查询流水线 #{pipeline['id']} 失败: {e}")
        return "timeout"
    def _await_task_pipeline(self, task_id: int, project_id: int,
                             issue_iid: int, output: str = "",
                             repo: dict | None = None) -> str:
        """成功收尾前的流水线等待入口（issue #40）：拿任务提交 sha 并等待终态。

        查不到提交（Claude 未推送代码，仅评论/分析）→ "no_pipeline" 不等待；
        查询提交失败（GitLab 报错）→ 同样降级 "no_pipeline"（不阻塞成功收尾）。
        查不到提交且最终回复以「等待用户决策」提问结尾（issue #67）→
        "awaiting_decision"：无人值守下 Claude 停在提问节点后自行退出，
        并无任何交付，任务不能判成功，提问应反馈到 issue 等待用户回复。
        """
        try:
            sha, _ = self._call_with_fallback(
                repo, lambda c: c.find_commit_for_issue(project_id, issue_iid))
        except GitLabError as e:
            self.db.add_log(task_id, "warn", f"查询任务提交失败，跳过流水线等待: {e}")
            return "no_pipeline"
        if not sha:
            if self._output_ends_with_question(output):
                self.db.add_log(
                    task_id, "info",
                    "未找到任务提交，且 Claude 最终回复以提问结尾，"
                    "判定为等待用户决策（问题反馈到 issue）")
                return "awaiting_decision"
            self.db.add_log(task_id, "info", "未找到任务提交，无流水线可等，直接成功收尾")
            return "no_pipeline"
        self.db.add_log(task_id, "info", f"等待任务提交 {sha[:8]} 触发的 CI 流水线到达终态…")
        return self._wait_pipeline_for_commit(task_id, project_id, sha, repo)
    def _await_pipeline_and_finish_succeeded(self, task_id: int, project_id: int,
                                             issue_iid: int, output: str,
                                             repo: dict | None = None) -> None:
        """成功收尾前的流水线等待与成功收尾（issue #40 + #47 抽取）。

        claude 与 hermes 两引擎共用：等待任务提交触发的 CI 流水线终态，
        failed/canceled/timeout → 失败收尾；success/skipped/no_pipeline →
        成功收尾（打 bot-done、记录 commit、发通知）；awaiting_decision →
        提问反馈收尾（issue #67，任务未完成，等用户回复）。
        """
        # issue #40：成功收尾前等待任务提交触发的 CI 流水线终态。
        # 此前 claude exit 0 即判成功，流水线还在运行任务就显示
        # 已完成（任务 #63 于 13:31:45 收尾，流水线到 13:48:34 才结束）。
        pipeline_state = self._await_task_pipeline(task_id, project_id,
                                                   issue_iid, output, repo)
        if pipeline_state == "awaiting_decision":
            self._finish_asked(task_id, output, repo=repo)
            return
        if pipeline_state == "stopped":
            self._finish_stopped(task_id)
            return
        if pipeline_state in ("failed", "canceled"):
            self._finish_failed(
                task_id,
                f"CI 流水线状态为 {pipeline_state}，任务视为失败",
                output, repo=repo)
            return
        if pipeline_state == "timeout":
            self._finish_failed(
                task_id,
                "CI 流水线超时未完成，任务视为失败",
                output, repo=repo)
            return
        # success / skipped / no_pipeline → 成功收尾
        self._finish_succeeded(task_id, output, repo=repo)
