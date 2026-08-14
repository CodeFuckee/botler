"""本地 git 仓库 remote 解析模块测试（git remote -v 输出 → remote 列表）。"""

import subprocess

import pytest

from botler.git_remote import (
    NoGitRemoteError,
    list_local_remotes,
    mask_url_token,
    parse_git_remote_output,
    parse_remote_url,
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


class TestParseRemoteUrl:
    """remote URL 凭据解析（issue #60）：从 URL 提取 per-repo token 与 host。"""

    def test_https_url_with_token(self):
        info = parse_remote_url(
            "https://agent:glpat-xyz@gitlab.example.com:509/group/project.git")
        assert info["scheme"] == "https"
        assert info["host"] == "gitlab.example.com:509"
        assert info["username"] == "agent"
        assert info["token"] == "glpat-xyz"

    def test_https_url_without_port(self):
        info = parse_remote_url(
            "https://agent:tok-1@gitlab.example.com/group/project.git")
        assert info["host"] == "gitlab.example.com"
        assert info["token"] == "tok-1"

    def test_url_without_credentials(self):
        info = parse_remote_url("https://gitlab.example.com/group/project.git")
        assert info["scheme"] == "https"
        assert info["host"] == "gitlab.example.com"
        assert info["username"] is None
        assert info["token"] is None

    def test_username_only_no_token(self):
        """只有用户名没有密码（如 https://agent@host/...）：token 为 None。"""
        info = parse_remote_url("https://agent@gitlab.example.com/group/project.git")
        assert info["username"] == "agent"
        assert info["token"] is None

    def test_urlencoded_token_unquoted(self):
        """token 经 URL 编码时解码还原（git 凭据 URL 常见写法）。"""
        info = parse_remote_url(
            "https://agent:glpat%2Dabc%40def@gitlab.example.com/group/project.git")
        assert info["token"] == "glpat-abc@def"

    def test_token_containing_at_sign(self):
        """token 内含 @ 时只按最后一个 @ 分割 host。"""
        info = parse_remote_url(
            "https://agent:tok@with@sign@gitlab.example.com/group/project.git")
        assert info["token"] == "tok@with@sign"
        assert info["host"] == "gitlab.example.com"

    def test_token_containing_colon(self):
        """token 内含冒号时只按第一个冒号分割 username。"""
        info = parse_remote_url(
            "https://agent:tok:with:colon@gitlab.example.com/group/project.git")
        assert info["username"] == "agent"
        assert info["token"] == "tok:with:colon"

    def test_scp_like_url_has_no_token(self):
        """scp-like（git@host:path）与 ssh 形态：无内嵌 token。"""
        assert parse_remote_url("git@gitlab.example.com:group/project.git")["token"] is None
        assert parse_remote_url("ssh://git@gitlab.example.com/group/project.git")["token"] is None

    def test_empty_and_invalid(self):
        assert parse_remote_url("")["token"] is None
        assert parse_remote_url("not a url")["token"] is None
        assert parse_remote_url(None)["token"] is None


class TestMaskUrlToken:
    """仓库 URL 脱敏（issue #60）：token 不出现在展示的 URL 上。"""

    def test_masks_token(self):
        assert mask_url_token(
            "https://agent:glpat-secret@gitlab.example.com:509/group/project.git") == \
            "https://agent:***@gitlab.example.com:509/group/project.git"

    def test_masks_urlencoded_token(self):
        assert mask_url_token(
            "https://agent:glpat%2Dsecret@gitlab.example.com/group/project.git") == \
            "https://agent:***@gitlab.example.com/group/project.git"

    def test_clean_url_unchanged(self):
        url = "https://gitlab.example.com/group/project.git"
        assert mask_url_token(url) == url

    def test_username_only_unchanged(self):
        """只有用户名没有 token：无需脱敏。"""
        url = "https://agent@gitlab.example.com/group/project.git"
        assert mask_url_token(url) == url

    def test_idempotent_on_masked_url(self):
        """已脱敏的 URL 再脱敏结果不变（防重复处理）。"""
        masked = "https://agent:***@gitlab.example.com/group/project.git"
        assert mask_url_token(masked) == masked

    def test_scp_like_unchanged(self):
        url = "git@gitlab.example.com:group/project.git"
        assert mask_url_token(url) == url

    def test_empty_and_none(self):
        assert mask_url_token("") == ""
        assert mask_url_token(None) == ""
