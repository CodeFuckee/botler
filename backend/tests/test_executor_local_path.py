"""ClaudeExecutor 本地工作区测试：local_path 仓库直接在该文件夹执行。"""

import subprocess
from pathlib import Path

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor, ExecutorError
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


def _make_local_repo(tmp_path) -> Path:
    """创建带一个提交的本地仓库，remote 指向本地 bare 仓库（fetch 不联网）。"""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "main"], check=True)
    # git init --bare 的 HEAD 默认指向 master（不存在），push 不会更新它；
    # 显式指向 main，否则 clone 时 origin/HEAD 悬空
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
    return repo


def _repo_dict(local_path: str) -> dict:
    return {
        "name": "project",
        "url": "git@gitlab.example.com:group/project.git",
        "local_path": local_path,
        "remote_name": "origin",
    }


class TestPrepareWorkspaceLocalPath:
    def test_uses_local_path_as_workdir(self, executor, tmp_path):
        repo = _make_local_repo(tmp_path)
        workdir, _env = executor.prepare_workspace(_repo_dict(str(repo)))
        assert Path(workdir) == repo

    def test_cleans_dirty_local_changes(self, executor, tmp_path):
        """本地文件夹里的未提交改动会被重置清掉（每次执行同步到远端）。"""
        repo = _make_local_repo(tmp_path)
        executor.prepare_workspace(_repo_dict(str(repo)))
        (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert not (repo / "dirty.txt").exists()
        # 远端文件仍然完好
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"

    def test_resume_keeps_dirty_changes(self, executor, tmp_path):
        """恢复执行（resume=True）：不清工作区——保留 Claude 上次的未提交改动。"""
        repo = _make_local_repo(tmp_path)
        executor.prepare_workspace(_repo_dict(str(repo)))
        (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
        executor.prepare_workspace(_repo_dict(str(repo)), resume=True)
        # dirty 文件保留，远端文件未被覆盖
        assert (repo / "dirty.txt").exists()
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"

    def test_missing_local_path(self, executor, tmp_path):
        with pytest.raises(ExecutorError):
            executor.prepare_workspace(_repo_dict(str(tmp_path / "nope")))

    def test_local_path_not_a_git_repo(self, executor, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "some.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ExecutorError):
            executor.prepare_workspace(_repo_dict(str(plain)))

    def test_remote_name_fallback_to_origin(self, executor, tmp_path):
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["remote_name"] = None  # 老数据没有 remote_name，应回退 origin
        workdir, _env = executor.prepare_workspace(d)
        assert Path(workdir) == repo


# ---- issue #12 复现：origin/HEAD 缺失必现失败 + askpass 脚本被删致 fetch 凭据间歇失败 ----

class TestPrepareWorkspaceIssue12:
    """issue #12：运行任务失败日志——
    第 1 次 `git fetch` HTTP Basic: Access denied（askpass 脚本被上次执行 unlink，
    并发/重试时脚本不存在 → git 回退 credential helper 旧凭据 → 间歇性失败）；
    第 2、3 次 `git reset --hard origin/HEAD` → ambiguous argument（工作区
    origin/HEAD 符号引用不存在（手工加 remote 的仓库无此引用），必现失败）。
    """

    def test_reset_without_origin_head(self, executor, tmp_path):
        """origin/HEAD 符号引用缺失时 prepare_workspace 不应失败。

        修复前：reset --hard origin/HEAD → 'ambiguous argument' → ExecutorError；
        修复后：reset 目标跟随实际 checkout 的分支（origin/main）。
        """
        repo = _make_local_repo(tmp_path)
        # 模拟手工加 remote 的仓库：删除 origin/HEAD 符号引用
        subprocess.run(["git", "-C", str(repo), "remote", "set-head", "origin", "-d"],
                       check=True)
        workdir, _env = executor.prepare_workspace(_repo_dict(str(repo)))
        assert Path(workdir) == repo

    def test_askpass_script_kept_after_prepare(self, executor, tmp_path):
        """prepare 结束后 askpass 脚本应保留（供后续 fetch 复用）。

        修复前：prepare 末尾 unlink 脚本 → 并发任务/下次重试 fetch 时脚本
        不存在 → git 回退 credential helper 旧凭据 → HTTP Basic: Access denied。
        """
        repo = _make_local_repo(tmp_path)
        executor.prepare_workspace(_repo_dict(str(repo)))
        script = executor.workspace_root / ".botler-askpass-project.sh"
        assert script.exists(), "askpass 脚本在 prepare 后被删除，fetch 凭据间歇失败"
