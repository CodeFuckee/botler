"""结构化执行报告评论测试（issue #252）。

覆盖：base_sha 基线采集（首次采集/续跑不覆盖/失败不阻塞）、成功收尾评论
含改动文件表格与测试摘要、失败收尾评论含失败原因与相关文件、无 diff 数据
时段落隐藏不报错、自定义评论模版生效。
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
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


@pytest.fixture
def executor(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _git_repo_with_base(tmp_path: Path):
    """构造只含 base 提交的临时 git 仓库，返回 (workdir, base_sha)。"""
    workdir = tmp_path / "demo"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workdir)], check=True)
    subprocess.run(["git", "-C", str(workdir), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(workdir), "config", "user.name", "t"],
                   check=True)
    (workdir / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(workdir), "commit", "-qm", "base"],
                   check=True)
    base = subprocess.run(["git", "-C", str(workdir), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return workdir, base


def _make_changes(workdir: Path) -> None:
    """在 base 之后追加任务改动（修改 a.txt、新增 new.py）。"""
    with open(workdir / "a.txt", "a", encoding="utf-8") as f:
        f.write("a2\n")
    (workdir / "new.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(workdir), "commit", "-qm", "work"],
                   check=True)


def _mk_repo(db, local_path=None) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git",
                   local_path=str(local_path) if local_path else None)
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1) -> int:
    return db.create_task(repo_id, 42, issue_iid, "任务")


def _success_mock(executor, tmp_path, comments):
    executor.gitlab = SimpleNamespace(
        find_commit_for_issue=lambda pid, iid: "deadbeef" * 5,
        add_labels=lambda *a, **k: None,
        last_note_author_id=lambda pid, iid: None,
        get_bot_id=lambda: 7,
        get_issue=lambda pid, iid: {"state": "opened", "closed_by": None},
        add_comment=lambda pid, iid, body: comments.append((pid, iid, body)))
    executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"


class TestCaptureBaseSha:
    def test_captures_once_on_first_run(self, executor, tmp_path):
        """首次执行采集 base_sha，续跑/重试不覆盖首次基线。"""
        db = executor.db
        workdir, base = _git_repo_with_base(tmp_path)
        repo_id = _mk_repo(db, local_path=workdir)
        task_id = _mk_task(db, repo_id)
        db.claim_task(task_id)

        executor._capture_base_sha(task_id, workdir)
        task = db.get_task(task_id)
        assert task["base_sha"] == base
        # 续跑：工作区 HEAD 前移，基线保持首次值
        _make_changes(workdir)
        executor._capture_base_sha(task_id, workdir)
        assert db.get_task(task_id)["base_sha"] == base

    def test_failure_does_not_block(self, executor, tmp_path):
        """非 git 工作区：采集失败仅记日志，不抛异常。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.claim_task(task_id)
        executor._capture_base_sha(task_id, tmp_path / "not-a-git-repo")
        assert db.get_task(task_id)["base_sha"] is None


class TestStructuredSuccessComment:
    def test_success_comment_has_diff_and_test_summary(self, executor, tmp_path):
        """成功收尾：评论含改动文件表格、测试摘要、commit 链接与用时。"""
        db = executor.db
        workdir, base = _git_repo_with_base(tmp_path)
        repo_id = _mk_repo(db, local_path=workdir)
        task_id = _mk_task(db, repo_id)
        comments = []
        _success_mock(executor, tmp_path, comments)
        db.claim_task(task_id)
        # 模拟首次执行已采集基线；任务改动已提交到工作区
        db.set_task_status(task_id, None, base_sha=base)
        _make_changes(workdir)

        output = json.dumps(
            {"result": "开发完成，全部测试通过\n2 passed, 0 failed in 1.5s",
             "session_id": "s1"}, ensure_ascii=False)
        executor._finish_succeeded(task_id, output, repo=db.get_repo(repo_id))

        assert len(comments) == 1
        _, _, body = comments[0]
        assert "任务已完成" in body
        assert "## 改动文件" in body
        assert "| a.txt | +1 | -0 |" in body
        assert "| new.py | +1 | -0 |" in body
        assert "新增 2 行" in body
        assert "**新增文件**" in body and "new.py" in body
        assert "## 测试摘要" in body
        assert "2 passed" in body
        assert "开发完成，全部测试通过" in body  # 结果摘要
        assert "deadbeef" in body  # commit 链接
        assert "用时：" in body

    def test_success_comment_hides_empty_sections(self, executor, tmp_path):
        """无基线/无测试输出：改动文件与测试摘要段落隐藏，不报错。"""
        db = executor.db
        repo_id = _mk_repo(db)  # 无 local_path → 无工作区 → 无 diff
        task_id = _mk_task(db, repo_id)
        comments = []
        _success_mock(executor, tmp_path, comments)
        db.claim_task(task_id)

        executor._finish_succeeded(task_id, "ok")

        assert len(comments) == 1
        _, _, body = comments[0]
        assert "任务已完成" in body
        assert "## 改动文件" not in body
        assert "## 测试摘要" not in body

    def test_custom_comment_template_used(self, executor, tmp_path):
        """templates.comment 自定义模版生效（issue #252 可配置）。"""
        db = executor.db
        executor.config.update_comment_template(
            "自定义报告：\n\n改动：{diff_stat}\n\n测试：{test_summary}\n"
            "提交：{commit_link}\n用时：{duration}")
        workdir, base = _git_repo_with_base(tmp_path)
        repo_id = _mk_repo(db, local_path=workdir)
        task_id = _mk_task(db, repo_id)
        comments = []
        _success_mock(executor, tmp_path, comments)
        db.claim_task(task_id)
        db.set_task_status(task_id, None, base_sha=base)
        _make_changes(workdir)

        output = json.dumps({"result": "ok"}, ensure_ascii=False)
        executor._finish_succeeded(task_id, output, repo=db.get_repo(repo_id))

        assert len(comments) == 1
        _, _, body = comments[0]
        assert body.startswith("自定义报告")
        assert "改动：" in body and "| a.txt |" in body
        assert "提交：" in body and "deadbeef" in body


