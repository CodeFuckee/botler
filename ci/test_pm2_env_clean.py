# -*- coding: utf-8 -*-
"""pm2 启动环境净化回归测试：生产进程不得继承 gitlab-runner CI 环境
（issue #481）。

背景：deploy_to_code01 在 CI job shell 里执行 `pm2 start`，pm2 fork
模式会把调用方 shell 的环境整包传给生产进程——生产 uvicorn 因此带着
CI_JOB_ID / CI_JOB_TOKEN（作业结束即失效）/ CI_PROJECT_DIR（指向会被
清理的构建目录）/ GITLAB_CI=true / GIT_CONFIG_* 等运行。CI_JOB_TOKEN
在作业结束后立即失效，若被 git 凭据流程误用会 403（issue #18 曾致
部署后任务失败根因）；GITLAB_CI=true 会让生产进程误以为身处 CI。

本测试断言：
1. deploy job 必须用净化环境启动 pm2（走 deploy/pm2-clean-env.py）；
2. 净化函数正确剔除 CI_*/GITLAB_CI/GIT_CONFIG_*，保留其余环境变量。
"""
import os
import sys
from pathlib import Path

CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"
CLEAN_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "pm2-clean-env.py"


def _deploy_body(text):
    """截取 deploy_to_code01 job 的 script 主体（job 头到下一 job 头）。"""
    start = text.index("deploy_to_code01:")
    end = text.index("# ================================================================\n# Stage: sync", start)
    return text[start:end]


def test_deploy_starts_pm2_with_clean_env():
    """deploy 启动生产 pm2 必须经环境净化，禁止裸 pm2 start 继承 CI 环境。

    修复前：`pm2 start "$STABLE_DIR/deploy/botler.config.cjs"` 直接从
    CI job shell 启动，生产进程继承 CI_JOB_TOKEN 等失效凭据。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _deploy_body(text)
    assert "pm2-clean-env.py" in body, (
        "deploy 未用净化环境启动 pm2（deploy/pm2-clean-env.py）")
    assert "pm2 start \"$STABLE_DIR/deploy/botler.config.cjs\"" not in body, (
        "deploy 仍用裸 pm2 start（未净化 CI 环境）")


def _import_clean_env():
    """导入 pm2-clean-env.py 的 clean_env 函数（复用同一实现，防漂移）。

    文件名含连字符无法直接 import，用 importlib 按路径加载。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("pm2_clean_env", CLEAN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.clean_env


def test_clean_env_strips_ci_variables(monkeypatch):
    """净化环境必须剔除 CI_* / GITLAB_CI / GIT_CONFIG_*。"""
    monkeypatch.setenv("CI_JOB_ID", "10612")
    monkeypatch.setenv("CI_JOB_TOKEN", "glcbt-expired")
    monkeypatch.setenv("CI_PROJECT_DIR", "/home/ckd/builds/expired")
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/ci-gitconfig")
    monkeypatch.setenv("HOME", "/home/ckd")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("BOTLER_DATA_DIR", "/home/ckd/codes/botler/data")

    clean = _import_clean_env()()
    assert "CI_JOB_ID" not in clean
    assert "CI_JOB_TOKEN" not in clean
    assert "CI_PROJECT_DIR" not in clean
    assert "GITLAB_CI" not in clean
    assert "GIT_CONFIG_GLOBAL" not in clean
    # 非 CI 变量必须保留（生产运行所需）
    assert clean.get("HOME") == "/home/ckd"
    assert clean.get("PATH") == "/usr/bin:/bin"
    assert clean.get("BOTLER_DATA_DIR") == "/home/ckd/codes/botler/data"
