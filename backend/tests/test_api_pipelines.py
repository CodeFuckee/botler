"""概览页流水线状态 API 测试：GET /api/pipelines/overview（issue #39）。

遍历所有配置仓库（含未启用，issue #39 第二轮），返回各仓库最新一次
CI/CD 流水线状态（整体状态 + 按 jobs 聚合的 stage 进度），供概览页以
GitLab CI/CD 风格展示：
- 运行完成没有（pipeline.status 是否终态）
- 运行成功还是失败
- 运行到哪个阶段（stage 状态：success/failed/running/pending/canceled）
- 还有哪些阶段（stage 列表按 .gitlab-ci.yml 顺序）

与 reconcile-all（issue #38）一致：多仓库场景下单个仓库失败不中断整体
（HTTP 200），失败明细放入 errors 列表；无流水线仓库 pipeline 为 null；
每条结果带 enabled 字段供前端标注未启用仓库。
"""

import io
import time
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.api.pipelines import aggregate_stages, _stage_status, _commit_time_utc
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
        # commit 详情桩（issue #43）：按 (project_id, sha) 配置，默认 None（404 语义）
        self.commits_by_sha: dict[tuple[int, str], dict | None] = {}
        self.fail_commits: set[tuple[int, str]] = set()
        # 产物下载桩（issue #329）
        self.artifact_bytes: dict[tuple[int, int], bytes] = {}
        self.fail_artifacts: dict[int, GitLabError] = {}
        # 单文件报告下载桩（issue #337）：(project_id, job_id, path) → 字节
        self.artifact_files: dict[tuple[int, int, str], bytes] = {}
        self.fail_artifact_files: dict[int, GitLabError] = {}
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

    def get_commit(self, project_id, sha):
        self.calls.append(f"commit:{project_id}:{sha}")
        if (project_id, sha) in self.fail_commits:
            raise GitLabError("模拟 commit 查询故障")
        return self.commits_by_sha.get((project_id, sha))

    # 产物下载桩（issue #329）：按 (project_id, job_id) 返回字节；
    # fail_artifacts 按 job_id 注入 GitLabError（404 无产物 / 500 故障）
    def download_job_artifacts(self, project_id, job_id):
        self.calls.append(f"artifacts:{project_id}:{job_id}")
        err = self.fail_artifacts.get(job_id)
        if err:
            raise err
        return httpx.Response(
            200,
            content=self.artifact_bytes.get((project_id, job_id),
                                            b"PK\x03\x04test-artifacts"),
            headers={"content-type": "application/zip"})

    # 单文件报告下载桩（issue #337）：按 (project_id, job_id, path) 返回
    # 字节；fail_artifact_files 按 job_id 注入 GitLabError
    def download_job_artifact_file(self, project_id, job_id, path):
        self.calls.append(f"artifact-file:{project_id}:{job_id}:{path}")
        err = self.fail_artifact_files.get(job_id)
        if err:
            raise err
        content = self.artifact_files.get((project_id, job_id, path))
        if content is None:
            raise GitLabError("报告文件不存在（404）", 404)
        return httpx.Response(200, content=content)


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
             allow_failure: bool = False, artifacts=None) -> dict:
    job = {
        "id": job_id, "name": f"job{job_id}", "stage": stage, "status": status,
        "allow_failure": allow_failure,
        "web_url": f"https://gitlab.example.com/group/proj/-/jobs/{job_id}",
    }
    if artifacts is not None:
        job["artifacts"] = artifacts
    return job


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
        """stage 顺序 = .gitlab-ci.yml 定义顺序（job id 升序，jobs 已按序传入）。"""
        jobs = [make_job(1, "build"), make_job(2, "build"),
                make_job(3, "test", status="failed"),
                make_job(4, "deploy", status="pending")]
        stages = aggregate_stages(jobs)
        assert [s["name"] for s in stages] == ["build", "test", "deploy"]
        assert [s["status"] for s in stages] == ["success", "failed", "pending"]

    def test_aggregate_reorders_reversed_api_jobs(self):
        """GitLab jobs API 默认按 job id 倒序返回（issue #44 复现）：
        stage 顺序必须按 .gitlab-ci.yml 定义顺序（job id 升序）展示，
        而不是 API 返回顺序（sync->deploy->build 倒置）。
        """
        jobs = [make_job(4226, "sync"), make_job(4225, "deploy"),
                make_job(4224, "build"), make_job(4223, "build")]
        stages = aggregate_stages(jobs)
        assert [s["name"] for s in stages] == ["build", "deploy", "sync"]
        assert [s["status"] for s in stages] == ["success", "success", "success"]

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
        # 每条结果带 enabled 字段（供前端标注未启用仓库，issue #39 第二轮）
        assert a["enabled"] is True and b["enabled"] is True
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


    def test_overview_hides_disabled_repos_when_setting_off(self, api_app):
        """ui.show_disabled_repos=false：未启用仓库从概览结果中过滤（issue #142）。

        设置关闭后，灵感 / CI/CD 页面都不再显示未启用项目；未启用仓库不再
        发起 GitLab 查询（避免无效流量），也不进 errors 列表。
        """
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="on", enabled=True)
        _add_repo(db, project_id=43, name="off", enabled=False)
        stub.pipelines_by_project = {42: make_pipeline(731, status="success")}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}
        app.state.ctx.config.update_section("ui", {"show_disabled_repos": False})

        resp = TestClient(app).get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert [p["repo_name"] for p in data["pipelines"]] == ["on"]
        assert data["errors"] == []
        # 未启用仓库不应发起流水线查询（enabled 过滤发生在遍历之前）
        assert "pipeline:43" not in stub.calls

    def test_overview_includes_disabled_repo(self, client):
        """未启用仓库也返回并查询流水线（issue #39 第二轮），enabled 字段透传。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="on")
        _add_repo(db, project_id=43, name="off", enabled=False)

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        assert sorted(p["repo_name"] for p in data["pipelines"]) == ["off", "on"]
        assert data["errors"] == []
        off = next(p for p in data["pipelines"] if p["repo_name"] == "off")
        assert off["enabled"] is False
        assert off["pipeline"] is None and off["stages"] == []
        # 未启用仓库同样查询 GitLab（不因 enabled 跳过）
        assert "pipeline:43" in stub.calls

    def test_overview_disabled_repo_with_pipeline(self, client):
        """未启用仓库有流水线：正常展示状态与 stage 进度。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="off", enabled=False)
        stub.pipelines_by_project = {42: make_pipeline(731, status="running")}
        stub.jobs_by_pipeline = {731: [
            make_job(1, "build"), make_job(2, "test", status="running")]}

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        assert data["errors"] == []
        assert len(data["pipelines"]) == 1
        off = data["pipelines"][0]
        assert off["enabled"] is False
        assert off["pipeline"]["status"] == "running"
        assert [(s["name"], s["status"]) for s in off["stages"]] == [
            ("build", "success"), ("test", "running")]

    def test_overview_disabled_repo_query_failure(self, client):
        """未启用仓库查询失败：与启用仓库一致进 errors，不整体失败。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="off", enabled=False)
        stub.fail_projects = {42}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pipelines"]) == 1
        off = data["pipelines"][0]
        assert off["enabled"] is False
        assert off["pipeline"] is None and off["stages"] == []
        assert len(data["errors"]) == 1
        assert "仓库 off" in data["errors"][0]

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


# ---- 最近流水线对应提交的提交时间（issue #43） ----

class TestCommitTimeUtc:
    """纯函数：GitLab committed_date（ISO 8601 带时区）→ UTC 无后缀时间串。"""

    def test_offset_converted_to_utc(self):
        assert _commit_time_utc("2026-08-13T12:00:00.000+08:00") == "2026-08-13 04:00:00"

    def test_z_suffix_treated_as_utc(self):
        assert _commit_time_utc("2026-08-13T12:00:00.000Z") == "2026-08-13 12:00:00"

    def test_naive_input_treated_as_utc(self):
        """无时区后缀输入按 UTC 处理（GitLab 可能输出无后缀时间）。"""
        assert _commit_time_utc("2026-08-13T12:00:00") == "2026-08-13 12:00:00"

    def test_negative_offset_converted_to_utc(self):
        assert _commit_time_utc("2026-08-13T01:30:00.000-05:00") == "2026-08-13 06:30:00"

    def test_milliseconds_kept_truncated(self):
        assert _commit_time_utc("2026-08-13T04:00:00.999+00:00") == "2026-08-13 04:00:00"

    def test_empty_and_none(self):
        assert _commit_time_utc(None) is None
        assert _commit_time_utc("") is None

    def test_invalid_format(self):
        assert _commit_time_utc("not-a-date") is None
        assert _commit_time_utc("2026/08/13 12:00") is None


class TestOverviewCommitTime:
    """API：每条结果带 commit_time（最近流水线对应提交的提交时间，UTC 无后缀）。"""

    def test_overview_includes_commit_time(self, client):
        """正常路径：有流水线仓库返回对应提交的提交时间（转 UTC 无后缀）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}
        stub.commits_by_sha = {(42, "abc123"): {
            "id": "abc123",
            "committed_date": "2026-08-13T12:00:00.000+08:00"}}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        entry = data["pipelines"][0]
        assert entry["commit_time"] == "2026-08-13 04:00:00"
        # 提交时间按 pipeline.sha 查询
        assert "commit:42:abc123" in stub.calls

    def test_overview_commit_query_failure_silent_null(self, client):
        """commit 查询故障：静默降级为 None，不进 errors，卡片其余部分正常。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}
        stub.fail_commits = {(42, "abc123")}

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        entry = data["pipelines"][0]
        assert entry["commit_time"] is None
        assert entry["pipeline"]["id"] == 731
        assert [(s["name"], s["status"]) for s in entry["stages"]] == [("build", "success")]

    def test_overview_commit_not_found_silent_null(self, client):
        """commit 不存在（force-push 后 sha 失效，GitLab 404）：静默降级。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}
        # commits_by_sha 未配置 → get_commit 返回 None（404 语义）

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert data["pipelines"][0]["commit_time"] is None

    def test_overview_no_pipeline_skips_commit_query(self, client):
        """无流水线仓库不查 commit（避免无效 API 调用），commit_time 为 None。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        b = next(p for p in data["pipelines"] if p["repo_name"] == "b")
        assert b["commit_time"] is None
        assert not any(c.startswith("commit:43:") for c in stub.calls)

    def test_overview_pipeline_without_sha(self, client):
        """边界：pipeline 缺 sha 字段时 commit_time 为 None 且不查 commit。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(731, sha="")}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        assert data["pipelines"][0]["commit_time"] is None
        assert not any(c.startswith("commit:") for c in stub.calls)

    def test_overview_commit_without_date_field(self, client):
        """边界：commit 对象缺 committed_date 字段时 commit_time 为 None。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}
        stub.commits_by_sha = {(42, "abc123"): {"id": "abc123"}}

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        assert data["pipelines"][0]["commit_time"] is None


# ---- per-repo token（issue #60） ----

class TestRepoClient:
    """_repo_client：仓库本地目录 remote URL 内嵌 token → per-repo GitLabClient。"""

    def _make_git_repo(self, path, remote_url: str, remote_name: str = "origin"):
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add",
                        remote_name, remote_url], check=True)
        return path

    def test_client_built_from_remote_token(self, client, tmp_path, monkeypatch):
        """local_path 仓库 remote 带 token：client 用该 token 与 remote host。"""
        tc, stub, db, tmp_dir = client
        from botler.api.pipelines import _repo_client
        from botler.gitlab_client import GitLabClient
        repo_dir = tmp_dir / "repo"
        self._make_git_repo(repo_dir,
                            "https://agent:glpat-repo1@gitlab.example.com:509/group/repo.git")
        row = {"id": 1, "name": "repo", "local_path": str(repo_dir),
               "remote_name": "origin"}
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        got = _repo_client(ctx, row)
        assert isinstance(got, GitLabClient)
        assert got.token == "glpat-repo1"
        assert got.url == "https://gitlab.example.com:509"
        assert got.verify_ssl is False  # 沿用全局配置

    def test_clean_remote_falls_back(self, client, tmp_path):
        """remote URL 无内嵌 token：返回 None（回退全局 bot token）。"""
        tc, stub, db, tmp_dir = client
        from botler.api.pipelines import _repo_client
        repo_dir = tmp_dir / "repo"
        self._make_git_repo(repo_dir, "https://gitlab.example.com/group/repo.git")
        row = {"id": 1, "name": "repo", "local_path": str(repo_dir),
               "remote_name": "origin"}
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        assert _repo_client(ctx, row) is None

    def test_missing_local_dir_falls_back(self, client, tmp_path):
        """本地目录不存在：返回 None（回退全局），不抛异常。"""
        tc, stub, db, tmp_dir = client
        from botler.api.pipelines import _repo_client
        row = {"id": 1, "name": "repo", "local_path": str(tmp_dir / "nope"),
               "remote_name": "origin"}
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        assert _repo_client(ctx, row) is None

    def test_remote_name_selection(self, client, tmp_path):
        """remote_name 指定的 remote 才用于解析 token（upstream 带 token）。"""
        tc, stub, db, tmp_dir = client
        from botler.api.pipelines import _repo_client
        repo_dir = tmp_dir / "repo"
        self._make_git_repo(repo_dir, "https://gitlab.example.com/group/repo.git",
                            remote_name="origin")
        import subprocess
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "upstream",
                        "https://agent:glpat-up@gitlab.example.com/group/repo.git"],
                       check=True)
        row = {"id": 1, "name": "repo", "local_path": str(repo_dir),
               "remote_name": "upstream"}
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        got = _repo_client(ctx, row)
        assert got is not None and got.token == "glpat-up"

    def test_workspace_fallback_dir(self, client, tmp_path, monkeypatch):
        """无 local_path 时用 workspace/<name> 目录（与 executor 一致）。"""
        tc, stub, db, tmp_dir = client
        from botler import git_remote
        from botler.api.pipelines import _repo_client
        ws = tmp_dir / "ws"
        repo_dir = ws / "myrepo"
        self._make_git_repo(repo_dir,
                            "https://agent:glpat-ws@gitlab.example.com/group/repo.git")
        monkeypatch.setattr(git_remote, "_WORKSPACE_ROOT", ws)
        row = {"id": 1, "name": "myrepo", "local_path": None,
               "remote_name": None}  # remote_name 缺省 → origin
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        got = _repo_client(ctx, row)
        assert got is not None and got.token == "glpat-ws"

    def test_client_cached_within_ttl(self, client, tmp_path):
        """同一仓库 TTL 内复用 client 实例（避免每轮轮询重建 httpx client）。"""
        tc, stub, db, tmp_dir = client
        from botler.api.pipelines import _repo_client
        repo_dir = tmp_dir / "repo"
        self._make_git_repo(repo_dir,
                            "https://agent:glpat-c1@gitlab.example.com/group/repo.git")
        row = {"id": 1, "name": "repo", "local_path": str(repo_dir),
               "remote_name": "origin"}
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        first = _repo_client(ctx, row)
        second = _repo_client(ctx, row)
        assert first is not None and first is second

    def test_fallback_not_cached(self, client, tmp_path):
        """解析失败（无 token）不缓存：每次调用重试解析。"""
        tc, stub, db, tmp_dir = client
        from botler.api.pipelines import _repo_client
        repo_dir = tmp_dir / "repo"
        self._make_git_repo(repo_dir, "https://gitlab.example.com/group/repo.git")
        row = {"id": 1, "name": "repo", "local_path": str(repo_dir),
               "remote_name": "origin"}
        config = ConfigManager(str(tmp_dir / "config.yaml"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              config_path=str(tmp_dir / "config.yaml"))
        assert _repo_client(ctx, row) is None
        # 给仓库补上带 token 的 remote 后再次调用应解析成功（未被缓存 None）
        import subprocess
        subprocess.run(["git", "-C", str(repo_dir), "remote", "set-url", "origin",
                        "https://agent:glpat-late@gitlab.example.com/group/repo.git"],
                       check=True)
        got = _repo_client(ctx, row)
        assert got is not None and got.token == "glpat-late"


class TestOverviewPerRepoToken:
    """API 集成：概览页对每个仓库使用各自的 token 客户端查流水线。"""

    def _per_repo_stub(self, pipelines_by_project):
        stub = StubGitLab()
        stub.pipelines_by_project = pipelines_by_project
        stub.jobs_by_pipeline = {}
        return stub

    def test_overview_uses_per_repo_client(self, client, monkeypatch):
        """remote 带 token 的仓库：流水线查询走 per-repo client，全局桩不被调用。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        per = self._per_repo_stub({42: make_pipeline(731)})
        per.jobs_by_pipeline = {731: [make_job(1, "build")]}
        from botler.api import pipelines as pipelines_mod
        monkeypatch.setattr(pipelines_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert data["pipelines"][0]["pipeline"]["id"] == 731
        assert per.calls.count("pipeline:42") == 1
        # 全局 bot token 桩未被用于该仓库
        assert stub.calls.count("pipeline:42") == 0

    def test_overview_fallback_to_global_client(self, client, monkeypatch):
        """remote 无 token 的仓库：回退全局 client（旧行为）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.pipelines_by_project = {42: make_pipeline(731)}
        stub.jobs_by_pipeline = {731: [make_job(1, "build")]}
        from botler.api import pipelines as pipelines_mod
        monkeypatch.setattr(pipelines_mod, "_repo_client", lambda c, row: None)

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        assert resp.json()["errors"] == []
        assert stub.calls.count("pipeline:42") == 1

    def test_per_repo_token_invalid_goes_to_errors(self, client, monkeypatch):
        """per-repo token 失效（401）：该仓库进 errors，不中断整体（HTTP 200）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        bad = StubGitLab()
        bad.fail_projects = {42}
        from botler.api import pipelines as pipelines_mod
        monkeypatch.setattr(pipelines_mod, "_repo_client", lambda c, row: bad)

        resp = tc.get("/api/pipelines/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) == 1
        assert "仓库 a" in data["errors"][0]

    def test_per_repo_commit_time_uses_per_repo_client(self, client, monkeypatch):
        """提交时间查询同样走 per-repo client。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        per = self._per_repo_stub({42: make_pipeline(731)})
        per.jobs_by_pipeline = {731: [make_job(1, "build")]}
        per.commits_by_sha = {(42, "abc123"): {
            "id": "abc123", "committed_date": "2026-08-13T12:00:00.000+08:00"}}
        from botler.api import pipelines as pipelines_mod
        monkeypatch.setattr(pipelines_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = tc.get("/api/pipelines/overview")

        data = resp.json()
        assert data["pipelines"][0]["commit_time"] == "2026-08-13 04:00:00"
        assert "commit:42:abc123" in per.calls
        assert not any(c.startswith("commit:") for c in stub.calls)


# ---- job 产物明细（issue #329） ----

class TestJobArtifacts:
    """aggregate_stages 携带 job id 与精简后的产物列表（过滤 trace/metadata 噪音）。"""

    def test_job_carries_id_and_artifacts(self):
        """archive + 报告类型保留，trace/metadata 噪音过滤。"""
        jobs = [make_job(11, "build", artifacts=[
            {"file_type": "archive", "size": 208520,
             "filename": "artifacts.zip", "file_format": "zip"},
            {"file_type": "cobertura", "size": 27832,
             "filename": "cobertura-coverage.xml.gz", "file_format": "gzip"},
            {"file_type": "trace", "size": 2009224,
             "filename": "job.log", "file_format": None},
            {"file_type": "metadata", "size": 263,
             "filename": "metadata.gz", "file_format": "gzip"},
        ])]
        stages = aggregate_stages(jobs)
        job = stages[0]["jobs"][0]
        assert job["id"] == 11
        assert job["artifacts"] == [
            {"file_type": "archive", "size": 208520,
             "filename": "artifacts.zip", "file_format": "zip"},
            {"file_type": "cobertura", "size": 27832,
             "filename": "cobertura-coverage.xml.gz", "file_format": "gzip"},
        ]

    def test_job_without_artifacts_field(self):
        """无 artifacts 字段（旧数据/未上传产物）→ 空列表，不崩溃。"""
        jobs = [make_job(1, "build")]
        job = aggregate_stages(jobs)[0]["jobs"][0]
        assert job["artifacts"] == []
        assert job["id"] == 1

    def test_malformed_artifacts_tolerated(self):
        """artifacts 非列表 / 元素非对象 / 缺字段逐项兜底。"""
        jobs = [make_job(1, "build", artifacts="oops"),
                make_job(2, "build", artifacts=[None, {"filename": "a.zip"}]),
                make_job(3, "build", artifacts=[
                    {"file_type": "archive", "filename": "a.zip"}])]
        stages = aggregate_stages(jobs)
        assert [j["artifacts"] for j in stages[0]["jobs"]] == [
            [],
            [{"file_type": None, "filename": "a.zip",
              "size": None, "file_format": None}],
            [{"file_type": "archive", "filename": "a.zip",
              "size": None, "file_format": None}],
        ]


# ---- 流水线产物下载（issue #329） ----

class TestArtifactsDownload:
    """GET /api/pipelines/{repo_id}/artifacts?job_id= 后端代理下载。"""

    def test_download_ok(self, client):
        """正常下载：200 + zip 字节 + Content-Disposition attachment。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): b"PK\x03\x04fake-zip-bytes"}

        resp = tc.get(f"/api/pipelines/{repo_id}/artifacts?job_id=5")

        assert resp.status_code == 200
        assert resp.content == b"PK\x03\x04fake-zip-bytes"
        assert resp.headers["content-type"] == "application/zip"
        cd = resp.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert 'filename="job-5-artifacts.zip"' in cd
        assert "artifacts:42:5" in stub.calls

    def test_download_uses_per_repo_fallback_global(self, client):
        """仓库 remote 无 token 时回退全局客户端（桩即全局），且按 repo 查询。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 9): b"zzz"}

        resp = tc.get(f"/api/pipelines/{repo_id}/artifacts?job_id=9")

        assert resp.status_code == 200
        assert "artifacts:42:9" in stub.calls

    def test_download_404_no_artifacts(self, client):
        """GitLab 404（任务无产物）→ 接口 404 与中文提示。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.fail_artifacts = {7: GitLabError("资源不存在（404）", 404)}

        resp = tc.get(f"/api/pipelines/{repo_id}/artifacts?job_id=7")

        assert resp.status_code == 404
        assert "无产物" in resp.json()["detail"]

    def test_download_gitlab_5xx_maps_502(self, client):
        """GitLab 侧故障（500）→ 接口 502，不透传堆栈。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.fail_artifacts = {8: GitLabError("GitLab API 错误 500: boom", 500)}

        resp = tc.get(f"/api/pipelines/{repo_id}/artifacts?job_id=8")

        assert resp.status_code == 502
        assert "GitLab 产物下载失败" in resp.json()["detail"]

    def test_download_unknown_repo_404(self, client):
        """repo_id 不存在 → 404，不发起 GitLab 调用。"""
        tc, stub, db, tmp_path = client

        resp = tc.get("/api/pipelines/9999/artifacts?job_id=1")

        assert resp.status_code == 404
        assert "仓库不存在" in resp.json()["detail"]
        assert not any(c.startswith("artifacts:") for c in stub.calls)

    def test_download_missing_job_id_422(self, client):
        """job_id 必填：缺失时 FastAPI 校验 422。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")

        resp = tc.get(f"/api/pipelines/{repo_id}/artifacts")

        assert resp.status_code == 422