class TestStructuredFailureComment:
    def test_failure_comment_has_reason_and_files(self, executor, tmp_path):
        """失败收尾：评论含失败原因、相关文件（改动 diff）与日志尾部。"""
        db = executor.db
        workdir, base = _git_repo_with_base(tmp_path)
        repo_id = _mk_repo(db, local_path=workdir)
        task_id = _mk_task(db, repo_id)
        comments = []
        executor.gitlab = SimpleNamespace(
            add_comment=lambda pid, iid, body: comments.append((pid, iid, body)),
            add_labels=lambda *a, **k: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"
        db.claim_task(task_id)
        db.set_task_status(task_id, None, base_sha=base)
        _make_changes(workdir)

        output = ("=== 1 failed, 2 passed in 3s ===\n"
                  "Traceback (most recent call last):\nValueError: boom")
        executor._finish_failed(task_id, "测试失败: ValueError: boom", output,
                                repo=db.get_repo(repo_id))

        assert len(comments) == 1
        _, _, body = comments[0]
        assert "无法完成此 issue" in body
        assert "测试失败: ValueError: boom" in body
        assert "## 相关文件" in body
        assert "| a.txt | +1 | -0 |" in body
        assert "## 测试摘要" in body
        assert "❌" in body
        # issue #274：失败评论带分类前缀（本用例无关键词 → 兜底 unknown）
        assert "失败分类：未知（unknown）" in body

    def test_failure_comment_has_category_prefix_and_persisted(self, executor, tmp_path):
        """issue #274：失败收尾按规则分类落库 tasks.failure_category，
        失败评论带分类前缀（401 → 环境类）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = []
        executor.gitlab = SimpleNamespace(
            add_comment=lambda pid, iid, body: comments.append((pid, iid, body)),
            add_labels=lambda *a, **k: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"
        db.claim_task(task_id)

        executor._finish_failed(task_id, "获取 issue 失败: 401 Unauthorized",
                                "connect error", repo=db.get_repo(repo_id))

        # 分类落库
        assert db.get_task(task_id)["failure_category"] == "env"
        # 评论带分类前缀（徽章 + 处理建议）
        assert len(comments) == 1
        _, _, body = comments[0]
        assert "> **失败分类：环境类（env）**" in body
        assert "请检查仓库 token 与网络配置后点重试" in body

    def test_failure_comment_hides_files_without_diff(self, executor, tmp_path):
        """失败但无 diff（无基线/无工作区）：相关文件段落隐藏，不报错。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = []
        executor.gitlab = SimpleNamespace(
            add_comment=lambda pid, iid, body: comments.append((pid, iid, body)),
            add_labels=lambda *a, **k: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"
        db.claim_task(task_id)

        executor._finish_failed(task_id, "无法连接 GitLab", "plain log")

        assert len(comments) == 1
        _, _, body = comments[0]
        assert "无法完成此 issue" in body
        assert "无法连接 GitLab" in body
        assert "## 相关文件" not in body
