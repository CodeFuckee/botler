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


# ---- issue #147：任务开始前校验当前分支切回默认主分支 + git pull 同步 ----

class TestPrepareWorkspaceDefaultBranchAndPull:
    """issue #147：模版库前两点「切回默认主分支 + git pull」下沉为代码。
    任务开始时平台自动：校验当前分支（非默认主分支 → checkout 切回）、
    git pull（--rebase）同步远端最新代码，agent 无需再自行执行。
    """

    @staticmethod
    def _make_repo_with_default(tmp_path: Path, branch: str = "main") -> tuple[Path, Path]:
        """创建远端 bare 仓库 + 工作仓库，默认分支为 branch（bare HEAD 指向它）。

        返回 (bare, repo)。与 _make_local_repo 的区别：支持任意默认分支名，
        便于验证「默认主分支不是 main」与「bare HEAD 指向不存在分支」场景。
        """
        bare = tmp_path / f"remote-{branch}.git"
        seed = tmp_path / f"seed-{branch}"
        repo = tmp_path / f"repo-{branch}"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", branch, str(seed)], check=True)
        (seed / "file.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=test@botler.local",
                        "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", branch],
                       check=True)
        subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD",
                        f"refs/heads/{branch}"], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        return bare, repo

    @staticmethod
    def _current_branch(repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()

    def test_switches_to_default_branch_when_on_other_branch(self, executor, tmp_path):
        """当前在非默认分支时，prepare 应自动切回默认主分支。"""
        _bare, repo = self._make_repo_with_default(tmp_path, branch="main")
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "dev"],
                       check=True)
        assert self._current_branch(repo) == "dev"
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "main"

    def test_uses_non_main_default_branch(self, executor, tmp_path):
        """默认主分支不是 main（如 trunk）时，应切到该分支而非硬编码 main。"""
        _bare, repo = self._make_repo_with_default(tmp_path, branch="trunk")
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "dev"],
                       check=True)
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "trunk"
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"

    def test_detached_head_reattaches_to_default_branch(self, executor, tmp_path):
        """detached HEAD 状态（rev-parse 输出 HEAD）应重新检出默认主分支。"""
        _bare, repo = self._make_repo_with_default(tmp_path, branch="main")
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", head], check=True)
        assert self._current_branch(repo) == "HEAD"
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "main"

    def test_pull_syncs_latest_remote_commit(self, executor, tmp_path):
        """远端默认主分支有新提交时，prepare 的 git pull 应同步最新提交。"""
        bare, repo = self._make_repo_with_default(tmp_path, branch="main")
        executor.prepare_workspace(_repo_dict(str(repo)))
        # 远端 seed 仓库新增提交并推送
        seed = tmp_path / "seed-main"
        (seed / "file.txt").write_text("hello v2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=test@botler.local",
                        "commit", "-q", "-m", "v2"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"],
                       check=True)
        # 第二次 prepare 应把远端新提交同步进工作区
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello v2\n"
        assert self._current_branch(repo) == "main"

    def test_pull_invoked_with_rebase(self, executor, tmp_path):
        """prepare 必须显式执行 git pull --rebase（模版前两点下沉为代码）。"""
        _bare, repo = self._make_repo_with_default(tmp_path, branch="main")
        calls: list[tuple] = []
        original = executor._git

        def recording_git(workdir, *args, env=None, timeout=300):
            calls.append(args)
            return original(workdir, *args, env=env, timeout=timeout)

        executor._git = recording_git
        try:
            executor.prepare_workspace(_repo_dict(str(repo)))
        finally:
            executor._git = original
        assert any(a[0] == "pull" and a[1] == "--rebase" for a in calls), \
            f"prepare 未执行 git pull --rebase: {calls!r}"

    def test_head_symref_missing_branch_falls_back_to_main(self, executor, tmp_path):
        """bare HEAD 指向不存在的分支（init --bare 默认 master）时回退已有 main。"""
        bare = tmp_path / "remote-git.git"
        seed = tmp_path / "seed-git"
        repo = tmp_path / "repo-git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        # init --bare 默认 HEAD → refs/heads/master（不存在），只推送 main
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "f.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=t@b", "commit", "-q", "-m", "v1"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        # 克隆仓库不含 main 分支（远端 HEAD 悬空）→ 先建一个 dev 分支
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "dev"],
                       check=True)
        workdir, _env = executor.prepare_workspace(_repo_dict(str(repo)))
        assert Path(workdir) == repo
        assert self._current_branch(repo) == "main"

    # ---- issue #148：远端默认主分支本地跟踪引用缺失（单分支克隆等） ----

    @staticmethod
    def _make_single_branch_remote(tmp_path: Path) -> tuple[Path, Path]:
        """创建带 main + dev 两个分支的远端 bare 仓库 + dev 单分支克隆。

        返回 (bare, repo)。克隆使用 --single-branch --branch dev，fetch
        refspec 只覆盖 refs/heads/dev，本地永远不会出现 refs/remotes/origin/
        main——即使远端默认分支是 main。用于复现 issue #148：任务 #249
        （graph2plan，125#38）工作区当前分支 master ≠ 远端默认 main，且
        origin/main 跟踪引用缺失 → checkout -B main --track origin/main
        报 "fatal: 'origin/main' is not a commit"。
        """
        bare = tmp_path / "remote-single.git"
        seed = tmp_path / "seed-single"
        repo = tmp_path / "repo-single"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "file.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=test@botler.local",
                        "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "main"],
                       check=True)
        # 再推送一个 dev 分支：单分支克隆只拉取 dev，形成受限 fetch refspec
        subprocess.run(["git", "-C", str(seed), "checkout", "-q", "-b", "dev"],
                       check=True)
        (seed / "dev.txt").write_text("dev\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=test@botler.local",
                        "commit", "-q", "-m", "dev"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dev"],
                       check=True)
        # 远端默认分支指向 main（服务端权威 HEAD）
        subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD",
                        "refs/heads/main"], check=True)
        subprocess.run(["git", "clone", "-q", "--single-branch", "--branch", "dev",
                        str(bare), str(repo)], check=True)
        # 复现前提：本地确实不存在 origin/main 跟踪引用
        check = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             "refs/remotes/origin/main"],
            capture_output=True)
        assert check.returncode != 0, "复现前提不成立：origin/main 不应存在"
        return bare, repo

    def test_single_branch_clone_switches_to_default_branch(self, executor, tmp_path):
        """单分支克隆（受限 fetch refspec）缺默认分支跟踪引用时切回不失败。

        issue #148 复现：任务 #249 工作区当前分支非默认主分支，且远端默认
        主分支（main）未在本地拉取过 → 修复前 checkout -B main --track
        origin/main 报 "'origin/main' is not a commit"（exit 128）→ 任务
        重试耗尽失败。修复后应先显式拉取该分支补齐跟踪引用再切回。
        """
        _bare, repo = self._make_single_branch_remote(tmp_path)
        assert self._current_branch(repo) == "dev"
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "main"
        # 切回后跟踪引用已补齐，工作区内容为远端 main 快照
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"
        assert not (repo / "dev.txt").exists()

    def test_already_on_default_branch_with_missing_tracking_ref(self, executor, tmp_path):
        """本地已在默认分支名但跟踪引用缺失时，reset --hard 不应失败。

        单分支克隆 dev 后手工建同名的本地 main 分支（无上游）：当前分支已
        是 main，_checkout_default_branch 提前返回，但后续 reset --hard
        origin/main 仍依赖跟踪引用——修复前报 'ambiguous argument'。修复后
        无论当前分支是否已是默认分支，都先补齐跟踪引用。
        """
        _bare, repo = self._make_single_branch_remote(tmp_path)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"],
                       check=True)
        assert self._current_branch(repo) == "main"
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "main"
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"

