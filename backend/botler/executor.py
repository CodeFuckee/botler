"""Claude Code 执行器。

流程（设计方案 §5.5）：
1. 准备干净工作区（fetch / checkout main / reset --hard / clean -fd）
2. 渲染提示词（全局/仓库模版 + 变量）
3. 注入环境变量（GITLAB_TOKEN 等只走子进程 env，不进提示词 transcript）
4. subprocess 跑 `claude -p --output-format json`，带超时
5. 结果判定：exit 0 且 issue 已关闭 → 成功；否则重试（最多 max_retries）
6. 收尾：仍失败 → issue 留失败评论 + 打 bot-failed 标签

断点续跑（issue #8）：每次执行后把 claude 会话 id 落库；重试或平台
重启恢复（调度器 requeue_interrupted 重新入队）时用 `claude --resume`
接续上次会话，且工作区只 fetch 不清空（保留 Claude 已做的修改），
从上次中断处继续而非从头重跑。会话文件丢失时自动降级为全新会话。

git 凭据通过 GIT_ASKPASS 注入，token 不落盘（askpass 脚本在每次 clean 时被清除）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ConfigManager
from .database import (
    Database, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED,
)
from .gitlab_client import GitLabClient, GitLabError
from .templates import TemplateRenderer

logger = logging.getLogger(__name__)

# 明确「无法解决」的表述（模版要求 Claude 如实汇报），命中则不再重试
UNRESOLVABLE_PATTERNS = [
    r"无法解决", r"无法修复", r"无法完成", r"不能解决", r"不能修复", r"未能解决",
    r"无法复现", r"cannot (?:be )?(?:fix|solve|resolve)", r"can'?t (?:fix|solve|resolve)",
    r"not able to (?:fix|solve|resolve)", r"could not (?:fix|solve|resolve)",
    r"out of scope", r"unable to (?:fix|solve|resolve)",
]
_UNRESOLVABLE_RE = re.compile("|".join(UNRESOLVABLE_PATTERNS), re.IGNORECASE)

# 日志保留行数（落盘 + 失败评论摘要）
LOG_TAIL_LINES = 400
COMMENT_TAIL_CHARS = 3000

# 恢复执行引导语（issue #8）：中断恢复时不用完整模版重发 issue 描述，
# 而是让 Claude 检查工作区现状后从断点继续（避免重复分析与重复评论）
RESUME_PROMPT = """【继续处理（中断恢复）】你正在处理 {repo_name} 仓库的 issue #{issue_iid}「{issue_title}」：{issue_url}

