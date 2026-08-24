"""任务执行前预检（issue #238）测试。

覆盖：
- 各检查项函数（git 凭据/token、local_path、磁盘空间、工作区）的
  通过 / 失败 / 跳过路径；
- 执行器集成：预检失败 → 任务直接 failed（不重试、不调用 _run_once、
  不消耗模型调用）、检查明细 ✓/✗ 落库 tasks.precheck_result 并在 API 暴露；
- 预检通过 / 未启用 / 预检自身异常 → 任务行为与现状一致（照常执行）。

预检本身要快（< 10s）：git 探测失败路径用本地不存在路径快速触发，
不依赖外部网络。
"""

import json
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.precheck import (
    check_disk_space,
    check_git_credentials,
    check_local_path,
    check_workspace,
    format_precheck_failure,
    parse_precheck,
    serialize_precheck,
)
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


def _git(repo_path: Path, *args: str) -> None:
    """在 repo_path 执行 git 命令（初始化测试仓库用）。"""
    subprocess.run(["git", *args], cwd=str(repo_path), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def local_git_repo(tmp_path):
    """真实本地 git 仓库（带一个提交），作为 file:// 远端与 local_path 工作区。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture
def executor(tmp_path):
    """预检默认开启（worker: {} → precheck_enabled 默认 true）的 executor。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


class TestCheckGitCredentials:
    """git 凭据/token 有效性检查（git ls-remote 探测）。"""

    def test_url_repo_reachable(self, local_git_repo):
        """URL 方式仓库可克隆：ls-remote 通过。"""
        repo = {"url": f"file://{local_git_repo}", "name": "demo"}
        ok, detail = check_git_credentials(repo, Path("/tmp"), None)
        assert ok is True
        assert "探测通过" in detail

    def test_url_repo_unreachable(self):
        """仓库不可克隆（远端不存在）：未通过，给出明确原因。"""
        repo = {"url": "file:///tmp/definitely-not-exist-repo.git", "name": "demo"}
        ok, detail = check_git_credentials(repo, Path("/tmp"), None)
        assert ok is False
        assert "失败" in detail and "git" in detail

    def test_url_missing(self):
        """仓库既无 url 也无 local_path：未通过。"""
        ok, detail = check_git_credentials({"name": "demo"}, Path("/tmp"), None)
        assert ok is False
        assert "未配置 url" in detail

    def test_local_path_repo_with_remote(self, local_git_repo):
        """local_path 仓库对所选 remote 探测：通过。"""
        # 给本地仓库加一个远端（指向自身，file:// 协议无需凭据）
        _git(local_git_repo, "remote", "add", "origin", f"file://{local_git_repo}")
        repo = {"local_path": str(local_git_repo), "remote_name": "origin", "name": "demo"}
        ok, detail = check_git_credentials(repo, local_git_repo, None)
        assert ok is True
        assert "origin" in detail

    def test_local_path_remote_missing(self, local_git_repo):
        """local_path 仓库缺少所选 remote：未通过（fetch/push 路径不可用）。"""
        repo = {"local_path": str(local_git_repo), "remote_name": "origin", "name": "demo"}
        ok, detail = check_git_credentials(repo, local_git_repo, None)
        assert ok is False
        assert "origin" in detail

    def test_ls_remote_timeout(self, monkeypatch, local_git_repo):
        """git 探测超时（远端无响应）：未通过，不无限等待。"""

        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd=a, timeout=k.get("timeout", 8))

        monkeypatch.setattr(subprocess, "run", _timeout)
        repo = {"url": f"file://{local_git_repo}", "name": "demo"}
        ok, detail = check_git_credentials(repo, Path("/tmp"), None, timeout=8)
        assert ok is False
        assert "超时" in detail