# ---- 报告查看（issue #337） ----

# 报告样本（与 test_report_parsers.py 同构，保持自包含）
SARIF_SAMPLE = """{
  "version": "2.1.0",
  "runs": [{
    "tool": {"driver": {"name": "Bandit"}},
    "results": [{
      "ruleId": "B101",
      "level": "error",
      "message": {"text": "Use of assert detected."},
      "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "botler/api/pipelines.py"},
        "region": {"startLine": 42, "startColumn": 8}}}]
    }]
  }]
}"""

DEPS_SAMPLE = """{
  "version": "15.0.0",
  "vulnerabilities": [{
    "id": "CVE-2023-1234",
    "name": "requests",
    "severity": "High",
    "solution": "升级到修复版本 ['2.31.0']",
    "identifiers": [{"type": "cve", "name": "CVE-2023-1234", "url": ""}],
    "location": {
      "file": "backend/requirements.txt",
      "dependency": {"package": {"name": "requests"}, "version": "2.28.1"},
      "operating_system": "unknown"
    }
  }]
}"""

JUNIT_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="1" skipped="1" tests="3" time="1.23">
    <testcase classname="tests.test_a" name="test_ok" time="0.1"/>
    <testcase classname="tests.test_a" name="test_fail" time="0.2">
      <failure message="assert 1 == 2">assert 1 == 2</failure>
    </testcase>
  </testsuite>