上次处理因平台重新部署而中断，你的对话与工作区改动已保留。请先检查当前状态
（git status / git log / 未提交改动），弄清上次做到哪一步，然后从断点继续：
完成剩余的修复/实现 → 自测 → 推送 → 用 GitLab API 关闭 issue。
不要从零重新分析 issue（除非确认上次未开始实质工作），不要重复已经完成的工作。"""


class ExecutorError(Exception):
    pass


class ClaudeExecutor:
    def __init__(self, config: ConfigManager, db: Database,
                 gitlab: GitLabClient, renderer: TemplateRenderer,
                 workspace_root: str | None = None):
        self.config = config
        self.db = db
        self.gitlab = gitlab
        self.renderer = renderer
        base = Path(workspace_root) if workspace_root else Path(__file__).resolve().parents[1] / "workspace"
        self.workspace_root = base.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    # ---- 工作区管理 ----

    def _repo_workdir(self, repo: dict) -> Path:
        """仓库工作区：有 local_path（本地文件夹方式添加）时直接用该文件夹。"""
        if repo.get("local_path"):
            return Path(repo["local_path"])
        return self.workspace_root / repo["name"]

    def _git(self, workdir: Path, *args: str, env: dict | None = None,
             timeout: int = 300) -> None:
        """执行 git 命令，失败抛 ExecutorError。"""
        cmd = ["git", "-c", "http.sslVerify=false"] + list(args)
        try:
            result = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExecutorError(f"git 命令超时: {args[0]} {args[1] if len(args) > 1 else ''}")
        if result.returncode != 0:
            raise ExecutorError(
                f"git {args[0]} 失败 (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[-500:]}")

    def _askpass_script(self, repo_name: str) -> Path:
        """生成 GIT_ASKPASS 脚本（用户名 oauth2，密码 = bot token）。

        放在工作区父目录（不能在 clone 目标目录里，否则 git clone 拒绝非空目录）。
        """
        script = self.workspace_root / f".botler-askpass-{repo_name}.sh"
        token = self.config.get().gitlab_token
        # token 里可能含引号，用单引号包裹并转义
        esc = token.replace("'", "'\\''")
        script.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) echo "oauth2" ;;\n'
            '  *Password*) echo \'%s\' ;;\n'
            '  *) echo \'%s\' ;;\n'
            "esac\n" % (esc, esc),
            encoding="utf-8",
        )
        script.chmod(0o700)
        return script

    def prepare_workspace(self, repo: dict, resume: bool = False) -> tuple[Path, dict]:
        """确保工作区存在且干净，返回 (workdir, git_env)。

        local_path 仓库直接用该文件夹（不 clone）；普通仓库首次执行时 clone。
        resume=True（会话断点续跑）时只 fetch 更新远端引用，跳过
        checkout / reset --hard / clean -fd——保留 Claude 上次的未提交改动
        与本地提交，供恢复会话接续使用。
        """
        cfg = self.config.get()
        workdir = self._repo_workdir(repo)
        askpass = self._askpass_script(repo["name"])
        git_env = {
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(Path.home()),
        }
        git_env.update(os.environ)

        if not (workdir / ".git").exists():
            if repo.get("local_path"):
                raise ExecutorError(
                    f"本地文件夹不是 git 仓库: {workdir}（local_path 方式要求存在 .git 目录）")
            logger.info("首次克隆仓库 %s", repo["name"])
            # 不要预先创建 workdir：git clone 要求目标目录不存在（或为空）
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "-c", "http.sslVerify=false", "clone", repo["url"], str(workdir)]
            try:
                result = subprocess.run(cmd, env=git_env, capture_output=True,
                                        text=True, timeout=600)
            except subprocess.TimeoutExpired:
                raise ExecutorError(f"克隆仓库 {repo['name']} 超时")
            if result.returncode != 0:
                raise ExecutorError(
                    f"克隆仓库 {repo['name']} 失败: {(result.stderr or result.stdout).strip()[-500:]}")

        # 每次执行前重置到远端 main，从根上消除脏状态。
        # remote_name 记录本地方式添加时用户选中的 remote（老数据缺省为 origin）
        remote = repo.get("remote_name") or "origin"
        self._git(workdir, "fetch", remote, "--prune", env=git_env)
        if not resume:
            try:
                self._git(workdir, "checkout", "main", env=git_env)
            except ExecutorError:
                logger.warning("%s: 无 main 分支，尝试 checkout master", repo["name"])
                self._git(workdir, "checkout", "master", env=git_env)
            self._git(workdir, "reset", "--hard", f"{remote}/HEAD", env=git_env)
            self._git(workdir, "clean", "-fd", env=git_env)
        # askpass 脚本被 clean -fd 清除（.botler-askpass.sh 不受 .gitignore 保护时会被删；
        # 保险起见再显式删除，避免 token 残留）
        askpass.unlink(missing_ok=True)
        return workdir, git_env

    # ---- 提示词与环境 ----

    def _build_prompt(self, repo: dict, issue: dict) -> str:
        template = self.renderer.resolve_template(repo)
        variables = self.renderer.build_variables(repo["name"], issue)
        return self.renderer.render(template, variables)

    def _build_env(self, repo: dict, issue: dict) -> dict:
        cfg = self.config.get()
        env = dict(os.environ)
        env["GITLAB_TOKEN"] = cfg.gitlab_token
        env["GITLAB_URL"] = cfg.gitlab_url
        env["PROJECT_ID"] = str(issue["project_id"])
        env["ISSUE_IID"] = str(issue["iid"])
        return env

    # ---- 会话断点续跑（issue #8）----

    def _extract_session_id(self, output: str) -> str | None:
        """从 claude JSON 输出解析 session_id（无 / 非法 JSON 返回 None）。"""
        if not output:
            return None
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            return None
        sid = data.get("session_id") if isinstance(data, dict) else None
        return sid or None

    def _claude_home(self) -> Path:
        """claude 会话文件根目录（~/.claude，session 的 .jsonl 落盘处）。"""
        return Path.home() / ".claude"

    def _session_file(self, session_id: str) -> Path | None:
        """查找 session 文件 ~/.claude/projects/*/<sid>.jsonl；不存在返回 None。"""
        projects = self._claude_home() / "projects"
        if not projects.is_dir():
            return None
        try:
            for proj in projects.iterdir():
                f = proj / f"{session_id}.jsonl"
                if f.is_file():
                    return f
        except OSError:
            return None
        return None

    def _resume_prompt(self, repo: dict, issue: dict) -> str:
        """恢复执行引导语：基于上次会话继续，不重复已完成的工作。"""
        variables = self.renderer.build_variables(repo["name"], issue)
        return self.renderer.render(RESUME_PROMPT, variables)

    # ---- 单次执行 ----

    def _run_once(self, task_id: int, repo: dict, issue: dict,
                  resume_session: str | None = None) -> tuple[int, str]:
        """执行一次 claude -p。返回 (exit_code, output)。

        resume_session 非空时为断点续跑：claude --resume 接续上次会话，
        工作区保留（不清空 Claude 已做的修改）；执行后解析 JSON 输出中的
        session_id 落库，供下次重试 / 平台重启继续。
        """
        cfg = self.config.get()
        workdir, git_env = self.prepare_workspace(repo, resume=bool(resume_session))
        if resume_session:
            prompt = self._resume_prompt(repo, issue)
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

        deadline = time.time() + cfg.task_timeout_seconds
        chunks: list[str] = []
        timed_out = False

        # 边读边写日志文件，避免内存堆积；超时则杀整个进程组
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            while True:
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
                    f.write(chunk)
                    chunks.append(chunk)
                    if len(chunks) > 20000:  # 约 20MB 上限
                        chunks.pop(0)
                if time.time() >= deadline and proc.poll() is None:
                    timed_out = True
                    break
                time.sleep(0.05)

        if timed_out:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
            output = "".join(chunks)
            self.db.add_log(task_id, "error",
                            f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止进程组")
            self._persist_session_id(task_id, output)
            return 124, output  # 124 = timeout 约定退出码

        exit_code = proc.wait(timeout=30)
        output = "".join(chunks)
        self.db.add_log(task_id, "info", f"claude 退出码: {exit_code}")
        self._persist_session_id(task_id, output)
        return exit_code, output

    def _persist_session_id(self, task_id: int, output: str) -> None:
        """执行结束后把 claude 会话 id 落库（供下次重试 / 平台重启断点续跑）。"""
        session_id = self._extract_session_id(output)
        if session_id:
            self.db.set_task_status(task_id, None, claude_session_id=session_id)

    def _log_file(self, task_id: int) -> Path:
        base = Path(__file__).resolve().parents[1] / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"task_{task_id}.log"

    # ---- 重试与结果判定 ----

    def _is_unresolvable(self, output: str) -> bool:
        return bool(_UNRESOLVABLE_RE.search(output))

    def _extract_error(self, output: str, max_chars: int = 3000) -> str:
        """从一次执行的输出中提取错误信息（trace 优先，否则取尾部）。

        claude -p --output-format json 时输出为 JSON，核心内容在 result 字段；
        result 内若含 Python Traceback 则从其起始处截取（异常堆栈对调试最有价值）。
        """
        if not output:
            return ""
        text = output
        try:
            data = json.loads(output)
            if isinstance(data, dict) and isinstance(data.get("result"), str):
                text = data["result"]
        except (ValueError, TypeError):
            pass
        idx = text.rfind("Traceback (most recent call last)")
        if idx != -1:
            text = text[idx:]
        return text[-max_chars:]

    def _issue_state(self, project_id: int, iid: int) -> str:
        try:
            return self.gitlab.get_issue(project_id, iid).get("state", "unknown")
        except GitLabError as e:
            return f"error: {e}"

    def run_task(self, task_id: int) -> None:
        """任务主流程：单次或重试执行，写状态机与收尾评论。"""
        cfg = self.config.get()
        task = self.db.get_task(task_id)
        if task is None:
            logger.warning("任务 %s 不存在，跳过", task_id)
            return
        repo = self.db.get_repo(task["repo_id"])
        if repo is None:
            self.db.set_task_status(task_id, STATUS_FAILED,
                                    error_message="仓库记录不存在")
            return

        project_id, issue_iid = task["project_id"], task["issue_iid"]
        self.db.set_task_status(task_id, None, log_path=str(self._log_file(task_id)))
        try:
            issue = self.gitlab.get_issue(project_id, issue_iid)
        except GitLabError as e:
            self._finish_failed(task_id, f"获取 issue {project_id}#{issue_iid} 失败: {e}")
            return

        max_retries = cfg.max_retries
        attempt = 0
        last_output = ""
        last_exit = -1
        attempt_details: list[dict] = []  # 每次失败的详情（退出码 + 提取的 trace/错误），供 error_detail 落库

        while True:
            attempt += 1
            # issue #8 断点续跑：上次执行留过 claude 会话 → 接续（resume）；
            # 会话文件丢失（如 ~/.claude 未持久化）→ 清除后降级全新会话
            task = self.db.get_task(task_id)
            resume_session = task["claude_session_id"] if task else None
            if resume_session and not self._session_file(resume_session):
                self.db.set_task_status(task_id, None, claude_session_id=None)
                self.db.add_log(
                    task_id, "warn",
                    f"上次会话 {resume_session[:8]}… 的会话文件已不存在，降级为全新会话")
                resume_session = None
            self.db.set_task_status(
                task_id, STATUS_RUNNING,
                attempt_count=attempt,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                finished_at=None, error_message=None)
            self.db.add_log(task_id, "info", f"第 {attempt} 次尝试开始")
            logger.info("任务 %s（%s#%s）第 %s 次执行", task_id, project_id, issue_iid, attempt)

            # 首次尝试时在 issue 上回复「处理中」，提升体验（不刷屏，重试不再重复）
            if attempt == 1:
                try:
                    self.gitlab.add_comment(
                        project_id, issue_iid,
                        "🤖 Botler 已收到该 issue，开始处理中…")
                except GitLabError as e:
                    self.db.add_log(task_id, "warn", f"发送处理中评论失败: {e}")

            try:
                exit_code, output = self._run_once(task_id, repo, issue, resume_session)
            except ExecutorError as e:
                exit_code, output = -1, f"[executor] {e}"
                self.db.add_log(task_id, "error", output)
            except Exception as e:  # 兜底异常
                exit_code, output = -1, f"[executor] 未预期异常: {e}"
                self.db.add_log(task_id, "error", output)

            last_output, last_exit = output, exit_code

            if exit_code == 0:
                state = self._issue_state(project_id, issue_iid)
                self.db.add_log(task_id, "info", f"执行结束，issue 当前状态: {state}")
                if state == "closed":
                    self._finish_succeeded(task_id, output)
                    return
                # exit 0 但 issue 未关：Claude 可能自认无法解决（不重试）或 API 调用失败（可重试）
                if self._is_unresolvable(output):
                    detail = {"attempt": attempt, "exit_code": exit_code,
                              "error": self._extract_error(output)}
                    self._finish_failed(task_id, "Claude Code 报告无法解决该 issue", output,
                                        error_detail=self._dump_error_detail(
                                            [*attempt_details, detail], last_exit))
                    return

            # 记录本次失败详情（含 trace 提取），供界面「查看详细原因」按钮展示
            attempt_details.append({
                "attempt": attempt,
                "exit_code": exit_code,
                "error": self._extract_error(output),
            })

            # 环境性失败 → 按策略重试
            if attempt > max_retries:
                break
            self.db.set_task_status(task_id, STATUS_RETRYING)
            self.db.add_log(task_id, "warn", f"第 {attempt} 次失败（exit {exit_code}），准备重试（剩余 {max_retries - attempt} 次）")
            time.sleep(5)

        self._finish_failed(
            task_id, f"重试耗尽（{max_retries} 次）后仍失败，最后退出码 {last_exit}",
            last_output,
            error_detail=self._dump_error_detail(attempt_details, last_exit))

    # ---- 收尾 ----

    def _dump_error_detail(self, attempts: list[dict], last_exit: int) -> str:
        """把每次尝试的失败详情序列化为 error_detail（JSON 字符串，界面「详情」按钮展示）。"""
        return json.dumps(
            {"summary": f"重试耗尽后仍失败，最后退出码 {last_exit}", "attempts": attempts},
            ensure_ascii=False)

    def _tail_output(self, output: str) -> str:
        lines = output.strip().splitlines()
        if len(lines) > LOG_TAIL_LINES:
            lines = lines[-LOG_TAIL_LINES:]
        return "\n".join(lines)

    def _finish_succeeded(self, task_id: int, output: str) -> None:
        self.db.set_task_status(
            task_id, STATUS_SUCCEEDED,
            exit_code=0,
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.db.add_log(task_id, "info", "任务成功：issue 已由 Claude Code 关闭")
        self._write_log_tail(task_id, output)
        logger.info("任务 %s 成功", task_id)

    def _finish_failed(self, task_id: int, reason: str, output: str = "",
                       error_detail: str | None = None) -> None:
        task = self.db.get_task(task_id)
        self.db.set_task_status(
            task_id, STATUS_FAILED,
            exit_code=None,
            error_message=reason,
            error_detail=error_detail,
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.db.add_log(task_id, "error", f"任务失败: {reason}")
        self._write_log_tail(task_id, output)

        # 在 issue 上留失败评论 + 打标签
        if task:
            summary = reason
            tail = self._tail_output(output)
            if tail and tail != output.strip():
                summary += f"\n\n日志尾部：\n```\n{tail[-COMMENT_TAIL_CHARS:]}\n```"
            try:
                self.gitlab.add_comment(
                    task["project_id"], task["issue_iid"],
                    f"🤖 Botler 自动回复：无法完成此 issue。\n\n**原因**：{summary}")
                self.gitlab.add_labels(task["project_id"], task["issue_iid"], ["bot-failed"])
                self.db.add_log(task_id, "info", "已在 issue 上留失败评论并打 bot-failed 标签")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留失败评论失败: {e}")
        logger.warning("任务 %s 失败: %s", task_id, reason)

    def _write_log_tail(self, task_id: int, output: str) -> None:
        tail = self._tail_output(output)
        if not tail:
            return
        try:
            with open(self._log_file(task_id), "a", encoding="utf-8", errors="replace") as f:
                f.write("\n----- 执行结束（摘要）-----\n" + tail + "\n")
        except OSError:
            pass