class TestCheckLocalPath:
    """本地路径检查（local_path 配置时）。"""

    def test_skip_when_not_configured(self):
        """未配置 local_path：跳过（不算失败）。"""
        ok, detail = check_local_path({"name": "demo"})
        assert ok is None
        assert "跳过" in detail

    def test_missing(self, tmp_path):
        ok, detail = check_local_path(
            {"name": "demo", "local_path": str(tmp_path / "nope")})
        assert ok is False
        assert "不存在" in detail

    def test_is_file(self, tmp_path):
        f = tmp_path / "a-file"
        f.write_text("x", encoding="utf-8")
        ok, detail = check_local_path({"name": "demo", "local_path": str(f)})
        assert ok is False
        assert "不是目录" in detail

    def test_not_writable(self, tmp_path, monkeypatch):
        d = tmp_path / "repo"
        d.mkdir()
        monkeypatch.setattr(os, "access", lambda p, m: False)
        ok, detail = check_local_path({"name": "demo", "local_path": str(d)})
        assert ok is False
        assert "不可写" in detail

    def test_ok(self, tmp_path):
        d = tmp_path / "repo"
        d.mkdir()
        ok, detail = check_local_path({"name": "demo", "local_path": str(d)})
        assert ok is True
        assert "存在且可写" in detail


class TestCheckDiskSpace:
    """磁盘剩余空间阈值检查。"""

    DU = namedtuple("disk_usage", "total used free")

    def test_below_threshold(self, tmp_path, monkeypatch):
        """剩余空间低于阈值（默认 2GB）：未通过。"""
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda p: self.DU(100 * 1024 ** 2, 90 * 1024 ** 2, 10 * 1024 ** 2))
        ok, detail = check_disk_space(tmp_path, 2048)
        assert ok is False
        assert "10.0 MB < 阈值 2048 MB" in detail

    def test_above_threshold(self, tmp_path, monkeypatch):
        """剩余空间不低于阈值：通过。"""
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda p: self.DU(0, 0, 5 * 1024 ** 3))  # 5GB 剩余
        ok, detail = check_disk_space(tmp_path, 2048)
        assert ok is True
        assert "≥ 阈值 2048 MB" in detail

    def test_target_missing_falls_back_to_parent(self, tmp_path, monkeypatch):
        """探测目标不存在（仓库尚未 clone）：回退最近存在的父目录。"""
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda p: self.DU(0, 0, 5 * 1024 ** 3))
        ok, _ = check_disk_space(tmp_path / "not-exists" / "deep", 2048)
        assert ok is True

    def test_probe_failure(self, tmp_path, monkeypatch):
        """磁盘探测异常：未通过并给出原因。"""

        def _boom(p):
            raise OSError("stale handle")

        monkeypatch.setattr(shutil, "disk_usage", _boom)
        ok, detail = check_disk_space(tmp_path, 2048)
        assert ok is False
        assert "探测失败" in detail