# ---- issue #148 强化：默认主分支解析不硬编码 main（master 默认分支场景） ----

class TestPrepareWorkspaceDefaultBranchResolution:
    """issue #148 用户复测仍失败（任务 #249）：怀疑目标仓库默认主分支是
    master 而非 main。复现：`git ls-remote --symref` 探测失败（网络/认证
    抖动）且本地缺 `refs/remotes/origin/HEAD`（手工加 remote 的仓库常见）
    时，旧解析链一路回退到硬编码 `"main"`，而远端只有 master → checkout /
    fetch main 必然失败。修复后逐级降级探测，全程不硬编码 main：
    `ls-remote --symref`（服务端权威）→ `git remote show`（HEAD branch:）
    → 本地跟踪引用（origin/HEAD 符号引用 → main → master → 字典序）。
    """

    @staticmethod
    def _make_master_only_repo(tmp_path: Path) -> tuple[Path, Path]:
        """远端只有 master 分支（默认主分支 master）的裸仓库 + 工作仓库。

        返回 (bare, repo)。与 TestPrepareWorkspaceDefaultBranchAndPull 的
        _make_repo_with_default 的区别：远端仅存在 master 一个分支——用户
        怀疑的任务 #249 目标仓库形态（若解析链硬编码 main，必失败）。
        """
        bare = tmp_path / "remote-master.git"
        seed = tmp_path / "seed-master"
        repo = tmp_path / "repo-master"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "master", str(seed)], check=True)
        (seed / "file.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=test@botler.local",
                        "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "master"],
                       check=True)
        subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD",
                        "refs/heads/master"], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        return bare, repo

    @staticmethod
    def _drop_origin_head(repo: Path) -> None:
        """删除本地 refs/remotes/origin/HEAD（手工加 remote 的仓库常缺该引用）。

        git fetch --prune 不会重建它（本地实测验证），删除后本地再无
        origin/HEAD 兜底，旧代码会一路回退到硬编码 "main"。
        """
        subprocess.run(["git", "-C", str(repo), "remote", "set-head", "origin", "-d"],
                       check=True)

    @staticmethod
    def _current_branch(repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()

    @staticmethod
    def _patch_git_probe_failure(monkeypatch, fail_remote_show: bool = False) -> None:
        """让 git ls-remote（及可选 git remote show）探测命令返回失败。

        模拟远端探测命令的网络/认证异常（exit 128），其余 git 命令
        （fetch / checkout / reset / pull 等）原样执行。
        """
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
                if "ls-remote" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 128, "",
                        "fatal: unable to access 'http://remote/': Could not resolve host")
                if fail_remote_show and len(cmd) >= 3 \
                        and cmd[1] == "remote" and cmd[2] == "show":
                    return subprocess.CompletedProcess(
                        cmd, 128, "",
                        "fatal: unable to access 'http://remote/': Could not resolve host")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_master_default_not_hardcoded_when_lsremote_fails(self, executor, tmp_path,
                                                             monkeypatch):
        """ls-remote 探测失败且本地缺 origin/HEAD 时不能硬编码 main。

        用户怀疑任务 #249 目标仓库默认主分支是 master：远端只有 master，
        ls-remote 探测抖动失败 → 旧解析链兜底硬编码 "main" → 后续 checkout /
        fetch main 必然失败（任务重试耗尽）。修复后应经 git remote show /
        本地跟踪引用解析出 master，prepare 成功且工作区停在 master。
        """
        _bare, repo = self._make_master_only_repo(tmp_path)
        self._drop_origin_head(repo)
        self._patch_git_probe_failure(monkeypatch)
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "master"
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"

    def test_resolve_falls_back_to_local_tracking_refs(self, executor, tmp_path,
                                                      monkeypatch):
        """ls-remote 与 git remote show 都失败时，用本地跟踪引用兜底解析。

        远端彻底不可达（探测命令全部失败）时不能回退硬编码 main，应扫描
        本地 refs/remotes/origin/* 跟踪分支（fetch --prune 已同步），按
        main → master → 字典序 取实际存在的分支。
        """
        _bare, repo = self._make_master_only_repo(tmp_path)
        self._drop_origin_head(repo)
        self._patch_git_probe_failure(monkeypatch, fail_remote_show=True)
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert self._current_branch(repo) == "master"
        assert (repo / "file.txt").read_text(encoding="utf-8") == "hello\n"


