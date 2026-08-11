"""备份/恢复 API：列表 / 创建 / 下载 / 删除 / 恢复（本地历史 + 上传）。

错误映射：BackupError.status_code（404 = 备份不存在 / 400 = 校验失败）。
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ..backup import BackupError

router = APIRouter(prefix="/backups", tags=["backups"])


def _backup(request: Request):
    return request.app.state.ctx.backup


def _handle(e: BackupError) -> HTTPException:
    return HTTPException(e.status_code, str(e))


@router.get("")
def list_backups(request: Request):
    """备份列表 + 备份配置（定时开关 / 保留天数）。"""
    b = _backup(request)
    s = request.app.state.ctx.config.get()
    return {
        "backups": b.list_backups(),
        "config": {
            "enabled": s.backup_enabled,
            "retention_days": s.backup_retention_days,
        },
    }


@router.post("")
def create_backup(request: Request):
    """手动创建一份备份。"""
    try:
        return _backup(request).create_backup(trigger="manual")
    except BackupError as e:
        raise _handle(e) from e


@router.get("/{name}/download")
def download_backup(request: Request, name: str):
    """下载备份包到本地电脑。"""
    try:
        path = _backup(request)._safe_path(name)
    except BackupError as e:
        raise _handle(e) from e
    return FileResponse(path, filename=name, media_type="application/gzip")


@router.delete("/{name}")
def delete_backup(request: Request, name: str):
    """删除一份备份。"""
    try:
        return _backup(request).delete_backup(name)
    except BackupError as e:
        raise _handle(e) from e


from pydantic import BaseModel  # noqa: E402


class RestoreRequest(BaseModel):
    name: str


@router.post("/restore")
def restore_local(request: Request, body: RestoreRequest):
    """从服务器本地历史备份恢复（覆盖数据 + 自动重启）。"""
    try:
        return _backup(request).restore_backup(body.name)
    except BackupError as e:
        raise _handle(e) from e


@router.post("/restore/upload")
async def restore_upload(request: Request, file: UploadFile):
    """上传备份包恢复（覆盖数据 + 自动重启）。"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            tmp = f.name
            while chunk := await file.read(1 << 20):
                f.write(chunk)
        return _backup(request).restore_upload(tmp)
    except BackupError as e:
        raise _handle(e) from e
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
