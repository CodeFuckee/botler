"""全局事件流（SSE）测试：轮询改事件驱动刷新（issue #478）。

需求：概览页/统计页由「固定间隔轮询」改为「后端事件通知 + 前端按需
刷新」——后端在数据真实变化点发布轻量通知事件，前端通过单一 SSE 长
连接（GET /api/events）订阅。
- AppEventBus：订阅 / 发布 / 退订；队列满丢最旧不阻塞发布者
- /api/events 端点：订阅全局总线并推送事件 data（JSON {"type": ...}），
  Content-Type text/event-stream，心跳注释行保活，连接断开自动退订
- 发布点：任务创建/状态变化（database）、issue 缓存清理、灵感增删改、
  设置保存、流水线概览缓存重拉后发布对应类型事件
"""

import asyncio
import json
import queue

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.database import Database
from botler.gitlab_client import GitLabClient
from botler.events import AppEventBus, global_bus

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


@pytest.fixture(autouse=True)
def _clean_global_bus():
    """用例隔离：清空全局总线订阅，避免跨用例泄漏。"""
    with global_bus._lock:  # noqa: SLF001 测试内直接清理单例
        subs = list(global_bus._subs)  # noqa: SLF001
    for q in subs:
        global_bus.unsubscribe(q)
    yield
    with global_bus._lock:  # noqa: SLF001
        subs = list(global_bus._subs)  # noqa: SLF001
    for q in subs:
        global_bus.unsubscribe(q)


@pytest.fixture
def app():
    """测试 app：挂 api 路由与最小 ctx（含独立事件总线）。

    issue #486：build_context 显式传 db_path 指向临时库——此前默认
    Database() 会受 BOTLER_DB 环境变量影响，在指向真实库（如生产
    data/backend/botler.db）的环境下运行测试，会在真实库中新增
    demo 仓库与测试灵感且不清理。db_path 隔离后测试写临时库，不再
    污染生产/开发数据库。
    """
    from botler.main import create_app  # noqa: F401
    # 直接构建最小 app，避免 create_app 的完整上下文（调度器/对账等）
    from botler.main import build_context
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        config_path = f"{td}/config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(CONFIG_TEXT)
        ctx = build_context(config_path, db_path=f"{td}/events.db")
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        yield app


# ---- AppEventBus 单元测试 ----

class TestAppEventBus:
    def test_publish_delivers_to_subscriber(self):
        bus = AppEventBus()
        q = bus.subscribe()
        bus.publish({"type": "task"})
        assert q.get_nowait() == {"type": "task"}

    def test_publish_delivers_to_all_subscribers(self):
        bus = AppEventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish({"type": "issue"})
        assert q1.get_nowait()["type"] == "issue"
        assert q2.get_nowait()["type"] == "issue"

    def test_unsubscribe_stops_delivery(self):
        bus = AppEventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish({"type": "task"})
        with pytest.raises(queue.Empty):
            q.get_nowait()

    def test_full_queue_drops_oldest_not_block(self):
        """队列满丢最旧再入队，发布者不阻塞（慢消费者不拖垮业务线程）。"""
        bus = AppEventBus(maxsize=2)
        q = bus.subscribe()
        bus.publish({"type": "task", "seq": 1})
        bus.publish({"type": "task", "seq": 2})
        bus.publish({"type": "task", "seq": 3})
        # 队列满：丢 1 留 2，入 3
        assert q.get_nowait()["seq"] == 2
        assert q.get_nowait()["seq"] == 3
        with pytest.raises(queue.Empty):
            q.get_nowait()


# ---- /api/events SSE 端点 ----

def _parse_sse_data(lines):
    """从 SSE 原始行提取 data 事件（跳过心跳注释与空行）。"""
    events = []
    for line in lines:
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


class FakeRequest:
    """最小 request 替身：is_disconnected 返回 False（连接保持）。"""

    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


