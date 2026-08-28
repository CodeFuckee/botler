# -*- coding: utf-8 -*-
"""sync_wiki_to_github 对「GitHub Wiki 仓库不存在」的行为回归测试（issue #464）。

背景：GitLab Wiki（8 个页面）与 GITHUB_PUSH_TOKEN 均正常，但 GitHub 仓库
CodeFuckee/botler 的 Wiki Git 端点（botler.wiki.git）不存在，git clone 返回
GitHub 服务器错误「Repository not found」（流水线 #1353/#1354/#1381 等多次
实测）。修复前 sync_wiki_to_github job 因此直接失败（exit 128、红名显示）；
修复后应识别该外部依赖未就绪场景，输出明确诊断提示并以 exit 0 跳过，待
GitHub 侧初始化 Wiki 后自动恢复同步。
"""
import os
import stat
import subprocess
from pathlib import Path

import yaml

CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"

# GitHub Wiki 端点不存在的 fake git（复现 CI 实测错误输出）
_FAKE_GIT_404 = """#!/usr/bin/env bash
set -u
last=""
for a in "$@"; do
  if [[ "$a" == *github.com/CodeFuckee/botler.wiki.git* ]]; then
    echo 'remote: Repository not found.' >&2
    echo "fatal: repository 'https://github.com/CodeFuckee/botler.wiki.git/' not found" >&2
    exit 128
  fi
  last="$a"
done
if [[ "${1:-}" == "clone" ]]; then
  mkdir -p "$last"
fi
exit 0
"""

# GitHub Wiki 端点存在但网络/权限失败的 fake git
_FAKE_GIT_NETERR = """#!/usr/bin/env bash
set -u
last=""
for a in "$@"; do
  if [[ "$a" == *github.com/CodeFuckee/botler.wiki.git* ]]; then
    echo 'fatal: unable to access: SSL connection timeout' >&2
    exit 128
  fi
  last="$a"
done
if [[ "${1:-}" == "clone" ]]; then
  mkdir -p "$last"
fi
exit 0
"""


def _extract_job_script():
    """从 .gitlab-ci.yml 提取 sync_wiki_to_github 的完整 script。"""
    cfg = yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))
    script = cfg["sync_wiki_to_github"]["script"]
    assert isinstance(script, list) and script, "sync_wiki_to_github 应包含 script"
    return "\n".join(script)


def _install_fake_git(tmp_path, monkeypatch, fake_body):
    fake = tmp_path / "git"
    fake.write_text(fake_body, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


def _run_job_script(tmp_path, monkeypatch):
    """在隔离环境中执行 sync_wiki_to_github 的 script，返回 CompletedProcess。"""
    script = _extract_job_script()
    env = {
        **os.environ,
        "CI_JOB_ID": "test-464",
        "CI_SERVER_URL": "https://gitlab.example.com",
        "CI_PROJECT_PATH": "chenkaidi/botler",
        "CI_JOB_TOKEN": "dummy-token",
        "GITHUB_PUSH_TOKEN": "dummy-pat",
        "HOME": str(tmp_path),
    }
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=120
    )


def test_wiki_sync_job_skips_when_github_wiki_repo_not_found(tmp_path, monkeypatch):
    """GitHub Wiki 仓库不存在（Repository not found）时，作业应跳过（exit 0）并给出诊断提示。"""
    _install_fake_git(tmp_path, monkeypatch, _FAKE_GIT_404)
    result = _run_job_script(tmp_path, monkeypatch)
    assert result.returncode == 0, (
        f"GitHub Wiki 仓库不存在时作业应跳过（exit 0），实际退出码 {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "Repository not found" in combined, "应识别出 GitHub Wiki 仓库不存在的根因"
    assert "跳过" in combined, "应明确提示本次跳过同步"


def test_wiki_sync_job_real_failure_still_reported(tmp_path, monkeypatch):
    """GitHub Wiki 端点存在但克隆失败（网络/权限等）时，作业应如实失败而非静默跳过。"""
    _install_fake_git(tmp_path, monkeypatch, _FAKE_GIT_NETERR)
    result = _run_job_script(tmp_path, monkeypatch)
    assert result.returncode != 0, "非 404 的克隆失败应如实失败（红名暴露），不应静默跳过"
    assert "SSL connection timeout" in result.stderr + result.stdout, "应输出真实失败原因"
