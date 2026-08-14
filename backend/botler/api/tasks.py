"""任务 API：列表（分页/过滤）、详情（含日志）、日志、实时执行（issue #20）、
SSE 事件流（实时输出功能）。"""

from __future__ import annotations

import json
from pathlib import Path
from queue import Empty

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from botler.events import parse_claude_stream_line, parse_hermes_event_line
from botler.executor import (
    find_session_file, format_display_line, parse_transcript, read_log_delta,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

STATUSES = {"queued", "running", "retrying", "succeeded", "failed", "interrupted"}
# 任务仍可能产出新事件的活跃状态（SSE 实时推送期间订阅总线）
LIVE_STATUSES = ("queued", "running", "retrying")


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
    # issue #62：包含已软删除的仓库，任务历史仍能解析出仓库名
    repos = {r["id"]: {"name": r["name"], "url": r["url"]}
             for r in c.db.list_repos(include_deleted=True)}
    return {
        "tasks": [_task_to_dict(r, repos.get(r["repo_id"])) for r in rows],
        # total 与 list_tasks 同套过滤条件（issue #50 翻页组件按 total 计算总页数）
        "total": c.db.count_tasks(status=statuses, repo_id=repo_id, search=search),
        "stats": c.db.task_stats(),
    }


@router.post("/stop-all")
def stop_all_tasks(request: Request):
    """一键停止所有活跃任务（issue #35）。

    排队/执行/重试中的任务统一标记 interrupted（终态），执行中的
    claude 进程组被强制终止；被停止的任务不会在平台重启后自动恢复。
    返回被停止的任务 id 列表与数量（无活跃任务时为空列表）。
    """
    c = ctx_of(request)
    stopped = c.scheduler.stop_all()
    return {"stopped": stopped, "count": len(stopped)}


@router.post("/reconcile-all")
def reconcile_all(request: Request):
    """一键对账所有启用仓库（issue #38）。

    同步执行全量对账扫描：把所有启用仓库中「assignee 是 bot 但任务表无
    活跃记录」的 open issues 补入队。与仓库页单仓库对账
    （/repos/{id}/reconcile，issue #17）一致，同步执行并直接返回结果；
    多仓库场景下单个仓库失败不中断整体，失败明细放入 errors 列表返回。
    """
    c = ctx_of(request)
    result = c.reconciler.reconcile_once()
    return {"ok": True, "scanned": result["scanned"], "enqueued": result["enqueued"],
            "errors": result.get("errors", [])}


@router.post("/{task_id}/retry")
def retry_task(request: Request, task_id: int):
    """手动重试任务（issue #36）：终态失败任务重新入队执行。

    仅 failed（失败）与 interrupted（已中断）状态可重试；同 issue 已有
    活跃任务时返回 409（去重索引冲突）。成功后任务回到调度器仓库 FIFO
    队列由 worker 正常领取执行，保留 claude 会话断点续跑（接续上次进度）。
    """
    c = ctx_of(request)
    result = c.db.retry_task(task_id)
    if result == "not_found":
        raise HTTPException(404, "任务不存在")
    if result == "bad_state":
        raise HTTPException(400, "仅失败（failed）或已中断（interrupted）的任务可手动重试")
    if result == "conflict":
        raise HTTPException(409, "该 issue 已有活跃任务，无法重试")
    c.scheduler.enqueue(task_id)
    return {"task_id": task_id, "status": "queued"}


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


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _event_stream(ctx, task_id: int):
    """SSE 事件流生成器：回放日志已有事件 → 实时订阅总线 → done 收尾。

    - 回放：日志文件（执行器逐行写入的引擎原始输出）按行解析为归一化
      事件，seq 从 1 递增重算（与执行时发布顺序一致，断线重连/终态
      回看时前端按 seq 去重不重复渲染）
    - 实时：任务仍活跃时订阅 executor 事件总线，零延迟推送；15s 无事件
      发心跳注释行（EventSource 忽略）；每次事件后检查任务终态，终态
      即收尾（客户端断开时生成器随响应取消自然退出）
    - done：流结束哨兵事件（前端据此关闭连接）
    """
    engine = str(getattr(ctx.config.get(), "engine", "") or "claude").strip().lower()
    parser = parse_claude_stream_line if engine != "hermes" else parse_hermes_event_line

    row = ctx.db.get_task(task_id)
    # 注意 sqlite3.Row 无 .get()（issue #11），统一索引访问
    log_path = row["log_path"] if row is not None else None
    # 先订阅再回放：回放逐行 yield 的间隙 executor 仍在发布事件，若订阅
    # 在回放之后建立，间隙事件会丢失（总线不保留订阅前事件）。先订阅
    # 让回放期间的实时事件在队列中积累，回放完成后排空 → 无缝衔接；
    # 队列满丢最旧安全（更早的历史已由回放覆盖）
    sub = (ctx.executor.event_bus.subscribe(task_id)
           if row is not None and row["status"] in LIVE_STATUSES else None)
    seq = 0
    try:
        # 回放日志已有事件（文件缺失/不可读静默跳过）
        if log_path and Path(log_path).is_file():
            try:
                text = Path(log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for line in text.splitlines():
                events = parser(line.strip())
                if not events:
                    continue
                for event in events:
                    seq += 1
                    event["seq"] = seq
                    event.setdefault("ts", "")
                    yield _sse(event)

        # 实时推送（任务活跃期间）：排空订阅队列，无事件时 15s 心跳；
        # 每次事件后检查任务终态，终态即收尾（客户端断开时生成器随
        # 响应取消自然退出）
        if sub is not None:
            while True:
                try:
                    event = sub.get(timeout=15)
                except Empty:
                    yield ": ping\n\n"
                    if ctx.db.get_task(task_id)["status"] not in LIVE_STATUSES:
                        break
                    continue
                event.setdefault("ts", "")
                yield _sse(event)
                if ctx.db.get_task(task_id)["status"] not in LIVE_STATUSES:
                    break
    finally:
        if sub is not None:
            sub.close()
    yield _sse({"kind": "done", "seq": seq + 1, "ts": ""})


@router.get("/{task_id}/events")
def task_events(request: Request, task_id: int):
    """实时事件流（SSE）：任务执行过程逐事件推送 + 历史回放。

    连接建立即回放日志文件已有事件（终态任务=完整回放后 done 收尾；
    运行中任务=回放后续接总线实时推送）。断线重连（EventSource 自动）
    重新走回放路径，天然补齐断档且不重复（前端按 seq 去重）。
    """
    c = ctx_of(request)
    if c.db.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(_event_stream(c, task_id),
                             media_type="text/event-stream")


def ctx_of(request: Request):
    return request.app.state.ctx
