"""Claude Code 执行器。

流程（设计方案 §5.5）：
1. 准备干净工作区（fetch / 切回默认主分支 / reset --hard / clean -fd / git pull --rebase）
2. 渲染提示词（全局/仓库模版 + 变量）
3. 注入环境变量（GITLAB_TOKEN 等只走子进程 env，不进提示词 transcript）
4. subprocess 跑 `claude -p --output-format json`，带超时
5. 结果判定：exit 0 且 issue 已关闭 → 成功；否则重试（最多 max_retries）
6. 收尾：仍失败 → issue 留失败评论 + 打 bot-failed 标签

断点续跑（issue #8）：每次执行后把 claude 会话 id 落库；重试或平台
重启恢复（调度器 requeue_interrupted 重新入队）时用 `claude --resume`
接续上次会话，且工作区只 fetch 不清空（保留 Claude 已做的修改），
从上次中断处继续而非从头重跑。会话文件丢失时自动降级为全新会话。

git 凭据通过 GIT_ASKPASS 注入：askpass 脚本（0700）保留在工作区根目录，
每次 prepare 覆盖刷新（token 轮换自动生效）。保留不删除，避免并发/重试时
脚本缺失导致 fetch 回退 credential helper 旧凭据（issue #12）。

拆分重构（issue #192）后本文件为 executor 包主模块：仅保留
``ClaudeExecutor`` 主类的引擎分发与状态机编排；职责按模块拆分：
executor/workspace.py（git 工作区）、executor/process.py（引擎子进程
执行/结果判定/CI 等待）、executor/session.py（会话文件解析/日志增量）、
executor/prompt.py（提示词渲染/脱敏）。对外符号（ClaudeExecutor /
format_display_line / parse_transcript 等）由本模块统一再导出，引用方
（api/tasks.py、scheduler.py、测试等）无需改动。"""

from __future__ import annotations

import calendar
import json
import os  # noqa: F401 （对外再导出，测试 monkeypatch botler.executor.os）
import subprocess
import threading
import time
from pathlib import Path

from ..config import ConfigManager
from ..database import (
    Database, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED,
    STATUS_INTERRUPTED,
)
from ..dsh_runner import DshRunner, DshSdkNotInstalledError
from ..env_snapshot import (  # noqa: F401 （对外再导出，测试 monkeypatch 目标）
    collect_env_snapshot,
    error_snapshot,
    serialize_snapshot,
)
from ..events import EventBus
from ..log_redact import redact
from ..git_remote import (  # noqa: F401 （NoGitRemoteError/list_local_remotes/parse_remote_url 对外再导出）
    NoGitRemoteError, build_repo_client_with_username,
    list_local_remotes, parse_remote_url,
)
from ..gitlab_client import GitLabClient, GitLabError, is_transient_error
from ..hermes_sdk_runner import HermesSdkRunner, HermesSdkNotInstalledError
from ..plugins import PluginKind, get_plugin, has_plugin, list_plugins
from ..templates import TemplateRenderer
from ..failure_classify import (
    CATEGORY_ENGINE,
    category_advice,
    category_label,
    classify_failure,
)
from .. import engine_health
from ..report import (
    DEFAULT_COMMENT_TEMPLATE,
    DEFAULT_FAILURE_COMMENT_TEMPLATE,
    EMPTY_DIFF,
    build_diff_table,
    collect_diff_data,
    format_duration,
    format_test_summary,
    parse_test_summary,
    render_comment,
)
# executor 包拆分后的职责模块（issue #192）：workspace/process/session/prompt
from .common import (
    COMMENT_TAIL_CHARS, FINISH_RETRY_ATTEMPTS, FINISH_RETRY_BASE_DELAY,
    FINISH_RETRY_MAX_DELAY, ISSUE_FETCH_BASE_DELAY, ISSUE_FETCH_MAX_ATTEMPTS,
    ISSUE_FETCH_MAX_DELAY, LOG_TAIL_LINES, STOP_EXIT_CODE, ExecutorError,
    _row_get, logger,
)
from .prompt import PromptMixin, format_display_line
from .process import ProcessMixin
from .session import (
    SessionMixin, find_session_file, parse_transcript, read_log_delta,
    read_session_prompt,
)
from .workspace import WorkspaceMixin

__all__ = [
    "ClaudeExecutor",
    "ExecutorError",
    "format_display_line",
    "find_session_file",
    "parse_transcript",
    "read_log_delta",
    "read_session_prompt",
    "DshRunner",
    "DshSdkNotInstalledError",
    "HermesSdkRunner",
    "HermesSdkNotInstalledError",
]

