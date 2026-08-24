"""模板版本历史与回滚 API（issue #262）。

模板编辑页每次保存模板时由 settings / repos API 记录新版本（相同内容
不重复记录，见 database.record_template_version）；本模块提供：
- GET  /api/template-versions?key=xxx&limit=N   历史版本列表（最新在前）
- POST /api/template-versions/{version_id}/rollback  回滚到指定版本

template_key 约定：global:default / global:resume / global:comment /
repo:{repo_id}（见 database._SCHEMA 注释）。回滚即把版本内容写回生效源
（config.yaml 的 templates 段 / repos.prompt_template），并作为一次新
保存记录新版本（note 标注「回滚到版本 N」），保证「最新版本 = 当前生效
内容」，回滚后任务立即使用旧版模板。
"""

from fastapi import APIRouter, HTTPException, Query, Request

from ..audit import record_audit
from ..config import DEFAULT_RESUME_PROMPT
from ..database import (
    TEMPLATE_KEY_GLOBAL_COMMENT,
    TEMPLATE_KEY_GLOBAL_DEFAULT,
    TEMPLATE_KEY_GLOBAL_RESUME,
    TEMPLATE_KEY_REPO_PREFIX,
)
from ..report import DEFAULT_COMMENT_TEMPLATE
from .repos import _repo_row_to_dict, _sync_repo_to_config

router = APIRouter(prefix="/template-versions")


def ctx_of(request: Request):
    return request.app.state.ctx


@router.get("")
def list_versions(request: Request,
                  key: str = Query(...),
                  limit: int = Query(100, ge=1, le=500)):
    """历史版本列表（最新在前），latest 为当前最新版本（前端展示版本号）。"""
    c = ctx_of(request)
    rows = [dict(r) for r in c.db.list_template_versions(key, limit=limit)]
    return {"key": key, "latest": rows[0] if rows else None, "versions": rows}


@router.post("/{version_id}/rollback")
def rollback(request: Request, version_id: int):
    """回滚到指定版本：内容写回生效源，回滚本身记为新版本（备注来源）。"""
    c = ctx_of(request)
    ver = c.db.get_template_version(version_id)
    if ver is None:
        raise HTTPException(404, "模板版本不存在")
    key = ver["template_key"]
    content = ver["content"]
    if key == TEMPLATE_KEY_GLOBAL_DEFAULT:
        c.config.update_section("templates", {"default": content})
    elif key == TEMPLATE_KEY_GLOBAL_RESUME:
        # 回滚到「内置默认」内容时归一为空串保存（恢复内置默认语义，
        # config.yaml 不留冗余全文；渲染时 get() 归一为内置默认）
        patch = "" if content == DEFAULT_RESUME_PROMPT else content
        c.config.update_section("templates", {"resume": patch})
    elif key == TEMPLATE_KEY_GLOBAL_COMMENT:
        patch = "" if content == DEFAULT_COMMENT_TEMPLATE else content
        c.config.update_section("templates", {"comment": patch})
    elif key.startswith(TEMPLATE_KEY_REPO_PREFIX):
        try:
            repo_id = int(key.split(":", 1)[1])
        except ValueError:
            raise HTTPException(400, "非法模板 key")
        row = c.db.get_repo(repo_id)
        if row is None or row["deleted_at"]:
            raise HTTPException(404, "仓库不存在或已删除")
        # 空内容 = 清空覆盖，回退全局默认模版（与 update_template 同语义）
        c.db.update_repo(repo_id, prompt_template=content or None)
        updated = _repo_row_to_dict(c.db.get_repo(repo_id))
        if any(r.project_id == updated["gitlab_project_id"]
               for r in c.config.get().repos):
            _sync_repo_to_config(request.app, updated)
    else:
        raise HTTPException(400, f"未知模板 key: {key}")
    # 回滚本身也落一个新版本（note 标注来源）；内容与最新版本相同时
    # 自动跳过（连续回滚同一目标不产生重复版本）
    new_id = c.db.record_template_version(
        key, content, note=f"回滚到版本 {ver['version_no']}")
    record_audit(request, c.db, "template.rollback", "template_version",
                 str(version_id), {
                     "key": key,
                     "version_no": ver["version_no"],
                     "new_version_id": new_id,
                 })
    return {
        "ok": True,
        "key": key,
        "content": content,
        "version_no": ver["version_no"],
        "new_version_id": new_id,
    }
