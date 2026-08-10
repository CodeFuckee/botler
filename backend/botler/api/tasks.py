"""任务 API：列表（分页/过滤）、详情（含日志）、日志。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/tasks", tags=["tasks"])

STATUSES = {"queued", "running", "retrying", "succeeded", "failed", "interrupted"}


def _task_to_dict(row, repo_name: str | None = None) -> dict:
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "repo_name": repo_name,
        "project_id": row["project_id"],
        "issue_iid": row["issue_iid"],
        "issue_title": row["issue_title"],
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "triggered_by": row["triggered_by"],
        "exit_code": row["exit_code"],
        "error_message": row["error_message"],
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
    if status and status not in STATUSES:
        raise HTTPException(400, f"未知状态: {status}（可选: {sorted(STATUSES)}）")
    rows = c.db.list_tasks(status=status, repo_id=repo_id, search=search,
                           limit=limit, offset=offset)
    repo_names = {r["id"]: r["name"] for r in c.db.list_repos()}
    return {
        "tasks": [_task_to_dict(r, repo_names.get(r["repo_id"])) for r in rows],
        "total": c.db.count_tasks(status=status),
        "stats": c.db.task_stats(),
    }


@router.get("/{task_id}")
def get_task(request: Request, task_id: int):
    c = ctx_of(request)
    row = c.db.get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    repo = c.db.get_repo(row["repo_id"])
    task = _task_to_dict(row, repo["name"] if repo else None)
    task["logs"] = [dict(l) for l in c.db.list_logs(task_id)]
    # 附上完整执行日志文件尾部（stdout/stderr）
    file_tail = None
    if row["log_path"]:
        try:
            with open(row["log_path"], "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            file_tail = "\n".join(lines[-200:])
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


def ctx_of(request: Request):
    return request.app.state.ctx
