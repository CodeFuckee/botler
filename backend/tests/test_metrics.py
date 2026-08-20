"""Prometheus 指标端点测试（issue #208）。

需求：平台只有 /api/health 与日志，无结构化运行指标。验收标准：
- /metrics 输出有效 Prometheus 文本格式；
- 关键指标随任务运行更新（任务状态计数 / 执行时长 histogram /
  webhook 接收计数 / GitLab API 调用与错误计数 / 队列深度 / 磁盘与
  DB 大小 gauge）；
- 测试覆盖。

本文件覆盖：
  1. /metrics 路由：200 + text/plain 内容类型 + 文本可被标准解析器解析；
  2. render_metrics 输出：全部预期指标族存在、数值正确；
  3. 任务状态 gauge：随 tasks 表状态实时更新，缺失状态补 0；
  4. 执行时长 histogram：桶累计 / sum / count 正确，非法时间戳与
     未结束任务排除，空库不报错；
  5. webhook 接收计数：error（校验失败）/ rejected / accepted 分类；
  6. GitLab API 计数：请求计数、HTTP 错误计数、传输层错误计数；
  7. 队列深度 / 磁盘 / DB 大小 gauge；
  8. reset_for_tests 隔离（计数起点独立，不跨用例残留）。
"""

import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

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

