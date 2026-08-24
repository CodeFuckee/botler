"""灵感 API（issue #131，issue #143 扩展一键提交为 GitLab issue）。

需求：概览页在「开放 Issue」下方、「CI/CD 流水线」上方增加灵感板块，
用户可按仓库随手记录关于该仓库新功能的灵感；灵感默认只保存在 Botler
本地 SQLite 数据库（issue #131），issue #143 起支持一键将灵感提交为
GitLab issue——灵感内容作为 issue 标题与描述、默认标签 feature + ui。

接口设计：
- GET    /api/inspirations/overview：聚合所有未软删除仓库（按优先级
  升序、同优先级按仓库 id，与 list_repos 一致），每个仓库带灵感列表
  （按 updated_at 降序）。无灵感的仓库也返回（前端展示空状态 + 添加
  表单），与开放 issue 板块同构。设置 ui.show_disabled_repos=false 时
  只返回已启用仓库（issue #142）。
- POST   /api/inspirations：创建灵感（repo_id + content 必填）。
- PUT    /api/inspirations/{id}：更新灵感内容（刷新 updated_at）。
- DELETE /api/inspirations/{id}：删除灵感。
- POST   /api/inspirations/{id}/add-issue（issue #143 / #153 / #159 /
  #162 / #186）：将灵感一键提交为 GitLab issue——灵感内容作为标题与描述，
  默认标签 feature + ui，分配人 = 仓库用户（issue #153：仓库设置页读取
  remote url 得到的 userinfo 用户名，解析为 GitLab 用户 id；issue #159：
  存储值为空时提交时按 remote url 运行时读取兜底并写回仓库表，未配置/
  解析失败则不指定分配人）；走 owner token（复用 issues 模块的
  _issue_edit_call，绝不回退 bot token），创建成功后清空概览缓存并
  删除该灵感（issue #162：成功推送后从灵感列表移除，失败保留可重试）。
- GET    /api/inspirations/{id}/messages（issue #166）：返回该灵感与
  AI agent 的对话历史（按时间升序）。
- POST   /api/inspirations/{id}/messages（issue #166）：向 AI agent
  发送一条消息并返回回复——用户消息 + AI 回复成对保存到本地数据库；
  对话模型复用设置页「AI API 供应商」（ai_providers）第一个启用且
  API Key 非空的项；AI 调用失败时回滚已保存的用户消息并返回 502
  （对话历史保持成对完整，前端保留输入可重试）。

校验：repo_id 必须指向存在且未软删除的仓库（400）；content 去除首尾
空白后非空（400）、长度不超过 5000 字（400，随手笔记的合理上限）；
add-issue 要求灵感存在（404）、所属仓库存在且未软删除（400）、仓库
已启用（400）；issue #186：灵感内容超过 GitLab 标题上限（255 字符，
GITLAB_ISSUE_TITLE_MAX_LEN）时标题截断到上限内并加省略号标记、描述
保留完整内容（GitLab 描述字段可容纳远超 255 字符，标题才是硬限制）；
对话消息要求灵感存在（404）、消息内容非空且不超过 2000 字（400）、
已配置可用的 AI 对话供应商（400）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..gitlab_client import GitLabError, GITLAB_ISSUE_TITLE_MAX_LEN
from .issues import _issue_edit_call, _trim_issue, clear_issue_cache

logger = logging.getLogger(__name__)

# 灵感一键提交 issue 的默认标签（issue #143）：需求约定 feature + ui
INSPIRATION_ISSUE_LABELS = ("feature", "ui")

router = APIRouter(prefix="/inspirations", tags=["inspirations"])

# 灵感内容长度上限（去除首尾空白后）：随手笔记场景的合理上限
MAX_CONTENT_LEN = 5000

# 灵感 AI 对话（issue #166）参数：单条消息长度上限（去除首尾空白后）、
# 传给模型的上下文最大消息条数（历史过长时截取最近 N 条，防止撑爆
# 模型上下文窗口）、对话请求超时（秒，文本接口通常 30s 内返回）
MAX_CHAT_CONTENT_LEN = 2000
CHAT_CONTEXT_MESSAGES = 20
CHAT_TIMEOUT = 60.0


class InspirationCreate(BaseModel):
    repo_id: int = Field(description="仓库本地 id（repos.id）")
    content: str = Field(description="灵感内容")


class InspirationUpdate(BaseModel):
    content: str = Field(description="灵感内容")


class InspirationChatMessage(BaseModel):
    content: str = Field(description="向 AI agent 发送的消息内容")
    provider: str | None = Field(default=None, description="可选 AI 供应商标识")


class InspirationChatProvider(BaseModel):
    provider: str | None = Field(default=None, description="供应商标识；null 清除选择")


def _validate_content(content: str) -> str:
    """校验并规范化灵感内容：去除首尾空白，非空且不超长。"""
    text = content.strip()
    if not text:
        raise HTTPException(400, "灵感内容不能为空")
    if len(text) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"灵感内容不能超过 {MAX_CONTENT_LEN} 字")
    return text


def _require_repo(c, repo_id: int):
    """校验仓库存在且未软删除，返回仓库行；否则抛 400。

    get_repo 不区分软删除（软删除仅写 deleted_at 标记），这里显式
    排除 deleted_at 非空的行——已删除仓库不允许再记录灵感（issue #131）。
    """
    repo = c.db.get_repo(repo_id)
    if repo is None or repo["deleted_at"] is not None:
        raise HTTPException(400, f"仓库不存在或已删除（id={repo_id}）")
    return repo


def _resolve_repo_user_id(c, repo, username: str) -> int | None:
    """把仓库用户（remote url 用户名）解析为 GitLab 用户 id（issue #153）。

    灵感一键提交 issue 时分配人 = 仓库用户。与添加 issue 弹窗的成员
    解析同一数据源：先在项目成员（members/all）里按 username 匹配
    （创建 issue 的 assignee 必须是项目成员）；成员项缺 user_id
    （GitLab 19 实测）时按 username 查 /users 补齐。项目成员里找不到
    时兜底查 /users（同名全局用户，成员接口可能因权限范围未返回）。

    用户不存在/不是项目成员 → 返回 None（调用方不指定分配人）；
    API/网络故障抛异常，由调用方捕获降级（不阻塞 issue 创建）。
    """
    from .issues import _issue_create_client

    client = _issue_create_client(c, repo)
    for m in client.list_project_members(repo["gitlab_project_id"]) or []:
        if not isinstance(m, dict) or m.get("username") != username:
            continue
        uid = m.get("user_id")
        if uid is None:
            uid = client.get_user_id_by_username(username)
        return uid if uid is not None else None
    return client.get_user_id_by_username(username)


def _row_to_dict(row) -> dict:
    """灵感行 → API 响应对象（含仓库名快照，展示无需再查仓库表）。"""
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "repo_name": row["repo_name"],
        "content": row["content"],
        "chat_provider": row["chat_provider"] if "chat_provider" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/overview")
def inspiration_overview(
    request: Request,
    limit: int = Query(default=0, ge=0, le=100),
):
    """返回轻量概览及可选首屏页（issue #219）。

    默认只返回各仓库的灵感总数，避免 15 秒轮询传输和渲染全部长期积累
    的灵感。传入 ``limit`` 时，每仓库最多附带该数量的最新灵感，保持
    兼容需要首屏预览的调用方；详情由独立分页接口按仓库按需读取。
    """
    c = request.app.state.ctx
    repos = c.db.list_repos()
    if not c.config.get().ui_show_disabled_repos:
        repos = [r for r in repos if r["enabled"]]
    totals = c.db.count_inspirations_by_repo()
    return {
        "repos": [
            {
                "repo_id": r["id"],
                "repo_name": r["name"],
                "enabled": bool(r["enabled"]),
                "priority": r["priority"],
                "inspiration_total": total,
                "inspirations": [
                    _row_to_dict(row)
                    for row in (c.db.list_inspirations_page(r["id"], 0, limit) if limit else [])
                ],
                "inspiration_has_more": total > limit,
            }
            for r in repos
            for total in [totals.get(r["id"], 0)]
        ],
    }


@router.get("/pages/{repo_id}")
def inspiration_page(
    request: Request,
    repo_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    """按仓库懒加载灵感页，按 ``updated_at DESC, id DESC`` 稳定排序。"""
    c = request.app.state.ctx
    _require_repo(c, repo_id)
    total = c.db.count_inspirations(repo_id)
    rows = c.db.list_inspirations_page(repo_id, offset, limit)
    return {
        "repo_id": repo_id,
        "offset": offset,
        "limit": limit,
        "total": total,
        "inspirations": [_row_to_dict(row) for row in rows],
        "has_more": offset + len(rows) < total,
    }


@router.post("", status_code=201)
def create_inspiration(request: Request, body: InspirationCreate):
    c = request.app.state.ctx
    _require_repo(c, body.repo_id)
    content = _validate_content(body.content)
    insp_id = c.db.create_inspiration(body.repo_id, content)
    row = c.db.get_inspiration(insp_id)
    assert row is not None  # 刚插入的记录必然可查
    return _row_to_dict(row)


@router.put("/{inspiration_id}")
def update_inspiration(request: Request, inspiration_id: int,
                       body: InspirationUpdate):
    c = request.app.state.ctx
    content = _validate_content(body.content)
    if not c.db.update_inspiration(inspiration_id, content):
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    row = c.db.get_inspiration(inspiration_id)
    assert row is not None
    return _row_to_dict(row)


@router.delete("/{inspiration_id}", status_code=204)
def delete_inspiration(request: Request, inspiration_id: int):
    c = request.app.state.ctx
    if not c.db.delete_inspiration(inspiration_id):
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    return None


@router.post("/{inspiration_id}/add-issue", status_code=201)
def add_issue_from_inspiration(request: Request, inspiration_id: int):
    """将灵感一键提交为 GitLab issue（issue #143）。

    概览页灵感条目「添加 Issue」按钮点击后调用：灵感内容同时作为
    issue 标题与描述，默认标签 feature + ui（GitLab 创建 issue 时
    不存在的标签会自动创建），分配人 = 仓库用户（issue #153/#159：
    优先用仓库表 remote_username，存储值为空时按 remote url 运行时
    读取兜底并写回，解析为 GitLab 用户 id；未配置/解析失败不指定
    分配人）；通过 GitLab API 在灵感所属仓库创建。

    写操作与概览页其他 issue 编辑一致（_issue_edit_call）：必须使用
    owner token，绝不回退 bot token——未配置 owner token 返回 400 并
    引导设置；token 失效/权限不足返回 502。创建成功后清空概览缓存并
    删除该灵感（issue #162：灵感已转为 GitLab issue，成功推送后从灵感
    列表移除，避免重复提交；删除为本地数据库操作，失败仅告警不阻塞
    返回成功），前端刷新开放 issue 列表即可看到新 issue。返回精简后
    的 issue 对象（含 iid/web_url，供前端展示创建成功提示与跳转链接）。

    错误映射：灵感不存在 → 404；所属仓库不存在/已软删除 → 400；仓库
    未启用 → 400（与概览页添加 issue 弹窗一致）；GitLab 创建失败 →
    502；网络错误 → 502。
    """
    c = request.app.state.ctx
    insp = c.db.get_inspiration(inspiration_id)
    if insp is None:
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    repo = _require_repo(c, insp["repo_id"])
    if not repo["enabled"]:
        raise HTTPException(400, "仓库未启用")
    # 数据库层已保证内容非空（创建/更新时校验），此处去首尾空白后
    # 仍为空属极端数据异常，防御性拦截
    content = insp["content"].strip()
    if not content:
        raise HTTPException(400, "灵感内容不能为空")
    # issue #153：分配人 = 仓库用户（仓库设置页读取 remote url 得到的
    # userinfo 用户名）。解析为 GitLab 用户 id 传入 create_issue；未配置
    # 仓库用户 / 解析失败（用户不存在、不是成员、API 故障）时保持原
    # 行为（不指定分配人），不阻塞 issue 创建——用户可在仓库设置页
    # 「重新读取 remote」后重试。
    #
    # issue #159：存量仓库（issue #153 之前添加）remote_username 未落库，
    # 仅依赖设置页手动「重新读取」体验不达预期——提交时在存储值为空时
    # 按仓库 remote url 运行时读取兜底（read_repo_remote_username，尽力
    # 而为：目录不可读 / 无该 remote / URL 无凭据返回 None），读到后
    # 写回仓库表（设置页同步可见、后续调用直接用缓存值），仍读不到则
    # 保持原行为（不指定分配人），任何情况都不阻塞 issue 创建。
    assignee_id = None
    username = (repo["remote_username"] or "").strip()
    if not username:
        try:
            from ..git_remote import read_repo_remote_username
            username = (read_repo_remote_username(repo) or "").strip()
            if username:
                # 运行时读到的仓库用户写回仓库表：仓库设置页展示与后续
                # 提交不再重复读取（与设置页「重新读取 remote URL」落库
                # 语义一致）。repo 可能是 sqlite3.Row，转 dict 再更新。
                c.db.update_repo(repo["id"], remote_username=username)
                repo = dict(repo)
                repo["remote_username"] = username
        except Exception as e:
            logger.warning("灵感提交 issue：运行时读取仓库用户失败，跳过分配人: %s",
                           str(e)[:200])
    if username:
        try:
            assignee_id = _resolve_repo_user_id(c, repo, username)
        except GitLabError as e:
            logger.warning("灵感提交 issue：解析仓库用户 %s 失败，跳过分配人: %s",
                           username, e)
        except httpx.HTTPError as e:
            logger.warning("灵感提交 issue：解析仓库用户 %s 网络错误，跳过分配人: %s",
                           username, str(e)[:200])
    # issue #186：GitLab issue 标题硬上限 255 字符（GITLAB_ISSUE_TITLE_MAX_LEN，
    # 超过则创建接口 400 拒绝 "title is too long"），而描述字段可容纳远超
    # 255 字符——灵感内容超长时标题截断到上限内并加省略号标记，描述保留
    # 完整内容（用户可在 GitLab 描述里输入超过 255 字符的正文）。
    title = content
    if len(title) > GITLAB_ISSUE_TITLE_MAX_LEN:
        title = title[:GITLAB_ISSUE_TITLE_MAX_LEN - 1] + "…"
    try:
        issue = _issue_edit_call(
            c, repo,
            lambda cl: cl.create_issue(
                repo["gitlab_project_id"], title,
                description=content,
                assignee_id=assignee_id,
                labels=list(INSPIRATION_ISSUE_LABELS)))
    except GitLabError as e:
        raise HTTPException(502, f"创建 issue 失败: {e}") from e
    except httpx.HTTPError as e:
        # owner client 可能指向不可达 host（配置的 GitLab 地址异常）
        raise HTTPException(502, f"创建 issue 网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    # issue #162：创建成功即从灵感列表删除——灵感已转为 GitLab issue，
    # 保留会诱导重复点击产生重复 issue（每次提交都会新建一个 issue）。
    # 删除属本地数据库操作（刚查过该行必然存在），正常不会失败；仍
    # 防御性捕获并告警，不阻塞返回成功——issue 创建成功是主流程结果。
    # 所有失败路径（GitLab 故障 / 未配置 owner token / 仓库禁用等）
    # 均不会走到这里，灵感保留可重试。
    try:
        c.db.delete_inspiration(inspiration_id)
    except Exception as e:
        logger.warning("灵感提交 issue 成功后删除灵感失败（id=%s）: %s",
                       inspiration_id, str(e)[:200])
    # 标签色省略（创建刚完成，前端随即刷新列表从 overview 获取完整数据）
    return _trim_issue(issue, {})


# ---- issue #166：灵感与 AI agent 对话 ----
# 概览页灵感板块「与 AI 对话」：用户围绕某条灵感与 AI agent 探讨。
# 对话消息成对保存到本地数据库（inspiration_messages 表），模型复用
# 设置页「AI API 供应商」（ai_providers，issue #46）配置的文本对话
# 供应商——取第一个 enabled 且 API Key 非空的项作为灵感对话模型，
# 用户可通过调整列表顺序 / 启用开关选择。

def _message_to_dict(row) -> dict:
    """灵感对话消息行 → API 响应对象。"""
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def _require_inspiration(c, inspiration_id: int):
    """校验灵感存在，返回灵感行；否则抛 404。"""
    insp = c.db.get_inspiration(inspiration_id)
    if insp is None:
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    return insp


def _available_chat_providers(settings) -> list[dict]:
    """返回所有启用且有 Key 的对话供应商，隐藏 API Key。"""
    result = []
    for item in getattr(settings, "ai_providers", None) or []:
        if (isinstance(item, dict) and bool(item.get("enabled", True))
                and str(item.get("api_key") or "").strip()):
            result.append({
                "name": str(item.get("name") or item.get("provider") or "AI 供应商"),
                "provider": str(item.get("provider") or "").strip(),
                "model": str(item.get("model") or "").strip(),
            })
    return [item for item in result if item["provider"]]


def _select_chat_provider(settings, requested: str | None,
                          saved: str | None) -> dict | None:
    """按请求参数、灵感保存值、首个可用项顺序解析供应商。"""
    from ..chat_models import resolve_chat_provider
    available = _available_chat_providers(settings)
    by_provider = {item["provider"]: item for item in available}
    key = str(requested or saved or "").strip()
    if key and key in by_provider:
        return next(p for p in (getattr(settings, "ai_providers", None) or [])
                    if isinstance(p, dict) and str(p.get("provider") or "").strip() == key
                    and bool(p.get("enabled", True))
                    and str(p.get("api_key") or "").strip())
    # 显式 provider 是用户本次请求的硬选择，失效时返回可理解的 400；
    # 灵感记录中的历史选择失效则回退首个可用项，避免配置变更后无法继续对话。
    if requested is not None and str(requested or "").strip():
        raise HTTPException(400, f"指定的 AI 供应商不可用：{key}")
    return resolve_chat_provider(settings)


@router.get("/{inspiration_id}/chat-providers")
def inspiration_chat_providers(request: Request, inspiration_id: int):
    """返回可选对话供应商及该灵感当前保存的选择。"""
    c = request.app.state.ctx
    insp = _require_inspiration(c, inspiration_id)
    providers = _available_chat_providers(c.config.get())
    selected = insp["chat_provider"] if insp["chat_provider"] in {
        p["provider"] for p in providers} else (providers[0]["provider"] if providers else None)
    return {"selected": selected, "providers": providers}


@router.put("/{inspiration_id}/chat-provider")
def update_inspiration_chat_provider(request: Request, inspiration_id: int,
                                     body: InspirationChatProvider):
    """保存/清除灵感对话供应商选择，不影响既有消息。"""
    c = request.app.state.ctx
    insp = _require_inspiration(c, inspiration_id)
    provider = str(body.provider or "").strip() or None
    if provider is not None:
        available = {p["provider"] for p in _available_chat_providers(c.config.get())}
        if provider not in available:
            raise HTTPException(400, f"指定的 AI 供应商不可用：{provider}")
    if not c.db.set_inspiration_chat_provider(inspiration_id, provider):
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    row = c.db.get_inspiration(inspiration_id)
    assert row is not None
    return {"chat_provider": row["chat_provider"]}


@router.get("/{inspiration_id}/messages")
def inspiration_messages(request: Request, inspiration_id: int):
    """返回灵感与 AI agent 的对话历史（issue #166），按时间升序。

    对话面板打开时调用：返回该灵感全部历史消息（用户 + AI 回复），
    前端按时间序渲染；无对话时返回空列表（前端展示引导文案）。
    """
    c = request.app.state.ctx
    _require_inspiration(c, inspiration_id)
    rows = c.db.list_inspiration_messages(inspiration_id)
    return {"messages": [_message_to_dict(r) for r in rows]}


@router.post("/{inspiration_id}/messages", status_code=201)
def send_inspiration_message(request: Request, inspiration_id: int,
                             body: InspirationChatMessage):
    """向 AI agent 发送一条消息并返回回复（issue #166）。

    灵感条目「对话」面板发送按钮调用：后端先保存用户消息，再携带
    「系统提示（含灵感内容与仓库名）+ 最近 N 条历史 + 新消息」调用
    对话模型，成功后保存 AI 回复并一并返回（前端 append 到消息列表）。

    失败处理：灵感不存在 → 404；消息内容为空 / 超长 → 400；未配置
    可用的 AI 对话供应商（ai_providers 为空或全部未启用/无 Key）→
    400 引导设置；模型调用失败 / 网络错误 → 502，且回滚删除刚保存的
    用户消息——对话历史保持「user 必有对应 assistant」的成对结构，
    前端输入框保留内容供用户重试。
    """
    c = request.app.state.ctx
    insp = _require_inspiration(c, inspiration_id)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "消息内容不能为空")
    if len(content) > MAX_CHAT_CONTENT_LEN:
        raise HTTPException(400, f"消息内容不能超过 {MAX_CHAT_CONTENT_LEN} 字")
    # 对话模型按显式参数 → 灵感保存值 → 首个可用项回退，保持旧兼容行为。
    from ..chat_models import ChatModelClient, ChatModelError
    settings = c.config.get()
    provider_cfg = _select_chat_provider(settings, body.provider, insp["chat_provider"])
    if provider_cfg is None:
        raise HTTPException(
            400, "未配置 AI 对话模型：请先在设置页「AI API 供应商」添加"
                 "并启用一个供应商（需填写 API Key）")
    # 系统提示：角色设定 + 当前探讨的灵感内容与所属仓库（AI 回复围绕
    # 该灵感展开）；历史取最近 CHAT_CONTEXT_MESSAGES 条控制上下文长度
    repo = dict(c.db.get_repo(insp["repo_id"]) or {})
    repo_name = (repo.get("name") or "").strip() or "未知仓库"
    system_prompt = (
        "你是 Botler 平台的 AI 灵感顾问。用户会分享关于某个仓库的新功能"
        "灵感，请作为产品与技术顾问与用户一起探讨：帮助完善想法、补充"
        "边界场景与细节、评估可行性并指出潜在风险与成本、给出分步落地"
        "的建议。用中文回答，简洁有条理；灵感内容不明确时先向用户提问"
        "澄清。\n当前探讨的灵感（仓库：%s）：\n%s" % (repo_name, insp["content"]))
    history = c.db.list_inspiration_messages(
        inspiration_id, limit=CHAT_CONTEXT_MESSAGES)
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": r["role"], "content": r["content"]} for r in history]
    messages.append({"role": "user", "content": content})
    # 先保存用户消息；AI 调用失败时回滚删除（delete_inspiration_message），
    # 保证对话历史成对完整
    user_msg_id = c.db.add_inspiration_message(inspiration_id, "user", content)
    try:
        client = ChatModelClient(
            name=str(provider_cfg.get("name") or "AI 供应商"),
            provider=str(provider_cfg.get("provider") or "custom").strip(),
            base_url=str(provider_cfg.get("base_url") or "").strip(),
            api_key=str(provider_cfg.get("api_key") or "").strip(),
            model=str(provider_cfg.get("model") or "").strip(),
            timeout=CHAT_TIMEOUT,
            verify_ssl=getattr(settings, "verify_ssl", True))
        reply = client.chat(messages)
    except ChatModelError as e:
        c.db.delete_inspiration_message(user_msg_id)
        raise HTTPException(502, f"AI 回复失败: {e}") from e
    except httpx.HTTPError as e:
        c.db.delete_inspiration_message(user_msg_id)
        raise HTTPException(502, f"AI 回复网络错误: {str(e)[:200]}") from e
    reply = (reply or "").strip()
    if not reply:
        c.db.delete_inspiration_message(user_msg_id)
        raise HTTPException(502, "AI 回复为空，请稍后重试")
    assistant_msg_id = c.db.add_inspiration_message(
        inspiration_id, "assistant", reply)
    user_row = c.db.get_inspiration_message(user_msg_id)
    assistant_row = c.db.get_inspiration_message(assistant_msg_id)
    assert user_row is not None and assistant_row is not None  # 刚写入必然可查
    return {"messages": [
        _message_to_dict(user_row),
        _message_to_dict(assistant_row),
    ]}
