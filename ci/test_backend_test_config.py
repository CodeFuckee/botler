# -*- coding: utf-8 -*-
"""后端测试流水线串行执行的配置回归测试（issue #175）。"""
from pathlib import Path


CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"


def test_backend_test_defaults_to_one_pytest_worker():
    """共享文件系统上的 SQLite 测试默认串行，避免 xdist 锁协议异常。"""
    text = CI_FILE.read_text(encoding="utf-8")
    start = text.index("backend:test:")
    body = text[start:text.index("backend:mypy:", start)]
    assert 'PYTEST_WORKERS="${PYTEST_WORKERS:-1}"' in body
    assert 'PYTEST_WORKERS=$(( NPROC > 4 ? 4 : NPROC ))' not in body


def test_backend_test_uses_local_tmpfs_for_pytest_databases():
    """测试临时 SQLite 文件不得落到 shell runner 的共享工作区。"""
    text = CI_FILE.read_text(encoding="utf-8")
    start = text.index("backend:test:")
    body = text[start:text.index("backend:mypy:", start)]
    assert 'PYTEST_BASETEMP="/tmp/botler-pytest-${CI_PIPELINE_ID:-local}-${CI_JOB_ID:-local}"' in body
    assert '--basetemp="$PYTEST_BASETEMP"' in body