</testsuites>"""


class TestReportView:
    """GET /api/pipelines/{repo_id}/report?job_id=&file=&file_type= 报告查看。"""

    def _setup(self, tc, stub, db, file_type, content, filename="backend/report.sarif"):
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_files = {(42, 5, filename): content.encode("utf-8")}
        return repo_id

    def test_sast_report_ok(self, client):
        tc, stub, db, tmp_path = client
        repo_id = self._setup(tc, stub, db, "sast", SARIF_SAMPLE,
                              filename="backend/bandit-report.sarif")
        resp = tc.get(
            f"/api/pipelines/{repo_id}/report",
            params={"job_id": 5, "file": "backend/bandit-report.sarif",
                    "file_type": "sast"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == 5
        assert data["filename"] == "backend/bandit-report.sarif"
        assert data["file_type"] == "sast"
        report = data["report"]
        assert report["kind"] == "sast"
        assert report["tool"] == "Bandit"
        assert report["summary"]["total"] == 1
        r = report["results"][0]
        assert r["rule"] == "B101"
        assert r["severity"] == "high"
        assert r["file"] == "botler/api/pipelines.py"
        assert r["line"] == 42
        assert "artifact-file:42:5:backend/bandit-report.sarif" in stub.calls

    def test_dependency_scanning_report_ok(self, client):
        tc, stub, db, tmp_path = client
        repo_id = self._setup(tc, stub, db, "dependency_scanning", DEPS_SAMPLE,
                              filename="backend/deps-python-report.json")
        resp = tc.get(
            f"/api/pipelines/{repo_id}/report",
            params={"job_id": 5, "file": "backend/deps-python-report.json",
                    "file_type": "dependency_scanning"})
        assert resp.status_code == 200
        report = resp.json()["report"]
        assert report["kind"] == "deps"
        assert report["results"][0]["severity"] == "High"
        assert report["results"][0]["package"] == "requests"

    def test_junit_report_ok(self, client):
        tc, stub, db, tmp_path = client
        repo_id = self._setup(tc, stub, db, "junit", JUNIT_SAMPLE,
                              filename="backend/junit.xml")
        resp = tc.get(
            f"/api/pipelines/{repo_id}/report",
            params={"job_id": 5, "file": "backend/junit.xml", "file_type": "junit"})
        assert resp.status_code == 200
        report = resp.json()["report"]
        assert report["kind"] == "test"
        assert report["summary"]["tests"] == 3
        assert report["summary"]["failures"] == 1
        statuses = [r["status"] for r in report["results"]]
        assert statuses == ["passed", "failed"]

    def test_file_type_derived_from_extension(self, client):
        """未传 file_type 时按扩展名推导：.sarif→sast / .json→dependency_scanning。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_files = {(42, 5, "x.sarif"): SARIF_SAMPLE.encode("utf-8")}
        resp = tc.get(f"/api/pipelines/{repo_id}/report",
                      params={"job_id": 5, "file": "x.sarif"})
        assert resp.status_code == 200
        assert resp.json()["report"]["kind"] == "sast"

    def test_unknown_repo_404(self, client):
        tc, stub, db, tmp_path = client
        resp = tc.get("/api/pipelines/9999/report",
                      params={"job_id": 1, "file": "a.sarif", "file_type": "sast"})
        assert resp.status_code == 404
        assert "仓库不存在" in resp.json()["detail"]
        assert not any(c.startswith("artifact-file:") for c in stub.calls)

    @pytest.mark.parametrize(("metadata_name", "file_type", "archive_name",
                              "content", "kind"), [
        ("gl-sast-report.json", "sast", "backend/bandit-report.sarif",
         SARIF_SAMPLE, "sast"),
        ("gl-dependency-scanning-report.json", "dependency_scanning",
         "backend/deps-python-report.json", DEPS_SAMPLE, "deps"),
        ("junit.xml.gz", "junit", "backend/junit.xml", JUNIT_SAMPLE, "test"),
    ])
    def test_report_metadata_filename_falls_back_to_archive_path(
            self, client, metadata_name, file_type, archive_name, content, kind):
        """jobs API 的内部报告名 404 时，从 ZIP 归档定位 CI 原始报告。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(archive_name, content)
        stub.artifact_bytes = {(42, 5): archive.getvalue()}

        resp = tc.get(
            f"/api/pipelines/{repo_id}/report",
            params={"job_id": 5, "file": metadata_name,
                    "file_type": file_type})

        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == archive_name
        assert data["report"]["kind"] == kind
        assert f"artifact-file:42:5:{metadata_name}" in stub.calls
        assert "artifacts:42:5" in stub.calls

    def test_report_archive_has_no_matching_file_maps_404(self, client):
        """回退归档存在但没有对应类型报告时，仍返回明确的 404。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("dist/index.html", "ok")
        stub.artifact_bytes = {(42, 5): archive.getvalue()}

        resp = tc.get(
            f"/api/pipelines/{repo_id}/report",
            params={"job_id": 5, "file": "junit.xml.gz",
                    "file_type": "junit"})

        assert resp.status_code == 404
        assert "报告文件" in resp.json()["detail"]

    def test_report_corrupt_archive_maps_502(self, client):
        """单文件名 404 且 ZIP 归档损坏时，报告为上游产物异常。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): b"not-a-zip"}

        resp = tc.get(
            f"/api/pipelines/{repo_id}/report",
            params={"job_id": 5, "file": "gl-sast-report.json",
                    "file_type": "sast"})

        assert resp.status_code == 502
        assert "归档" in resp.json()["detail"]

    def test_gitlab_404_maps_404(self, client):
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.fail_artifact_files = {5: GitLabError("报告文件不存在（404）", 404)}
        stub.fail_artifacts = {5: GitLabError("任务无产物（404）", 404)}
        resp = tc.get(f"/api/pipelines/{repo_id}/report",
                      params={"job_id": 5, "file": "nope.sarif", "file_type": "sast"})
        assert resp.status_code == 404
        assert "报告文件" in resp.json()["detail"]

    def test_gitlab_5xx_maps_502(self, client):
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.fail_artifact_files = {8: GitLabError("GitLab API 错误 500: boom", 500)}
        resp = tc.get(f"/api/pipelines/{repo_id}/report",
                      params={"job_id": 8, "file": "a.sarif", "file_type": "sast"})
        assert resp.status_code == 502
        assert "GitLab" in resp.json()["detail"]

    def test_parse_failure_502(self, client):
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_files = {(42, 5, "bad.sarif"): b"not json {"}
        resp = tc.get(f"/api/pipelines/{repo_id}/report",
                      params={"job_id": 5, "file": "bad.sarif", "file_type": "sast"})
        assert resp.status_code == 502
        assert "解析失败" in resp.json()["detail"]

    def test_invalid_file_path_422(self, client):
        """路径穿越 / 绝对路径 / 空文件名 → 422，不发起 GitLab 调用。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        for bad in ["../secret", "/etc/passwd", ""]:
            resp = tc.get(f"/api/pipelines/{repo_id}/report",
                          params={"job_id": 5, "file": bad, "file_type": "sast"})
            assert resp.status_code == 422, f"file={bad!r} 应 422"
        assert not any(c.startswith("artifact-file:") for c in stub.calls)

    def test_missing_params_422(self, client):
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        resp = tc.get(f"/api/pipelines/{repo_id}/report", params={"job_id": 5})
        assert resp.status_code == 422
        resp = tc.get(f"/api/pipelines/{repo_id}/report", params={"file": "a.sarif"})
        assert resp.status_code == 422

    def test_unknown_file_type_422(self, client):
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_files = {(42, 5, "a.zip"): b"blob"}
        resp = tc.get(f"/api/pipelines/{repo_id}/report",
                      params={"job_id": 5, "file": "a.zip", "file_type": "archive"})
        assert resp.status_code == 422
        assert "报告类型" in resp.json()["detail"]