# ---- issue #147 补充：git pull 拉取冲突时保留现场交由 agent 手工合并 ----

class TestPrepareWorkspacePullConflict:
    """issue #147 补充需求「如果拉取代码的时候出现了冲突，让 agent 来进行
    合并」：prepare_workspace 的 git pull --rebase 遇到合并冲突时不抛错，
    保留冲突现场并登记工作区，_build_prompt 据此追加手工合并指引。
    """

    @staticmethod
    def _make_remote_repo(tmp_path: Path) -> tuple[Path, Path]:
        """创建远端 bare 仓库 + 工作仓库（默认分支 main，含 init 提交）。

        返回 (bare, repo)。与 TestPrepareWorkspaceDefaultBranchAndPull 的
        _make_repo_with_default 等价，但用独立目录名避免用例间共享状态。
        """
        bare = tmp_path / "remote-conf.git"
        seed = tmp_path / "seed-conf"
        repo = tmp_path / "repo-conf"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "file.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                        "-c", "user.email=test@botler.local",
                        "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "main"],
                       check=True)
        subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD",
                        "refs/heads/main"], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
        return bare, repo

    @staticmethod
    def _issue() -> dict:
        return {
            "title": "拉取冲突处理",
            "description": "拉取代码出现冲突时由 agent 手工合并",
            "web_url": "https://gitlab.example.com/group/project/-/issues/1",
            "project_id": 1,
            "iid": 1,
        }

    @staticmethod
    def _repo_with_template(repo: Path) -> dict:
        d = _repo_dict(str(repo))
        d["prompt_template"] = ""
        return d

    def test_rebase_conflict_keeps_workspace_and_marks(self, executor, tmp_path):
        """pull --rebase 真实冲突：prepare 不抛错、保留冲突现场并登记工作区，
        提示词追加手工合并指引。"""
        _bare, repo = self._make_remote_repo(tmp_path)
        original = executor._git

        def conflicting_pull(workdir, *args, env=None, timeout=300):
            if args and args[0] == "pull":
                # reset --hard 之后的干净工作区上制造真实 rebase 冲突：
                # 本地提交修改 file.txt + 远端新提交修改同一文件
                subprocess.run(["git", "-C", str(workdir), "config", "user.name",
                                "Test"], check=True)
                subprocess.run(["git", "-C", str(workdir), "config", "user.email",
                                "test@botler.local"], check=True)
                (Path(workdir) / "file.txt").write_text("local\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(workdir), "add", "."], check=True)
                subprocess.run(["git", "-C", str(workdir), "commit", "-q",
                                "-m", "local"], check=True)
                seed = Path(workdir).parent / "seed-conf"
                (seed / "file.txt").write_text("remote\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
                subprocess.run(["git", "-C", str(seed), "-c", "user.name=Test",
                                "-c", "user.email=test@botler.local",
                                "commit", "-q", "-m", "remote"], check=True)
                subprocess.run(["git", "-C", str(seed), "push", "-q", "origin",
                                "main"], check=True)
                result = subprocess.run(
                    ["git", "-C", str(workdir), "pull", "--rebase", "origin", "main"],
                    capture_output=True, text=True)
                assert result.returncode != 0, "测试前提：pull --rebase 应产生冲突"
                raise ExecutorError(
                    f"git pull 失败 (exit {result.returncode}): "
                    f"{(result.stderr or result.stdout).strip()[-500:]}")
            return original(workdir, *args, env=env, timeout=timeout)

        executor._git = conflicting_pull
        try:
            workdir, _env = executor.prepare_workspace(_repo_dict(str(repo)))
        finally:
            executor._git = original
        assert Path(workdir) == repo
        # 冲突现场保留：rebase 进行中 + 存在未合并路径
        assert (repo / ".git" / "rebase-merge").exists()
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "-u"],
                             capture_output=True, text=True, check=True).stdout
        assert out.strip(), "工作区应保留未合并路径供 agent 解决"
        # 工作区登记为拉取冲突，提示词追加解决指引
        assert repo in executor._pull_conflict_workdirs
        prompt = executor._build_prompt(self._repo_with_template(repo), self._issue())
        assert "工作区存在拉取冲突" in prompt
        assert "git rebase --continue" in prompt

    def test_non_conflict_pull_failure_still_raises(self, executor, tmp_path):
        """凭据/网络等非冲突失败照常抛 ExecutorError，不误判为拉取冲突。"""
        _bare, repo = self._make_remote_repo(tmp_path)
        original = executor._git

        def failing_pull(workdir, *args, env=None, timeout=300):
            if args and args[0] == "pull":
                raise ExecutorError(
                    "git pull 失败 (exit 128): remote: HTTP Basic: Access denied")
            return original(workdir, *args, env=env, timeout=timeout)

        executor._git = failing_pull
        try:
            with pytest.raises(ExecutorError):
                executor.prepare_workspace(_repo_dict(str(repo)))
        finally:
            executor._git = original
        assert repo not in executor._pull_conflict_workdirs

    def test_conflict_marker_cleared_after_clean_prepare(self, executor, tmp_path):
        """冲突登记后，下一次干净 prepare（pull 成功）应清除登记。"""
        _bare, repo = self._make_remote_repo(tmp_path)
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert repo not in executor._pull_conflict_workdirs
        # 直接模拟登记后干净重跑，验证 else 分支清除登记
        executor._pull_conflict_workdirs.add(repo)
        executor.prepare_workspace(_repo_dict(str(repo)))
        assert repo not in executor._pull_conflict_workdirs

    def test_is_pull_conflict_detects_unmerged_paths(self, executor, tmp_path):
        """工作区存在未合并路径时判定为冲突（即使错误文本不含冲突字样）。"""
        _bare, repo = self._make_remote_repo(tmp_path)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@b"],
                       check=True)
        # side 从 init 提交分出后改 file.txt，main 也改同一文件 → 真实冲突
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"],
                       check=True)
        (repo / "file.txt").write_text("theirs\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "theirs"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
        (repo / "file.txt").write_text("ours\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ours"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "merge", "--no-commit", "side"],
                       capture_output=True, check=False)
        exc = ExecutorError("git pull 失败 (exit 1): 与冲突无关的失败信息")
        assert executor._is_pull_conflict(repo, os.environ.copy(), exc) is True

    def test_is_pull_conflict_detects_untracked_overwrite_error(self, executor, tmp_path):
        """错误文本含「untracked 文件将被覆盖」时判定为拉取冲突（交由 agent 处理）。"""
        _bare, repo = self._make_remote_repo(tmp_path)
        exc = ExecutorError(
            "git pull 失败 (exit 1): error: The following untracked working tree "
            "files would be overwritten by merge:\n\tblocker.txt\n"
            "Please move or remove them before you merge. Aborting")
        assert executor._is_pull_conflict(repo, os.environ.copy(), exc) is True

    def test_prompt_without_conflict_has_no_handoff_section(self, executor, tmp_path):
        """无冲突时提示词不追加拉取冲突指引。"""
        _bare, repo = self._make_remote_repo(tmp_path)
        executor.prepare_workspace(_repo_dict(str(repo)))
        prompt = executor._build_prompt(self._repo_with_template(repo), self._issue())
        assert "工作区存在拉取冲突" not in prompt

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


