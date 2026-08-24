# -*- coding: utf-8 -*-
"""后端测试流水线串行执行的配置回归测试（issue #175）。

issue #469：backend:test 已迁移到带 windows 标签的 runner 上运行，
runner 默认 shell 为 pwsh（PowerShell 7），因此脚本与 before_script
必须为 PowerShell 语法；bash 语法在 pwsh 下逐行报错（流水线 #1394
实测 before_script 中 export 报错导致 job 失败）。
"""
from pathlib import Path


CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"


def _backend_test_body(text):
    start = text.index("backend:test:")
    return text[start:text.index("backend:mypy:", start)]



def test_backend_test_defaults_to_one_pytest_worker():
    """共享文件系统上的 SQLite 测试默认串行，避免 xdist 锁协议异常。

    注：backend:test 已在 windows runner（pwsh shell）上运行（issue #469），
    脚本为 PowerShell 语法；此处断言 pwsh 版本的串行默认逻辑。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _backend_test_body(text)
    assert 'if ($env:PYTEST_WORKERS) { $env:PYTEST_WORKERS } else { "1" }' in body
    assert 'PYTEST_WORKERS=$(( NPROC > 4 ? 4 : NPROC ))' not in body


def test_backend_test_uses_local_tmpfs_for_pytest_databases():
    """测试临时 SQLite 文件不得落到 shell runner 的共享工作区。

    注：Windows 下用 $env:TEMP（本机用户临时目录），落在本地文件系统。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _backend_test_body(text)
    assert 'Join-Path $env:TEMP "botler-pytest-$env:CI_PIPELINE_ID-$env:CI_JOB_ID"' in body
    assert '--basetemp="$PYTEST_BASETEMP"' in body


def test_backend_test_uses_pwsh_before_script_not_bash():
    """before_script 必须为 pwsh 语法（issue #469）。

    流水线 #1394 实测：backend:test 的 before_script 仍继承 bash 语法的
    .backend_setup（export PATH=... 等），在 windows runner（pwsh shell）
    下执行报错导致 job 失败。因此 backend:test 必须改用 pwsh 版
    .backend_setup_windows，且 before_script 不得包含 bash 专属语法。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _backend_test_body(text)
    # extends 必须使用 pwsh 版模板，而非 bash 版 .backend_setup
    assert "extends: [.backend_setup_windows, .docs_only_skip]" in body
    # before_script 不得再出现 bash 专属语法（export / renice / ionice / set +e）
    assert "export PATH=" not in body
    assert "renice" not in body
    assert "ionice" not in body
    assert "set +e" not in body


def test_backend_setup_windows_template_is_pwsh():
    """.backend_setup_windows 模板必须是纯 PowerShell 语法（issue #469）。"""
    text = CI_FILE.read_text(encoding="utf-8")
    assert ".backend_setup_windows:" in text
    start = text.index(".backend_setup_windows:")
    end = text.index("Stage: security", start)
    tmpl = text[start:end]
    # PowerShell 语法特征
    assert '$ErrorActionPreference = "Stop"' in tmpl
    assert "$env:CI_PROJECT_DIR" in tmpl
    assert "$env:USERPROFILE" in tmpl
    assert "$env:PATH" in tmpl
    # 不得出现 bash 语法
    assert "export " not in tmpl
    assert "renice" not in tmpl
    assert "ionice" not in tmpl
    assert "set +e" not in tmpl
