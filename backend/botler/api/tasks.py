"""任务 API：列表（分页/过滤）、详情（含日志）、日志、实时执行（issue #20）、
SSE 事件流（实时输出功能）、数据导出（issue #228）。"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from queue import Empty

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from botler.env_snapshot import parse_snapshot
from botler.events import parse_claude_stream_line, parse_hermes_event_line
from botler.executor import (
    find_session_file, format_display_line, parse_transcript, read_log_delta,
    read_session_prompt,
)
from botler.failure_classify import category_advice
from botler.repo_params import effective_task_params

router = APIRouter(prefix="/tasks", tags=["tasks"])

STATUSES = {"queued", "running", "retrying", "succeeded", "failed", "interrupted", "canceled_by_user"}
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


def _usage_to_dict(row) -> dict | None:
    """把 task_usage 行转为 API dict（issue #235：token 用量卡片数据）。

    raw_usage 为引擎采集的原始 usage JSON（claude usage / dsh 聚合 /
    hermes 会话计数器），解析失败返回 None 不报错；无记录返回 None。
    """
    if row is None:
        return None
    raw = None
    if row["raw_usage"]:
        try:
            raw = json.loads(row["raw_usage"])
        except (ValueError, TypeError):
            raw = None
    return {
        "engine": row["engine"] or "",
        "model": row["model"] or None,
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "estimated_cost": row["estimated_cost"],
        "currency": row["currency"] or "USD",
        "raw_usage": raw,
    }


def _task_to_dict(row, repo: dict | None = None, usage_row=None,
                   settings=None) -> dict:
    """把任务行转为 API 字典。

    error_detail 为 executor 写入的 JSON 字符串（每次尝试的失败详情），
    这里解析成结构化对象供前端「查看详细原因」按钮使用；解析失败返回 None。
    repo 为仓库信息 dict（含 name/url 与仓库级覆盖字段），用于拼接
    repo_name / commit_url 与解析任务生效参数。
    usage_row（issue #235）：该任务最近一次执行的 token 用量行（无则 None）。
    settings（issue #237）：全局配置，配合 repo 按「仓库级 > 全局」解析
    该任务实际生效的超时/重试/引擎与来源；未传入（无仓库上下文等）时
    生效字段返回 None，前端展示「—」。
    """
    detail = None
    if row["error_detail"]:
        try:
            detail = json.loads(row["error_detail"])
        except ValueError:
            detail = None
    repo_url = repo.get("url") if repo else None
    # issue #237：任务实际生效参数——按「仓库级 > 全局」解析（仓库字段
    # 留空 = 继承全局）；settings 未传入（旧调用方）时返回 None，前端兜底
    eff = (effective_task_params(repo, settings)
           if settings is not None else None)
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
        # issue #274：任务失败原因自动分类——收尾时规则分类落库
        # tasks.failure_category；failure_advice 为对应处理建议文案
        # （详情页展示分类徽章 + 建议；空分类返回空串，前端不报错）
        "failure_category": row["failure_category"] or "",
        # issue #242：任务人工优先级——任务列表页/概览页对排队任务
        # 「置顶/上移/下移/置底」调整的字段（NULL = 按系统规则排序；
        # 调度器派发时 manual_priority 优先于仓库/标签规则）
        "manual_priority": row["manual_priority"],
        "failure_advice": (category_advice(row["failure_category"])
                           if row["failure_category"] else ""),
        "resumed": bool(row["claude_session_id"] or row["dsh_session_id"]),
        # 会话断点续跑标记（issue #8 claude / issue #84 dsh；任一引擎恢复
        # 过会话即为 true。issue #281 起 dsh 会话 id 任务开始即落库）
        "dsh_session_id": row["dsh_session_id"] or None,
        # issue #281：dsh 引擎会话 id（任务开始即落库，中断恢复凭此 id
        # 经 DeepSeek Harness SDK resume；前端任务详情页展示供人工排查）
        # issue #120：执行引擎按任务落库——任务页/概览页展示该任务实际
        # 使用的引擎（claude / hermes / dsh；未执行或旧任务可能为空串）
        "engine": row["engine"] or "",
        # issue #237：任务实际生效的超时/重试/引擎与来源（仓库级覆盖 or
        # 继承全局）——任务列表/详情展示「生效参数」用；与 executor 执行
        # 时解析口径一致（effective_task_params），展示与执行不脱节
        "timeout_seconds": eff["timeout_seconds"] if eff else None,
        "timeout_source": eff["timeout_source"] if eff else None,
        "max_retries": eff["max_retries"] if eff else None,
        "max_retries_source": eff["max_retries_source"] if eff else None,
        "effective_engine": eff["engine"] if eff else None,
        "engine_source": eff["engine_source"] if eff else None,
        # issue #236：引擎降级原因——主引擎不可用自动降级到备用引擎时的
        # 原因文案（如「引擎 claude 不可用（...），已降级 dsh 执行」）；
        # 未发生降级为空串，任务详情页展示
        "engine_fallback": row["engine_fallback"] or "",
        # issue #276：任务执行环境快照（引擎版本/模型/起始提交/平台版本/
        # config hash JSON）；旧任务无快照返回 None
        "environment": parse_snapshot(row["environment"]),
        "commit_sha": row["commit_sha"],
        "commit_url": _commit_url(repo_url, row["commit_sha"]),  # issue #19
        "log_path": row["log_path"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
        # issue #235：任务 token 用量（engine/model/prompt/completion/
        # total/estimated_cost/currency/raw_usage；无用量数据为 None，
        # 前端显示「无数据」而不是报错）
        "usage": _usage_to_dict(usage_row),
    }


@router.get("")
def list_tasks(
    request: Request,
    status: str | None = Query(None),
    repo_id: int | None = Query(None),
    search: str | None = Query(None),
    include_usage: bool = Query(False),
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
    # issue #62：包含已软删除的仓库，任务历史仍能解析出仓库名；
    # issue #237：带出仓库级覆盖字段（timeout_seconds/max_retries/engine）
    # 供按「仓库级 > 全局」解析任务生效参数
    repos = {r["id"]: {"name": r["name"], "url": r["url"],
                       "timeout_seconds": r["timeout_seconds"],
                       "max_retries": r["max_retries"],
                       "engine": r["engine"]}
             for r in c.db.list_repos(include_deleted=True)}
    # issue #235：任务列表可选展示 token 用量（include_usage=1 时批量
    # 查询，避免逐任务 N+1；默认不查询，列表无额外开销）
    usage_map = (c.db.get_task_usage_map([r["id"] for r in rows])
                 if include_usage else {})
    return {
        "tasks": [_task_to_dict(r, repos.get(r["repo_id"]),
                                usage_map.get(r["id"]),
                                c.config.get()) for r in rows],
        # total 与 list_tasks 同套过滤条件（issue #50 翻页组件按 total 计算总页数）
        "total": c.db.count_tasks(status=statuses, repo_id=repo_id, search=search),
        "stats": c.db.task_stats(),
    }


# ---- 任务数据导出（issue #228）----
# CSV 表头用中文（Excel 直接可读，UTF-8 BOM 保证中文不乱码），JSON 用
# 英文 key（供离线脚本/工具解析）；两者字段集合一致、顺序即列顺序。
EXPORT_COLUMNS = [
    ("id", "id"),
    ("repo_id", "仓库ID"),
    ("repo_name", "仓库"),
    ("project_id", "项目ID"),
    ("issue_iid", "Issue编号"),
    ("issue_title", "Issue标题"),
    ("issue_url", "Issue链接"),
    ("status", "状态"),
    ("engine", "引擎"),
    ("triggered_by", "来源"),
    ("attempt_count", "尝试次数"),
    ("exit_code", "退出码"),
    ("error_message", "错误信息"),
    ("failure_category", "失败分类"),
    ("commit_sha", "提交SHA"),
    ("commit_url", "提交链接"),
    ("created_at", "创建时间"),
    ("started_at", "开始时间"),
    ("finished_at", "结束时间"),
    ("duration_seconds", "用时(秒)"),
]


def _normalize_date_bound(value: str, start: bool) -> str:
    """时间范围参数归一化（issue #228）：'YYYY-MM-DD' 补齐当日边界
    （start → 00:00:00，end → 23:59:59），完整 'YYYY-MM-DD HH:MM:SS'
    原样返回，与 tasks.created_at（UTC 无时区串）同格式可直接字符串
    比较。格式不合法抛 ValueError（API 层转 400）。
    """
    v = value.strip()
    try:
        if len(v) == 10:
            dt = datetime.strptime(v, "%Y-%m-%d")
            return (dt.strftime("%Y-%m-%d 00:00:00") if start
                    else dt.strftime("%Y-%m-%d 23:59:59"))
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError("应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS")


def _export_task_row(row, repo: dict | None) -> dict:
    """任务行 → 导出扁平字典（issue #228）。

    repo 为仓库信息 dict（含 name/url），可能为 None——仓库已软删除时
    任务历史仍可导出，仓库名/链接为空。用时 = finished_at - created_at
    （与任务列表「用时」、概览统计 issue #180 语义一致）；缺时间字段或
    时钟异常（负值）返回 None 不报错。
    """
    repo_url = repo.get("url") if repo else None
    duration: int | None = None
    if row["created_at"] and row["finished_at"]:
        try:
            start = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(row["finished_at"], "%Y-%m-%d %H:%M:%S")
            secs = int((end - start).total_seconds())
            duration = secs if secs >= 0 else None
        except (TypeError, ValueError):
            duration = None
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "repo_name": repo.get("name") if repo else None,
        "project_id": row["project_id"],
        "issue_iid": row["issue_iid"],
        "issue_title": row["issue_title"],
        "issue_url": _issue_url(repo_url, row["issue_iid"]),
        "status": row["status"],
        "engine": row["engine"] or "",
        "triggered_by": row["triggered_by"],
        "attempt_count": row["attempt_count"],
        "exit_code": row["exit_code"],
        "error_message": row["error_message"],
        "failure_category": row["failure_category"] or "",
        "commit_sha": row["commit_sha"],
        "commit_url": _commit_url(repo_url, row["commit_sha"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_seconds": duration,
    }


@router.get("/export")
def export_tasks(
    request: Request,
    format: str = Query("csv", pattern="^(csv|json)$"),
    status: str | None = Query(None),
    repo_id: int | None = Query(None),
    search: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    """导出任务数据（issue #228）：CSV / JSON 文件下载。

    过滤条件与任务列表一致（status 支持逗号分隔多值、repo_id、search），
    另支持按任务创建时间范围过滤（date_from / date_to，'YYYY-MM-DD' 或
    'YYYY-MM-DD HH:MM:SS'，按 tasks.created_at 匹配）。CSV 带 UTF-8 BOM
    （\ufeff）——Excel 直接打开中文不乱码，字段含 id/仓库/issue/状态/引擎/
    用时/错误/时间等；JSON 为同字段的扁平对象数组（英文 key），供离线
    分析脚本直接 json.load。响应均为 attachment 下载。
    """
    c = ctx_of(request)
    statuses: str | list[str] | None = status
    if status:
        statuses = [s.strip() for s in status.split(",")]
        unknown = [s for s in statuses if s not in STATUSES]
        if unknown:
            raise HTTPException(400, f"未知状态: {','.join(unknown)}（可选: {sorted(STATUSES)}）")
        if len(statuses) == 1:
            statuses = statuses[0]
    try:
        from_dt = _normalize_date_bound(date_from, start=True) if date_from else None
        to_dt = _normalize_date_bound(date_to, start=False) if date_to else None
    except ValueError as e:
        raise HTTPException(400, f"时间范围参数格式错误: {e}")
    if from_dt and to_dt and from_dt > to_dt:
        raise HTTPException(400, "date_from 不能晚于 date_to")

    rows = c.db.list_tasks_export(status=statuses, repo_id=repo_id, search=search,
                                  date_from=from_dt, date_to=to_dt)
    # 与列表一致：包含已软删除仓库，任务历史仍能解析出仓库名（issue #62）
    repos = {r["id"]: {"name": r["name"], "url": r["url"]}
             for r in c.db.list_repos(include_deleted=True)}
    records = [_export_task_row(r, repos.get(r["repo_id"])) for r in rows]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if format == "json":
        payload = json.dumps(records, ensure_ascii=False, indent=2)
        return Response(
            content=payload,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="tasks_export_{ts}.json"'},
        )

    # CSV：\ufeff BOM 前缀 + \r\n 行终止——Excel 直接打开中文不乱码
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([h for _, h in EXPORT_COLUMNS])
    for rec in records:
        writer.writerow([rec.get(k) if rec.get(k) is not None else ""
                         for k, _ in EXPORT_COLUMNS])
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="tasks_export_{ts}.csv"'},
    )


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

    仅 failed（失败）/ interrupted（已中断）/ canceled_by_user（移出队列）
    状态可重试；同 issue 已有活跃任务时返回 409（去重索引冲突）。成功后
    任务回到调度器仓库 FIFO 队列由 worker 正常领取执行，保留 claude 会话
    断点续跑（接续上次进度）。
    """
    c = ctx_of(request)
    result = c.db.retry_task(task_id)
    if result == "not_found":
        raise HTTPException(404, "任务不存在")
    if result == "bad_state":
        raise HTTPException(
            400, "仅失败（failed）、已中断（interrupted）或已移出队列（canceled_by_user）的任务可手动重试")
    if result == "conflict":
        raise HTTPException(409, "该 issue 已有活跃任务，无法重试")
    # issue #69：清除历史停止请求残留——一键停止过的任务重试后，若旧请求
    # 仍登记在 executor，worker 领取时会命中检查被立即打回 interrupted
    c.executor.clear_stop_request(task_id)
    c.scheduler.enqueue(task_id)
    return {"task_id": task_id, "status": "queued"}


