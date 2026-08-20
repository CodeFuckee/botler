"""任务执行前预检（issue #238）。

执行器领取任务后、消耗任何模型调用前对环境做快速预检：环境性失败
（git 凭据/token 失效、仓库不可克隆、本地路径不可写、磁盘空间不足、
工作区异常）直接判任务失败（**不重试、不消耗模型调用**），错误原因与
检查明细（✓/✗）写入任务记录，并在任务详情页「元信息」区展示。

预检项（每项独立判定，任一未通过即整体失败）：
- git_token：git 凭据/token 有效性——``git ls-remote`` 探测远端可达性与
  认证（URL 方式仓库直接探测 url；local_path 仓库在本地目录对所选
  remote 探测，与 fetch/push 同路径）
- local_path：仓库配置 local_path 时，本地目录存在且可写（未配置跳过）
- disk_space：磁盘剩余空间不低于配置阈值（默认 2GB）
- workspace：工作区根目录存在且可写、目标仓库目录未被文件占用

预检本身要求快（< 10 秒）：git 探测带超时上限，其余为本地文件系统
检查（微秒级），不显著增加任务开始延迟。预检通过的任务行为与现状
完全一致（不改变执行流程）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# git ls-remote 探测超时（秒）：单次探测预算，整体预检远小于 10s
GIT_LS_REMOTE_TIMEOUT = 8

# 检查项 key → 展示名（任务详情「元信息」区展示顺序即此处顺序）
PRECHECK_ITEMS = (
    ("git_token", "Git 凭据/Token"),
    ("local_path", "本地路径"),
    ("disk_space", "磁盘剩余空间"),
    ("workspace", "工作区可用"),
)


def check_git_credentials(repo: dict, workdir: Path, git_env: dict,
                          timeout: int = GIT_LS_REMOTE_TIMEOUT
                          ) -> tuple[bool | None, str]:
    """Git 凭据/token 有效性：``git ls-remote`` 探测远端可达性与认证。

    - URL 方式仓库：直接对 repo.url ls-remote（与首次 clone 同一路径）；
    - local_path 仓库：在本地目录对所选 remote（缺省 origin）ls-remote
      （与 fetch/push 同一路径）。
    任一失败（网络不可达 / 认证失败 / 命令异常 / 超时）即视为「仓库
    不可克隆 / 凭据无效」。仓库既无 url 也无 local_path 时判失败。
    返回 (ok, detail)：ok 为 True（通过）/ False（未通过）。
    """
    local_path = repo.get("local_path")
    if local_path:
        remote = repo.get("remote_name") or "origin"
        label = f"git ls-remote {remote}"
        try:
            result = subprocess.run(
                ["git", "-c", "http.sslVerify=false", "ls-remote", remote, "HEAD"],
                cwd=str(workdir), env=git_env, capture_output=True, text=True,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"{label} 超时（>{timeout}s），仓库远端不可达或响应缓慢"
        except OSError as e:
            return False, f"{label} 执行失败: {e}"
    else:
        url = repo.get("url")
        if not url:
            return False, "仓库未配置 url（无法探测克隆路径）"
        label = f"git ls-remote {url}"
        try:
            result = subprocess.run(
                ["git", "-c", "http.sslVerify=false", "ls-remote", url, "HEAD"],
                env=git_env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"{label} 超时（>{timeout}s），仓库远端不可达或响应缓慢"
        except OSError as e:
            return False, f"{label} 执行失败: {e}"
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()[-300:] or "未知错误"
        return False, f"{label} 失败（exit {result.returncode}）: {err}"
    return True, f"{label} 探测通过（仓库可克隆）"


def check_local_path(repo: dict) -> tuple[bool | None, str]:
    """本地路径检查：配置 local_path 时目录存在且可写；未配置返回 (None, 跳过)。"""
    local_path = repo.get("local_path")
    if not local_path:
        return None, "未配置 local_path，跳过"
    p = Path(local_path)
    if not p.exists():
        return False, f"本地路径不存在: {p}"
    if not p.is_dir():
        return False, f"本地路径不是目录: {p}"
    if not os.access(p, os.W_OK):
        return False, f"本地路径不可写: {p}"
    return True, f"本地路径存在且可写: {p}"


def check_disk_space(target: Path, min_free_mb: int) -> tuple[bool, str]:
    """磁盘剩余空间检查：目标文件系统剩余空间不低于阈值（MiB）。

    target 为实际落盘目录（URL 仓库 = 工作区根目录；local_path 仓库 =
    本地目录所在文件系统）。target 不存在时回退其最近存在的父目录，
    仍不存在（异常场景）按失败处理。
    """
    p = target
    while p is not None and not p.exists():
        p = p.parent
    if p is None:
        return False, f"磁盘空间探测路径不存在: {target}"
    try:
        usage = shutil.disk_usage(p)
    except OSError as e:
        return False, f"磁盘空间探测失败（{p}）: {e}"
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_free_mb:
        return (False,
                f"磁盘剩余 {free_mb:.1f} MB < 阈值 {min_free_mb} MB（{p}）")
    return True, f"磁盘剩余 {free_mb:.1f} MB ≥ 阈值 {min_free_mb} MB（{p}）"


def check_workspace(workspace_root: Path, workdir: Path) -> tuple[bool, str]:
    """工作区可用性检查：根目录存在且可写，目标仓库目录未被文件占用。"""
    if not workspace_root.exists():
        return False, f"工作区根目录不存在: {workspace_root}"
    if not workspace_root.is_dir():
        return False, f"工作区根目录不是目录: {workspace_root}"
    if not os.access(workspace_root, os.W_OK):
        return False, f"工作区根目录不可写: {workspace_root}"
    if workdir.exists() and not workdir.is_dir():
        return False, f"目标仓库目录被文件占用: {workdir}"
    if workdir.exists() and not os.access(workdir, os.W_OK):
        return False, f"目标仓库目录不可写: {workdir}"
    return True, "工作区可用"


def serialize_precheck(result: dict) -> str:
    """预检结果 dict → JSON 字符串（落库 tasks.precheck_result）。"""
    return json.dumps(result, ensure_ascii=False, default=str)


def parse_precheck(text: str | None) -> dict | None:
    """tasks.precheck_result JSON 字符串 → dict；空/非法返回 None。"""
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def format_precheck_failure(result: dict) -> str:
    """预检失败原因摘要：列出全部未通过检查项（含具体原因）。

    用于任务 error_message（详情页/列表页直接可读）与失败评论。
    """
    failed = [c for c in result.get("checks", []) if c.get("ok") is False]
    if not failed:
        return "任务执行前预检失败（无明细）"
    parts = [
        f"{c.get('label') or c.get('name')}检查未通过：{c.get('detail') or '原因未知'}"
        for c in failed
    ]
    return "任务执行前预检失败：" + "；".join(parts)


def _now_iso() -> str:
    """预检时间戳（ISO8601 本地时区，前端按本地时间展示）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
