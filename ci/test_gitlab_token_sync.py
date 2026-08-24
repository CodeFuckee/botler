# -*- coding: utf-8 -*-
"""部署凭据同步回归测试：deploy 必须把 CI 变量中的有效 GitLab bot
token 同步进生产配置（issue #481）。

背景（issue #481「backend:test 已迁移到另一台服务器，但运行
backend:test 阶段时生产页面仍卡顿」的深层根因之一）：
生产 botler 的 data/backend/config.yaml 与 data/backend/.env 中
gitlab.bot_token 是人工维护的陈旧值，token 轮换（如更新 CI 变量
GITLAB_BOT_TOKEN）后生产配置不跟着变。修复前 deploy 第 3b 步
「.env 已存在则保留服务器配置」，导致失效 token 永不过期——生产应用
所有 GitLab API 调用 401（对账每 5 分钟失败、应用启动失败、概览数据
拿不到）→ 页面数据加载不出/卡顿。之前的修复（#461 renice、#467 稳定
目录、#469 迁移 backend:test）针对的都是资源争抢，从未同步这个持续
存在的失效 token，因此「一直没修好」。

本测试断言：
1. deploy job 必须调用凭据同步脚本（deploy/sync-gitlab-credentials.py）；
2. 同步脚本行为正确：把 GITLAB_BOT_TOKEN 环境变量覆盖写入 .env 与
   config.yaml（不能保留陈旧值），token 为空时跳过，重复执行幂等。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"
SYNC_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sync-gitlab-credentials.py"

VALID_TOKEN = "glpat-test-valid-token-0123456789"
STALE_TOKEN = "glpat-stale-invalid-token-abcdefghij"


def _deploy_body(text):
    """截取 deploy_to_code01 job 的 script 主体（job 头到下一 job 头）。"""
    start = text.index("deploy_to_code01:")
    end = text.index("# ================================================================\n# Stage: sync", start)
    return text[start:end]


def _run_sync(data_dir: Path, token: str | None = None) -> subprocess.CompletedProcess:
    """以指定环境执行同步脚本（data_dir 下必须有 .env 与 config.yaml）。"""
    env = dict(os.environ)
    if token is None:
        env.pop("GITLAB_BOT_TOKEN", None)
    else:
        env["GITLAB_BOT_TOKEN"] = token
    return subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), str(data_dir)],
        env=env, capture_output=True, text=True)


@pytest.fixture()
def prod_dir(tmp_path):
    """构造含陈旧 token 的 DATA_DIR（其下 backend/.env 与 config.yaml）。"""
    d = tmp_path / "backend"
    d.mkdir()
    (d / ".env").write_text(
        f"GITLAB_BOT_TOKEN={STALE_TOKEN}\nWEBHOOK_SECRET=whsec_test\n", encoding="utf-8")
    (d / "config.yaml").write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        f"  bot_token: {STALE_TOKEN}\n"
        "  webhook_secret: whsec_test\n"
        "  verify_ssl: false\n"
        "worker: {}\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------
# 1. deploy job 配置断言（回归：修复前 deploy 不调用同步脚本）
# ---------------------------------------------------------------

def test_deploy_syncs_bot_token_from_ci_variable():
    """deploy 必须调用凭据同步脚本，且依赖 CI 变量 GITLAB_BOT_TOKEN。

    修复前：deploy 第 3b 步「.env 已存在则保留服务器配置」，CI 变量中
    的新 token 永远不会同步进生产配置 → 生产 token 永久失效。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _deploy_body(text)
    assert "sync-gitlab-credentials.py" in body, (
        "deploy 未调用凭据同步脚本（deploy/sync-gitlab-credentials.py）")


# ---------------------------------------------------------------
# 2. 同步脚本行为测试（复现：陈旧 token 不被更新）
# ---------------------------------------------------------------

def test_sync_updates_env_with_new_token(prod_dir):
    """陈旧 .env 的 GITLAB_BOT_TOKEN 必须被覆盖为 CI 变量中的新值。"""
    r = _run_sync(prod_dir, VALID_TOKEN)
    assert r.returncode == 0, f"同步脚本失败: {r.stderr}"
    env_text = (prod_dir / "backend" / ".env").read_text(encoding="utf-8")
    assert f"GITLAB_BOT_TOKEN={VALID_TOKEN}" in env_text
    assert STALE_TOKEN not in env_text


def test_sync_updates_config_yaml_with_new_token(prod_dir):
    """陈旧 config.yaml 的 gitlab.bot_token 必须同步为新值。"""
    r = _run_sync(prod_dir, VALID_TOKEN)
    assert r.returncode == 0, f"同步脚本失败: {r.stderr}"
    cfg_text = (prod_dir / "backend" / "config.yaml").read_text(encoding="utf-8")
    assert f"bot_token: {VALID_TOKEN}" in cfg_text
    assert STALE_TOKEN not in cfg_text


def test_sync_preserves_other_config_keys(prod_dir):
    """同步只改 bot_token，不得破坏 config.yaml 其他字段。"""
    _run_sync(prod_dir, VALID_TOKEN)
    cfg_text = (prod_dir / "backend" / "config.yaml").read_text(encoding="utf-8")
    assert "url: https://gitlab.example.com" in cfg_text
    assert "webhook_secret: whsec_test" in cfg_text
    assert "verify_ssl: false" in cfg_text


def test_sync_skips_when_token_env_missing(prod_dir):
    """GITLAB_BOT_TOKEN 未注入时跳过同步，保留原配置（幂等安全）。"""
    r = _run_sync(prod_dir, None)
    assert r.returncode == 0
    assert STALE_TOKEN in (prod_dir / "backend" / ".env").read_text(encoding="utf-8")
    assert STALE_TOKEN in (prod_dir / "backend" / "config.yaml").read_text(encoding="utf-8")


def test_sync_idempotent(prod_dir):
    """重复执行同步（token 已是最新）应保持幂等、不报错。"""
    assert _run_sync(prod_dir, VALID_TOKEN).returncode == 0
    assert _run_sync(prod_dir, VALID_TOKEN).returncode == 0
    cfg_text = (prod_dir / "backend" / "config.yaml").read_text(encoding="utf-8")
    assert cfg_text.count(f"bot_token: {VALID_TOKEN}") == 1