@router.post("/{task_id}/stop")
def stop_task(request: Request, task_id: int):
    """手动停止单个任务（issue #214：任务列表/详情页「停止」按钮）。

    仅活跃任务（queued/running/retrying）可停止：状态落库为 interrupted
    （终态，重启后不会自动恢复执行）→ 登记停止请求并终止执行中引擎
    进程（executor.request_stop，幂等：进程未创建时仅登记，worker 领取
    时 _stop_requested 检查命中即 _finish_stopped）→ 排队中的任务从
    调度器内存队列移除。顺序保证与 stop_all 一致：先落库再登记停止请求，
    worker 感知停止请求时状态必已 interrupted。
    停止不可逆：与一键停止（issue #35）一致，用户需在 UI 确认后调用。
    """
    c = ctx_of(request)
    result = c.db.stop_task(task_id)
    if result == "not_found":
        raise HTTPException(404, "任务不存在")
    if result == "bad_state":
        raise HTTPException(400, "仅排队中（queued）、执行中（running）或重试中（retrying）的任务可停止")
    c.executor.request_stop(task_id)
    c.scheduler.remove_queued(task_id)
    return {"task_id": task_id, "status": "interrupted"}


@router.post("/{task_id}/priority")
def set_task_priority(request: Request, task_id: int, action: str = Query(...)):
    """排队任务人工优先级操作（issue #242）。

    action 取值：
    - top：置顶——人工优先级设为同仓库排队任务最前（0）
    - up：上移——与前一任务交换（未设置人工优先级的上移到手动序列尾）
    - down：下移——与后一任务交换
    - bottom：置底——移到手动序列末尾
    - clear：清除人工优先级（NULL）——恢复按系统规则排序
    仅排队中（queued）任务可操作；已 running 任务不受影响（返回 400）。
    操作结果写入 task_logs 供审计追溯。
    """
    c = ctx_of(request)
    if action not in ("top", "up", "down", "bottom", "clear"):
        raise HTTPException(400, f"未知动作: {action}（可选: top/up/down/bottom/clear）")
    if action == "clear":
        result = c.db.set_task_manual_priority(task_id, None)
        if result == "not_found":
            raise HTTPException(404, "任务不存在")
        if result == "bad_state":
            raise HTTPException(400, "仅排队中（queued）的任务可调整人工优先级")
        return {"task_id": task_id, "manual_priority": None}
    result, new_priority = c.db.reorder_manual_priority(task_id, action)
    if result == "not_found":
        raise HTTPException(404, "任务不存在")
    if result == "bad_state":
        raise HTTPException(400, "仅排队中（queued）的任务可调整人工优先级")
    if result == "bad_action":
        raise HTTPException(400, f"未知动作: {action}")
    return {"task_id": task_id, "manual_priority": new_priority}


