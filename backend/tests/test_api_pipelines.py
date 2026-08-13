"""概览页流水线状态 API 测试：GET /api/pipelines/overview（issue #39）。

遍历所有启用仓库，返回各仓库最新一次 CI/CD 流水线状态（整体状态 +
按 jobs 聚合的 stage 进度），供概览页以 GitLab CI/CD 风格展示：
- 运行完成没有（pipeline.status 是否终态）
- 运行成功还是失败
- 运行到哪个阶段（stage 状态：success/failed/running/pending/canceled）
- 还有哪些阶段（stage 列表按 .gitlab-ci.yml 顺序）

与 reconcile-all（issue #38）一致：多仓库场景下单个仓库失败不中断整体
（HTTP 200），失败明细放入 errors 列表；停用仓库跳过；无流水线仓库
pipeline 为 null。
"""

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.api.pipelines import aggregate_stages, _stage_status
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError


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


class StubGitLab:
    """流水线查询桩：latest_pipelines 按 project_id 配置，可故障注入、可计数。"""

    def __init__(self):
        self.pipelines_by_project: dict[int, dict | None] = {}
        self.jobs_by_pipeline: dict[int, list[dict]] = {}
        self.fail_projects: set[int] = set()
        self.fail_jobs_pipelines: set[int] = set()
        self.calls: list[str] = []

    def get_latest_pipeline(self, project_id):
        self.calls.append(f"pipeline:{project_id}")
        if project_id in self.fail_projects:
            raise GitLabError("模拟 GitLab API 故障")
        return self.pipelines_by_project.get(project_id)

    def list_pipeline_jobs(self, project_id, pipeline_id):
        self.calls.append(f"jobs:{project_id}:{pipeline_id}")
        if pipeline_id in self.fail_jobs_pipelines:
            raise GitLabError("模拟 jobs 查询故障")
        return self.jobs_by_pipeline.get(pipeline_id, [])


def make_pipeline(pid: int, status: str = "success", ref: str = "main",
                  sha: str = "abc123") -> dict:
    return {
        "id": pid, "iid": pid, "project_id": 42, "sha": sha, "ref": ref,
        "status": status, "source": "push",
        "web_url": f"https://gitlab.example.com/group/proj/-/pipelines/{pid}",
        "created_at": "2026-08-13T12:00:00.000+08:00",
        "updated_at": "2026-08-13T12:05:00.000+08:00",
        "finished_at": "2026-08-13T12:05:00.000+08:00",
        "duration": 60,
    }


def make_job(job_id: int, stage: str, status: str = "success",
             allow_failure: bool = False) -> dict:
    return {
        "id": job_id, "name": f"job{job_id}", "stage": stage, "status": status,
        "allow_failure": allow_failure,
        "web_url": f"https://gitlab.example.com/group/proj/-/jobs/{job_id}",
    }


@pytest.fixture
def api_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    # 每个用例之间清空模块级缓存，避免用例互相污染
    from botler.api import pipelines as pipelines_mod
    pipelines_mod.clear_pipeline_cache()
    return app, stub, db, tmp_path


@pytest.fixture
def client(api_app):
    app, stub, db, tmp_path = api_app
    return TestClient(app), stub, db, tmp_path


def _add_repo(db, project_id=42, name="demo", enabled=True) -> int:
    return db.upsert_repo(
        project_id=project_id, name=name,
        url=f"https://gitlab.example.com/{name}.git", enabled=enabled)


# ---- 纯函数：stage 状态聚合 ----

class TestStageAggregation:
    def test_all_success_stage(self):
        jobs = [make_job(1, "build"), make_job(2, "build")]
        assert _stage_status(jobs) == "success"

    def test_failed_job_makes_stage_failed(self):
        jobs = [make_job(1, "build"), make_job(2, "build", status="failed")]
        assert _stage_status(jobs) == "failed"

    def test_allow_failure_failed_job_ignored(self):
        """allow_failure 的 job 失败不算 stage 失败（GitLab passed with warnings 语义）。"""
        jobs = [make_job(1, "build"),
                make_job(2, "build", status="failed", allow_failure=True)]
        assert _stage_status(jobs) == "success"

    def test_failed_beats_running(self):
        """一个 job 失败、另一个还在跑：stage 整体 failed（GitLab 语义）。"""
        jobs = [make_job(1, "build", status="failed"),
                make_job(2, "build", status="running")]
        assert _stage_status(jobs) == "failed"

    def test_running_beats_pending(self):
        jobs = [make_job(1, "build", status="pending"),
                make_job(2, "build", status="running")]
        assert _stage_status(jobs) == "running"

    def test_pending_variants(self):
        for s in ("pending", "created", "waiting_for_resource", "preparing", "scheduled"):
            assert _stage_status([make_job(1, "build", status=s)]) == "pending", s

    def test_canceled_stage(self):
        jobs = [make_job(1, "build", status="success"),
                make_job(2, "build", status="canceled")]
        assert _stage_status(jobs) == "canceled"

    def test_manual_and_skipped_jobs_do_not_downgrade(self):
        jobs = [make_job(1, "build", status="success"),
                make_job(2, "build", status="manual"),
                make_job(3, "build", status="skipped")]
        assert _stage_status(jobs) == "success"

    def test_empty_jobs(self):
        assert _stage_status([]) == "success"
        assert aggregate_stages([]) == []

    def test_aggregate_preserves_stage_order(self):
        """stage 顺序 = jobs 中首次出现顺序（.gitlab-ci.yml 定义顺序）。"""
        jobs = [make_job(1, "build"), make_job(2, "build"),
                make_job(3, "test", status="failed"),
                make_job(4, "deploy", status="pending")]
        stages = aggregate_stages(jobs)
        assert [s["name"] for s in stages] == ["build", "test", "deploy"]
        assert [s["status"] for s in stages] == ["success", "failed", "pending"]

    def test_aggregate_skips_missing_stage_field(self):
        jobs = [make_job(1, "build"), {"id": 9, "name": "no-stage", "status": "success"}]
        stages = aggregate_stages(jobs)
        assert [s["name"] for s in stages] == ["build"]


