# -*- coding: utf-8 -*-
"""后端测试流水线串行执行的配置回归测试（issue #175）。"""
from pathlib import Path


CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"


def test_backend_test_defaults_to_one_pytest_worker():
    """共享文件系统上的 SQLite 测试默认串行，避免 xdist 锁协议异常。

    注：backend:test 已在 windows runner（pwsh shell）上运行（issue #469），
    脚本为 PowerShell 语法；此处断言 pwsh 版本的串行默认逻辑。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    start = text.index("backend:test:")
    body = text[start:text.index("backend:mypy:", start)]
    assert 'if ($env:PYTEST_WORKERS) { $env:PYTEST_WORKERS } else { "1" }' in body
    assert 'PYTEST_WORKERS=$(( NPROC > 4 ? 4 : NPROC ))' not in body


def test_backend_test_uses_local_tmpfs_for_pytest_databases():
    """测试临时 SQLite 文件不得落到 shell runner 的共享工作区。

    注：Windows 下用 $env:TEMP（本机用户临时目录），落在本地文件系统。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    start = text.index("backend:test:")
    body = text[start:text.index("backend:mypy:", start)]
    assert 'Join-Path $env:TEMP "botler-pytest-$env:CI_PIPELINE_ID-$env:CI_JOB_ID"' in body
    assert '--basetemp="$PYTEST_BASETEMP"' in body