class TestGlobalEventsSSE:
    """GET /api/events 的事件流生成器（_event_stream）。

    全局事件流为无限长连接（无 done 收尾），TestClient 同步模式无法
    测试无限流（会阻塞到超时），改为直接驱动异步生成器验证。
    """

    def test_stream_delivers_published_events(self):
        """订阅全局总线后，发布的事件经 SSE data 推送（JSON 解析正确）。"""
        from botler.api.events import _event_stream
        bus = AppEventBus()
        req = FakeRequest()
        events = []

        async def main():
            async def publisher():
                await asyncio.sleep(0.1)
                bus.publish({"type": "task", "seq": 1})
                await asyncio.sleep(0.1)
                bus.publish({"type": "issue", "seq": 2})

            async def consumer():
                async for chunk in _event_stream(bus, req):
                    if chunk.startswith("data: "):
                        events.append(json.loads(chunk[len("data: "):]))
                    if len(events) >= 2:
                        break

            await asyncio.gather(publisher(), consumer())

        asyncio.run(main())
        assert [e["type"] for e in events] == ["task", "issue"]
        assert events[0]["seq"] == 1

    def test_stream_sends_heartbeat_ping_when_idle(self, monkeypatch):
        """空闲超过心跳间隔发 ': ping' 注释行（EventSource 忽略）保活。"""
        import botler.api.events as events_mod
        from botler.api.events import _event_stream
        monkeypatch.setattr(events_mod, "PING_INTERVAL_SECONDS", 0.05)
        bus = AppEventBus()
        req = FakeRequest()
        got = []

        async def main():
            async for chunk in _event_stream(bus, req):
                got.append(chunk)
                if any(c.startswith(": ping") for c in got):
                    break

        asyncio.run(main())
        assert any(c == ": ping\n\n" for c in got), f"got={got}"

    def test_stream_stays_open_without_events(self):
        """无事件时不立即结束连接（挂起等待，供心跳/事件唤醒）。"""
        from botler.api.events import _event_stream
        bus = AppEventBus()
        req = FakeRequest()

        async def main():
            # 0.3s 内不应结束（无事件、未到心跳间隔）
            async def collect():
                async for _chunk in _event_stream(bus, req):
                    break
            try:
                await asyncio.wait_for(collect(), timeout=0.3)
            except asyncio.TimeoutError:
                return True
            return False

        assert asyncio.run(main()) is True

    def test_stream_unsubscribes_on_close(self):
        """生成器关闭（客户端断开）后在 finally 退订，不泄漏订阅。"""
        from botler.api.events import _event_stream
        bus = AppEventBus()
        req = FakeRequest()

        async def main():
            agen = _event_stream(bus, req)
            # 后台推进生成器进入订阅循环
            task = asyncio.create_task(agen.__anext__())
            await asyncio.sleep(0.1)
            assert len(bus._subs) == 1  # 已订阅
            # 取消生成器任务（模拟客户端断开）→ await 内部抛
            # CancelledError，finally 退订后结束
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            await asyncio.sleep(0.05)
            return len(bus._subs) == 0

        assert asyncio.run(main()) is True

    def test_route_registered(self, app):
        """/api/events 路由已挂载（GET）。"""
        paths = app.openapi().get("paths", {})
        assert "/api/events" in paths


# ---- 发布点集成 ----

