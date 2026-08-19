"""概览页「发掘」API（issue #189）。

需求：概览页每个仓库卡片右上角新增「发掘」按钮，点击后根据该项目实现的
功能去 GitHub 搜索类似仓库，翻找类似仓库里 issue（用户对类似项目提出的
需求），整理成若干条需求后写入该仓库的 GitLab issue，分配人选择仓库的
owner，一条需求一个 issue。

接口设计：
- POST /api/repos/{repo_id}/discover（同步执行）：
  1. 校验仓库存在、未软删除、已启用；
  2. 收集项目上下文（复用自省 issue #187 收集链路：本地项目文件夹优先，
     GitLab 仓库 API 兜底；均缺失时仅基于仓库元信息继续并提示模型如实
     说明，收集失败不阻塞）；
  3. 调用 AI 对话模型（复用设置页「AI API 供应商」第一个启用且 Key
     非空的项，issue #166/#187 同一链路）基于项目功能生成 GitHub 搜索
     关键词（严格 JSON 数组）；
  4. 调 GitHub REST API search/repositories 搜索类似仓库（按 star 排序
     取前 N，跨关键词去重）；
  5. 翻找类似仓库的开放 issue（issues API，过滤 pull request），收集
     用户需求原文；
  6. 调用 AI 对话模型把原始需求整理成若干条需求（严格 JSON 数组：标题 +
     说明 + 参考来源；去重、合并同类项、封顶条数）；
  7. 逐条在该仓库创建 GitLab issue：标题带【发掘】前缀、标签 feature
     （需求语义，平台有效标签池）、分配人 = 仓库 owner（与自省 issue
     #187 同一解析链路：GitLab 项目 owner 优先，仓库 remote 用户名兜底，
     解析失败不指定分配人），一条需求一个 issue；
  8. 创建成功后清空概览缓存，前端刷新即可看到新 issue。写 issue 与
     概览页其他 issue 编辑一致（_issue_edit_call）：必须使用 owner
     token，绝不回退 bot token。
- 无论是否找到用户需求 issue，都把相似仓库列表随响应返回（issue #301）：
  未翻找到任何用户需求 issue 时跳过 AI 整理与建 issue，返回
  {issues: [], count: 0, similar_repos: [...]}（201）；找到时返回
  {issues, count, similar_repos}，前端一并展示。

GitHub 访问：匿名调用（可选环境变量 GITHUB_TOKEN 提升限额），限流
403/429 明确报错引导；网络错误 502；未配置 GitHub Token 不影响任务启动。

错误映射：仓库不存在 → 404；仓库已删除/未启用 → 400；未配置 AI 对话
模型 → 400（引导设置页配置）；AI 调用失败/回复为空/解析失败 → 502；
GitHub 搜索失败/限流/无相似仓库 → 502；GitHub issue 采集失败 → 502；
未找到用户需求 issue 时不报错，直接返回相似仓库列表（count=0，不建
issue，issue #301）；GitLab 创建 issue 失败 → 502；网络错误 → 502。
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..gitlab_client import GITLAB_ISSUE_TITLE_MAX_LEN, GitLabError
from .issues import (
    _issue_edit_call, _trim_issue, clear_issue_cache,
)
from .introspection import (
    _build_review_prompt, _collect_gitlab_context, _collect_local_context,
    _project_root, _resolve_owner_assignee,
)

logger = logging.getLogger(__name__)

# 路由与仓库管理共用 /repos 前缀，避免前端新增第二种资源前缀
router = APIRouter(prefix="/repos", tags=["repos-discover"])

# 发掘 issue 默认标签（issue #189）：需求语义，属于平台有效标签池
DISCOVER_LABELS = ("feature",)

# AI 调用超时（秒）：搜索词生成较快，需求整理上下文大放宽到 120s
DISCOVER_TIMEOUT_QUERY = 60.0
DISCOVER_TIMEOUT_AGGREGATE = 120.0

# GitHub REST API（按需求固定 github.com，与后端版本检查同一站点族）
GITHUB_API_BASE = "https://api.github.com"
GITHUB_ACCEPT = "application/vnd.github+json"
# 单个 GitHub 请求超时（秒）
GITHUB_TIMEOUT = 20.0

# 发掘流程各阶段数量上限：控制总耗时（GitHub 匿名限额 10 次搜索/分钟、
# 60 次核心请求/小时）与 issue 刷屏规模
MAX_SEARCH_QUERIES = 4          # AI 生成的搜索关键词最多取前 4 个
SEARCH_PER_PAGE = 5             # 每个关键词搜索结果取前 5 个
MAX_SIMILAR_REPOS = 5           # 去重后最多考察 5 个相似仓库
ISSUES_PER_REPO = 15            # 每个相似仓库翻找前 15 条开放 issue
MAX_RAW_ISSUES = 40             # 原始需求 issue 总量封顶
MAX_DISCOVER_ISSUES = 8         # 整理后最多创建 8 条需求 issue

# AI 搜索词生成系统提示词：要求严格 JSON 数组（便于直接喂 GitHub 搜索）
DISCOVER_QUERY_SYSTEM_PROMPT = (
    "你是 Botler 平台的「项目发掘」agent。任务：根据给定仓库实现的功能，"
    "提炼出适合在 GitHub 上搜索「功能相似开源项目」的搜索关键词。"
    "请严格输出 JSON 数组，元素为搜索字符串（建议 2~4 个，覆盖项目核心"
    "功能、技术栈与定位；每个关键词用英文，便于 GitHub 搜索）。"
    "示例输出：[\"self-hosted gitlab bot\", \"ai issue triage\"]。"
    "不要输出任何其他内容。"
)

# AI 需求整理系统提示词：要求严格 JSON 数组（标题 + 说明 + 参考来源）
DISCOVER_AGGREGATE_SYSTEM_PROMPT = (
    "你是 Botler 平台的「需求发掘」agent。任务：下面是 Botler 在 GitHub "
    "相似开源项目里翻找出的用户 issue（用户对这些项目提出的需求）。请把"
    "这些原始需求整理成若干条（建议 3~8 条）对目标项目有价值的新需求："
    "合并同类项、去掉与目标项目无关/过时/重复的内容、按价值排序。"
    "请严格输出 JSON 数组，每个元素为对象：{\"title\": \"需求标题（中文，"
    "一句话，不超过 40 字）\", \"detail\": \"需求说明（中文，2~5 句，"
    "具体可落地）\", \"sources\": [\"参考 issue 链接（可为空数组）\"]}。"
    "不要输出任何其他内容。"
)


class GitHubApiError(RuntimeError):
    """GitHub REST API 调用失败（含限流/网络错误），message 可直接展示。"""


# ---- GitHub REST API 访问（测试经 _github_api_get 打桩） ----


def _github_headers() -> dict[str, str]:
    """GitHub 请求头：Accept 固定 vnd.github+json；可选环境变量
    GITHUB_TOKEN 提升匿名限额（403/429 限流时配置后重试）。"""
    headers = {"Accept": GITHUB_ACCEPT}
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_api_get(path: str, params: dict | None = None):
    """GET GitHub REST API（path 形如 /search/repositories，不含域名）。

    返回解析后的 JSON；非 2xx 抛 GitHubApiError（403/429 明确提示限流）。
    测试通过 monkeypatch 本函数注入桩数据/故障。
    """
    url = f"{GITHUB_API_BASE}{path}"
    try:
        resp = httpx.get(url, params=params or {}, headers=_github_headers(),
                         timeout=GITHUB_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as e:
        raise GitHubApiError(f"网络错误: {str(e)[:200]}") from e
    if resp.status_code == 403 or resp.status_code == 429:
        raise GitHubApiError(
            f"GitHub API 限流（HTTP {resp.status_code}）：匿名限额已用完，"
            "请稍后重试，或在环境变量 GITHUB_TOKEN 配置 GitHub Token "
            "提升限额")
    if resp.status_code != 200:
        raise GitHubApiError(f"GitHub API 返回 HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise GitHubApiError("GitHub API 响应解析失败") from e


def _github_search_repos(query: str) -> list[dict]:
    """按关键词搜索 GitHub 仓库（按 star 降序取前 SEARCH_PER_PAGE 个）。"""
    data = _github_api_get("/search/repositories", {
        "q": query, "sort": "stars", "order": "desc",
        "per_page": SEARCH_PER_PAGE,
    })
    items = data.get("items") if isinstance(data, dict) else None
    repos = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        full_name = str(it.get("full_name") or "").strip()
        if not full_name:
            continue
        repos.append({
            "full_name": full_name,
            "html_url": str(it.get("html_url") or "").strip(),
            "description": str(it.get("description") or "").strip() or None,
            "stars": it.get("stargazers_count"),
        })
    return repos


def _github_repo_issues(full_name: str) -> list[dict]:
    """翻找相似仓库的开放 issue（issues API 同时返回 PR，需过滤）。"""
    data = _github_api_get(f"/repos/{full_name}/issues", {
        "state": "open", "per_page": ISSUES_PER_REPO,
    })
    issues = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        # PR 在 issues 列表里带 pull_request 键，不属于用户需求，跳过
        if isinstance(it.get("pull_request"), dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        body = str(it.get("body") or "").strip()
        issues.append({
            "title": title,
            "body": body[:2000] if body else None,
            "html_url": str(it.get("html_url") or "").strip() or None,
        })
    return issues


def _parse_json_array(text: str) -> list | None:
    """从 AI 回复中提取 JSON 数组：先整体解析，失败则截取首 [ 到末 ] 的
    片段再解析（兼容模型回包带 ```json 代码围栏或前后缀说明文字）。"""
    if not text or not text.strip():
        return None
    candidates = [text.strip()]
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        # 去掉常见 markdown 代码围栏 ```json ... ```
        cand = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cand.strip())
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def _chat_once(c, provider_cfg: dict, system: str, user_content: str,
               timeout: float) -> str:
    """调用 AI 对话模型（复用设置页「AI API 供应商」第一个启用且 Key 非空
    的项，与灵感/自省同一链路）；失败抛 HTTPException(502)。"""
    from ..chat_models import ChatModelClient, ChatModelError
    try:
        chat = ChatModelClient(
            name=str(provider_cfg.get("name") or "AI 供应商"),
            provider=str(provider_cfg.get("provider") or "custom").strip(),
            base_url=str(provider_cfg.get("base_url") or "").strip(),
            api_key=str(provider_cfg.get("api_key") or "").strip(),
            model=str(provider_cfg.get("model") or "").strip(),
            timeout=timeout,
            verify_ssl=getattr(c.config.get(), "verify_ssl", True))
        reply = chat.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ])
    except ChatModelError as e:
        raise HTTPException(502, f"AI 调用失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"AI 调用网络错误: {str(e)[:200]}") from e
    reply = (reply or "").strip()
    if not reply:
        raise HTTPException(502, "AI 回复为空，请稍后重试")
    return reply


def _build_requirements_prompt(repo: dict, raw_issues: list[dict]) -> str:
    """组装需求整理用户侧上下文：目标仓库元信息 + 相似仓库原始 issue。"""
    lines = [
        "目标仓库：",
        f"- 仓库名：{(repo.get('name') or '').strip()}",
        f"- 仓库地址：{(repo.get('url') or '').strip()}",
        "\n【GitHub 相似仓库用户 issue（原始需求）】",
    ]
    if not raw_issues:
        lines.append("（未采集到任何用户需求 issue）")
    for i, it in enumerate(raw_issues, 1):
        lines.append(f"{i}. [{it['repo']}] {it['title']}"
                     f"（{it['html_url'] or '无链接'}）")
        if it.get("body"):
            lines.append(f"   {it['body'][:500]}")
    lines.append("\n请基于以上原始需求整理出对目标项目有价值的新需求，"
                 "并严格按 JSON 数组格式输出。")
    return "\n".join(lines)


@router.post("/{repo_id}/discover", status_code=201)
def discover_repo(request: Request, repo_id: int):
    """概览页「发掘」按钮（issue #189）：根据项目实现的功能去 GitHub 搜索
    类似仓库、翻找用户需求 issue，整理成若干条需求写入该仓库 GitLab issue，
    分配人为仓库 owner，一条需求一个 issue。

    同步执行（AI 两轮 + GitHub 采集 + 创建 issue 一次请求完成）；返回
    创建的 issue 精简对象列表、需求总数与相似仓库列表（issue #301：无论
    是否找到用户需求 issue 都把相似仓库返回，未找到时 count=0 不建 issue），
    前端展示成功提示、跳转链接与相似仓库清单。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    row = dict(row)  # sqlite3.Row → dict，统一按 dict 访问（issue #187）
    if row["deleted_at"] is not None:
        raise HTTPException(400, "仓库已删除")
    if not row["enabled"]:
        raise HTTPException(400, "仓库未启用")

    # AI 对话模型：复用设置页「AI API 供应商」第一个启用且 Key 非空的项
    # （与灵感对话 issue #166 / 自省 issue #187 同一链路）
    settings = c.config.get()
    from ..chat_models import resolve_chat_provider
    provider_cfg = resolve_chat_provider(settings)
    if provider_cfg is None:
        raise HTTPException(
            400, "未配置 AI 对话模型：请先在设置页「AI API 供应商」添加"
                 "并启用一个供应商（需填写 API Key）")

    # 1. 收集项目上下文（尽力而为：本地文件夹优先，GitLab API 兜底）
    from .issues import _issue_create_client
    client = _issue_create_client(c, row)
    root = _project_root(row)
    local_ctx = _collect_local_context(root) if root else None
    gitlab_ctx = None
    if local_ctx is None or not local_ctx.get("tree"):
        try:
            gitlab_ctx = _collect_gitlab_context(
                client, row["gitlab_project_id"])
        except (GitLabError, httpx.HTTPError) as e:
            logger.warning("发掘：GitLab 上下文兜底收集失败（repo=%s）: %s",
                           row["id"], e)
            gitlab_ctx = None
    user_content = _build_review_prompt(row, local_ctx, gitlab_ctx)

    # 2. AI 第一轮：基于项目功能生成 GitHub 搜索关键词（严格 JSON 数组）
    reply_query = _chat_once(c, provider_cfg, DISCOVER_QUERY_SYSTEM_PROMPT,
                             user_content, DISCOVER_TIMEOUT_QUERY)
    parsed_query = _parse_json_array(reply_query)
    queries = [str(q).strip() for q in parsed_query or []
               if isinstance(q, str) and str(q).strip()][:MAX_SEARCH_QUERIES]
    if not queries:
        raise HTTPException(502, "AI 未生成有效的 GitHub 搜索关键词，"
                                 "请稍后重试")

    # 3. GitHub 搜索类似仓库（按 star 排序，跨关键词去重，总量封顶）
    similar_repos: list[dict] = []
    seen_names: set[str] = set()
    for q in queries:
        try:
            repos = _github_search_repos(q)
        except GitHubApiError as e:
            raise HTTPException(502, f"GitHub 搜索类似仓库失败: {e}") from e
        for repo in repos:
            name = repo["full_name"]
            if name in seen_names:
                continue
            seen_names.add(name)
            similar_repos.append(repo)
            if len(similar_repos) >= MAX_SIMILAR_REPOS:
                break
        if len(similar_repos) >= MAX_SIMILAR_REPOS:
            break
    if not similar_repos:
        raise HTTPException(502, "GitHub 上未找到功能相似的仓库，请稍后重试")

    # 4. 翻找类似仓库的开放 issue（过滤 PR），收集用户需求原文
    raw_issues: list[dict] = []
    for repo in similar_repos:
        try:
            issues = _github_repo_issues(repo["full_name"])
        except GitHubApiError as e:
            raise HTTPException(
                502, f"翻找相似仓库 issue 失败（{repo['full_name']}）: {e}"
            ) from e
        for it in issues:
            raw_issues.append({**it, "repo": repo["full_name"]})
            if len(raw_issues) >= MAX_RAW_ISSUES:
                break
        if len(raw_issues) >= MAX_RAW_ISSUES:
            break

    # 5. 无论是否找到用户需求 issue，都把相似仓库返回给前端展示
    #    （issue #301）：未翻找到任何用户需求 issue 时跳过 AI 整理与建
    #    issue，直接返回相似仓库列表（count=0），不再报 502。
    requirements: list[dict] = []
    if raw_issues:
        # 5.1 AI 第二轮：把原始需求整理成若干条需求（严格 JSON 数组）
        reply_agg = _chat_once(c, provider_cfg, DISCOVER_AGGREGATE_SYSTEM_PROMPT,
                               _build_requirements_prompt(row, raw_issues),
                               DISCOVER_TIMEOUT_AGGREGATE)
        parsed_agg = _parse_json_array(reply_agg)
        seen_titles: set[str] = set()
        for item in parsed_agg or []:
            if not isinstance(item, dict):
                continue
            # 标题单行化：去掉换行/回车（GitLab issue 标题应为单行），去空白
            title = re.sub(r"[\r\n\t]+", " ", str(item.get("title") or "")).strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            sources = item.get("sources") or []
            if not isinstance(sources, list):
                sources = []
            requirements.append({
                "title": title,
                "detail": str(item.get("detail") or "").strip(),
                "sources": [str(s).strip() for s in sources
                            if isinstance(s, str) and str(s).strip()],
            })
            if len(requirements) >= MAX_DISCOVER_ISSUES:
                break
        if not requirements:
            raise HTTPException(502, "AI 未整理出有效的需求，请稍后重试")
    else:
        logger.info("发掘：未翻找到用户需求 issue（repo=%s），"
                    "仅返回相似仓库列表", row["id"])

    # 6. 未整理出任何需求（未找到用户需求 issue）：直接返回相似仓库列表
    if not requirements:
        return {"issues": [], "count": 0, "similar_repos": similar_repos}

    # 7. 逐条创建 GitLab issue：分配人 = 仓库 owner（解析失败不阻塞）
    try:
        assignee_id = _resolve_owner_assignee(c, row, client)
    except Exception:  # noqa: BLE001 防御性兜底：分配人解析失败不阻塞
        logger.exception("发掘：解析仓库 owner 分配人异常（repo=%s），跳过分配人",
                         row["id"])
        assignee_id = None

    repo_name = (row["name"] or "").strip() or f"项目{row['gitlab_project_id']}"
    created: list[dict] = []
    for i, req in enumerate(requirements, 1):
        title = f"【发掘】{repo_name}：{req['title']}"
        if len(title) > GITLAB_ISSUE_TITLE_MAX_LEN:
            title = title[:GITLAB_ISSUE_TITLE_MAX_LEN - 1] + "…"
        sources_text = "\n".join(f"- {s}" for s in req["sources"]) or "（无）"
        detail = req["detail"] or "（AI 未提供详细说明）"
        description = (
            f"本 issue 由 Botler 概览页「发掘」按钮生成：Botler 根据仓库"
            f"「{repo_name}」实现的功能，在 GitHub 搜索功能相似的开源项目，"
            "翻找其 issue 中用户提出的需求，整理为以下需求建议。\n\n"
            f"【需求说明】\n{detail}\n\n"
            f"【参考来源】\n{sources_text}\n\n"
            f"（第 {i}/{len(requirements)} 条需求）")
        try:
            issue = _issue_edit_call(
                c, row,
                lambda cl, _t=title, _d=description: cl.create_issue(
                    row["gitlab_project_id"], _t,
                    description=_d,
                    assignee_id=assignee_id,
                    labels=list(DISCOVER_LABELS)))
        except GitLabError as e:
            raise HTTPException(
                502, f"创建需求 issue 失败（已创建 {len(created)} 条）: {e}"
            ) from e
        except httpx.HTTPError as e:
            raise HTTPException(
                502, f"创建需求 issue 网络错误（已创建 {len(created)} 条）: "
                     f"{str(e)[:200]}") from e
        created.append(_trim_issue(issue, {}))
    clear_issue_cache()
    # 无论是否找到用户需求 issue，都把相似仓库列表随响应返回（issue #301）
    return {"issues": created, "count": len(created),
            "similar_repos": similar_repos}
