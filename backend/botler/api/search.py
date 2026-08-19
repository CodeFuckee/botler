"""全局搜索 API（issue #216）：任务 / issue / 灵感 / 仓库跨模块检索。

需求：任务列表有过滤、概览按仓库展示 issue/灵感，但无跨模块全局搜索
——想找「某个 issue 相关的历史任务」「某关键词的灵感」「某仓库的全部
记录」需要逐个页面翻。本模块提供统一检索入口：

- GET /api/search?q=<关键词>&limit=<每模块条数>：四个模块并行返回，
  结果按模块分组（tasks / issues / inspirations / repos），点击可跳转
  对应详情页（前端 SearchOverlay 处理跳转）。

各模块数据源与匹配规则：
- tasks：本地 SQLite tasks 表，按 issue 标题 / 编号（issue_iid）模糊
  匹配（与任务列表页搜索同字段），最新任务在前；
- issues：GitLab 开放 issue，按标题 / 正文模糊匹配。复用概览页
  （api/issues）的 10 秒 TTL 聚合缓存（get_issues_overview），不额外
  打爆 GitLab API；只覆盖已启用仓库（与概览页一致）；
- inspirations：本地 SQLite inspirations 表，按内容模糊匹配，JOIN
  repos 带出仓库名；
- repos：本地 SQLite repos 表，按名称模糊匹配，排除软删除仓库。

中文关键词：Issue 正文提出 SQLite FTS5 建索引（或 LIKE 兜底）。但
FTS5 默认 unicode61 分词器不切分中文（整段连续 CJK 视为单个 token），
trigram 分词器要求关键词 ≥3 字符、1~2 字中文词（如「搜索」「任务」）
无法命中——均不满足「中文关键词检索可用」验收标准。本模块按 Issue
正文允许的 LIKE 兜底方案实现：LIKE 字面子串匹配（转义 % _ \\）对任意
长度的中文关键词都能命中，且平台数据量级（仓库/任务/灵感均为小表）
全表扫描性能足够。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from .issues import get_issues_overview
from .tasks import _task_to_dict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

# 每模块默认返回条数 / 上限：下拉面板展示空间有限，截断避免响应过大
DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def ctx_of(request: Request):
    """从请求中取全局依赖容器（与 tasks/repos 等模块同模式）。"""
    return request.app.state.ctx


def _search_issues(c, term: str, limit: int) -> list[dict]:
    """从概览聚合（10s 缓存）中按标题/正文模糊匹配开放 issue。

    复用 get_issues_overview 的缓存与 per-repo client；GitLab 故障时
    对应仓库的 issues 为空（_collect 已收集错误并跳过），搜索仍返回
    其余模块结果，不因单仓库故障整体失败。
    """
    low = term.lower()
    out: list[dict] = []
    for entry in get_issues_overview(c).get("repos") or []:
        for issue in entry.get("issues") or []:
            title = issue.get("title") or ""
            desc = issue.get("description") or ""
            if low in title.lower() or low in desc.lower():
                out.append(
                    {
                        "project_id": entry["project_id"],
                        "iid": issue.get("iid"),
                        "title": title,
                        "description": desc,
                        "web_url": issue.get("web_url"),
                        "state": issue.get("state"),
                        "updated_at": issue.get("updated_at"),
                        "labels": issue.get("labels") or [],
                        "repo_id": entry["repo_id"],
                        "repo_name": entry["repo_name"],
                    }
                )
                if len(out) >= limit:
                    return out
    return out


@router.get("")
def search(
    request: Request,
    q: str = Query(..., description="搜索关键词"),
    limit: int = Query(
        DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="每模块返回条数上限"
    ),
):
    """跨模块全局搜索：任务 / issue / 灵感 / 仓库。

    关键词去除首尾空白后为空 → 400（前端输入框防抖不会带空词请求，
    防御性校验）。四个模块互不干扰：任一模块无匹配返回空数组。
    """
    c = ctx_of(request)
    term = (q or "").strip()
    if not term:
        raise HTTPException(400, "搜索关键词不能为空")
    # 任务序列化需要仓库名/URL（与 tasks 列表端点同法：包含软删除仓库，
    # 历史任务仍能解析出仓库名，issue #62）
    repos = {
        # issue #237：带出仓库级覆盖字段，任务序列化按「仓库级 > 全局」解析生效参数
        r["id"]: {"name": r["name"], "url": r["url"],
                  "timeout_seconds": r["timeout_seconds"],
                  "max_retries": r["max_retries"], "engine": r["engine"]}
        for r in c.db.list_repos(include_deleted=True)
    }
    return {
        "query": term,
        "tasks": [
            _task_to_dict(r, repos.get(r["repo_id"]), settings=c.config.get())
            for r in c.db.search_tasks(term, limit)
        ],
        "issues": _search_issues(c, term, limit),
        "inspirations": [
            {
                "id": r["id"],
                "repo_id": r["repo_id"],
                "repo_name": r["repo_name"],
                "content": r["content"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in c.db.search_inspirations(term, limit)
        ],
        "repos": [
            {
                "id": r["id"],
                "gitlab_project_id": r["gitlab_project_id"],
                "name": r["name"],
                "url": r["url"],
                "enabled": bool(r["enabled"]),
                "priority": r["priority"],
            }
            for r in c.db.search_repos(term, limit)
        ],
    }
