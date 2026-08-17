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
- POST   /api/inspirations/{id}/add-issue（issue #143 / #153 / #159）：
  将灵感一键提交为 GitLab issue——灵感内容同时作为标题与描述，默认
  标签 feature + ui，分配人 = 仓库用户（issue #153：仓库设置页读取
  remote url 得到的 userinfo 用户名，解析为 GitLab 用户 id；issue #159：
  存储值为空时提交时按 remote url 运行时读取兜底并写回仓库表，未配置/
  解析失败则不指定分配人）；走 owner token（复用 issues 模块的
  _issue_edit_call，绝不回退 bot token），创建成功后清空概览缓存。

校验：repo_id 必须指向存在且未软删除的仓库（400）；content 去除首尾
空白后非空（400）、长度不超过 5000 字（400，随手笔记的合理上限）；
add-issue 要求灵感存在（404）、所属仓库存在且未软删除（400）、仓库
已启用（400）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..gitlab_client import GitLabError
from .issues import _issue_edit_call, _trim_issue, clear_issue_cache

logger = logging.getLogger(__name__)

# 灵感一键提交 issue 的默认标签（issue #143）：需求约定 feature + ui
INSPIRATION_ISSUE_LABELS = ("feature", "ui")

router = APIRouter(prefix="/inspirations", tags=["inspirations"])

# 灵感内容长度上限（去除首尾空白后）：随手笔记场景的合理上限
MAX_CONTENT_LEN = 5000


class InspirationCreate(BaseModel):
    repo_id: int = Field(description="仓库本地 id（repos.id）")
    content: str = Field(description="灵感内容")


class InspirationUpdate(BaseModel):
    content: str = Field(description="灵感内容")


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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/overview")
def inspiration_overview(request: Request):
    """概览页灵感板块聚合数据：所有未软删除仓库 + 各自灵感列表。

    仓库顺序与仓库列表一致（priority 升序、同优先级按 id）；灵感按
    updated_at 降序（最新改动在前）。无灵感的仓库返回空列表。
    """
    c = request.app.state.ctx
    repos = c.db.list_repos()
    # issue #142：设置关闭时隐藏未启用项目（灵感 / CI/CD 页面一致）
    if not c.config.get().ui_show_disabled_repos:
        repos = [r for r in repos if r["enabled"]]
    insp_rows = c.db.list_inspirations()
    by_repo: dict[int, list[dict]] = {}
    for row in insp_rows:
        by_repo.setdefault(row["repo_id"], []).append(_row_to_dict(row))
    return {
        "repos": [
            {
                "repo_id": r["id"],
                "repo_name": r["name"],
                "enabled": bool(r["enabled"]),
                "priority": r["priority"],
                "inspirations": by_repo.get(r["id"], []),
            }
            for r in repos
        ],
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
    引导设置；token 失效/权限不足返回 502。创建成功后清空概览缓存，
    前端刷新开放 issue 列表即可看到新 issue。返回精简后的 issue 对象
    （含 iid/web_url，供前端展示创建成功提示与跳转链接）。

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
    try:
        issue = _issue_edit_call(
            c, repo,
            lambda cl: cl.create_issue(
                repo["gitlab_project_id"], content,
                description=content,
                assignee_id=assignee_id,
                labels=list(INSPIRATION_ISSUE_LABELS)))
    except GitLabError as e:
        raise HTTPException(502, f"创建 issue 失败: {e}") from e
    except httpx.HTTPError as e:
        # owner client 可能指向不可达 host（配置的 GitLab 地址异常）
        raise HTTPException(502, f"创建 issue 网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    # 标签色省略（创建刚完成，前端随即刷新列表从 overview 获取完整数据）
    return _trim_issue(issue, {})