# ---- botler.main 集成测试：模块级导入前设置临时 config/db 环境 ----
# 必须早于任何 botler 模块导入：botler.database 的 DB_PATH 在模块导入时
# 即求值（os.environ.get("BOTLER_DB", 默认路径)），若先导入 botler 模块
# 再设置环境变量，DB_PATH 会绑定真实库（backend/botler.db），CI 上 xdist
# 多 worker 并发对该库执行 _migrate 的 ALTER TABLE ADD COLUMN 会触发
# "sqlite3.OperationalError: duplicate column name: precheck_result"
# （issue #395 修复触发的流水线 #1289 即因此失败）。
_MODULE_TMP = tempfile.mkdtemp(prefix="botler-metrics-")
os.environ["BOTLER_CONFIG"] = os.path.join(_MODULE_TMP, "config.yaml")
os.environ["BOTLER_DB"] = os.path.join(_MODULE_TMP, "botler.db")
with open(os.environ["BOTLER_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write(CONFIG_TEXT)

from botler import metrics
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabClient, GitLabError
from botler.metrics import DURATION_BUCKETS, render_metrics, reset_for_tests
from botler.webhook import WebhookHandler, WebhookError
from botler.main import app as main_app  # noqa: E402

del os.environ["BOTLER_CONFIG"]
del os.environ["BOTLER_DB"]


# ---- 工具函数 ----

def _parse_value(text: str, name: str, labels: dict | None = None) -> float | None:
    """解析 Prometheus 文本，返回指定序列（名称 + 标签）的数值；不存在返回 None。

    直方图桶名带 _bucket/_sum/_count 后缀，按完整 series 名精确匹配。
    """
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and (labels is None or sample.labels == labels):
                return sample.value
    return None


def _parse_all(text: str) -> set[str]:
    """返回文本中出现的全部指标族名 + series 名（校验格式有效：解析不抛错）。

    计数器未自增时只有 HELP/TYPE 声明（无样本），family 名仍存在；
    直方图额外带 _bucket/_sum/_count 后缀样本。
    """
    names = set()
    for family in text_string_to_metric_families(text):
        names.add(family.name)
        names.update(s.name for s in family.samples)
    return names


def _zero(text: str, name: str, labels: dict | None = None) -> float:
    """读取指标值，series 不存在（Prometheus 对 0 值计数器不发射样本）视为 0。"""
    value = _parse_value(text, name, labels)
    return 0.0 if value is None else value


def _mk_repo(db: Database, project_id: int = 42, name: str = "demo") -> int:
    db.upsert_repo(project_id, name, "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db: Database, status: str, started: str, finished: str,
             repo_id: int | None = None, issue_iid: int | None = None) -> int:
    """创建任务并置为指定状态（含 started_at / finished_at 时间串，UTC）。"""
    rid = repo_id if repo_id is not None else _mk_repo(db)
    iid = issue_iid if issue_iid is not None else _next_iid(db, rid)
    task_id = db.create_task(rid, 42, iid, "指标测试任务", triggered_by="test")
    db.set_task_status(task_id, status, started_at=started, finished_at=finished)
    return task_id


def _next_iid(db: Database, repo_id: int) -> int:
    """自增 issue_iid（避免部分唯一索引冲突）。"""
    rows = db.list_tasks(repo_id=repo_id)
    return max([r["issue_iid"] for r in rows], default=0) + 1


@pytest.fixture(autouse=True)
def _reset_metrics():
    """每个用例独立计数起点（webhook/GitLab 计数器不跨用例残留）。"""
    reset_for_tests()
    yield


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def fake_scheduler():
    class FakeScheduler:
        def __init__(self):
            self._stats = {"running": 0, "queued": 0}

        def stats(self):
            return dict(self._stats)

        def set_stats(self, running=0, queued=0):
            self._stats = {"running": running, "queued": queued}

    return FakeScheduler()


@pytest.fixture
def ctx(db, fake_scheduler):
    """render_metrics 的最小上下文（db + scheduler，与生产 AppContext 接口一致）。"""
    return SimpleNamespace(db=db, scheduler=fake_scheduler)


# ---- 1. 文本格式有效性 ----

class TestMetricsTextFormat:
    def test_endpoint_returns_valid_prometheus(self):
        """GET /metrics：200 + text/plain，全文可被标准解析器解析（格式有效）。"""
        client = TestClient(main_app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        names = _parse_all(resp.text)
        assert names, "metrics 文本应为空或可解析的 Prometheus 格式"

    def test_render_contains_expected_families(self, ctx):
        """输出包含全部预期指标族（验收标准「/metrics 输出有效格式」）。

        计数器（webhook / GitLab API）在发生事件后才发射样本与族声明，
        先各触发一次，再断言全量指标族存在。
        """
        metrics.inc_webhook_received("accepted")
        metrics.inc_gitlab_api_request("GET")
        metrics.inc_gitlab_api_error("GET")
        text = render_metrics(ctx)
        expected = {
            # 指标族（HELP/TYPE 声明；counter 未自增时无样本但族存在）
            "botler_webhook_received_total",
            "botler_gitlab_api_requests_total",
            "botler_gitlab_api_errors_total",
            "botler_task_state",
            "botler_queue_depth",
            "botler_running_tasks",
            "botler_disk_free_bytes",
            "botler_disk_total_bytes",
            "botler_db_size_bytes",
            "botler_task_duration_seconds",
            # 直方图样本（含 _bucket/_sum/_count 后缀）
            "botler_task_duration_seconds_bucket",
            "botler_task_duration_seconds_sum",
            "botler_task_duration_seconds_count",
        }
        names = _parse_all(text)
        assert expected <= names, f"缺失指标族: {expected - names}"


# ---- 2. 任务状态计数 gauge ----

class TestTaskStateGauge:
    def test_task_state_reflects_db_counts(self, db, ctx):
        """状态计数随 tasks 表实时更新：同状态多条累加，缺失状态补 0。"""
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 10:01:00")
        _mk_task(db, "succeeded", "2026-08-18 11:00:00", "2026-08-18 11:02:00")
        _mk_task(db, "failed", "2026-08-18 12:00:00", "2026-08-18 12:00:30")
        _mk_task(db, "queued", "", "")

        text = render_metrics(ctx)
        assert _parse_value(text, "botler_task_state", {"status": "succeeded"}) == 2.0
        assert _parse_value(text, "botler_task_state", {"status": "failed"}) == 1.0
        assert _parse_value(text, "botler_task_state", {"status": "queued"}) == 1.0
        # 库中不存在但属于状态全集的状态 → 补 0（避免残留标签值）
        for status in ("running", "retrying", "interrupted", "canceled_by_user"):
            assert _zero(text, "botler_task_state", {"status": status}) == 0.0

    def test_task_state_empty_db_all_zero(self, ctx):
        """空库：全部状态计数为 0，不报错。"""
        text = render_metrics(ctx)
        for status in metrics.TASK_STATUSES:
            assert _zero(text, "botler_task_state", {"status": status}) == 0.0


# ---- 3. 队列深度 / 运行中 gauge ----

class TestQueueDepthGauge:
    def test_queue_depth_and_running_reflect_scheduler(self, db, fake_scheduler, ctx):
        fake_scheduler.set_stats(running=2, queued=5)
        text = render_metrics(ctx)
        assert _parse_value(text, "botler_queue_depth") == 5.0
        assert _parse_value(text, "botler_running_tasks") == 2.0

    def test_queue_zero_when_empty(self, db, fake_scheduler, ctx):
        text = render_metrics(ctx)
        assert _zero(text, "botler_queue_depth") == 0.0
        assert _zero(text, "botler_running_tasks") == 0.0


# ---- 4. 执行时长 histogram ----

class TestDurationHistogram:
    def test_histogram_buckets_sum_count(self, db, ctx):
        """桶累计计数 / sum / count 与库中任务时长一致（started_at→finished_at）。"""
        # 30s、120s、600s、7200s（跨 1h 桶）
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 10:00:30")
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 10:02:00")
        _mk_task(db, "failed", "2026-08-18 10:00:00", "2026-08-18 10:10:00")
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 12:00:00")

        text = render_metrics(ctx)
        assert _parse_value(text, "botler_task_duration_seconds_count") == 4.0
        assert _parse_value(
            text, "botler_task_duration_seconds_sum") == pytest.approx(30 + 120 + 600 + 7200)
        # 桶（累计）：<60=1（30s），<300=2（+120s），<900=3（+600s），
        # <1800=3，<3600=3，<7200=3，<21600=4（+7200s），+Inf=count=4
        expected_buckets = [1, 2, 3, 3, 3, 3, 4, 4]
        for i, le in enumerate([*DURATION_BUCKETS, float("inf")]):
            le_label = "+Inf" if le == float("inf") else str(le)
            got = _parse_value(
                text, "botler_task_duration_seconds_bucket", {"le": le_label})
            assert got == expected_buckets[i], f"le={le_label} 桶计数错误: {got}"

    def test_histogram_empty_db(self, ctx):
        """空库：count=0 / sum=0 / 各桶=0，不报错。"""
        text = render_metrics(ctx)
        assert _parse_value(text, "botler_task_duration_seconds_count") == 0.0
        assert _parse_value(text, "botler_task_duration_seconds_sum") == 0.0
        for le in [str(b) for b in DURATION_BUCKETS] + ["+Inf"]:
            assert _parse_value(
                text, "botler_task_duration_seconds_bucket", {"le": le}) == 0.0

    def test_histogram_excludes_invalid_timestamps(self, db, ctx):
        """非法时间串（julianday 解析失败）与负时长（时钟回拨）不参与统计。"""
        _mk_task(db, "succeeded", "not-a-time", "2026-08-18 10:01:00")  # 非法起始
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "garbage")     # 非法结束
        _mk_task(db, "failed", "2026-08-18 12:00:00", "2026-08-18 11:00:00")  # 负时长
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 10:01:00")  # 有效 60s
        text = render_metrics(ctx)
        assert _parse_value(text, "botler_task_duration_seconds_count") == 1.0
        assert _parse_value(
            text, "botler_task_duration_seconds_sum") == pytest.approx(60.0)

    def test_histogram_excludes_unfinished_tasks(self, db, ctx):
        """未结束任务（无 finished_at，如 queued/running）不计入时长。"""
        task_id = _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 10:01:00")
        _mk_task(db, "queued", "2026-08-18 10:00:00", "")   # 排队中无 finished_at
        rid = db.get_task(task_id)["repo_id"]
        running_id = db.create_task(rid, 42, _next_iid(db, rid), "运行中", triggered_by="test")
        db.set_task_status(running_id, "running",
                           started_at="2026-08-18 10:00:00", finished_at=None)
        text = render_metrics(ctx)
        assert _parse_value(text, "botler_task_duration_seconds_count") == 1.0


# ---- 5. webhook 接收计数 ----

class TestWebhookCounter:
    @pytest.fixture
    def webhook_ctx(self, tmp_path):
        """WebhookHandler 最小上下文（配置含 secret，gitlab 用桩）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))

        class StubGitLab:
            def get_bot_id(self):
                return 99

            def get_issue(self, project_id, iid):
                return {"iid": iid, "state": "opened", "labels": [],
                        "assignees": [{"id": 99}]}

            def last_note_author_id(self, project_id, iid):
                return None

        scheduler = SimpleNamespace(enqueue=lambda task_id: None)
        handler = WebhookHandler(config, db, StubGitLab(), scheduler)
        return SimpleNamespace(config=config, db=db, handler=handler)

    def test_error_counted_on_secret_mismatch(self, webhook_ctx, ctx):
        """secret 校验失败 → error 计数 +1（WebhookError 抛出）。"""
        with pytest.raises(WebhookError):
            webhook_ctx.handler.handle(
                {"object_kind": "issue"}, header_token="wrong-secret")
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_webhook_received_total", {"result": "error"}) == 1.0
        assert _zero(
            text, "botler_webhook_received_total", {"result": "accepted"}) == 0.0
        assert _zero(
            text, "botler_webhook_received_total", {"result": "rejected"}) == 0.0

    def test_rejected_counted_on_non_issue_event(self, webhook_ctx, ctx):
        """非 issue 事件 → rejected 计数 +1（accepted=False 返回，不抛错）。"""
        result = webhook_ctx.handler.handle(
            {"object_kind": "pipeline", "object_attributes": {}},
            header_token="test-secret")
        assert result["accepted"] is False
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_webhook_received_total", {"result": "rejected"}) == 1.0

    def test_accepted_counted_on_enqueue(self, webhook_ctx, ctx):
        """事件入队成功 → accepted 计数 +1。"""
        webhook_ctx.db.upsert_repo(42, "demo", "https://gitlab.example.com/demo.git")
        event = {
            "object_kind": "issue",
            "project": {"id": 42},
            "object_attributes": {"action": "open", "iid": 7, "title": "测试"},
            "issue": {"title": "测试", "assignees": [{"id": 99}]},
        }
        result = webhook_ctx.handler.handle(event, header_token="test-secret")
        assert result["accepted"] is True
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_webhook_received_total", {"result": "accepted"}) == 1.0
        assert _zero(
            text, "botler_webhook_received_total", {"result": "error"}) == 0.0


# ---- 6. GitLab API 调用 / 错误计数 ----

class TestGitlabApiCounters:
    def _client_with_transport(self, handler) -> GitLabClient:
        client = GitLabClient(
            "https://gitlab.example.com", "test-token", verify_ssl=False,
            retry_max_attempts=1)  # 单次尝试：错误计数每次调用恰好 +1
        client._http = httpx.Client(
            base_url=client._http.base_url,
            headers=client._http.headers,
            transport=httpx.MockTransport(handler),
        )
        return client

    def test_success_counts_request_only(self, ctx):
        def handler(request):
            return httpx.Response(200, json={"state": "opened", "iid": 7})

        client = self._client_with_transport(handler)
        client.get_issue(42, 7)
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_gitlab_api_requests_total", {"method": "GET"}) == 1.0
        assert _zero(
            text, "botler_gitlab_api_errors_total", {"method": "GET"}) == 0.0

    def test_http_error_counts_request_and_error(self, ctx):
        def handler(request):
            return httpx.Response(500, json={})

        client = self._client_with_transport(handler)
        with pytest.raises(GitLabError):
            client.get_issue(42, 7)
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_gitlab_api_requests_total", {"method": "GET"}) == 1.0
        assert _parse_value(
            text, "botler_gitlab_api_errors_total", {"method": "GET"}) == 1.0

    def test_transport_error_counts_error(self, ctx):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        client = self._client_with_transport(handler)
        with pytest.raises(GitLabError):
            client.get_issue(42, 7)
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_gitlab_api_requests_total", {"method": "GET"}) == 1.0
        assert _parse_value(
            text, "botler_gitlab_api_errors_total", {"method": "GET"}) == 1.0

    def test_paged_404_break_not_counted_as_error(self, ctx):
        """分页 404（既有语义视为数据结束）不计入错误，但计入请求。"""
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(404, json={})

        client = self._client_with_transport(handler)
        client._paged("/projects/42/issues")
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_gitlab_api_requests_total", {"method": "GET"}) == 1.0
        assert _zero(
            text, "botler_gitlab_api_errors_total", {"method": "GET"}) == 0.0


# ---- 7. 磁盘 / DB 大小 gauge ----

class TestSystemGauges:
    def test_db_size_gauge(self, db, ctx):
        """DB 文件存在 → db_size > 0。"""
        _mk_task(db, "succeeded", "2026-08-18 10:00:00", "2026-08-18 10:01:00")
        text = render_metrics(ctx)
        assert _parse_value(text, "botler_db_size_bytes") > 0

    def test_disk_gauges(self, ctx):
        """磁盘剩余/总空间 gauge 输出且为正。"""
        text = render_metrics(ctx)
        assert _parse_value(text, "botler_disk_free_bytes") > 0
        assert _parse_value(text, "botler_disk_total_bytes") > 0


# ---- 8. 测试隔离 ----

class TestResetForTests:
    def test_reset_clears_counters(self, ctx):
        """reset_for_tests 后计数归零（新注册表）：增量前无系列输出。"""
        metrics.inc_webhook_received("error")
        text_before = render_metrics(ctx)
        assert _parse_value(
            text_before, "botler_webhook_received_total", {"result": "error"}) == 1.0

        reset_for_tests()
        text_after = render_metrics(ctx)
        assert _parse_value(
            text_after, "botler_webhook_received_total", {"result": "error"}) is None

    def test_inc_functions_use_current_registry(self, ctx):
        """埋点函数在 reset 后仍写入新注册表（不残留旧对象）。"""
        reset_for_tests()
        metrics.inc_webhook_received("accepted")
        text = render_metrics(ctx)
        assert _parse_value(
            text, "botler_webhook_received_total", {"result": "accepted"}) == 1.0
