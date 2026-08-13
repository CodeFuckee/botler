"""任务 API：列表（分页/过滤）、详情（含日志）、日志、实时执行（issue #20）。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from botler.executor import (
    find_session_file, format_display_line, parse_transcript, read_log_delta,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

STATUSES = {"queued", "running", "retrying", "succeeded", "failed", "interrupted"}


def _commit_url(repo_url: str | None, sha: str | None) -> str | None:
    """拼接任务提交的 GitLab 页面地址（issue #19）。

    仓库 URL 为 http_url_to_repo（如 https://host/group/proj.git），
    去掉 .git 后缀后接 /-/commit/<sha>；URL 或 sha 缺失返回 None。
    """
    if not repo_url or not sha:
        return None
    base = repo_url[:-4] if repo_url.endswith(".git") else repo_url
    return f"{base}/-/commit/{sha}"


def _issue_url(repo_url: str | None, issue_iid: int | None) -> str | None:
    """拼接任务对应 issue 的 GitLab 页面地址（issue #32 概览页）。

    与 _commit_url 同理：仓库 URL 去 .git 后缀后接 /-/issues/<iid>；
    URL 或 issue_iid 缺失返回 None。
    """
    if not repo_url or not issue_iid:
        return None
    base = repo_url[:-4] if repo_url.endswith(".git") else repo_url
    return f"{base}/-/issues/{issue_iid}"


def _task_to_dict(row, repo: dict | None = None) -> dict:
    """把任务行转为 API 字典。

    error_detail 为 executor 写入的 JSON 字符串（每次尝试的失败详情），
    这里解析成结构化对象供前端「查看详细原因」按钮使用；解析失败返回 None。
    repo 为仓库信息 dict（含 name/url），用于拼接 repo_name 与 commit_url。
    """
    detail = None
    if row["error_detail"]:
        try:
            detail = json.loads(row["error_detail"])
        except ValueError:
            detail = None
    repo_url = repo.get("url") if repo else None
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "repo_name": repo.get("name") if repo else None,
        "project_id": row["project_id"],
        "issue_iid": row["issue_iid"],
        "issue_title": row["issue_title"],
        "issue_url": _issue_url(repo_url, row["issue_iid"]),  # issue #32 概览页
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "triggered_by": row["triggered_by"],
        "exit_code": row["exit_code"],
        "error_message": row["error_message"],
        "error_detail": detail,
        "resumed": bool(row["claude_session_id"]),  # 会话断点续跑标记（issue #8）
        "commit_sha": row["commit_sha"],
        "commit_url": _commit_url(repo_url, row["commit_sha"]),  # issue #19
        "log_path": row["log_path"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


@router.get("")
def list_tasks(
    request: Request,
    status: str | None = Query(None),
    repo_id: int | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    c = ctx_of(request)
    # status 支持逗号分隔多值（issue #32 概览页一次拉取 running+retrying），
    # 单值行为不变；多值会转为列表传给 db 层（list_tasks / count_tasks）。
    statuses: str | list[str] | None = status
    if status:
        statuses = [s.strip() for s in status.split(",")]
        unknown = [s for s in statuses if s not in STATUSES]
        if unknown:
            raise HTTPException(400, f"未知状态: {','.join(unknown)}（可选: {sorted(STATUSES)}）")
        if len(statuses) == 1:
            statuses = statuses[0]
    rows = c.db.list_tasks(status=statuses, repo_id=repo_id, search=search,
                           limit=limit, offset=offset)
    repos = {r["id"]: {"name": r["name"], "url": r["url"]} for r in c.db.list_repos()}
    return {
        "tasks": [_task_to_dict(r, repos.get(r["repo_id"])) for r in rows],
        "total": c.db.count_tasks(status=statuses),
        "stats": c.db.task_stats(),
    }


@router.get("/{task_id}")
def get_task(request: Request, task_id: int):
    c = ctx_of(request)
    row = c.db.get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    repo = c.db.get_repo(row["repo_id"])
    task = _task_to_dict(row, dict(repo) if repo else None)
    task["logs"] = [dict(l) for l in c.db.list_logs(task_id)]
    # 附上完整执行日志文件尾部（stdout/stderr）
    file_tail = None
    if row["log_path"]:
        try:
            with open(row["log_path"], "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            # claude JSON 输出行重排为可读文本（result 嵌套转义解码，issue #16）
            file_tail = "\n".join(format_display_line(l) for l in lines[-200:])
        except OSError:
            file_tail = None
    task["log_file_tail"] = file_tail
    return task


@router.get("/{task_id}/logs")
def task_logs(request: Request, task_id: int, limit: int = Query(500, ge=1, le=5000)):
    c = ctx_of(request)
    if c.db.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return {"logs": [dict(l) for l in c.db.list_logs(task_id, limit=limit)]}


@router.get("/{task_id}/execution")
def task_execution(request: Request, task_id: int,
                   after_byte: int = Query(0, ge=0)):
    """实时查看任务执行（issue #20）：日志增量 + 聊天记录。

    - log_delta：执行日志文件（claude stdout 实时追加）从 after_byte 起的
      新增行（每行经 format_display_line 解码为可读文本），log_offset 为
      下一轮应传的字节偏移（尾部半行自动回退，等补全后再返回）。
    - transcript：claude 会话文件解析出的聊天消息（user/assistant/
      tool_use/tool_result），任务运行中即可实时读取（executor 已提前
      落库 session_id）。会话文件缺失/解析失败返回空列表不报错。
    """
    c = ctx_of(request)
    row = c.db.get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")

    log_delta: list[str] = []
    log_offset = after_byte
    if row["log_path"]:
        lines, log_offset = read_log_delta(Path(row["log_path"]), after_byte)
        log_delta = [format_display_line(l) for l in lines]

    transcript: list[dict] = []
    truncated = False
    session_id = row["claude_session_id"]
    if session_id:
        session_file = find_session_file(session_id)
        if session_file:
            transcript, truncated = parse_transcript(session_file)

    return {
        "status": row["status"],
        "session_id": session_id,
        "log_offset": log_offset,
        "log_delta": log_delta,
        "transcript": transcript,
        "transcript_truncated": truncated,
    }


def ctx_of(request: Request):
    return request.app.state.ctx
