#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在净化后的环境中执行 pm2 命令（issue #481）。

背景：deploy_to_code01 在 CI job shell 里执行 `pm2 start`，pm2 fork
模式会把调用方 shell 的环境整包传给生产进程——生产 uvicorn 因此带着
CI_JOB_ID / CI_JOB_TOKEN（作业结束即失效）/ CI_PROJECT_DIR（指向会被
清理的构建目录）/ GITLAB_CI=true / GIT_CONFIG_* 等运行。CI_JOB_TOKEN
作业结束即失效，被 git 凭据流程误用会 403（issue #18 曾致部署后任务
失败）；GITLAB_CI=true 会让生产进程误以为身处 CI。

本脚本剔除 CI_*/GITLAB_CI/GIT_CONFIG_* 后再执行 pm2，其余环境变量
（PATH/HOME/BOTLER_* 等）原样保留。

用法：pm2-clean-env.py <pm2 参数...>
  例：pm2-clean-env.py start /home/ckd/codes/botler/deploy/botler.config.cjs
"""
from __future__ import annotations

import os
import subprocess
import sys


def clean_env() -> dict:
    """剔除 gitlab-runner CI 环境变量，保留其余（含 PATH/HOME/BOTLER_*）。

    与 executor/workspace.py 的 _clean_process_env 同策略（issue #18）：
    CI_* 前缀 + GITLAB_CI + GIT_CONFIG_* 均为 runner 注入的作业环境，
    不应进入生产进程。
    """
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CI_")
                    or k == "GITLAB_CI"
                    or k.startswith("GIT_CONFIG_"))}


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: pm2-clean-env.py <pm2 参数...>", file=sys.stderr)
        return 2
    result = subprocess.run(["pm2", *sys.argv[1:]], env=clean_env())
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
