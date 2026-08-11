"""本地 git 仓库 remote 解析模块测试（git remote -v 输出 → remote 列表）。"""

import subprocess

import pytest

from botler.git_remote import (
    NoGitRemoteError,
    list_local_remotes,
    parse_git_remote_output,
)


def _init_repo(path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)


class TestParseGitRemoteOutput:
    """git remote -v 输出解析（纯函数）。"""

    def test_origin_fetch_and_push_lines(self):
        text = (
            "origin\thttps://gitlab.example.com/group/project.git (fetch)\n"
            "origin\thttps://gitlab.example.com/group/project.git (push)\n"
        )
        assert parse_git_remote_output(text) == [
            {"name": "origin", "url": "https://gitlab.example.com/group/project.git"},
        ]

    def test_multiple_remotes(self):
        text = (
            "origin\thttps://gitlab.example.com/group/project.git (fetch)\n"
            "origin\thttps://gitlab.example.com/group/project.git (push)\n"
            "upstream\thttps://gitlab.example.com/upstream/project.git (fetch)\n"
            "upstream\thttps://gitlab.example.com/upstream/project.git (push)\n"
        )
        names = [r["name"] for r in parse_git_remote_output(text)]
        assert names == ["origin", "upstream"]

    def test_scp_like_ssh_url_preserved(self):
        text = (
            "origin\tgit@gitlab.example.com:group/project.git (fetch)\n"
            "origin\tgit@gitlab.example.com:group/project.git (push)\n"
        )
        assert parse_git_remote_output(text) == [
            {"name": "origin", "url": "git@gitlab.example.com:group/project.git"},
        ]

    def test_empty_output_raises(self):
        with pytest.raises(NoGitRemoteError):
            parse_git_remote_output("")

    def test_no_fetch_lines_raises(self):
        with pytest.raises(NoGitRemoteError):
            parse_git_remote_output("origin\thttps://x.git (push)\n")


class TestListLocalRemotes:
    def test_real_git_repo(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "remote", "add", "origin",
             "https://gitlab.example.com/group/project.git"],
            cwd=tmp_path, check=True)
        assert list_local_remotes(str(tmp_path)) == [
            {"name": "origin", "url": "https://gitlab.example.com/group/project.git"},
        ]

    def test_not_a_git_repo(self, tmp_path):
        with pytest.raises(NoGitRemoteError):
            list_local_remotes(str(tmp_path))

    def test_missing_path(self, tmp_path):
        with pytest.raises(NoGitRemoteError):
            list_local_remotes(str(tmp_path / "nope"))

    def test_git_repo_without_remote(self, tmp_path):
        _init_repo(tmp_path)
        with pytest.raises(NoGitRemoteError):
            list_local_remotes(str(tmp_path))