# ---- e2e 截图查看（issue #453） ----

# e2e:screenshots job（issue #445）的归档产物结构：
# frontend/screenshots/<页面>/<视口>.png + index.html（截图索引页）。
# 复现场景：CI/CD 详情右边栏应能直接查看 e2e 截图测试的截图，而不是
# 只能下载整个 zip 归档。以下测试先验证「截图列表 / 单张截图」两个
# 代理接口的行为（当前实现缺失，接口返回 404 即复现 bug）。
def _screenshot_png_bytes(size=(1440, 900), color=(30, 120, 210)) -> bytes:
    """Pillow 生成真实 PNG 字节（截图预览缩略图需要可解码的真实图片）。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _screenshot_zip() -> bytes:
    """构造与 e2e:screenshots job 归档同构的 zip（2 页面 × 2 视口 png）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("frontend/screenshots/index.html", "<html>screenshots</html>")
        zf.writestr("frontend/screenshots/overview/desktop-1440x900.png",
                    _screenshot_png_bytes(color=(30, 120, 210)))
        zf.writestr("frontend/screenshots/overview/mobile-375x667.png",
                    _screenshot_png_bytes(size=(375, 667), color=(210, 90, 30)))
        zf.writestr("frontend/screenshots/settings/desktop-1440x900.png",
                    _screenshot_png_bytes(color=(90, 200, 60)))
    return buf.getvalue()


