"""任务执行引擎插件（issue #140）。

把现有三引擎分发（issue #47/#84，executor.py 的 if/else）迁移为插件体系：
每个引擎是一个 ``ExecutorPlugin`` 插件，``ClaudeExecutor._run_once`` 按
``worker.engine`` 配置的引擎名查插件并委托执行。

内置插件（适配器委托现有实现，内部逻辑不动）：
- ``claude``：Claude Code CLI 无头模式（默认引擎）
- ``hermes``：hermes-agent runner 脚本（issue #47）
- ``dsh``：deepseek-harness SDK 进程内调用（issue #84）

新增引擎：实现 ``ExecutorPlugin.run`` 并注册（或外部插件模块加载），
``worker.engine`` 配置该引擎名即可启用，核心模块零改动。
"""

from __future__ import annotations

from typing import Any

from .base import ExecutorPlugin, register_plugin


def _row_get(row: dict | None, key: str, default=None):
    """从 SQLite 行（sqlite3.Row 或 dict）取字段，缺失返回默认值。"""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


class ClaudeEnginePlugin(ExecutorPlugin):
    """Claude Code CLI 无头模式（默认执行引擎）。

    断点续跑（issue #8）：resume_session 非空时 claude --resume 接续上次
    会话（工作区保留）；执行后解析 JSON 输出中的 session_id 落库。
    """

    name = "claude"
    description = "Claude Code CLI 无头模式（默认执行引擎）"

    def run(self, executor: Any, task_id: int, repo: dict, issue: dict,
            resume_session: str | None = None,
            resume_history: list | None = None) -> tuple[int, str]:
        return executor._run_claude_once(task_id, repo, issue, resume_session)


class HermesEnginePlugin(ExecutorPlugin):
    """hermes-agent 引擎（issue #47）：runner 脚本子进程调用。

    断点续跑：resume_history 为上次会话历史（工作区保留），显式传入优先；
    未传入时从任务落库的 hermes_history 解析（含会话 id），等价
    Q3-B conversation_history 落库断点续跑。
    """

    name = "hermes"
    description = "hermes-agent 引擎（经 hermes_runner.py 进程内调用，issue #47）"

    def run(self, executor: Any, task_id: int, repo: dict, issue: dict,
            resume_session: str | None = None,
            resume_history: list | None = None) -> tuple[int, str]:
        task_row = executor.db.get_task(task_id)
        messages, sid = executor._hermes_resume_data(
            _row_get(task_row, "hermes_history") if task_row is not None else None)
        if resume_history is not None:
            messages = resume_history
        return executor._run_hermes_once(task_id, repo, issue, messages, sid)


class DshEnginePlugin(ExecutorPlugin):
    """deepseek-harness SDK 引擎（issue #84）：SDK 进程内调用。

    断点续跑：resume_session 为上次会话 id（SDK 在 session_root 持久化
    会话，同一 id 接续对话；工作区保留）。
    """

    name = "dsh"
    description = "deepseek-harness SDK 引擎（进程内调用，issue #84）"

    def run(self, executor: Any, task_id: int, repo: dict, issue: dict,
            resume_session: str | None = None,
            resume_history: list | None = None) -> tuple[int, str]:
        return executor._run_dsh_once(task_id, repo, issue, resume_session)


# 模块导入即注册内置引擎插件（注册顺序 = 展示顺序）
register_plugin(ClaudeEnginePlugin())
register_plugin(HermesEnginePlugin())
register_plugin(DshEnginePlugin())