class TestCheckWorkspace:
    """工作区可用性检查。"""

    def test_root_missing(self, tmp_path):
        ok, detail = check_workspace(tmp_path / "nope", tmp_path / "repo")
        assert ok is False
        assert "根目录不存在" in detail

    def test_workdir_is_file(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        f = tmp_path / "repo-file"
        f.write_text("x", encoding="utf-8")
        ok, detail = check_workspace(root, f)
        assert ok is False
        assert "被文件占用" in detail

    def test_root_not_writable(self, tmp_path, monkeypatch):
        root = tmp_path / "ws"
        root.mkdir()
        monkeypatch.setattr(os, "access", lambda p, m: False)
        ok, detail = check_workspace(root, root / "repo")
        assert ok is False
        assert "不可写" in detail

    def test_ok(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        ok, detail = check_workspace(root, root / "repo")
        assert ok is True
        assert "可用" in detail


class TestSerialization:
    """预检结果序列化/反序列化（tasks.precheck_result 列契约）。"""

    def test_roundtrip(self):
        result = {
            "ok": True,
            "checks": [{"name": "git_token", "ok": True, "detail": "x"}],
            "checked_at": "2026-01-01T00:00:00+08:00",
        }
        assert parse_precheck(serialize_precheck(result)) == result

    def test_parse_invalid(self):
        assert parse_precheck("") is None
        assert parse_precheck(None) is None
        assert parse_precheck("not-json") is None
        assert parse_precheck("[1,2]") is None


class TestFormatFailure:
    """预检失败原因摘要（error_message 文案）。"""

    def test_lists_only_failed_checks(self):
        result = {
            "ok": False,
            "checks": [
                {"name": "git_token", "label": "Git 凭据/Token",
                 "ok": False, "detail": "认证失败"},
                {"name": "local_path", "label": "本地路径", "ok": None, "detail": "跳过"},
                {"name": "disk_space", "label": "磁盘剩余空间",
                 "ok": True, "detail": "通过"},
            ],
        }
        msg = format_precheck_failure(result)
        assert "任务执行前预检失败" in msg
        assert "Git 凭据/Token" in msg and "认证失败" in msg
        assert "磁盘剩余空间" not in msg  # 通过项不列出

    def test_empty_failed(self):
        msg = format_precheck_failure({"ok": False, "checks": []})
        assert "任务执行前预检失败" in msg


class TestExecutorPrecheckIntegration:
    """执行器集成：预检在消耗模型调用前拦截环境性失败。"""

    def _mk_repo(self, db, url="https://gitlab.example.com/group/demo.git") -> int:
        db.upsert_repo(42, "demo", url)
        return db.get_repo_by_project_id(42)["id"]

    def _mk_task(self, db, repo_id: int, issue_iid: int = 1) -> int:
        return db.create_task(repo_id, 42, issue_iid, "预检测试任务")

    def _install(self, executor, monkeypatch, tmp_path, run_once):
        """桩 gitlab + _run_once + 日志路径（对齐 test_executor.py 的 mock 方式）。"""
        calls = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": "opened"},
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)),
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )
        monkeypatch.setattr(executor, "_run_once", run_once)
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return calls

    def test_precheck_failure_fails_fast_without_model_call(
            self, executor, monkeypatch, tmp_path):
        """仓库不可克隆（token 失效/远端不可达）：任务直接 failed，
        不调用 _run_once（不消耗模型调用）、不进入重试。"""
        db = executor.db
        repo_id = self._mk_repo(db, url="file:///tmp/definitely-not-exist-repo.git")
        task_id = self._mk_task(db, repo_id)

        def boom(*a):
            raise AssertionError("预检失败的任务不应调用 _run_once（不消耗模型调用）")

        self._install(executor, monkeypatch, tmp_path, run_once=boom)

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert "任务执行前预检失败" in task["error_message"]
        assert task["attempt_count"] == 0, "预检失败不应进入重试循环"
        # 错误字段写入并可在详情页展示
        detail = json.loads(task["error_detail"])
        assert detail["ok"] is False
        # 失败分类为环境类（env）
        assert task["failure_category"] == "env"
        # 检查明细落库，4 个检查项齐全
        precheck = parse_precheck(task["precheck_result"])
        assert precheck is not None and precheck["ok"] is False
        names = [c["name"] for c in precheck["checks"]]
        assert names == ["git_token", "local_path", "disk_space", "workspace"]
        git_check = next(c for c in precheck["checks"] if c["name"] == "git_token")
        assert git_check["ok"] is False
        assert "失败" in git_check["detail"]

    def test_precheck_failure_category_env_ignores_429_number(
            self, executor, monkeypatch, tmp_path):
        """预检失败分类固定 env，不随失败文本抖动（issue #481 流水线
        #1429 回归防护）。

        历史缺陷：Windows runner（backend:test 迁移目标，issue #469）磁盘
        剩余空间数值恰好含 "429"（如 4294967.3 MB）时，failure_classify 的
        引擎类限流规则 r"429" 先于环境类规则命中 → 预检失败任务被误分类为
        engine，CI 随机失败。预检按 issue #238 设计本身就是环境性失败，
        分类应固定 env，不依赖错误文本。
        """
        db = executor.db
        repo_id = self._mk_repo(db, url="file:///tmp/definitely-not-exist-repo.git")
        task_id = self._mk_task(db, repo_id)

        def boom(*a):
            raise AssertionError("预检失败的任务不应调用 _run_once（不消耗模型调用）")

        self._install(executor, monkeypatch, tmp_path, run_once=boom)
        # 模拟 Windows 上磁盘剩余数值恰好含 "429"（修复前会误判 engine）
        monkeypatch.setattr(
            "botler.executor.check_disk_space",
            lambda target, min_free_mb: (
                True, f"磁盘剩余 4294967.3 MB ≥ 阈值 {min_free_mb} MB（{target}）"))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert task["failure_category"] == "env"

    def test_precheck_pass_then_task_runs_normally(
            self, executor, monkeypatch, tmp_path, local_git_repo):
        """预检通过（真实可克隆仓库）→ 任务照常执行到 _run_once → 成功
        （验收标准 2：预检通过的任务行为与现状一致）。"""
        db = executor.db
        repo_id = self._mk_repo(db, url=f"file://{local_git_repo}")
        task_id = self._mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码"},
                            ensure_ascii=False)
        self._install(executor, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["attempt_count"] == 1
        precheck = parse_precheck(task["precheck_result"])
        assert precheck is not None and precheck["ok"] is True
        for c in precheck["checks"]:
            assert c["ok"] is not False, f"检查项 {c['name']} 应通过: {c['detail']}"

    def test_precheck_disabled_task_unchanged(self, tmp_path, monkeypatch):
        """预检未启用（worker.precheck_enabled=false）：即使仓库不可克隆，
        任务行为与现状一致（不拦截）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT.replace("worker: {}",
                                "worker: {precheck_enabled: false}"),
            encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "disabled.db"))
        gitlab = GitLabClient("https://gitlab.example.com", "test-token",
                              verify_ssl=False)
        ex = ClaudeExecutor(config, db, gitlab, TemplateRenderer(config),
                            workspace_root=str(tmp_path / "ws-disabled"))
        repo_id = self._mk_repo(db, url="file:///tmp/definitely-not-exist-repo.git")
        task_id = self._mk_task(db, repo_id)
        output = json.dumps({"result": "完成"}, ensure_ascii=False)
        self._install(ex, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output))

        ex.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["precheck_result"] is None

    def test_precheck_exception_does_not_block(
            self, executor, monkeypatch, tmp_path):
        """预检自身异常：记日志降级为通过，不阻塞任务（预检为建议性）。"""
        db = executor.db
        repo_id = self._mk_repo(db, url="file:///tmp/definitely-not-exist-repo.git")
        task_id = self._mk_task(db, repo_id)
        output = json.dumps({"result": "完成"}, ensure_ascii=False)
        self._install(executor, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output))

        def broken(*a, **k):
            raise RuntimeError("预检意外异常")

        monkeypatch.setattr(executor, "_run_precheck", broken)

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        precheck = parse_precheck(task["precheck_result"])
        assert precheck is not None and precheck["ok"] is True
        assert "预检执行异常" in precheck["error"]


class TestPrecheckApi:
    """GET /api/tasks 详情：precheck_result 字段数据契约（元信息区展示用）。"""

    def test_task_detail_includes_precheck_result(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "api.db"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=None,
                              config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        client = TestClient(app)

        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 1, "预检测试任务")
        result = {
            "ok": True,
            "checks": [
                {"name": "git_token", "label": "Git 凭据/Token",
                 "ok": True, "detail": "探测通过"},
            ],
            "checked_at": "2026-01-01T00:00:00+08:00",
        }
        db.set_task_status(task_id, "succeeded",
                           precheck_result=serialize_precheck(result))

        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["precheck_result"] == result

    def test_task_detail_precheck_null_for_old_task(self, tmp_path):
        """旧任务无预检记录：precheck_result 为 null，前端显示「暂无预检记录」。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "api-old.db"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=None,
                              config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        client = TestClient(app)

        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 1, "旧任务")
        db.set_task_status(task_id, "succeeded")

        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["precheck_result"] is None
