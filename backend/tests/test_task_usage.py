"""任务 token 用量采集与费用统计测试（issue #235）。

覆盖：
1. 数据层：save_task_usage 覆盖语义（同任务重试以最后一次为准）、
   get_task_usage / get_task_usage_map、usage_stats 聚合
   （summary / by_repo / by_engine / by_date + repo/engine/时间段过滤）；
2. usage 模块：claude result 行解析（stream-json 与单行 result 同构、
   缓存 token 计入 prompt、modelUsage 模型名）、dsh usage chunk 聚合
   （多次模型调用累加）、费用估算（单价精确/子串匹配、无单价返回 None、
   引擎自带费用优先于单价估算）；
3. executor 侧：_persist_engine_usage 落库 + 日志、
   _persist_claude_usage 从输出解析；
4. API：任务详情/列表（include_usage）附用量字段（无数据为 null 不报错）、
   GET /api/usage/stats 聚合接口。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer
from botler.usage import (
    estimate_cost, extract_dsh_usage, finalize_usage,
    parse_claude_result_usage,
)

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


@pytest.fixture
def executor(tmp_path):
    """最小 ClaudeExecutor：临时 config + db + 假 gitlab 客户端。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "exec.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token",
                          verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


@pytest.fixture
def api_app(tmp_path):
    """最小测试 app：只挂 api 路由，ctx 用临时 config + db（无 gitlab 依赖）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, tmp_path


@pytest.fixture
def client(api_app):
    app, db, tmp_path = api_app
    return TestClient(app), db


def _mk_repo(db, project_id: int = 42, name: str = "demo") -> int:
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1,
             title: str = "修复登录问题") -> int:
    return db.create_task(repo_id, 42, issue_iid, title)


# ---- usage 模块：claude result 行解析 ----

class TestParseClaudeResultUsage:
    def test_stream_json_result_line(self):
        """stream-json 尾部 result 事件：缓存 token 计入 prompt，SDK 费用优先。"""
        data = {
            "type": "result", "subtype": "success",
            "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20,
                      "cache_read_input_tokens": 50, "output_tokens": 30},
            "total_cost_usd": 0.5,
            "modelUsage": {"deepseek-v4-flash[1m]": {
                "canonicalModel": "deepseek-v4-flash[1m]", "costUSD": 0.5}},
        }
        u = parse_claude_result_usage(data)
        assert u is not None
        assert u["prompt_tokens"] == 170          # 100 + 20 + 50
        assert u["completion_tokens"] == 30
        assert u["total_tokens"] == 200
        assert u["model"] == "deepseek-v4-flash[1m]"
        assert u["sdk_cost"] == 0.5

    def test_single_line_json_result(self):
        """--output-format json 单行 result（type 缺省也可识别，兼容旧协议）。"""
        u = parse_claude_result_usage(
            {"result": "ok", "usage": {"input_tokens": 7, "output_tokens": 3}})
        assert u is not None
        assert u["prompt_tokens"] == 7
        assert u["total_tokens"] == 10

    def test_no_usage_returns_none(self):
        """无 usage 字段（异常中断/旧协议）→ None（前端显示「无数据」）。"""
        assert parse_claude_result_usage({"type": "result", "result": "x"}) is None
        assert parse_claude_result_usage(None) is None
        assert parse_claude_result_usage({"type": "init"}) is None


# ---- usage 模块：dsh usage chunk 聚合 ----

class TestExtractDshUsage:
    def test_accumulates_multiple_calls(self):
        """多次模型调用（多回合/工具循环）逐事件累加。"""
        events = [
            {"type": "assistant/chunk", "data": {"chunk": {
                "type": "usage", "usage": {"prompt_tokens": 200,
                                           "completion_tokens": 100,
                                           "total_tokens": 300}}}},
            {"type": "assistant/chunk", "data": {"chunk": {
                "type": "usage", "usage": {"prompt_tokens": 50,
                                           "completion_tokens": 20,
                                           "total_tokens": 70}}}},
            {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
        ]
        u = extract_dsh_usage(events)
        assert u is not None
        assert u["prompt_tokens"] == 250
        assert u["completion_tokens"] == 120
        assert u["total_tokens"] == 370

    def test_total_missing_falls_back(self):
        """usage 缺 total_tokens 时按 prompt + completion 兜底。"""
        events = [{"type": "assistant/chunk", "data": {"chunk": {
            "type": "usage", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}}}]
        u = extract_dsh_usage(events)
        assert u["total_tokens"] == 5

    def test_no_usage_chunk_returns_none(self):
        assert extract_dsh_usage([]) is None
        assert extract_dsh_usage([{"type": "turn/end"}]) is None
        assert extract_dsh_usage(None) is None


# ---- usage 模块：费用估算 ----

class TestEstimateCost:
    PRICING = [
        {"model": "deepseek-v4-flash", "input_per_million": 0.2,
         "output_per_million": 0.8},
        {"model": "deepseek", "input_per_million": 0.1,
         "output_per_million": 0.5},
    ]

    def test_exact_match_wins(self):
        """精确匹配优先于子串匹配（deepseek-v4-flash 不应被 deepseek 先命中）。"""
        cost, cur = estimate_cost("deepseek-v4-flash", 1_000_000, 500_000,
                                  self.PRICING, "USD")
        assert cur == "USD"
        assert abs(cost - (0.2 + 0.4)) < 1e-9

    def test_substring_match(self):
        cost, _ = estimate_cost("deepseek-v4-flash[1m]", 1_000_000, 0,
                                self.PRICING, "USD")
        assert abs(cost - 0.2) < 1e-9

    def test_no_pricing_returns_none(self):
        """无单价 → None（任务详情只展示 token 数）。"""
        assert estimate_cost("claude-x", 100, 100, [], "USD") is None
        assert estimate_cost("claude-x", 100, 100, self.PRICING, "USD") is None


class TestFinalizeUsage:
    def test_sdk_cost_priority(self):
        """引擎自带费用（claude total_cost_usd）优先于单价估算。"""
        usage = {"prompt_tokens": 170, "completion_tokens": 30,
                 "total_tokens": 200, "sdk_cost": 0.5, "raw_usage": {}}
        rec = finalize_usage("claude", usage, model="m",
                             pricing=[{"model": "m", "input_per_million": 3,
                                       "output_per_million": 15}],
                             currency="USD")
        assert rec["estimated_cost"] == 0.5

    def test_pricing_estimate_when_no_sdk_cost(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000,
                 "sdk_cost": None, "raw_usage": {}}
        rec = finalize_usage("dsh", usage, model="deepseek-v4-flash",
                             pricing=[{"model": "deepseek-v4-flash",
                                       "input_per_million": 0.2,
                                       "output_per_million": 0.8}],
                             currency="USD")
        assert rec["estimated_cost"] is not None
        assert abs(rec["estimated_cost"] - 0.6) < 1e-6

    def test_no_price_keeps_none_cost(self):
        """无单价时 estimated_cost 为 None（只展示 token 数）。"""
        usage = {"prompt_tokens": 10, "completion_tokens": 5,
                 "total_tokens": 15, "sdk_cost": None, "raw_usage": {}}
        rec = finalize_usage("hermes", usage, model="m", pricing=[], currency="USD")
        assert rec["estimated_cost"] is None
        assert rec["total_tokens"] == 15

    def test_none_usage_returns_none(self):
        assert finalize_usage("claude", None) is None


# ---- 数据层 ----

class TestDatabaseTaskUsage:
    def test_save_get_map(self, tmp_path):
        db = Database(str(tmp_path / "u.db"))
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        db.save_task_usage(tid, engine="claude", model="m",
                           prompt_tokens=10, completion_tokens=5,
                           total_tokens=15, estimated_cost=0.01,
                           currency="USD", raw_usage="{}")
        row = db.get_task_usage(tid)
        assert row is not None
        assert row["engine"] == "claude" and row["total_tokens"] == 15
        assert dict(db.get_task_usage_map([tid]))[tid]["prompt_tokens"] == 10
        assert db.get_task_usage(9999) is None
        assert db.get_task_usage_map([]) == {}

    def test_overwrite_on_retry(self, tmp_path):
        """重试以最后一次执行为准（同任务覆盖，与 tasks.engine 语义一致）。"""
        db = Database(str(tmp_path / "u.db"))
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        db.save_task_usage(tid, engine="claude", model="m",
                           prompt_tokens=10, completion_tokens=5,
                           total_tokens=15, estimated_cost=0.01)
        db.save_task_usage(tid, engine="dsh", model="n",
                           prompt_tokens=30, completion_tokens=10,
                           total_tokens=40, estimated_cost=None)
        row = db.get_task_usage(tid)
        assert row["engine"] == "dsh"
        assert row["total_tokens"] == 40
        assert row["estimated_cost"] is None

    def test_usage_stats_aggregation(self, tmp_path):
        db = Database(str(tmp_path / "u.db"))
        rid = _mk_repo(db)
        tid1 = _mk_task(db, rid, issue_iid=1)
        tid2 = _mk_task(db, rid, issue_iid=2)
        db.save_task_usage(tid1, engine="claude", model="m",
                           prompt_tokens=1000, completion_tokens=500,
                           total_tokens=1500, estimated_cost=0.01)
        db.save_task_usage(tid2, engine="dsh", model="n",
                           prompt_tokens=2000, completion_tokens=1000,
                           total_tokens=3000, estimated_cost=0.02)
        st = db.usage_stats()
        assert st["summary"]["task_count"] == 2
        assert st["summary"]["prompt_tokens"] == 3000
        assert st["summary"]["total_tokens"] == 4500
        assert abs(st["summary"]["estimated_cost"] - 0.03) < 1e-9
        assert len(st["by_engine"]) == 2
        engines = {e["engine"]: e for e in st["by_engine"]}
        assert engines["claude"]["total_tokens"] == 1500
        assert st["by_repo"][0]["repo_name"] == "demo"
        assert st["by_date"][0]["task_count"] == 2

    def test_usage_stats_filters(self, tmp_path):
        db = Database(str(tmp_path / "u.db"))
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        db.save_task_usage(tid, engine="claude", model="m",
                           prompt_tokens=100, completion_tokens=50,
                           total_tokens=150)
        # repo + engine + 时间段过滤
        st = db.usage_stats(repo_id=rid, engine="claude",
                            since="2026-08-18", until="2026-08-18")
        assert st["summary"]["task_count"] == 1
        # 不匹配引擎 → 空
        assert db.usage_stats(engine="hermes")["summary"]["task_count"] == 0
        assert db.usage_stats()["summary"]["task_count"] == 1
        assert db.usage_stats(engine="bad")["by_engine"] == []


# ---- executor 侧 ----

class TestExecutorPersistUsage:
    def test_persist_engine_usage_writes_db_and_log(self, executor, tmp_path):
        db = executor.db
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        usage = {"prompt_tokens": 100, "completion_tokens": 50,
                 "total_tokens": 150, "sdk_cost": None, "raw_usage": {"a": 1}}
        executor._persist_engine_usage(tid, "dsh", usage, model="deepseek-v4-flash")
        row = db.get_task_usage(tid)
        assert row is not None
        assert row["engine"] == "dsh"
        assert row["model"] == "deepseek-v4-flash"
        assert row["prompt_tokens"] == 100
        assert row["estimated_cost"] is None  # 无单价
        logs = [l["message"] for l in db.list_logs(tid)]
        assert any("token 用量已记录" in m for m in logs)

    def test_persist_none_usage_no_write(self, executor):
        db = executor.db
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        executor._persist_engine_usage(tid, "claude", None)
        assert db.get_task_usage(tid) is None

    def test_persist_claude_usage_from_output(self, executor):
        db = executor.db
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        output = json.dumps({
            "type": "result",
            "usage": {"input_tokens": 10, "output_tokens": 3},
            "total_cost_usd": 0.01,
            "modelUsage": {"m": {"canonicalModel": "claude-x"}}})
        executor._persist_claude_usage(tid, output)
        row = db.get_task_usage(tid)
        assert row is not None
        assert row["prompt_tokens"] == 10
        assert row["total_tokens"] == 13
        assert abs(row["estimated_cost"] - 0.01) < 1e-9

    def test_persist_claude_usage_no_result_line(self, executor):
        db = executor.db
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        executor._persist_claude_usage(tid, "非 JSON 输出")
        assert db.get_task_usage(tid) is None


# ---- API ----

class TestTaskUsageAPI:
    def test_task_detail_includes_usage(self, client):
        api_client, db = client
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        db.save_task_usage(tid, engine="claude", model="m",
                           prompt_tokens=100, completion_tokens=50,
                           total_tokens=150, estimated_cost=0.01,
                           currency="USD", raw_usage='{"input_tokens":100}')
        r = api_client.get(f"/api/tasks/{tid}")
        assert r.status_code == 200
        usage = r.json()["usage"]
        assert usage["engine"] == "claude"
        assert usage["total_tokens"] == 150
        assert usage["estimated_cost"] == 0.01
        assert usage["raw_usage"] == {"input_tokens": 100}

    def test_task_detail_no_usage_is_null(self, client):
        """无用量数据 → usage 为 null（前端显示「无数据」而不是报错）。"""
        api_client, db = client
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        r = api_client.get(f"/api/tasks/{tid}")
        assert r.status_code == 200
        assert r.json()["usage"] is None

    def test_task_list_include_usage(self, client):
        api_client, db = client
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        db.save_task_usage(tid, engine="dsh", model="n",
                           prompt_tokens=10, completion_tokens=5,
                           total_tokens=15, estimated_cost=None)
        # 默认不返回（避免列表 N+1 开销）
        r = api_client.get("/api/tasks")
        assert r.json()["tasks"][0]["usage"] is None
        # include_usage=1 时批量返回
        r = api_client.get("/api/tasks?include_usage=1")
        tasks = {t["id"]: t for t in r.json()["tasks"]}
        assert tasks[tid]["usage"]["total_tokens"] == 15

    def test_usage_stats_endpoint(self, client):
        api_client, db = client
        rid = _mk_repo(db)
        tid = _mk_task(db, rid)
        db.save_task_usage(tid, engine="claude", model="m",
                           prompt_tokens=1000, completion_tokens=500,
                           total_tokens=1500, estimated_cost=0.01)
        r = api_client.get("/api/usage/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["task_count"] == 1
        assert body["by_engine"][0]["engine"] == "claude"
        assert body["by_repo"][0]["repo_name"] == "demo"
        # 过滤 + 非法日期 400
        assert api_client.get("/api/usage/stats?engine=hermes").json()["summary"]["task_count"] == 0
        assert api_client.get("/api/usage/stats?since=bad").status_code == 400