@router.post("/{task_id}/dequeue")
def dequeue_task(request: Request, task_id: int):
    """排队任务移出队列（issue #242）。

    把排队中（queued）任务置为终态 canceled_by_user（取消排队），并从
    调度器内存队列移除；操作写入 task_logs，状态与记录可追溯，用户可
    手动重试重新入队。已 running 任务不受影响（返回 400，需先停止）。
    """
    c = ctx_of(request)
    result = c.db.dequeue_task(task_id)
    if result == "not_found":
        raise HTTPException(404, "任务不存在")
    if result == "bad_state":
        raise HTTPException(400, "仅排队中（queued）的任务可移出队列（running 任务请先停止）")
    c.scheduler.remove_queued(task_id)
    return {"task_id": task_id, "status": "canceled_by_user"}


@router.get("/{task_id}")
def get_task(request: Request, task_id: int):
    c = ctx_of(request)
    row = c.db.get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    repo = c.db.get_repo(row["repo_id"])
    task = _task_to_dict(row, dict(repo) if repo else None,
                         usage_row=c.db.get_task_usage(task_id),
                         settings=c.config.get())
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
    prompt = None  # issue #90：渲染后的完整提示词（「查看提示词」按钮数据源）
    # 会话文件仅 claude 引擎有（jsonl）；dsh 引擎（issue #146）的提示词与
    # 聊天记录由执行侧落库 dsh_transcript（SDK 会话文件是 runtime 内部
    # 格式，无法像 claude jsonl 那样解析），此处一并读取返回
    session_id = row["claude_session_id"] or row["dsh_session_id"]
    if row["claude_session_id"]:
        session_file = find_session_file(row["claude_session_id"])
        if session_file:
            transcript, truncated = parse_transcript(session_file)
            prompt = read_session_prompt(session_file)
    elif row["dsh_transcript"]:
        try:
            data = json.loads(row["dsh_transcript"])
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            dsh_prompt = data.get("prompt")
            prompt = dsh_prompt if isinstance(dsh_prompt, str) and dsh_prompt else None
            dsh_msgs = data.get("messages")
            transcript = dsh_msgs if isinstance(dsh_msgs, list) else []
            truncated = bool(data.get("truncated"))

    return {
        "status": row["status"],
        "session_id": session_id,
        "log_offset": log_offset,
        "log_delta": log_delta,
        "transcript": transcript,
        "transcript_truncated": truncated,
        "prompt": prompt,
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
    # dsh 引擎（issue #84）的输出协议与 hermes 对齐（事件行 + 结果行），
    # 日志回放解析复用 parse_hermes_event_line
    parser = (parse_claude_stream_line if engine == "claude"
              else parse_hermes_event_line)

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
