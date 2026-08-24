"""本地环境检测 API（issue #22）：设置页展示服务器上各 agent 工具安装情况。

GET /api/environment：并发检测工具是否安装、已装版本、最新版本（npm/GitHub）。
POST /api/environment/upgrade：升级指定工具到最新版本（issue #465），
成功后延迟重启服务使新版本生效（与备份恢复重启同模式）。
POST /api/environment/install：安装未安装且可自动安装的工具（issue #468），
成功后延迟重启服务使新安装的版本生效（与升级/备份恢复重启同模式）。
前端进入设置页自动拉取，也可点「重新检测」刷新。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from botler import environment
from botler.audit import record_audit

router = APIRouter(prefix="/environment", tags=["environment"])


@router.get("")
def get_environment():
    """检测服务器本地工具安装情况与版本，返回 tools 列表与检测元信息。"""
    return environment.detect_local_environment()


class UpgradeBody(BaseModel):
    """工具升级请求体（issue #465）：目标工具 key。"""

    key: str


@router.post("/upgrade")
def upgrade_tool_api(request: Request, body: UpgradeBody):
    """升级指定工具到最新版本，成功后调度延迟重启服务。

    升级方式按工具发布源自动分派（npm 全局 / 当前解释器 pip /
    gh release 二进制）；失败返回 400 携带可读错误信息；成功返回
    升级结果与「服务即将重启」标记，前端据此提示用户稍后刷新。
    """
    c = request.app.state.ctx
    key = body.key.strip()
    if not key:
        raise HTTPException(400, "工具 key 不能为空")
    try:
        result = environment.upgrade_tool(key)
    except environment.UpgradeError as exc:
        raise HTTPException(400, str(exc)) from None
    # issue #260：升级操作写审计日志（尽力而为，失败不阻断）
    record_audit(request, c.db, "environment.upgrade", "environment",
                 key, {"name": result.get("name"),
                       "version": result.get("version")})
    result["restarting"] = environment.schedule_restart(delay=2.0)
    result["ok"] = True
    return result


class InstallBody(BaseModel):
    """工具安装请求体（issue #468）：目标工具 key。"""

    key: str


@router.post("/install")
def install_tool_api(request: Request, body: InstallBody):
    """安装未安装且可自动安装的工具，成功后调度延迟重启服务。

    安装方式按工具发布源自动分派（npm 全局 / 当前解释器 pip /
    gh release 二进制）；失败返回 400 携带可读错误信息；成功返回
    安装结果与「服务即将重启」标记，前端据此提示用户稍后刷新。
    """
    c = request.app.state.ctx
    key = body.key.strip()
    if not key:
        raise HTTPException(400, "工具 key 不能为空")
    try:
        result = environment.install_tool(key)
    except environment.InstallError as exc:
        raise HTTPException(400, str(exc)) from None
    # issue #260：安装操作写审计日志（尽力而为，失败不阻断）
    record_audit(request, c.db, "environment.install", "environment",
                 key, {"name": result.get("name"),
                       "version": result.get("version")})
    result["restarting"] = environment.schedule_restart(delay=2.0)
    result["ok"] = True
    return result
