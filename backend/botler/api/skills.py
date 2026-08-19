"""技能管理 API（issue #282）：技能页面的后端接口。

- ``GET /api/skills``：按执行引擎分组列出全部技能（每个引擎的技能
  目录根 + 目录下含 SKILL.md 的技能列表，附 frontmatter description）；
- ``GET /api/skills/{engine}/files?skill=...``：列出技能目录内全部
  md 文件（递归，相对路径）；
- ``GET /api/skills/{engine}/file?skill=...&path=...``：读取 md 文件内容；
- ``PUT /api/skills/{engine}/file``：保存 md 文件内容
  （body: {skill, path, content}）；
- ``POST /api/skills/sync``：同步所有 agent 技能（issue #328）——合并
  全部执行引擎技能去重后复制到各引擎技能根目录，返回结果统计。

技能名为技能目录相对引擎技能根的路径（``/`` 分隔，可嵌套，如
``software-development/spike``），因含斜杠走 query 参数而非路径段。

安全约束（见 skills.safe_md_path / resolve_skill_dir）：engine 必须是
已注册的执行引擎插件；skill 必须是技能根下含 SKILL.md 的目录、且
路径组件不得为 ``..``；文件读写仅允许技能目录内的 md / markdown 文件，
路径穿越（``../``、绝对路径）与非 md 文件一律 400 拒绝，文件不存在 404。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..plugins import PluginKind, PluginNotFoundError, get_plugin, list_plugins
from ..skills import (
    SKILL_MD,
    engine_skills_roots,
    iter_skills,
    list_md_files,
    read_md_file,
    resolve_skill_dir,
    sync_skills_to_engines,
    write_md_file,
)

router = APIRouter(prefix="/skills", tags=["skills"])


def _get_engine_plugin(engine: str):
    """按名称取执行引擎插件；未注册 404。"""
    try:
        return get_plugin(PluginKind.EXECUTOR, engine)
    except PluginNotFoundError:
        raise HTTPException(404, f"执行引擎未注册: {engine}") from None


def _get_skill_dir(engine: str, skill: str, plugin) -> Path:
    """解析引擎下技能目录；非法技能名或不存在 404。"""
    skill_dir = resolve_skill_dir(engine, skill, plugin)
    if skill_dir is None:
        raise HTTPException(404, f"技能不存在: {engine}/{skill}")
    return skill_dir


def _engine_view(plugin, is_default: bool) -> dict:
    """单执行引擎视图：技能目录根（含 exists 标记）+ 技能列表。"""
    roots = [{"path": str(p), "exists": p.is_dir()}
             for p in engine_skills_roots(plugin.name, plugin)]
    skills: list[dict] = []
    for root in roots:
        if not root["exists"]:
            continue
        for skill in iter_skills(Path(root["path"])):
            skills.append({
                "name": skill["name"],
                "description": skill["description"],
                "path": skill["path"],
                "root": root["path"],
            })
    return {
        "name": plugin.name,
        "description": plugin.description,
        "default": is_default,
        "roots": roots,
        "skills": skills,
    }


@router.get("")
def list_skills_api(request: Request):
    """技能列表：按执行引擎分组返回全部技能与目录根上下文。"""
    c = request.app.state.ctx
    engine = c.config.get().engine
    engines = [_engine_view(p, p.name == engine)
               for p in list_plugins(PluginKind.EXECUTOR)]
    return {"engine": engine, "engines": engines}


@router.get("/{engine}/files")
def list_skill_files_api(request: Request, engine: str, skill: str):
    """技能目录内 md 文件列表（递归，相对路径）。"""
    plugin = _get_engine_plugin(engine)
    skill_dir = _get_skill_dir(engine, skill, plugin)
    return {
        "engine": engine,
        "skill": skill,
        "root": str(skill_dir),
        "files": list_md_files(skill_dir),
    }


@router.get("/{engine}/file")
def read_skill_file_api(request: Request, engine: str, skill: str,
                        path: str):
    """读取技能目录内 md 文件内容。"""
    plugin = _get_engine_plugin(engine)
    skill_dir = _get_skill_dir(engine, skill, plugin)
    try:
        content = read_md_file(skill_dir, path)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    return {"engine": engine, "skill": skill, "path": path,
            "content": content}


class SkillFilePatch(BaseModel):
    """md 文件保存请求体：技能相对路径 + 文件相对路径 + 新内容。"""

    skill: str
    path: str
    content: str


@router.put("/{engine}/file")
def write_skill_file_api(request: Request, engine: str, body: SkillFilePatch):
    """保存技能目录内 md 文件内容，返回写入字节数。"""
    plugin = _get_engine_plugin(engine)
    skill_dir = _get_skill_dir(engine, body.skill, plugin)
    try:
        size = write_md_file(skill_dir, body.path, body.content)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "engine": engine, "skill": body.skill,
            "path": body.path, "size": size,
            "is_skill_md": Path(body.path).name.lower() == SKILL_MD.lower()}


@router.post("/sync")
def sync_skills_api(request: Request):
    """同步所有执行引擎技能（issue #328）。

    合并全部 executor 执行引擎（注册顺序）技能去重后，复制到每个引擎的
    全部技能根目录：缺失根目录自动创建、目标已存在同名技能跳过不覆盖。
    返回 merged / deduped / targets（各引擎新增·跳过·失败明细）/
    summary 汇总统计，供技能页「同步所有 agent 技能」按钮展示。
    """
    return sync_skills_to_engines()
