# -*- coding: utf-8 -*-
"""部署架构回归测试：生产进程必须运行在稳定部署目录（issue #467）。

issue #467 根因链（CI 跑后端测试时生产页面卡顿、设置加载不出）：
1. deploy_to_code01 用 `pm2 start deploy/botler.config.cjs` 直接以
   gitlab-runner 构建检出目录（$CI_PROJECT_DIR）为 ROOT 启动生产进程，
   生产代码 / 全部依赖（.venv）/ 前端静态资源（frontend/dist）都来自
   构建目录；
2. 同一流水线其他 job 复用构建 slot 时，会对生产所在目录就地执行
   git checkout（重写源码）、uv pip install（变异生产 .venv，安装期间
   新 import 读到半写文件 → ImportError/500）、vite build
   （emptyOutDir: true 清空重建生产静态资源目录 → 资源 404），
   叠加 CPU/磁盘争抢 → 页面卡顿、设置加载不出；
3. 会话签名密钥 session_secret.key 存在构建目录 backend/data/（未随
   data/ 持久化），deploy 落到不同 slot 时重新生成密钥 → 所有用户
   会话失效（401 洪峰，10:08:04 日志实证）→ 前端跳登录/加载不出。

本测试断言 deploy job 必须：
- 以稳定部署目录（STABLE_DIR=/home/ckd/codes/botler）为 pm2 运行根，
  禁止以 CI 构建目录（$CI_PROJECT_DIR）直接启动生产；
- 持久化会话签名密钥（backend/data → data/backend/data symlink 或
  BOTLER_SESSION_SECRET 注入），保证跨部署会话不失效。
"""
from pathlib import Path


CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"


def _deploy_body(text):
    """截取 deploy_to_code01 job 的 script 主体（job 头到下一 job 头）。"""
    start = text.index("deploy_to_code01:")
    end = text.index("# ================================================================\n# Stage: sync", start)
    return text[start:end]


def test_deploy_runs_from_stable_dir_not_ci_build_dir():
    """生产进程不得从 gitlab-runner 构建目录（$CI_PROJECT_DIR）运行。

    修复前：`pm2 start deploy/botler.config.cjs` 以构建检出目录为 ROOT
    启动生产，CI job 复用该目录就地变异生产代码/venv/dist；
    修复后：pm2 配置必须来自稳定部署目录（STABLE_DIR）。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _deploy_body(text)
    # 禁止以 CI 构建目录为根的裸 pm2 start
    assert "pm2 start deploy/botler.config.cjs" not in body, (
        "deploy 仍以 CI 构建目录为 ROOT 启动生产（pm2 start deploy/botler.config.cjs）")
    # 必须显式使用稳定部署目录
    assert "STABLE_DIR" in body or "/home/ckd/codes/botler" in body, (
        "deploy 未指定稳定部署目录（STABLE_DIR）")


def test_deploy_persists_session_secret():
    """会话签名密钥必须持久化，跨部署不失效（避免全员 401）。

    修复前：session_secret.key 存在构建目录 backend/data/，deploy 落到
    新 slot 重新生成密钥 → 所有会话失效；
    修复后：backend/data 必须指向 data/backend/data（或注入
    BOTLER_SESSION_SECRET 指向持久化密钥）。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    body = _deploy_body(text)
    has_symlink = 'backend/data' in body and 'ln -sfn' in body and 'DATA_DIR' in body
    has_env_secret = 'BOTLER_SESSION_SECRET' in body
    assert has_symlink or has_env_secret, (
        "deploy 未持久化会话签名密钥（缺 backend/data symlink 或 BOTLER_SESSION_SECRET）")