class TestPublishPoints:
    def test_create_task_publishes_task_event(self, tmp_path):
        """任务创建（入队）→ task 事件。"""
        q = global_bus.subscribe()
        db = Database(str(tmp_path / "t.db"), event_publisher=global_bus.publish)
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/g/demo.git")
        db.create_task(repo_id, 42, 7, "标题", triggered_by="webhook")
        assert q.get_nowait()["type"] == "task"

    def test_set_task_status_publishes_only_on_status_change(self, tmp_path):
        """状态变化发布 task 事件；仅更新附加字段（status=None）不发布。"""
        q = global_bus.subscribe()
        db = Database(str(tmp_path / "t.db"), event_publisher=global_bus.publish)
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/g/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "标题", triggered_by="webhook")
        assert q.get_nowait()["type"] == "task"

        db.set_task_status(task_id, "running")
        assert q.get_nowait()["type"] == "task"

        # 仅更新附加字段：不发布（任务列表无可见变化）
        db.set_task_status(task_id, None, log_path="/tmp/x.log")
        with pytest.raises(queue.Empty):
            q.get_nowait()

        # 状态再次变化：发布
        db.set_task_status(task_id, "succeeded")
        assert q.get_nowait()["type"] == "task"

    def test_claim_and_finish_publish(self, tmp_path):
        q = global_bus.subscribe()
        db = Database(str(tmp_path / "t.db"), event_publisher=global_bus.publish)
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/g/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "标题", triggered_by="webhook")
        q.get_nowait()  # 消费 create 事件
        assert db.claim_task(task_id) is True
        assert q.get_nowait()["type"] == "task"
        assert db.finish_task(task_id, "succeeded") is True
        assert q.get_nowait()["type"] == "task"

    def test_stop_active_tasks_publishes(self, tmp_path):
        q = global_bus.subscribe()
        db = Database(str(tmp_path / "t.db"), event_publisher=global_bus.publish)
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/g/demo.git")
        db.create_task(repo_id, 42, 7, "标题", triggered_by="webhook")
        q.get_nowait()
        stopped = db.stop_active_tasks()
        assert stopped
        assert q.get_nowait()["type"] == "task"

    def test_issue_cache_clear_publishes_issue_event(self):
        """开放 issue 数据变化（写操作清缓存）→ issue 事件。"""
        q = global_bus.subscribe()
        from botler.api import issues as issues_api
        issues_api.clear_issue_cache()
        assert q.get_nowait()["type"] == "issue"

    def test_inspiration_write_publishes(self, app):
        """灵感增删改 API → inspiration 事件（issue #486：创建后删除，不残留）。"""
        q = global_bus.subscribe()
        c = TestClient(app)
        ctx = app.state.ctx
        repo_id = ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/g/demo.git")
        # 创建灵感（repo_id 用 upsert 返回的真实 id，不再依赖环境已有仓库）
        resp = c.post("/api/inspirations",
                      json={"repo_id": repo_id, "content": "测试灵感"})
        assert resp.status_code == 201, resp.text
        ev = q.get(timeout=1)
        assert ev["type"] == "inspiration"
        insp_id = resp.json()["id"]
        # 删除灵感 → 再次广播 inspiration 事件，且数据不残留
        r = c.delete(f"/api/inspirations/{insp_id}")
        assert r.status_code == 204, r.text
        assert q.get(timeout=1)["type"] == "inspiration"
        assert ctx.db.get_inspiration(insp_id) is None

    def test_delete_inspiration_publishes_event(self, app):
        """删除灵感：204 + inspiration 事件 + 数据库不再存在（issue #486）。"""
        q = global_bus.subscribe()
        c = TestClient(app)
        ctx = app.state.ctx
        repo_id = ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/g/demo.git")
        insp_id = ctx.db.create_inspiration(repo_id, "测试灵感")

        r = c.delete(f"/api/inspirations/{insp_id}")

        assert r.status_code == 204, r.text
        assert q.get(timeout=1)["type"] == "inspiration"
        assert ctx.db.get_inspiration(insp_id) is None

    def test_delete_inspiration_not_found(self, app):
        """删除不存在的灵感返回 404（不发布事件）。"""
        c = TestClient(app)
        r = c.delete("/api/inspirations/999999")
        assert r.status_code == 404, r.text

    def test_delete_repo_removes_demo_data(self, app, monkeypatch):
        """删除仓库：200 + 软删除标记 + 列表不再出现（issue #486）。

        测试内新增的 demo 仓库删除后不残留；unregister_webhook 打桩
        避免对真实 GitLab 发起网络请求。
        """
        c = TestClient(app)
        ctx = app.state.ctx
        # 注销 webhook 打桩（不访问真实 GitLab）
        monkeypatch.setattr(
            GitLabClient, "unregister_webhook",
            lambda self, project_id: {"id": 1})
        repo_id = ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/g/demo.git")

        r = c.delete(f"/api/repos/{repo_id}")

        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
        row = ctx.db.get_repo(repo_id)
        assert row is not None
        assert not row["enabled"]
        assert row["deleted_at"] is not None
        # 列表不再返回（list_repos 默认过滤软删除行）
        listing = c.get("/api/repos").json()
        ids = [x["id"] for x in listing["repos"]]
        assert repo_id not in ids

    def test_delete_repo_not_found(self, app):
        """删除不存在的仓库返回 404。"""
        c = TestClient(app)
        r = c.delete("/api/repos/999999")
        assert r.status_code == 404, r.text

    def test_settings_save_publishes(self, app):
        """设置保存 → settings 事件。"""
        q = global_bus.subscribe()
        c = TestClient(app)
        resp = c.put("/api/settings", json={"worker": {"maintenance_mode": True}})
        assert resp.status_code == 200
        ev = q.get(timeout=1)
        assert ev["type"] == "settings"