class ClaudeExecutor(WorkspaceMixin, ProcessMixin, SessionMixin, PromptMixin):
    """Claude Code 执行器主类（issue #192 拆分后保留引擎分发与状态机编排）。

    工作区管理 / 引擎进程执行 / 会话解析 / 提示词构建分别由
    executor/workspace.py、executor/process.py、executor/session.py、
    executor/prompt.py 的 mixin 提供，本类聚合后对外行为与拆分前完全一致。
    """

    def __init__(self, config: ConfigManager, db: Database,
                 gitlab: GitLabClient, renderer: TemplateRenderer,
                 workspace_root: str | None = None,
                 event_bus: EventBus | None = None):
        self.config = config
        self.db = db
        self.gitlab = gitlab
        self.renderer = renderer
        # 实时事件总线（SSE 推送）：executor 读流时逐事件发布；API 层订阅。
        # seq 计数按任务递增且跨重试轮次持久——断线重连后 API 回放日志
        # （从 1 重算）与实时事件 seq 衔接，前端按 seq 去重不丢事件
        self.event_bus = event_bus if event_bus is not None else EventBus()
        self._seq: dict[int, int] = {}
        base = Path(workspace_root) if workspace_root else Path(__file__).resolve().parents[2] / "workspace"
        self.workspace_root = base.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        # 网页通知事件（issue #21）：任务收尾时记录，前端轮询弹系统通知
        from ..notifier import Notifier
        self.notifier = Notifier(db)
        # Webhook 消息推送（issue #136）：任务成功收尾时按设置页配置推送
        from ..webhook_push import WebhookPusher
        self.webhook_pusher = WebhookPusher(config)
        # 一键停止（issue #35）：运行中进程注册表 + 停止请求集合。
        # task_id 自增唯一，集合只增不减（进程退出注销的是注册表不是集合）。
        self._procs: dict[int, subprocess.Popen] = {}
        self._stop_requests: set[int] = set()
        self._proc_lock = threading.Lock()
        # 拉取冲突交接（issue #147 补充）：prepare_workspace 的
        # git pull --rebase 遇到合并冲突时保留冲突现场并登记工作区，
        # _build_prompt / _resume_prompt 据此追加「先手工解决冲突」指引，
        # 由 agent 完成合并，而不是让任务在准备阶段直接失败
        self._pull_conflict_workdirs: set[Path] = set()
    # ---- GitLab 调用兜底 ----
    def _call_with_fallback(self, repo, call):
        """用全局 client 执行 call(client)；遇 401/403（全局 token 失效）
        时用仓库 remote url 内嵌 token 构建 per-repo client 重试一次。

        issue #130 + #132：任务侧（生命周期评论、打标签等）绝不使用
        owner token——owner token 只允许在概览页 issue 编辑操作时由平台
        使用（见 api/issues.py），agent 无论如何都不能使用 owner token。
        因此这里固定走「全局 → remote」链路（issue #87 的 prefer_owner
        机制已按 #130 移除）。非编辑调用（流水线等待、查询提交等）同样
        只走此链路，绝不使用 owner token（严禁用于推送代码与处理流水线）。

        issue #65 补充：对账/webhook 已有此兜底，executor 的 issue 查询、
        评论、打标签仍只走全局 client——全局 token 被撤销后任务领取即
        401 失败、issue 上收不到任何评论（生产任务 #88/#89）。repo 为
        None（无仓库上下文的测试等）时仅用全局 client（行为同旧）。
        """
        if repo is None:
            return call(self.gitlab), self.gitlab
        try:
            return call(self.gitlab), self.gitlab
        except GitLabError as e:
            if e.status_code not in (401, 403):
                raise
            fallback, _ = build_repo_client_with_username(
                repo, self.config.get().verify_ssl)
            if fallback is None:
                raise
            logger.info("任务仓库 %s：全局 token 失效（%s），"
                        "改用 remote url 内嵌 token 重试", repo["name"], e)
            return call(fallback), fallback
    def _transient_retry(self, what: str, call, *,
                         attempts: int = FINISH_RETRY_ATTEMPTS,
                         base_delay: float = FINISH_RETRY_BASE_DELAY,
                         max_delay: float = FINISH_RETRY_MAX_DELAY):
        """对 call() 执行瞬时故障重试（指数退避）；非瞬时错误立即抛出。

        issue #280：GitLab 短暂不可用（502/503/限流/网络抖动）时，收尾
        评论/标签不能只试一次就放弃——一次 502 会让 issue 上「没有任何
        回复评论」，用户无法感知任务失败/处理中。重试耗尽后抛最后一个
        GitLabError，由调用方记日志降级。
        """
        last: GitLabError | None = None
        for attempt in range(attempts):
            try:
                return call()
            except GitLabError as e:
                if not is_transient_error(e):
                    raise
                last = e
                if attempt >= attempts - 1:
                    break
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning("%s瞬时故障（%s），%.0fs 后重试（第 %d/%d 次）",
                               what, e, delay, attempt + 1, attempts)
                time.sleep(delay)
        assert last is not None
        raise last

    # ---- 工作区管理 ----
    def _run_once(self, task_id: int, repo: dict, issue: dict,
                  resume_session: str | None = None,
                  resume_history: list | None = None,
                  engine: str | None = None) -> tuple[int, str]:
        """执行一次任务引擎（插件体系分发，issue #140）。返回 (exit_code, output)。

        按 ``worker.engine`` 配置的引擎名查执行引擎插件（内置 claude /
        hermes / dsh 见 botler.plugins.executors）并委托执行；未知引擎回退
        claude。``engine`` 参数（issue #236）为本次尝试实际使用的引擎——
        run_task 引擎降级后显式传入，缺省回退全局 worker.engine（兼容
        测试直接调用与外部插件委托）。断点续跑语义由各引擎插件承担：
        - claude（issue #8）：resume_session 非空时 --resume 接续上次会话；
        - hermes（issue #47）：resume_history 为历史消息（显式传入优先，
          未传入时从任务落库 hermes_history 解析）；
        - dsh（issue #84）：resume_session 为上次会话 id（SDK 持久化会话）。
        """
        cfg = self.config.get()
        if engine is None:
            engine = self._engine(cfg)
        plugin = get_plugin(PluginKind.EXECUTOR, engine)
        return plugin.run(self, task_id, repo, issue,
                          resume_session, resume_history)
    def _log_file(self, task_id: int) -> Path:
        base = Path(__file__).resolve().parents[2] / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"task_{task_id}.log"

    # ---- 引擎健康探测与降级（issue #236） ----
    def _engine_chain(self, cfg) -> list[str]:
        """引擎降级链：主引擎 + worker.fallback_engines 备用引擎。

        按顺序去重、剔除主引擎名与未注册引擎（未知引擎名回退 claude 由
        self._engine 处理，此处只过滤 fallback_engines 里的脏值）。
        """
        main = self._engine(cfg)
        chain = [main]
        for name in (cfg.fallback_engines or []):
            name = str(name).strip().lower()
            if name and name != main and name not in chain \
                    and has_plugin(PluginKind.EXECUTOR, name):
                chain.append(name)
        return chain

    def _select_engine_for_attempt(self, cfg, chain: list[str],
                                   tried_engines: set[str],
                                   engine: str) -> tuple[str, str | None, bool]:
        """每次任务尝试开始前的引擎健康探测（issue #236）。

        探测当前引擎：可用 → (当前引擎, None, False)；不可用 → 从降级链上
        选第一个「未尝试且探测通过」的备用引擎，返回 (新引擎, 降级原因文案,
        True)；链上全部探测失败时保持当前引擎（探测为建议性，执行仍会暴露
        真实故障，不因探测失败直接判任务失败），返回 (当前引擎, None, True)。
        """
        health = engine_health.probe_engine(engine, cfg)
        if health["status"] == "ok":
            return engine, None, False
        detail = health.get("detail") or "探测失败"
        for cand in chain:
            if cand == engine or cand in tried_engines:
                continue
            if engine_health.probe_engine(cand, cfg)["status"] == "ok":
                return (cand,
                        f"引擎 {engine} 不可用（{detail}），已降级 {cand} 执行",
                        True)
        return engine, None, True

    def _note_engine_degradation(self, task_id: int, project_id: int,
                                 issue_iid: int, repo: dict | None,
                                 reason: str) -> None:
        """引擎降级时在 issue 上留一条说明评论（issue #236，每任务最多一次）。

        验收标准：任务记录与 issue 评论均能看出「引擎 X 不可用，已降级 Y
        执行」。评论发送失败不阻塞任务（与处理中评论同容错策略）。
        """
        try:
            self._transient_retry(
                "留引擎降级评论",
                lambda: self._call_with_fallback(
                    repo, lambda c: c.add_comment(
                        project_id, issue_iid,
                        f"🤖 Botler 提示：{reason}")))
            self.db.add_log(task_id, "info", f"已在 issue 上注明引擎降级: {reason}")
        except GitLabError as e:
            self.db.add_log(task_id, "warn", f"留引擎降级评论失败: {e}")

    # ---- 重试与结果判定 ----
    def run_task(self, task_id: int) -> None:
        """任务主流程：单次或重试执行，写状态机与收尾评论。"""
        cfg = self.config.get()
        # issue #236：引擎降级链——主引擎 + worker.fallback_engines 备用引擎
        # （去重、剔除主引擎与未注册引擎）；未配置备用引擎时行为与旧版一致
        engine_chain = self._engine_chain(cfg)
        engine = engine_chain[0]
        task = self.db.get_task(task_id)
        if task is None:
            logger.warning("任务 %s 不存在，跳过", task_id)
            return
        repo = self.db.get_repo(task["repo_id"])
        if repo is None:
            self.db.set_task_status(task_id, STATUS_FAILED,
                                    error_message="仓库记录不存在")
            return

        # 原子抢占（issue #24）：多实例并存时同一任务可能被多次领取，
        # 只有状态为 queued/retrying 的任务能抢到 running；抢不到说明
        # 其他实例已领取或任务已结束，直接跳过避免重复执行/状态错乱。
        if not self.db.claim_task(task_id):
            logger.info("任务 %s 已被其他实例领取或已结束（状态非 queued/retrying），跳过", task_id)
            return

        # 用户一键停止（issue #35）：停止请求可能先于 worker 领取到达
        # （scheduler.stop_all 先落库再登记请求），领取后立即检查，
        # 避免已经停止的任务再发起执行
        if self._stop_requested(task_id):
            self._finish_stopped(task_id)
            return

        # issue #120：执行引擎按任务落库——记录本次实际执行的引擎
        # （claude / hermes / dsh），概览页 issue 右边栏按任务展示历史
        # 引擎，全局 worker.engine 切换后旧 issue 不再误显新引擎
        self.db.set_task_status(task_id, None, engine=engine)

        project_id, issue_iid = task["project_id"], task["issue_iid"]
        self.db.set_task_status(task_id, None, log_path=str(self._log_file(task_id)))
        # issue #280：拉取 issue 遇 GitLab 瞬时故障（502/503/限流/网络抖动）
        # 时按指数退避重试，不立即判失败——08-17 生产 GitLab 短暂不可用，
        # 44 个排队任务启动阶段 get_issue 一次 502 即全部打成 failed，且失败
        # 评论同样发不出，issue 上「没有任何回复评论」。重试耗尽才判失败。
        for attempt in range(ISSUE_FETCH_MAX_ATTEMPTS):
            try:
                issue, _ = self._call_with_fallback(
                    repo, lambda c: c.get_issue(project_id, issue_iid))
                break
            except GitLabError as e:
                if attempt >= ISSUE_FETCH_MAX_ATTEMPTS - 1 or not is_transient_error(e):
                    self._finish_failed(task_id,
                                        f"获取 issue {project_id}#{issue_iid} 失败: {e}",
                                        repo=repo)
                    return
                delay = min(ISSUE_FETCH_BASE_DELAY * (2 ** attempt), ISSUE_FETCH_MAX_DELAY)
                self.db.add_log(task_id, "warn",
                                f"获取 issue 瞬时故障（{e}），{delay:.0f}s 后重试"
                                f"（第 {attempt + 1}/{ISSUE_FETCH_MAX_ATTEMPTS} 次）")
                time.sleep(delay)

        max_retries = cfg.max_retries
        attempt = 0
        last_output = ""
        last_exit = -1
        attempt_details: list[dict] = []  # 每次失败的详情（退出码 + 提取的 trace/错误），供 error_detail 落库
        # issue #236 引擎降级状态：本任务已实际执行过的引擎集合（降级不
        # 重复回到已尝试引擎）、连续引擎类失败计数、是否已在 issue 上注明降级
        tried_engines: set[str] = set()
        engine_failures = 0
        degraded_noted = False

        while True:
            # 用户一键停止（issue #35）：重试循环每轮检查停止请求
            # （请求可能在第 N 次失败后、重试间隙到达），命中即终止
            if self._stop_requested(task_id):
                self._finish_stopped(task_id)
                return
            attempt += 1
            # issue #236 引擎健康探测：每次尝试开始前实时探测当前引擎，
            # 不可用立即降级到备用引擎（不消耗尝试次数），并在任务记录与
            # issue 评论注明「引擎 X 不可用，已降级 Y 执行」
            engine, degrade_reason, probe_failed = self._select_engine_for_attempt(
                cfg, engine_chain, tried_engines, engine)
            tried_engines.add(engine)
            if degrade_reason:
                self.db.set_task_status(task_id, None, engine=engine,
                                        engine_fallback=degrade_reason)
                self.db.add_log(task_id, "warn", degrade_reason)
                if not degraded_noted:
                    self._note_engine_degradation(
                        task_id, project_id, issue_iid, repo, degrade_reason)
                    degraded_noted = True
            elif probe_failed:
                self.db.add_log(
                    task_id, "warn",
                    f"引擎 {engine} 探测不可用且无可用备用引擎，继续尝试执行")
            # issue #8 断点续跑：上次执行留过 claude 会话 → 接续（resume）；
            # 会话文件丢失（如 ~/.claude 未持久化）→ 清除后降级全新会话。
            # hermes 引擎（issue #47）的断点续跑数据在 tasks.hermes_history，
            # 由 _run_once 内部读取（session 文件机制仅 claude 有）；
            # dsh 引擎（issue #84）的断点续跑数据在 tasks.dsh_session_id
            # （SDK 在 session_root 持久化会话，无需本地会话文件校验）。
            task = self.db.get_task(task_id)
            resume_session = None
            if engine == "hermes":
                pass
            elif engine == "dsh":
                resume_session = _row_get(task, "dsh_session_id") if task else None
            else:
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
                started_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                finished_at=None, error_message=None)
            self.db.add_log(task_id, "info", f"第 {attempt} 次尝试开始（引擎 {engine}）")
            logger.info("任务 %s（%s#%s）第 %s 次执行", task_id, project_id, issue_iid, attempt)

            # 首次尝试时在 issue 上回复「处理中」，提升体验（不刷屏，重试不再重复）。
            # issue #280：瞬时故障退避重试，避免 GitLab 短暂不可用时用户收不到任何回复
            if attempt == 1:
                try:
                    self._transient_retry(
                        "发送处理中评论",
                        lambda: self._call_with_fallback(
                            repo, lambda c: c.add_comment(
                                project_id, issue_iid,
                                "🤖 Botler 已收到该 issue，开始处理中…")))
                except GitLabError as e:
                    self.db.add_log(task_id, "warn", f"发送处理中评论失败: {e}")

            try:
                # engine 以第 6 个位置参数传入（resume_history=None）：
                # _run_once 的 engine 参数（issue #236）为本次尝试实际引擎，
                # 缺省回退全局 worker.engine；测试 monkeypatch 的
                # ``lambda *a`` 兼容位置参数调用
                exit_code, output = self._run_once(
                    task_id, repo, issue, resume_session, None, engine)
            except ExecutorError as e:
                exit_code, output = -1, f"[executor] {e}"
                self.db.add_log(task_id, "error", output)
            except Exception as e:  # 兜底异常
                exit_code, output = -1, f"[executor] 未预期异常: {e}"
                self.db.add_log(task_id, "error", output)

            # 统一日志脱敏（issue #259）：引擎输出（claude/hermes/dsh 子进程
            # 输出、异常消息）在进入评论 / 错误详情 / 日志尾部前打码——子进程
            # 输出可能回显 git remote 内嵌凭据、Authorization 头等敏感串
            output = redact(output)
            last_output, last_exit = output, exit_code

            # 被停止（issue #35）：进程组被杀（STOP_EXIT_CODE）且已登记
            # 停止请求 → 直接收尾，不进入重试分支（避免状态闪现 retrying）
            if exit_code == STOP_EXIT_CODE and self._stop_requested(task_id):
                self._finish_stopped(task_id)
                return

            if exit_code == 0:
                state = self._issue_state(project_id, issue_iid, repo)
                self.db.add_log(task_id, "info", f"执行结束，issue 当前状态: {state}")
                if engine in ("hermes", "dsh"):
                    # hermes（issue #47）/ dsh（issue #84）引擎：成功判定看
                    # runner 输出 JSON，非 JSON / error 非空 / dsh 回合未正常
                    # 完成落入下方重试分支（与 claude exit 0 无 JSON 一致）
                    result = (self._hermes_result(output) if engine == "hermes"
                              else self._dsh_result(output))
                    if result == "unresolvable":
                        detail = {"attempt": attempt, "engine": engine,
                                  "exit_code": exit_code,
                                  "error": self._extract_error(output)}
                        self._finish_failed(task_id, f"{engine} 报告无法解决该 issue", output,
                                            error_detail=self._dump_error_detail(
                                                [*attempt_details, detail], last_exit),
                                            repo=repo)
                        return
                    if result == "success":
                        # Q3-B：会话数据落库（断点续跑），收尾流程与 claude 引擎一致
                        # （dsh 的会话 id 已在 _run_dsh_once 内部落库）
                        if engine == "hermes":
                            self._persist_hermes_history(task_id, output)
                        self._await_pipeline_and_finish_succeeded(
                            task_id, project_id, issue_iid, output, repo)
                        return
                else:
                    # exit 0 但 Claude 自认无法解决 → 失败终态（不重试）
                    if self._is_unresolvable(output):
                        detail = {"attempt": attempt, "engine": engine,
                                  "exit_code": exit_code,
                                  "error": self._extract_error(output)}
                        self._finish_failed(task_id, "Claude Code 报告无法解决该 issue", output,
                                            error_detail=self._dump_error_detail(
                                                [*attempt_details, detail], last_exit),
                                            repo=repo)
                        return
                    # 成功判定（issue #25 第二轮）：完成任务即成功，不再要求关闭 issue。
                    # 模版库规范（docs/labels.md）：任务完成后不关闭 issue——留结果评论、
                    # 打 bot-done，等用户确认后手动关闭。旧逻辑以 issue closed 为成功
                    # 标志，exit 0 但 issue 仍 open 时判失败并重试，迫使 Claude 在完成
                    # 开发后违规关闭 issue（生产日志 task_30/31：issue #28 完成即被关）。
                    # 新判定：正常完成输出（JSON result，非「无法解决」）即成功，
                    # 无论 issue 是否仍 open。
                    # stream-json 多行输出下必须定位 type=result 行
                    # （_result_line 从尾部扫描），首个 JSON 对象是 init 行，
                    # 不能作为成功依据（异常中断的输出同样含 init 行）
                    if self._result_line(output) is not None:
                        self._await_pipeline_and_finish_succeeded(
                            task_id, project_id, issue_iid, output, repo)
                        return

            # 记录本次失败详情（含 trace 提取），供界面「查看详细原因」按钮展示
            attempt_details.append({
                "attempt": attempt,
                "engine": engine,
                "exit_code": exit_code,
                "error": self._extract_error(output),
            })

            # 环境性失败 → 按策略重试
            if attempt > max_retries:
                break
            # issue #236 连续引擎类失败降级：失败被分类为「引擎类」（命令
            # 缺失 / API key 无效 / SDK 错误，见 failure_classify.py）才累计；
            # 达到 fallback_after_failures 阈值且备用链上还有未尝试引擎时降级，
            # 任务级失败（代码改不对）不累计不降级（换引擎重试无意义）
            category = classify_failure(
                output, rules=cfg.failure_classify_rules)
            if category == CATEGORY_ENGINE:
                engine_failures += 1
            else:
                engine_failures = 0
            if engine_failures >= cfg.fallback_after_failures:
                next_engine = next(
                    (e for e in engine_chain
                     if e != engine and e not in tried_engines), None)
                if next_engine is not None:
                    reason = (f"引擎 {engine} 连续 {engine_failures} 次引擎类失败，"
                              f"已降级 {next_engine} 执行")
                    engine = next_engine
                    tried_engines.add(engine)
                    engine_failures = 0
                    self.db.set_task_status(task_id, None, engine=engine,
                                            engine_fallback=reason)
                    self.db.add_log(task_id, "warn", reason)
                    if not degraded_noted:
                        self._note_engine_degradation(
                            task_id, project_id, issue_iid, repo, reason)
                        degraded_noted = True
            self.db.set_task_status(task_id, STATUS_RETRYING)
            self.db.add_log(task_id, "warn", f"第 {attempt} 次失败（exit {exit_code}），准备重试（剩余 {max_retries - attempt} 次）")
            time.sleep(5)

        self._finish_failed(
            task_id, f"重试耗尽（{max_retries} 次）后仍失败，最后退出码 {last_exit}",
            last_output,
            error_detail=self._dump_error_detail(attempt_details, last_exit),
            repo=repo)

    # ---- 收尾 ----
    def _finish_stopped(self, task_id: int) -> None:
        """用户一键停止收尾（issue #35）：条件落 interrupted 终态（幂等）。

        常规路径状态已由 scheduler.stop_all → db.stop_active_tasks 统一
        落库，此处条件更新兜底「刚被领取尚未被停止流程覆盖」的任务；
        状态已终态时跳过覆盖（多实例场景由先完成者生效）。
        """
        if not self.db.finish_task(
                task_id, STATUS_INTERRUPTED,
                exit_code=None,
                error_message="用户手动停止（一键停止所有任务）",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            return
        self.db.add_log(task_id, "warn", "任务已停止：用户一键停止所有任务")
        # issue #69：停止请求已消费，收尾即清除登记——请求残留会导致任务
        # 手动重试后 worker 领取时再次命中，被立即打回 interrupted
        self.clear_stop_request(task_id)
    def _emit_task_event(self, task_id: int, event: str, reason: str = "") -> None:
        """任务收尾向全部已注册的 notifier 插件分发事件（issue #21/#136，
        插件化 issue #140）。查库失败不阻塞收尾。

        统一分发网页通知（in_app）与外部 webhook 推送（webhook）两类
        通道：webhook 插件需要 issue 完整信息（正文/链接供模板占位符
        渲染），分发前统一拉取一次，失败降级用任务记录数据；各通道自行
        检查启用条件（webhook.enabled / 地址配置，未启用返回 None 跳过），
        任一通道失败仅记日志，绝不阻塞任务收尾。
        """
        from ..webhook_push import WebhookPushError
        try:
            task = self.db.get_task(task_id)
            if task is None:
                return
            repo = self.db.get_repo(task["repo_id"])
            repo_name = repo["name"] if repo else ""
            repo_url = repo["url"] if repo else ""
            issue = None
            if repo is not None and event == "task_succeeded":
                try:
                    issue, _ = self._call_with_fallback(
                        repo, lambda c: c.get_issue(
                            task["project_id"], task["issue_iid"]))
                except Exception as e:  # noqa: BLE001 拉取失败降级用任务记录数据
                    logger.warning("通知分发查询 issue %s#%s 失败: %s",
                                   task["project_id"], task["issue_iid"], e)
            for plugin in list_plugins(PluginKind.NOTIFIER):
                try:
                    if event == "task_succeeded":
                        result = plugin.send_task_succeeded(
                            self, dict(task), repo_name=repo_name,
                            repo_url=repo_url, issue=issue)
                        if plugin.name == "webhook" and result is not None:
                            self.db.add_log(
                                task_id, "info",
                                f"webhook 推送成功（HTTP {result['status_code']}）")
                    elif event == "task_failed":
                        plugin.send_task_failed(
                            self, dict(task), reason, repo_name=repo_name)
                except WebhookPushError as e:
                    try:
                        self.db.add_log(task_id, "warn", f"webhook 推送失败: {e}")
                    except Exception:  # noqa: BLE001 日志落库失败忽略
                        pass
                    logger.warning("任务 %s webhook 推送失败: %s", task_id, e)
                except Exception:  # noqa: BLE001 任一通道失败不阻塞任务收尾
                    logger.exception("任务 %s 通知插件 %s 分发失败",
                                     task_id, plugin.name)
        except Exception:  # noqa: BLE001 通知失败不影响任务收尾
            logger.exception("任务 %s 通知事件记录失败", task_id)
    def _dump_error_detail(self, attempts: list[dict], last_exit: int) -> str:
        """把每次尝试的失败详情序列化为 error_detail（JSON 字符串，界面「详情」按钮展示）。"""
        return json.dumps(
            {"summary": f"重试耗尽后仍失败，最后退出码 {last_exit}", "attempts": attempts},
            ensure_ascii=False)
    def _tail_output(self, output: str) -> str:
        # 逐行重排 claude JSON 输出（result 嵌套转义解码，issue #16）
        lines = [format_display_line(l) for l in output.strip().splitlines()]
        if len(lines) > LOG_TAIL_LINES:
            lines = lines[-LOG_TAIL_LINES:]
        return "\n".join(lines)
    def _finish_succeeded(self, task_id: int, output: str,
                          repo: dict | None = None) -> None:
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_SUCCEEDED,
                exit_code=0,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 成功收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(task_id, "info", "任务成功：Claude Code 已完成处理（issue 保持打开，等用户确认后手动关闭）")
        self._write_log_tail(task_id, output)
        self._record_commit(task_id, repo)
        # issue #109：检测并恢复被 GitLab autoclose 自动关闭的 issue
        # （提交信息命中默认关闭模式时 GitLab 系统自动关闭，用户侧表现为
        # 「agent 自己 close issue」）；人工关闭不干预，检测失败不阻塞。
        task = self.db.get_task(task_id)
        if task is not None:
            self._restore_autoclosed_issue(task, repo)
        # issue #34：成功时由平台代码直接打 bot-done 标签（幂等），不再依赖
        # Claude 按模板打——Claude 忘打会导致 issue 无终态标签被重复领取。
        # issue #67：同步移除 in-progress（Claude 领取时打的处理中标签），
        # 避免收尾后与终态标签并存。
        # 打标签失败不阻塞任务成功（仅记 warn，用户可手动补标签）。
        if task is not None:
            try:
                self._call_with_fallback(
                    repo, lambda c: c.add_labels(
                        task["project_id"], task["issue_iid"], ["bot-done"],
                        remove=["in-progress"]))
                # issue #49：finished_at 语义 = 系统给 issue 打上 bot-done
                # 标记的时间。打标签成功后把 finished_at 更新为打标时刻，
                # 任务页「用时」以它与 created_at（系统接收时间）动态计算
                # 完整处理周期；打标失败保留收尾时刻（下方 warn 兜底）。
                self.db.set_task_status(
                    task_id, None,
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
                self.db.add_log(task_id, "info", "已在 issue 上打 bot-done 标签，等待用户确认后手动关闭")
            except GitLabError as e:
                self.db.add_log(task_id, "warn", f"打 bot-done 标签失败: {e}")
        # issue #79：结果评论不再依赖 Claude 按模板自行留言，平台兜底写
        # 完成报告（防重：最后一条评论是 bot 本人则跳过）；写评论失败
        # 不阻塞任务成功（与打标签一致的容错策略）。
        if task is not None:
            self._leave_success_comment(task, output, repo)
        # 任务消息分发（issue #21/#136，插件化 issue #140）：网页通知 +
        # webhook 推送统一由 notifier 插件分发（各通道自检启用条件，
        # 失败仅记日志不阻塞任务成功收尾）
        self._emit_task_event(task_id, "task_succeeded")
        logger.info("任务 %s 成功", task_id)
    def _task_duration_text(self, task: dict) -> str:
        """任务用时文案（created_at → finished_at，UTC 串，issue #252）。

        与前端 fmtDuration 同语义：系统接收到 issue → 收尾打 bot-done
        标记；finished_at 缺失（异常收尾/打标失败兜底）时返回空串
        （渲染层隐藏用时行）。
        """
        created = _row_get(task, "created_at") or ""
        finished = _row_get(task, "finished_at") or ""
        if not created or not finished:
            return ""
        try:
            start = calendar.timegm(time.strptime(created, "%Y-%m-%d %H:%M:%S"))
            end = calendar.timegm(time.strptime(finished, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            return ""
        return format_duration(max(0, end - start))
    def _build_report_comment(self, task: dict, output: str, *,
                              repo: dict | None = None,
                              failed: bool = False,
                              reason: str = "",
                              log_tail: str = "") -> str:
        """按可配置评论模版渲染结构化执行报告（issue #252）。

        采集：任务改动（相对任务开始前 main 基线 base_sha 的 git diff，
        改动文件表格 + 新增/删除列表）、测试摘要（从执行输出日志提取
        pass/fail 计数）、提交链接、用时。无基线/无数据 → 对应占位符
        为空，渲染后段落自动隐藏（验收标准 3），全程不抛错阻塞收尾
        （采集/渲染失败记 warn 后回退内置模版）。
        """
        base_sha = _row_get(task, "base_sha") or ""
        diff = EMPTY_DIFF
        workdir = None
        if repo is not None:
            try:
                workdir = self._repo_workdir(repo)
            except Exception:  # noqa: BLE001 工作区不可用 → 隐藏改动段落
                workdir = None
        if workdir is not None and base_sha:
            try:
                diff = collect_diff_data(workdir, base_sha)
            except Exception as e:  # noqa: BLE001 采集失败不阻塞收尾
                self.db.add_log(task["id"], "warn", f"采集任务改动失败: {e}")
        cfg = self.config.get()
        template = (cfg.comment_template or
                    (DEFAULT_FAILURE_COMMENT_TEMPLATE if failed
                     else DEFAULT_COMMENT_TEMPLATE))
        sha = _row_get(task, "commit_sha") or ""
        commit_link = ""
        if sha:
            short = sha[:8]
            url = f"{cfg.gitlab_url.rstrip('/')}/-/commit/{sha}"
            commit_link = f"[{short}]({url})"
        # issue #274：失败分类徽章与处理建议占位符（供自定义评论模版使用；
        # 未配置新占位符的模版保持原样，分类信息由 _finish_failed 前缀行补齐）
        category = _row_get(task, "failure_category") or ""
        variables = {
            "result_summary": "" if failed else self._success_summary(output),
            "diff_stat": build_diff_table(diff),
            "test_summary": format_test_summary(parse_test_summary(output)),
            "commit_link": commit_link,
            "commit_sha": sha[:8] if sha else "",
            "duration": self._task_duration_text(task),
            "error_message": reason,
            "log_tail": log_tail,
            "failure_category": category,
            "failure_category_badge": (
                f"{category_label(category)}（{category}）" if category else ""),
            "failure_advice": category_advice(category) if category else "",
        }
        try:
            return render_comment(template, variables)
        except Exception as e:  # noqa: BLE001 用户模版写坏 → 回退内置
            self.db.add_log(task["id"], "warn",
                            f"渲染结果评论失败（回退内置模版）: {e}")
            return render_comment(
                DEFAULT_FAILURE_COMMENT_TEMPLATE if failed
                else DEFAULT_COMMENT_TEMPLATE, variables)
    def _leave_success_comment(self, task: dict, output: str,
                               repo: dict | None = None) -> None:
        """任务成功时平台兜底写完成评论（issue #79）。

        此前结果评论依赖 Claude 按模板自行留言，全局 bot token 失效后
        Claude 侧 API 401 失败，任务成功（bot-done 已打）但 issue 上没
        有任何报告评论。防重：最后一条非系统评论是 bot 本人（Claude 已
        留）→ 跳过；检查/写评论失败均不阻塞任务成功（仅记 warn 日志）。
        """
        project_id, issue_iid = task["project_id"], task["issue_iid"]
        try:
            last_author, client = self._call_with_fallback(
                repo, lambda c: c.last_note_author_id(project_id, issue_iid))
        except GitLabError as e:
            self.db.add_log(task["id"], "warn", f"检查最后评论作者失败: {e}")
            return
        cfg = self.config.get()
        bot_ids = {cfg.bot_id} if getattr(cfg, "bot_id", None) else set()
        try:
            # remote 兜底客户端（如有）的账号同样视为 bot 本人——
            # 会话内 Claude 可能用 remote token 写评论（issue #79 修复后）
            bot_ids.add(client.get_bot_id())
        except Exception:  # noqa: BLE001 查询失败/无该方法不阻塞防重
            pass
        if last_author in bot_ids:
            self.db.add_log(task["id"], "info", "Claude 已留结果评论，平台不重复写")
            return
        body = self._build_report_comment(task, output, repo=repo)
        if not body.strip():
            # 兜底：渲染为空（极端场景）时保留最小可读文案，不写空评论
            body = ("🤖 Botler 自动回复：任务已完成。\n\n"
                    "开发已完成，请确认后手动关闭本 issue"
                    "（平台已打 bot-done 标签）。")
        try:
            self._call_with_fallback(
                repo, lambda c: c.add_comment(project_id, issue_iid, body))
            self.db.add_log(task["id"], "info", "已在 issue 上留任务完成评论")
        except GitLabError as e:
            self.db.add_log(task["id"], "warn", f"留任务完成评论失败: {e}")
    def _restore_autoclosed_issue(self, task: dict,
                                  repo: dict | None = None) -> None:
        """检测并恢复被 GitLab autoclose 自动关闭的 issue（issue #109）。

        背景：GitLab 实例开启了 autoclose_referenced_issues——提交信息
        命中默认关闭模式（fix: #NN / fixes #NN / closes #NN 等）且推送
        到默认主分支时，issue 被 GitLab 系统自动关闭（closed_by 为该
        项目的 project bot，非任何真人用户）。graph2plan 任务的提交
        信息「fix: #24 …」曾反复触发，用户侧表现为「agent 自己 close
        issue」（实际 agent 从未调用关闭 API）。

        恢复规则：
        - closed 且 closed_by 是本项目的 project bot（autoclose 特征）
          → reopen + 补说明评论 + warn 日志；
        - closed 但 closed_by 是真实用户（人工关闭）→ 不干预；
        - 任意步骤失败 → 仅记 warn，不阻塞任务成功收尾（本方法为
          尽力而为护栏，任何异常都必须被吞掉）。
        """
        project_id, issue_iid = task["project_id"], task["issue_iid"]
        try:
            issue = self._call_with_fallback(
                repo, lambda c: c.get_issue(project_id, issue_iid))[0]
        except Exception as e:  # noqa: BLE001 护栏方法：任何查询异常都不阻塞收尾
            self.db.add_log(task["id"], "warn",
                            f"autoclose 检测失败（查询 issue 状态出错）: {e}")
            return
        issue = issue or {}
        if issue.get("state") != "closed":
            return
        closed_by = issue.get("closed_by") or {}
        username = closed_by.get("username") or ""
        # autoclose 由该项目的 project bot 执行（username 形如
        # project_<id>_bot_<hash>），其余关闭者视为人工操作
        if not username.startswith(f"project_{project_id}_bot"):
            self.db.add_log(task["id"], "info",
                            "issue 为人工关闭（closed_by 非 project bot），平台不干预")
            return
        try:
            self._call_with_fallback(
                repo, lambda c: c.reopen_issue(project_id, issue_iid))
        except Exception as e:  # noqa: BLE001 同上：reopen 失败不阻塞收尾
            self.db.add_log(task["id"], "warn",
                            f"重新打开被 autoclose 误关的 issue 失败: {e}")
            return
        body = ("补充说明：本 Issue 曾被 GitLab 的 autoclose 机制自动关闭"
                "（提交信息中的 `fix: #N` / `closes #N` 等 issue 引用命中"
                "实例默认关闭模式，随代码推送自动触发，非人工/Agent 主动"
                "关闭操作）。平台已重新打开本 Issue，开发结果请人工验证后"
                "手动关闭。")
        try:
            self._call_with_fallback(
                repo, lambda c: c.add_comment(project_id, issue_iid, body))
        except Exception as e:  # noqa: BLE001 同上：补评论失败不阻塞收尾
            self.db.add_log(task["id"], "warn",
                            f"autoclose 补充说明评论失败: {e}")
        self.db.add_log(task["id"], "warn",
                        "检测到 issue 被 GitLab autoclose 自动关闭，"
                        "已重新打开并补说明评论")
    def _success_summary(self, output: str) -> str:
        """从执行输出提取结果摘要（claude 的 result / hermes 的 final_response）。

        供成功收尾评论使用（issue #79）：两引擎字段都没有 / 为空时返回
        空串（评论省略摘要段）；超长按 COMMENT_TAIL_CHARS 截断。
        """
        data = self._last_json_object(output)
        if not isinstance(data, dict):
            return ""
        summary = data.get("result")
        if not isinstance(summary, str):
            summary = data.get("final_response")
        if not isinstance(summary, str):
            return ""
        summary = summary.strip()
        if not summary:
            return ""
        if len(summary) > COMMENT_TAIL_CHARS:
            summary = summary[:COMMENT_TAIL_CHARS] + "…"
        return summary
    def _record_commit(self, task_id: int,
                       repo: dict | None = None) -> None:
        """任务成功时查询对应提交并落库（issue #19：任务页面 commit 链接）。

        Claude 按模板提交（message 含 "issue #N"）并关闭 issue 后，用
        GitLab commits API 匹配该提交，完整 sha 落库供前端拼链接。
        查询失败/找不到不阻塞任务成功（页面不显示链接即可）。
        """
        task = self.db.get_task(task_id)
        if task is None:
            return
        try:
            sha, _ = self._call_with_fallback(
                repo, lambda c: c.find_commit_for_issue(
                    task["project_id"], task["issue_iid"]))
        except GitLabError as e:
            self.db.add_log(task_id, "warn", f"查询任务提交失败: {e}")
            return
        if sha:
            self.db.set_task_status(task_id, None, commit_sha=sha)
            self.db.add_log(task_id, "info", f"已记录任务提交 {sha[:8]}")
    def _finish_failed(self, task_id: int, reason: str, output: str = "",
                       error_detail: str | None = None,
                       repo: dict | None = None) -> None:
        task = self.db.get_task(task_id)
        # issue #274：任务收尾时对失败原因做规则分类（env/engine/unsolvable/
        # unknown），结果落库 tasks.failure_category——详情页展示分类徽章+建议、
        # 失败评论带分类前缀、统计看板按分类聚合。综合失败原因、错误详情与
        # 执行输出三路文本匹配，未命中兜底 unknown（不抛错）。
        category = classify_failure(
            reason, error_detail or "", output,
            rules=self.config.get().failure_classify_rules)
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_FAILED,
                exit_code=None,
                error_message=reason,
                error_detail=error_detail,
                failure_category=category,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 失败收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(
            task_id, "error",
            f"任务失败: {reason}（失败分类：{category_label(category)}）")
        self._write_log_tail(task_id, output)

        # 在 issue 上留失败评论 + 打标签
        if task:
            # issue #252：失败任务输出结构化失败报告（失败原因 + 相关文件 +
            # 测试摘要 + 日志尾部）；无改动/无测试数据时对应段落自动隐藏
            tail = self._tail_output(output)
            log_tail = ""
            if tail and tail != output.strip():
                log_tail = f"```\n{tail[-COMMENT_TAIL_CHARS:]}\n```"
            body = self._build_report_comment(
                task, output, repo=repo, failed=True,
                reason=reason, log_tail=log_tail)
            # issue #274：失败评论同步带分类前缀（分类徽章 + 处理建议），
            # 无论默认/自定义模版都保证用户第一眼看到失败分类；兜底
            # unknown 同样带「未知」徽章，不抛错
            prefix = (f"> **失败分类：{category_label(category)}（{category}）**"
                      f" — {category_advice(category)}\n\n")
            body = prefix + body
            if not body.strip():
                # 兜底：渲染为空（极端场景）时保留最小可读文案
                body = f"🤖 Botler 自动回复：无法完成此 issue。\n\n**原因**：{reason}"
            # issue #280：GitLab 短暂不可用时评论/标签只试一次会因 502 发不出
            # （08-17 事故：失败评论与 bot-failed 标签全部 502 失败，issue 上
            # 「没有任何回复评论」）。瞬时故障退避重试，恢复后仍能送达。
            try:
                self._transient_retry(
                    "留失败评论",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_comment(
                            task["project_id"], task["issue_iid"], body)))
                self.db.add_log(task_id, "info", "已在 issue 上留失败评论")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留失败评论失败: {e}")
            try:
                self._transient_retry(
                    "打 bot-failed 标签",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_labels(
                            task["project_id"], task["issue_iid"], ["bot-failed"],
                            remove=["in-progress"])))
                self.db.add_log(task_id, "info", "已打 bot-failed 标签")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"打 bot-failed 标签失败: {e}")
            # 网页通知：任务需要人工介入（issue #21）
            self._emit_task_event(task_id, "task_failed", reason)
        logger.warning("任务 %s 失败: %s", task_id, reason)
    def _finish_asked(self, task_id: int, output: str,
                      repo: dict | None = None) -> None:
        """「等待用户决策」收尾（issue #67）：Claude 的提问反馈到 issue，任务判 failed。

        无人值守下 Claude 停在需要用户决策的提问节点后自行退出，任务实际
        未完成（无提交、无 CI）——不能判 succeeded 打 bot-done，也不能按
        普通失败重试（重试仍会停在同一个问题）。把提问原文贴到 issue 评论，
        打 blocked 标签（不在领取过滤标签中）：用户回复后经重新指派或
        对账扫描再次入队，新任务可读到回复后继续处理。
        """
        task = self.db.get_task(task_id)
        # issue #274：等待用户决策按「无法解决类（unsolvable）」分类落库
        # （agent 停在提问节点、无法独立继续，处理建议引导用户回复/改描述）
        category = classify_failure(
            "Claude 在执行中遇到需要用户决策的问题，等待用户回复",
            rules=self.config.get().failure_classify_rules)
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_FAILED,
                exit_code=None,
                error_message="Claude 在执行中遇到需要用户决策的问题，"
                              "提问已反馈至 issue，等待用户回复后重新处理",
                failure_category=category,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 提问收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(task_id, "error",
                        "任务未完成：Claude 停在等待用户决策的提问节点，问题已反馈到 issue")
        self._write_log_tail(task_id, output)

        if task:
            question = self._extract_question(output)
            comment = (
                f"> **失败分类：{category_label(category)}（{category}）**"
                f" — {category_advice(category)}\n\n"
                "🤖 Botler 自动回复：Claude 在执行中遇到需要您决策的问题，"
                "暂时无法继续，请回复后重新处理。\n\n"
                "**Claude 的问题**：\n\n"
                f"{question}\n\n"
                "请在本 issue 直接回复您的选择，回复后 bot 会重新领取处理。")
            # issue #280：瞬时故障退避重试，保证用户能收到提问反馈
            try:
                self._transient_retry(
                    "留提问评论",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_comment(
                            task["project_id"], task["issue_iid"], comment)))
                self.db.add_log(task_id, "info", "已在 issue 上留提问评论，等待用户回复")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留提问评论失败: {e}")
            try:
                self._transient_retry(
                    "打 blocked 标签",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_labels(
                            task["project_id"], task["issue_iid"], ["blocked"],
                            remove=["in-progress"])))
                self.db.add_log(task_id, "info", "已打 blocked 标签，等待用户回复")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"打 blocked 标签失败: {e}")
            # 网页通知：任务需要用户决策（issue #21 渠道复用失败通知）
            self._emit_task_event(task_id, "task_failed",
                                  "Claude 等待用户决策，问题已反馈到 issue")
        logger.warning("任务 %s 等待用户决策，问题已反馈到 issue", task_id)
    def _write_log_tail(self, task_id: int, output: str) -> None:
        tail = self._tail_output(output)
        if not tail:
            return
        try:
            with open(self._log_file(task_id), "a", encoding="utf-8", errors="replace") as f:
                f.write("\n----- 执行结束（摘要）-----\n" + tail + "\n")
        except OSError:
            pass