class TestScreenshotsView:
    """GET /api/pipelines/{repo_id}/screenshots 与 /screenshot-file（issue #453）。

    需求：CI/CD 详情右边栏直接查看 e2e:screenshots job 生成的截图。
    """

    def test_screenshots_list_ok(self, client):
        """归档内 png 应被列出（路径/页面/视口），供前端预览。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): _screenshot_zip()}

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshots",
                      params={"job_id": 5})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["job_id"] == 5
        paths = [s["path"] for s in data["screenshots"]]
        assert "frontend/screenshots/overview/desktop-1440x900.png" in paths
        assert "frontend/screenshots/settings/desktop-1440x900.png" in paths
        assert not any("index.html" in p for p in paths), "索引页不是截图，不应列出"
        # 每个条目携带页面/视口信息，前端按页面分组展示
        shot = next(s for s in data["screenshots"]
                    if s["path"].endswith("desktop-1440x900.png"))
        assert shot["page"] == "overview"
        assert shot["viewport"] == "desktop-1440x900"
        assert shot["size"] > 0
        assert "artifacts:42:5" in stub.calls

    def test_screenshots_archive_without_png_returns_empty(self, client):
        """归档内无 png（普通 build 产物）→ 200 + 空列表，不崩溃。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dist/index.html", "build output")
        stub.artifact_bytes = {(42, 5): buf.getvalue()}

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshots",
                      params={"job_id": 5})

        assert resp.status_code == 200
        assert resp.json()["screenshots"] == []

    def test_screenshots_gitlab_404_maps_404(self, client):
        """GitLab 侧产物 404（任务无产物）→ 明确 404。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.fail_artifacts = {7: GitLabError("任务无产物（404）", 404)}

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshots",
                      params={"job_id": 7})

        assert resp.status_code == 404
        assert "产物" in resp.json()["detail"]

    def test_screenshot_file_ok(self, client):
        """单张 png 应返回图片字节流（image/png），供 <img> 直接渲染。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): _screenshot_zip()}
        path = "frontend/screenshots/overview/desktop-1440x900.png"

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-file",
                      params={"job_id": 5, "path": path})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.content.startswith(b"\x89PNG")
        assert "artifacts:42:5" in stub.calls

    def test_screenshot_file_rejects_non_png_and_traversal(self, client):
        """非 png 扩展名 / 路径穿越 / 绝对路径 / 空路径 → 422，不下载归档。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        for bad in ["frontend/screenshots/index.html", "../secret.png",
                    "/etc/passwd.png", ""]:
            resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-file",
                          params={"job_id": 5, "path": bad})
            assert resp.status_code == 422, f"path={bad!r} 应 422"
        assert not any(c.startswith("artifacts:") for c in stub.calls)

    def test_screenshot_file_unknown_repo_404(self, client):
        tc, stub, db, tmp_path = client
        resp = tc.get("/api/pipelines/9999/screenshot-file",
                      params={"job_id": 1, "path": "a.png"})
        assert resp.status_code == 404
        assert "仓库不存在" in resp.json()["detail"]
        assert not any(c.startswith("artifacts:") for c in stub.calls)

    # ---- 截图预览缩略图（issue #456）----
    # 需求：查看截图的页面先加载预览图（缩略图网格不再直接拉取整张原图，
    # e2e 整页截图可达数 MB），点击放大进入大图预览时才加载原图。
    # 后端新增 /screenshot-preview 代理：拉取原图字节后用 Pillow 缩放出
    # 小尺寸 JPEG 预览图并缓存，前端缩略图 <img> 指向它。

    def test_screenshot_preview_ok(self, client):
        """预览图应返回 JPEG 缩略图：尺寸被缩放、字节远小于原图、可解码。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): _screenshot_zip()}
        path = "frontend/screenshots/overview/desktop-1440x900.png"

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-preview",
                      params={"job_id": 5, "path": path})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/jpeg")
        from PIL import Image
        with Image.open(io.BytesIO(resp.content)) as im:
            assert im.format == "JPEG"
            # 最长边不超过预览上限（480px），等比缩放 1440x900 → 480x300
            assert max(im.size) <= 480
            assert im.size == (480, 300), f"应等比缩放，实际 {im.size}"
        # 预览图字节应远小于原图（1440x900 全尺寸 PNG 数十 KB 级）
        assert len(resp.content) < 20000, f"预览图应足够小，实际 {len(resp.content)} 字节"

    def test_screenshot_preview_cached(self, client):
        """预览图应缓存：同 key 第二次请求不再下载原图字节。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): _screenshot_zip()}
        path = "frontend/screenshots/overview/desktop-1440x900.png"
        params = {"job_id": 5, "path": path}

        for _ in range(2):
            resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-preview",
                          params=params)
            assert resp.status_code == 200

        file_calls = [c for c in stub.calls if c.startswith("artifact-file:")]
        assert len(file_calls) == 1, "预览缓存命中时不应重复下载原图"

    def test_screenshot_preview_rejects_non_image_and_traversal(self, client):
        """非图片扩展名 / 路径穿越 / 绝对路径 / 空路径 → 422，不下载归档。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        for bad in ["frontend/screenshots/index.html", "../secret.png",
                    "/etc/passwd.png", ""]:
            resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-preview",
                          params={"job_id": 5, "path": bad})
            assert resp.status_code == 422, f"path={bad!r} 应 422"
        assert not any(c.startswith("artifacts:") for c in stub.calls)

    def test_screenshot_preview_unknown_repo_404(self, client):
        """仓库不存在 → 404，且不发起任何 GitLab 调用。"""
        tc, stub, db, tmp_path = client
        resp = tc.get("/api/pipelines/9999/screenshot-preview",
                      params={"job_id": 1, "path": "a.png"})
        assert resp.status_code == 404
        assert "仓库不存在" in resp.json()["detail"]
        assert not any(c.startswith("artifacts:") for c in stub.calls)

    def test_screenshot_preview_zip_fallback(self, client):
        """单文件下载 404 回退 zip 归档提取，仍能生成预览图。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.artifact_bytes = {(42, 5): _screenshot_zip()}
        path = "frontend/screenshots/overview/desktop-1440x900.png"

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-preview",
                      params={"job_id": 5, "path": path})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/jpeg")
        assert any(c.startswith("artifacts:") for c in stub.calls), \
            "单文件 404 后应回退下载 zip 归档"

    def test_screenshot_preview_undecodable_falls_back_to_original(self, client):
        """Pillow 无法解码（损坏图片）时兜底返回原图字节与原始 content-type，
        不 500（预览能力降级但不影响查看原图）。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("frontend/screenshots/broken/shot.png",
                        b"\x89PNG\r\n\x1a\nbroken-image-bytes")
        stub.artifact_bytes = {(42, 5): buf.getvalue()}
        path = "frontend/screenshots/broken/shot.png"

        resp = tc.get(f"/api/pipelines/{repo_id}/screenshot-preview",
                      params={"job_id": 5, "path": path})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.content.startswith(b"\x89PNG")