# ---- API ----

class TestPipelinesOverview:
    def test_overview_multiple_repos_with_and_without_pipeline(self, client):
        """正常路径：有流水线仓库返回状态+stages；无流水线仓库 pipeline=null。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.pipelines_by_project = {42: make_pipeline(731, status="success")}
        stub.jobs_by_pipeline = {731: [
            make_job(1, "build"), make_job(2, "test", status="running"),
            make_job(3, "deploy", status="pending")]}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert len(data["pipelines"]) == 2
        a = next(p for p in data["pipelines"] if p["repo_name"] == "a")
        b = next(p for p in data["pipelines"] if p["repo_name"] == "b")
        # 仓库 a：流水线正常
        assert a["pipeline"]["id"] == 731
        assert a["pipeline"]["status"] == "success"
        assert a["pipeline"]["ref"] == "main"
        assert a["pipeline"]["web_url"].endswith("/pipelines/731")
        assert [(s["name"], s["status"]) for s in a["stages"]] == [
            ("build", "success"), ("test", "running"), ("deploy", "pending")]
        # 仓库 b：无流水线
        assert b["pipeline"] is None
        assert b["stages"] == []

    def test_overview_partial_repo_failure(self, client):
        """部分仓库 GitLab 故障：正常仓库照常返回，失败明细进 errors（HTTP 200）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.pipelines_by_project = {42: make_pipeline(1)}
        stub.jobs_by_pipeline = {1: [make_job(1, "build")]}
        stub.fail_projects = {43}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pipelines"]) == 2
        bad = next(p for p in data["pipelines"] if p["repo_name"] == "b")
        assert bad["pipeline"] is None and bad["stages"] == []
        assert len(data["errors"]) == 1
        assert "仓库 b" in data["errors"][0]

    def test_overview_jobs_query_failure_reports_error(self, client):
        """pipeline 存在但 jobs 查询失败：该仓库进 errors，不整体失败。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.fail_jobs_pipelines = {731}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) == 1
        assert "仓库 demo" in data["errors"][0]

    def test_overview_all_repos_failed_still_200(self, client):
        """全部仓库失败：仍返回 200，errors 记录全部失败明细。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.fail_projects = {42, 43}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) == 2
        assert all(p["pipeline"] is None for p in data["pipelines"])

    def test_overview_skips_disabled_repo(self, client):
        """停用仓库不查询：结果只含启用仓库。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="on")
        _add_repo(db, project_id=43, name="off", enabled=False)

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        assert [p["repo_name"] for p in data["pipelines"]] == ["on"]
        assert data["errors"] == []
        assert "pipeline:43" not in stub.calls

    def test_overview_without_any_repo(self, client):
        """边界：没有任何仓库时返回空结果（不 500）。"""
        tc, stub, db, tmp_path = client

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        assert resp.json() == {"pipelines": [], "errors": []}

    def test_overview_cache_within_ttl(self, client):
        """10s TTL 缓存：连续两次请求只打一次 GitLab API。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(1)}
        stub.jobs_by_pipeline = {1: [make_job(1, "build")]}

        first = tc.get("/api/pipelines/overview").json()
        second = tc.get("/api/pipelines/overview").json()

        assert first == second
        # 缓存命中：GitLab 桩只被调用一轮（1 pipeline + 1 jobs）
        assert stub.calls.count("pipeline:42") == 1
        assert stub.calls.count("jobs:42:1") == 1

    def test_overview_cache_expires_after_ttl(self, client):
        """TTL 过期后重新拉取（缓存按时间失效）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(1)}
        stub.jobs_by_pipeline = {1: [make_job(1, "build")]}

        tc.get("/api/pipelines/overview")
        from botler.api import pipelines as pipelines_mod
        # 把缓存过期时间拨到过去，模拟 TTL 过期
        pipelines_mod._CACHE["expires_at"] = time.monotonic() - 1
        tc.get("/api/pipelines/overview")

        assert stub.calls.count("pipeline:42") == 2
