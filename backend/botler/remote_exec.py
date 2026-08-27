"""SSH 远程执行通道（远程项目 + zcode 引擎联动）。

统一封装 botler → 远程服务器的 SSH 命令构造与执行，是全部远程操作
（工作区 git 序列、引擎进程拉起、连通性探测、目录识别）的唯一出口，
便于测试 mock 与安全策略集中管理：

- :func:`ssh_argv`    构造 ssh argv（BatchMode 禁止交互提示防挂起、
                      ServerAlive keepalive、accept-new 首连自动记录
                      host key、私钥/端口/用户/附加选项）；
- :func:`run_remote`  一次性执行（subprocess.run，超时/非零退出由
                      调用方按 CompletedProcess 判定）；
- :func:`stream_remote` 长驻流式执行（Popen，stdout 行流经本地 drain
                      落日志/SSE，任务引擎执行用）；
- :func:`sh_quote`    远端命令参数引用（shlex.quote，防注入）。

remote 为 Settings.remotes 的一项（dict）：
``{name, host, port=22, user="", key_path="", extra_options=[]}``。
认证必须为 SSH 密钥免密（BatchMode=yes 下交互式密码提示直接失败，
不会挂起任务）。
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

# 公共 ssh 选项：BatchMode 禁止交互式密码/确认提示（密钥免密为前提，
# 否则立即失败而非挂起）；ServerAlive 每 15s 心跳、3 次无响应判断连
# （长任务下 NAT/防火墙静默断链能及时暴露）；accept-new 首次连接自动
# 记录 host key（已知主机变更仍拒绝，防 MITM）
SSH_BASE_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "StrictHostKeyChecking=accept-new",
]


def remote_target(remote: dict | Any) -> str:
    """user@host 形态目标串（无 user 时仅 host）。"""
    host = str(remote.get("host", "") or "").strip()
    user = str(remote.get("user", "") or "").strip()
    return f"{user}@{host}" if user else host


def ssh_argv(remote: dict | Any, command: str) -> list[str]:
    """构造 ssh argv：``ssh [options] [user@]host <command>``。

    :param remote:  Settings.remotes 项（host 必填，其余可选）
    :param command: 在远端执行的完整命令串（调用方负责用 :func:`sh_quote`
                    引用动态参数）
    """
    host = str(remote.get("host", "") or "").strip()
    if not host:
        raise ValueError("远程主机缺少 host 配置")
    argv = ["ssh", *SSH_BASE_OPTIONS]
    key_path = str(remote.get("key_path", "") or "").strip()
    if key_path:
        argv.extend(["-i", key_path])
    port = remote.get("port", 22)
    argv.extend(["-p", str(port)])
    for opt in remote.get("extra_options") or []:
        opt = str(opt).strip()
        if opt:
            argv.extend(["-o", opt])
    argv.append(remote_target(remote))
    argv.append(command)
    return argv


def run_remote(remote: dict | Any, command: str, timeout: float = 60.0):
    """一次性执行远端命令（subprocess.run，capture 输出）。

    返回 CompletedProcess（returncode/stdout/stderr）；超时抛
    subprocess.TimeoutExpired、本机无 ssh 抛 FileNotFoundError，由调用方
    归类处理。命令必须以 ``git``/``test``/``echo`` 等白名单语义由调用方
    构造（本模块不做二次校验）。
    """
    return subprocess.run(  # noqa: S603 命令由服务端配置/代码构造
        ssh_argv(remote, command),
        capture_output=True, text=True, timeout=timeout)


def stream_remote(remote: dict | Any, command: str, stdin: int | None = None):
    """流式执行远端命令（Popen，stdout 合并 stderr 行流）。

    供任务引擎长驻执行：本地逐行 drain（_drain_process_output），进程组
    语义与本地 CLI 引擎一致（start_new_session，停止时 kill 本地进程组
    即断开 ssh 会话）。``stdin=subprocess.PIPE`` 时调用方可向远端引擎
    写 prompt（任务提示词不进远端 argv）。
    """
    return subprocess.Popen(  # noqa: S603 命令由服务端配置/代码构造
        ssh_argv(remote, command),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=stdin,
        text=True, start_new_session=True)


def sh_quote(arg: str) -> str:
    """远端命令参数引用（shlex.quote，防 shell 注入）。"""
    return shlex.quote(str(arg))
