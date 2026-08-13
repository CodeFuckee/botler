"""SSE 事件流端点测试（实时输出功能第四轮）。

需求：GET /api/tasks/{task_id}/events 以 SSE 推送任务执行事件流。
- 连接建立先回放日志文件已有事件（历史/断线重连补齐），再订阅事件总线
  实时推送（任务运行中）；任务终态/流结束后发 done 事件收尾
- 任务不存在 404；日志文件缺失/不可读不报错（空回放）
- 事件 data 为归一化事件 JSON（seq/ts/kind/…），SSE Content-Type 正确
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.events import EventBus
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""

LIVE_STATUSES = ("queued", "running", "retrying")


def _log_line(kind: str, **extra) -> str:
    """构造日志文件里的 claude stream-json 原始行。"""
    if kind == "init":
        return json.dumps({"type": "system", "subtype": "init",
                           "session_id": "sess-1", "cwd": "/w", "model": "m"},
                          ensure_ascii=False)
    if kind == "text":
        return json.dumps({"type": "assistant",
                           "message": {"role": "assistant", "content": [
                               {"type": "text", "text": extra.get("text", "hi")}]}},
                          ensure_ascii=False)
    if kind == "tool":
        return json.dumps({"type": "assistant",
                           "message": {"role": "assistant", "content": [
                               {"type": "tool_use", "name": "Bash",
                                "input": {"command": "ls"}}]}},
                          ensure_ascii=False)
    if kind == "result":
        return json.dumps({"type": "result", "subtype": "success",
                           "result": "完成", "exit_code": 0}, ensure_ascii=False)
    raise AssertionError(f"未知行类型 {kind}")


@pytest.fixture
def api_app(tmp_path):
    """测试 app：挂 api 路由，ctx 含 executor（带事件总线）与临时 db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    executor = ClaudeExecutor(config, db, gitlab, renderer,
                              workspace_root=str(tmp_path / "workspace"),
                              event_bus=EventBus())
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, executor=executor,
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, executor, tmp_path


@pytest.fixture
def client(api_app):
    app, db, executor, tmp_path = api_app
    return TestClient(app), db, executor, tmp_path


def _mk_task(db, tmp_path, status: str, log_lines: list[str] | None) -> int:
    repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    task_id = db.create_task(repo_id, 42, 7, "修复登录问题", triggered_by="webhook")
    db.set_task_status(task_id, status)
    if log_lines is not None:
        log_path = tmp_path / f"task_{task_id}.log"
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        db.set_task_status(task_id, None, log_path=str(log_path))
    return task_id


def _parse_sse_data(lines: list[str]) -> list[dict]:
    """从 SSE 原始行提取 data 事件（跳过心跳注释与空行）。"""
    events = []
    for line in lines:
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


class TestTaskEventsSSE:
    """GET /api/tasks/{task_id}/events。"""

    def test_404_unknown_task(self, client):
        c, db, executor, tmp_path = client
        resp = c.get("/api/tasks/999/events")
        assert resp.status_code == 404

    def test_finished_task_replays_log_then_done(self, client):
        """终态任务：回放日志文件全部事件（按行序），最后 done 收尾。"""
        c, db, executor, tmp_path = client
        task_id = _mk_task(db, tmp_path, "succeeded",
                           [_log_line("init"), _log_line("text", text="你好"),
                            _log_line("tool"), _log_line("result")])

        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = _parse_sse_data(list(resp.iter_lines()))

        kinds = [e["kind"] for e in events]
        assert kinds == ["status", "text", "tool", "result", "done"]
        assert events[0]["session_id"] == "sess-1"
        assert events[1]["text"] == "你好"
        assert events[3]["result"] == "完成"

    def test_running_task_streams_live_events(self, client):
        """运行中任务：先回放已有日志，再实时推送总线事件，终态后 done。

        当前 starlette TestClient（httpx 兼容模式）的流式请求要等响应
        完整结束才返回（handle_request 阻塞），无法边读边驱动生成器；
        用后台线程模拟 worker 发布事件并把任务推向终态，生成器走完
        回放 → 实时推送 → done 自然结束，主线程收集全部事件断言顺序。
        """
        c, db, executor, tmp_path = client
        task_id = _mk_task(db, tmp_path, "running", [_log_line("init")])

        def driver():
            # 等生成器完成回放并进入订阅循环（订阅在回放前已建立，
            # 回放期间发布的事件在队列积累，回放后依次排空）
            time.sleep(0.5)
            executor.event_bus.publish(task_id, {"seq": 2, "ts": "2026-08-13T10:00:02Z",
                                                 "kind": "text", "text": "实时推送"})
            time.sleep(0.3)
            db.set_task_status(task_id, "succeeded")
            executor.event_bus.publish(task_id, {"seq": 3, "ts": "2026-08-13T10:00:03Z",
                                                 "kind": "result", "result": "完成"})

        t = threading.Thread(target=driver)
        t.start()
        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            assert resp.status_code == 200
            events = _parse_sse_data(list(resp.iter_lines()))
        t.join()

        kinds = [e["kind"] for e in events]
        assert kinds == ["status", "text", "result", "done"]
        assert events[0]["session_id"] == "sess-1"
        assert events[1]["text"] == "实时推送"

    def test_no_log_file_still_streams(self, client):
        """无日志文件（log_path 空）：不报错，直接订阅/done。"""
        c, db, executor, tmp_path = client
        task_id = _mk_task(db, tmp_path, "succeeded", None)

        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            assert resp.status_code == 200
            events = _parse_sse_data(list(resp.iter_lines()))
        assert [e["kind"] for e in events] == ["done"]

    def test_missing_log_file_on_disk_ok(self, client):
        """log_path 指向的文件已被删除：回放跳过，不报错。"""
        c, db, executor, tmp_path = client
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")
        db.set_task_status(task_id, "succeeded",
                           log_path=str(tmp_path / "gone.log"))

        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            assert resp.status_code == 200
            events = _parse_sse_data(list(resp.iter_lines()))
        assert [e["kind"] for e in events] == ["done"]

    def test_hermes_engine_log_lines_parsed(self, client):
        """engine=hermes 时按 hermes 事件行解析回放。"""
        c, db, executor, tmp_path = client
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")
        lines = [
            json.dumps({"event": "thinking", "text": "思考"}),
            json.dumps({"event": "tool_start", "tool": "bash", "input": "ls"}),
            json.dumps({"final_response": "完成", "messages": [],
                        "session_id": "hs", "error": None}),
        ]
        log_path = tmp_path / f"task_{task_id}.log"
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        db.set_task_status(task_id, "succeeded", log_path=str(log_path))
        executor.config.get().engine = "hermes"

        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            events = _parse_sse_data(list(resp.iter_lines()))
        kinds = [e["kind"] for e in events]
        assert kinds == ["thinking", "tool", "done"]
        assert events[0]["text"] == "思考"

    def test_replay_seq_monotonic(self, client):
        """回放事件的 seq 严格递增（前端按序渲染与去重依据）。"""
        c, db, executor, tmp_path = client
        task_id = _mk_task(db, tmp_path, "succeeded",
                           [_log_line("init"), _log_line("text"), _log_line("tool")])

        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            events = _parse_sse_data(list(resp.iter_lines()))
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
