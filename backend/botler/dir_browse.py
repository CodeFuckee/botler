"""服务器目录浏览：列出指定路径下的子目录列表。

供「本地文件夹方式添加仓库」的目录选择对话框使用：
前端在浏览器里逐级浏览服务器文件系统（整个文件系统从 / 开始），
选中目录后回填路径再读取 git remote。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 伪文件系统挂载点：无实际仓库意义且可能巨大（/proc/<pid>/...），浏览时过滤。
# 判断基于 realpath 后的绝对路径（含这些根下的深层路径）。
_PSEUDO_FS_ROOTS = ("/proc", "/sys", "/dev", "/run")

# 单次浏览最多返回的子目录数（防超大目录拖垮响应）
DEFAULT_MAX_ENTRIES = 500


class DirBrowseError(Exception):
    """路径不存在、不是目录或无法读取。"""


def _is_pseudo_fs(path: str) -> bool:
    for root in _PSEUDO_FS_ROOTS:
        if path == root or path.startswith(root + "/"):
            return True
    return False


def list_subdirectories(path: str, max_entries: int = DEFAULT_MAX_ENTRIES) -> dict:
    """列出 path 下的子目录，返回 {"path", "parent", "subdirs"}。

    subdirs 每项: {"name", "path", "is_git", "readable"}：
      - name: 目录名（含隐藏目录，由前端按需过滤）
      - is_git: 目录含 .git（目录或 worktree 指针文件）即视为 git 仓库
      - readable: 目录是否可读可进入（前端禁用不可读目录）
    排序不区分大小写；超过 max_entries 截断。路径不存在、不是目录或
    无法读取时抛 DirBrowseError。
    """
    if not path or not path.strip():
        raise DirBrowseError("路径不能为空")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise DirBrowseError(f"路径不存在或不是文件夹: {path}")
    if _is_pseudo_fs(real):
        raise DirBrowseError(f"不支持浏览系统目录: {real}")

    subdirs: list[dict] = []
    try:
        with os.scandir(real) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if not is_dir:
                    continue
                subdirs.append({
                    "name": entry.name,
                    "path": os.path.join(real, entry.name),
                    "is_git": os.path.lexists(os.path.join(real, entry.name, ".git")),
                    "readable": os.access(entry.path, os.R_OK | os.X_OK),
                })
    except PermissionError:
        raise DirBrowseError(f"无法读取目录（权限不足）: {real}")
    except OSError as e:
        raise DirBrowseError(f"读取目录失败: {real}: {e}")

    subdirs.sort(key=lambda d: d["name"].lower())
    if len(subdirs) > max_entries:
        logger.warning("目录 %s 子目录数 %d 超过上限，截断为 %d",
                       real, len(subdirs), max_entries)
        subdirs = subdirs[:max_entries]

    parent = None if real == "/" else os.path.dirname(real)
    return {"path": real, "parent": parent, "subdirs": subdirs}