# ---- issue #416：工作区 origin 仓库路径与配置大小写不一致 → agent 仓库锁定误判 ----

class TestPrepareWorkspaceOriginNormalize:
    """issue #416（诊断任务 #605）：local_path 工作区 origin 的仓库路径与配置
    url 大小写不一致（chenkaidi/Graph2plan vs chenkaidi/graph2plan）时，注入
    agent 提示词的仓库锁定自检是严格字符串比较（模板注入配置路径），GitLab
    项目路径实际大小写不敏感 → agent 误判「仓库不一致」终止任务，重试耗尽
    failed。prepare_workspace 应把 origin 的 path 规范化为配置 url 的 path
    （保留凭据与 host），保证自检通过；路径真实不同（不同仓库）不修改，
    留给 agent 自检按原规则拦截（安全护栏）。
    """

    @staticmethod
    def _origin_url(repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=True).stdout.strip()

    @staticmethod
    def _skip_network_git(executor, monkeypatch) -> None:
        """fetch/pull/reset/checkout/clean 变 no-op（测试 remote 不可达），其余原样。"""
        real_git = executor._git

        def fake_git(workdir, *args, env=None, timeout=300):
            if args and args[0] in ("fetch", "pull", "reset", "checkout", "clean"):
                return None
            return real_git(workdir, *args, env=env, timeout=timeout)

        monkeypatch.setattr(executor, "_git", fake_git)

    @staticmethod
    def _skip_remote_probe(monkeypatch) -> None:
        """ls-remote / git remote show 探测返回失败（远端不可达），其余命令透传。

        避免测试真实连网 gitlab.example.com（DNS 超时拖慢用例），默认分支
        解析降级走本地跟踪引用。
        """
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
                if "ls-remote" in cmd or (len(cmd) >= 3
                                          and cmd[1] == "remote" and cmd[2] == "show"):
                    return subprocess.CompletedProcess(
                        cmd, 128, "",
                        "fatal: unable to access 'https://gitlab.example.com/': "
                        "Could not resolve host")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_origin_case_mismatch_normalized_to_config_path(self, executor, tmp_path,
                                                            monkeypatch):
        """origin 大写 vs 配置小写：prepare 后 origin 规范化为配置路径，凭据保留。"""
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["url"] = "https://gitlab.example.com/group/graph2plan.git"
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "https://agent:tok@gitlab.example.com/group/Graph2plan.git"],
                       check=True)
        self._skip_network_git(executor, monkeypatch)
        self._skip_remote_probe(monkeypatch)
        executor.prepare_workspace(d)
        assert self._origin_url(repo) == \
            "https://agent:tok@gitlab.example.com/group/graph2plan.git"

    def test_origin_path_already_match_not_rewritten(self, executor, tmp_path,
                                                     monkeypatch):
        """origin 与配置路径完全一致：不修改 origin。"""
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["url"] = "https://gitlab.example.com/group/graph2plan.git"
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "https://agent:tok@gitlab.example.com/group/graph2plan.git"],
                       check=True)
        self._skip_network_git(executor, monkeypatch)
        self._skip_remote_probe(monkeypatch)
        executor.prepare_workspace(d)
        assert self._origin_url(repo) == \
            "https://agent:tok@gitlab.example.com/group/graph2plan.git"

    def test_origin_different_repo_path_not_rewritten(self, executor, tmp_path,
                                                      monkeypatch):
        """origin 指向不同仓库（真实不一致）：不修改，留给 agent 自检拦截。"""
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["url"] = "https://gitlab.example.com/group/graph2plan.git"
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "https://agent:tok@gitlab.example.com/group/other.git"],
                       check=True)
        self._skip_network_git(executor, monkeypatch)
        self._skip_remote_probe(monkeypatch)
        executor.prepare_workspace(d)
        assert self._origin_url(repo) == \
            "https://agent:tok@gitlab.example.com/group/other.git"

    def test_origin_ssh_form_skipped(self, executor, tmp_path, monkeypatch):
        """scp-like / ssh 形态 origin：跳过规范化，原样保留。"""
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["url"] = "https://gitlab.example.com/group/graph2plan.git"
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "git@gitlab.example.com:group/Graph2plan.git"], check=True)
        self._skip_network_git(executor, monkeypatch)
        self._skip_remote_probe(monkeypatch)
        executor.prepare_workspace(d)
        assert self._origin_url(repo) == "git@gitlab.example.com:group/Graph2plan.git"

    def test_origin_without_credentials_normalized(self, executor, tmp_path,
                                                   monkeypatch):
        """origin 无凭据：规范化 path 后仍不带 userinfo 段。"""
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["url"] = "https://gitlab.example.com/group/graph2plan.git"
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "https://gitlab.example.com/group/Graph2plan.git"], check=True)
        self._skip_network_git(executor, monkeypatch)
        self._skip_remote_probe(monkeypatch)
        executor.prepare_workspace(d)
        assert self._origin_url(repo) == \
            "https://gitlab.example.com/group/graph2plan.git"

    def test_origin_host_mismatch_skipped(self, executor, tmp_path, monkeypatch):
        """origin host 与配置 host 不同：跳过规范化（不越权修改）。"""
        repo = _make_local_repo(tmp_path)
        d = _repo_dict(str(repo))
        d["url"] = "https://gitlab.example.com/group/graph2plan.git"
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "https://agent:tok@other.example.com/group/Graph2plan.git"],
                       check=True)
        self._skip_network_git(executor, monkeypatch)
        self._skip_remote_probe(monkeypatch)
        executor.prepare_workspace(d)
        assert self._origin_url(repo) == \
            "https://agent:tok@other.example.com/group/Graph2plan.git"
