"""ClaudeExecutor 本地工作区测试：local_path 仓库直接在该文件夹执行。"""

import os
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
    # -c 显式提供 git 身份：GitHub Actions runner 无全局身份配置，
    # 测试不依赖环境（issue #10 镜像 CI 实测 git commit 失败）
    subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                    "-c", "user.email=test@botler.local",
                    "commit", "-q", "-m", "init"], check=True)
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


# ---- issue #91 复现：root 残留致 git clean Permission denied → 任务重试耗尽 ----

class TestPrepareWorkspaceCleanTolerant:
    """issue #91（诊断任务 #136）：local_path 工作区存在非本进程用户
    （如 root 跑过 flutter build）留下的 untracked 目录时，git clean -fd
    删除其中条目报 Permission denied 而整体失败 → 任务重试耗尽。
    修复后：尽力 Python 层删除，仍删不掉的降级警告继续，不阻塞任务。
    """

    def _mk_root_leftover(self, repo: Path) -> Path:
        """模拟 root 残留：untracked 目录 + 无写权限（等价非本进程用户属主）。"""
        leftover = repo / "ephemeral_root_bak_20260812"
        leftover.mkdir()
        (leftover / "generated.cc").write_text("stale", encoding="utf-8")
        symlinks = leftover / ".plugin_symlinks"
        symlinks.mkdir()
        (symlinks / "window_manager").symlink_to("/root/.pub-cache/never-exists")
        leftover.chmod(0o555)  # 无写权限：git clean 删除内部条目 Permission denied
        return leftover

    def test_clean_permission_denied_does_not_fail_task(self, executor, tmp_path):
        """无写权限残留导致 clean 权限失败时，prepare_workspace 应成功并清理干净。

        修复前：git clean -fd exit 1 → ExecutorError → 任务失败重试耗尽；
        修复后：残留属主为本进程用户（chmod 可恢复权限）→ Python 层删除
        干净 → 任务正常继续。
        """
        repo = _make_local_repo(tmp_path)
        leftover = self._mk_root_leftover(repo)
        try:
            workdir, _env = executor.prepare_workspace(_repo_dict(str(repo)))
            assert Path(workdir) == repo
            assert not leftover.exists(), "可恢复权限的残留应被清理干净"
        finally:
            # 修复后残留可能已被删除，chmod 需容错
            if leftover.exists():
                leftover.chmod(0o755)  # 恢复权限，避免 tmp_path 清理失败

    def test_clean_permission_denied_unrecoverable_warns_and_continues(
            self, executor, tmp_path, monkeypatch):
        """残留属主非本进程用户（chmod 也失败）时，降级警告继续不阻塞任务。

        修复前：git clean 权限失败 → ExecutorError → 任务失败；
        修复后：残留删不掉（需 root 权限），记录警告后继续执行，
        由用户手动清理，任务不再因此失败。
        """
        repo = _make_local_repo(tmp_path)
        leftover = self._mk_root_leftover(repo)
        real_chmod = os.chmod

        def fake_chmod(path, *args, **kwargs):
            # 模拟 root 属主：对残留目录恢复权限也失败（真实场景 EACCES）
            if Path(path) == leftover:
                raise PermissionError(13, "Permission denied")
            return real_chmod(path, *args, **kwargs)

        monkeypatch.setattr("botler.executor.os.chmod", fake_chmod)
        try:
            workdir, _env = executor.prepare_workspace(_repo_dict(str(repo)))
            assert Path(workdir) == repo
            # 残留仍在（进程无权限删除），但任务不再因此失败
            assert leftover.exists(), "root 属主残留删不掉时应原样保留"
        finally:
            real_chmod(leftover, 0o755)  # 恢复权限，避免 tmp_path 清理失败

    def test_clean_non_permission_error_still_raises(self, executor, tmp_path,
                                                     monkeypatch):
        """非权限类的 git clean 失败保持原行为：仍抛 ExecutorError。

        宽容处理只针对 Permission denied（外部环境污染），其余错误
        （如磁盘故障、git 损坏）不能静默吞掉。
        """
        repo = _make_local_repo(tmp_path)
        real_git = executor._git

        def fake_git(workdir, *args, **kwargs):
            if args and args[0] == "clean":
                raise ExecutorError("git clean 失败 (exit 1): fatal: index corrupted")
            return real_git(workdir, *args, **kwargs)

        monkeypatch.setattr(executor, "_git", fake_git)
        with pytest.raises(ExecutorError):
            executor.prepare_workspace(_repo_dict(str(repo)))
