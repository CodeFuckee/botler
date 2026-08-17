#!/usr/bin/env python3
"""Botler E2E 数据库种子脚本（issue #212）。

在启动 uvicorn 前初始化一个「只含确定性假数据」的 SQLite 库：
  - 1 个仓库（repo id=1，gitlab project_id=1，name=botler，优先级 10）；
  - 1 条已成功任务（task id=1，issue_iid=101，status=succeeded），
    并指向一条 claude stream-json 格式的执行日志文件（SSE 事件流回放源）；
  - 少量任务日志行。

用法：
    BOTLER_DB=/tmp/e2e.db python3 frontend/e2e/scripts/seed-e2e-db.py
环境变量：
    BOTLER_DB      SQLite 文件路径（必填）
    E2E_LOG_FILE   执行日志文件路径（可选，默认 fixtures/task-log.ndjson）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 仓库根目录（本脚本位于 frontend/e2e/scripts/ 下，向上三级）
ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from botler.database import Database  # noqa: E402

# claude stream-json 执行日志样例（SSE 回放解析为逐事件展示）
LOG_LINES = [
    '{"type":"system","subtype":"init","session_id":"e2e-session-0001","cwd":"/tmp/botler-e2e","model":"claude-3-5-sonnet"}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"E2E 事件流冒烟：开始执行任务"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"先定位问题根因，再动手修复"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"echo hello"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_01","content":"hello\\n"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"修复完成，准备推送代码"}]}}',
    '{"type":"result","result":"E2E 冒烟完成，任务成功","subtype":"success","exit_code":0}',
]


def main() -> int:
    db_path = os.environ.get("BOTLER_DB", "")
    if not db_path:
        print("错误：请设置 BOTLER_DB 环境变量指定种子数据库路径")
        return 1
    log_file = Path(os.environ.get("E2E_LOG_FILE", str(ROOT / "frontend/e2e/fixtures/task-log.ndjson")))

    db = Database(db_path)

    # 1) 仓库
    repo_id = db.upsert_repo(
        project_id=1, name="botler",
        url="https://gitlab.example.com/botler",
        enabled=True, priority=10,
    )
    print(f"仓库已写入: id={repo_id}")

    # 2) 执行日志文件（SSE 回放源，claude stream-json 每行一个事件）
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    print(f"执行日志已写入: {log_file}")

    # 3) 成功任务
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    finish = (now - timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S")
    created = (now - timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S")
    task_id = db.create_task(
        repo_id=repo_id, project_id=1, issue_iid=101,
        issue_title="E2E 示例任务：修复概览页按钮样式",
        triggered_by="webhook",
        issue_labels=["bug"],
        issue_updated_at=start,
        issue_created_at=created,
    )
    if task_id is None:
        print("警告：已存在活跃任务，沿用现有数据")
        return 0
    db.set_task_status(
        task_id, "succeeded",
        attempt_count=1, exit_code=0,
        log_path=str(log_file),
        started_at=start, finished_at=finish,
        commit_sha="e2e1234567890abcdef",
        engine="claude",
    )
    print(f"任务已写入: id={task_id} status=succeeded")

    # 4) 任务日志行
    db.add_logs(task_id, [
        ("INFO", "webhook 收到 push 事件，创建任务"),
        ("INFO", "worker 领取任务，开始执行（engine=claude）"),
        ("INFO", "任务执行完成，共 1 次尝试"),
    ])
    print("任务日志已写入")

    # 5) 一条灵感（概览页灵感板块有可展示数据）
    insp_id = db.create_inspiration(repo_id, "E2E 冒烟灵感：为概览页增加导出功能")
    print(f"灵感已写入: id={insp_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
