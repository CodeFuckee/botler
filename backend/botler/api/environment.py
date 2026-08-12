"""本地环境检测 API（issue #22）：设置页展示服务器上各 agent 工具安装情况。

GET /api/environment：并发检测工具是否安装、已装版本、最新版本（npm/GitHub）。
前端进入设置页自动拉取，也可点「重新检测」刷新。
"""

from __future__ import annotations

from fastapi import APIRouter

from botler import environment

router = APIRouter(prefix="/environment", tags=["environment"])


@router.get("")
def get_environment():
    """检测服务器本地工具安装情况与版本，返回 tools 列表与检测元信息。"""
    return environment.detect_local_environment()
