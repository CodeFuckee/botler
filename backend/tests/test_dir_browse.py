"""目录浏览模块测试（list_subdirectories / resolve_default_path）。

供「本地文件夹方式添加仓库」的目录选择对话框使用：
前端浏览服务器文件系统时逐级获取子目录列表。
"""

import os

import pytest

import botler.dir_browse as dir_browse
from botler.dir_browse import DirBrowseError, list_subdirectories, resolve_default_path


def _names(result: dict) -> list[str]:
    return [d["name"] for d in result["subdirs"]]


class TestListSubdirectories:
    def test_lists_only_directories_sorted(self, tmp_path):
        (tmp_path / "zeta").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "file.txt").write_text("x")
        result = list_subdirectories(str(tmp_path))
        assert _names(result) == ["alpha", "zeta"]
        # 文件不进入列表
        assert all(d["name"] != "file.txt" for d in result["subdirs"])
        assert all(d["is_git"] is False for d in result["subdirs"])
        assert all(d["readable"] is True for d in result["subdirs"])
        assert all(d["path"].startswith(str(tmp_path) + os.sep) for d in result["subdirs"])

    def test_case_insensitive_sort(self, tmp_path):
        for name in ("B", "a", "c", "D"):
            (tmp_path / name).mkdir()
        # 不区分大小写排序: a < b < c < d
        assert _names(list_subdirectories(str(tmp_path))) == ["a", "B", "c", "D"]

    def test_git_repo_marked(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        plain = tmp_path / "plain"
        plain.mkdir()
        result = list_subdirectories(str(tmp_path))
        by_name = {d["name"]: d for d in result["subdirs"]}
        assert by_name["repo"]["is_git"] is True
        assert by_name["plain"]["is_git"] is False

    def test_git_file_worktree_marked(self, tmp_path):
        """git worktree 的 .git 是文件而非目录，同样应标记为仓库。"""
        repo = tmp_path / "worktree-repo"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /somewhere/else\n")
        result = list_subdirectories(str(tmp_path))
        by_name = {d["name"]: d for d in result["subdirs"]}
        assert by_name["worktree-repo"]["is_git"] is True

    def test_hidden_dirs_included(self, tmp_path):
        """隐藏目录由前端本地过滤，后端应原样返回。"""
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()
        assert ".hidden" in _names(list_subdirectories(str(tmp_path)))

    def test_pseudo_fs_filtered(self, tmp_path, monkeypatch):
        """浏览伪文件系统根目录本身应被拒绝（前端列表已过滤，不会进入）。"""
        pseudo = tmp_path / "pseudo"
        (pseudo / "data").mkdir(parents=True)
        monkeypatch.setattr(dir_browse, "_PSEUDO_FS_ROOTS", (str(pseudo),))
        with pytest.raises(DirBrowseError):
            list_subdirectories(str(pseudo))

    def test_pseudo_fs_nested_path_filtered(self, tmp_path, monkeypatch):
        """伪文件系统根目录下的深层路径同样被拒绝。"""
        pseudo = tmp_path / "proc"
        (pseudo / "1" / "maps").mkdir(parents=True)
        monkeypatch.setattr(dir_browse, "_PSEUDO_FS_ROOTS", (str(pseudo),))
        with pytest.raises(DirBrowseError):
            list_subdirectories(str(pseudo / "1"))

    def test_missing_path_raises(self, tmp_path):
        with pytest.raises(DirBrowseError):
            list_subdirectories(str(tmp_path / "nope"))

    def test_blank_path_raises(self):
        """空/空白路径不应悄悄解析到当前工作目录。"""
        with pytest.raises(DirBrowseError):
            list_subdirectories("")
        with pytest.raises(DirBrowseError):
            list_subdirectories("   ")

    def test_file_path_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(DirBrowseError):
            list_subdirectories(str(f))

    def test_empty_dir(self, tmp_path):
        result = list_subdirectories(str(tmp_path))
        assert result["subdirs"] == []

    def test_max_entries_truncated(self, tmp_path):
        for i in range(510):
            (tmp_path / f"dir-{i:03d}").mkdir()
        result = list_subdirectories(str(tmp_path))
        assert len(result["subdirs"]) == 500

    def test_unreadable_dir_marked(self, tmp_path, monkeypatch):
        (tmp_path / "locked").mkdir()
        real_access = os.access

        def fake_access(path, mode):
            if str(path).endswith("locked"):
                return False
            return real_access(path, mode)

        monkeypatch.setattr(dir_browse.os, "access", fake_access)
        result = list_subdirectories(str(tmp_path))
        by_name = {d["name"]: d for d in result["subdirs"]}
        assert by_name["locked"]["readable"] is False

    def test_symlink_dir_included(self, tmp_path):
        target = tmp_path / "real-dir"
        target.mkdir()
        link = tmp_path / "link-dir"
        link.symlink_to(target, target_is_directory=True)
        result = list_subdirectories(str(tmp_path))
        by_name = {d["name"]: d for d in result["subdirs"]}
        assert by_name["link-dir"]["is_git"] is False

    def test_parent_and_path_normalized(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        result = list_subdirectories(str(nested))
        assert result["path"] == str(nested)
        assert result["parent"] == str(tmp_path / "a")

    def test_dot_dot_escapes_resolved(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        # "sub/../sub" realpath 后仍是 sub，应成功浏览
        result = list_subdirectories(str(sub / ".." / "sub"))
        assert result["path"] == str(sub)


class TestListSubdirectoriesRoot:
    def test_root_parent_is_none(self):
        result = list_subdirectories("/")
        assert result["parent"] is None
        # 真实根目录下的标准目录应出现（CI 环境可读）
        assert "etc" in _names(result)


class TestResolveDefaultPath:
    """resolve_default_path：目录选择对话框的初始定位目录。

    优先级：配置值（~ 展开、相对路径基于服务器工作目录）→ 服务器用户主目录 → /。
    """

    def test_none_returns_home(self):
        assert resolve_default_path(None) == os.path.expanduser("~")

    def test_empty_returns_home(self):
        assert resolve_default_path("") == os.path.expanduser("~")
        assert resolve_default_path("   ") == os.path.expanduser("~")

    def test_tilde_alone_returns_home(self):
        assert resolve_default_path("~") == os.path.expanduser("~")

    def test_tilde_expanded_to_existing_subdir(self, tmp_path, monkeypatch):
        """配置为 ~/xxx 且存在时展开到该子目录（不依赖真实主目录）。"""
        sub = tmp_path / "projects"
        sub.mkdir()
        monkeypatch.setattr(dir_browse.os.path, "expanduser", lambda p: str(sub) if p.startswith("~") else p)
        assert resolve_default_path("~/projects") == str(sub)

    def test_absolute_path_kept(self, tmp_path):
        assert resolve_default_path(str(tmp_path)) == str(tmp_path)

    def test_relative_path_resolved_from_cwd(self, tmp_path, monkeypatch):
        """相对路径基于服务器工作目录解析。"""
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(tmp_path)
        assert resolve_default_path("sub") == str(sub)

    def test_missing_configured_path_falls_back_to_home(self):
        assert resolve_default_path("/nonexistent/xyz") == os.path.expanduser("~")

    def test_file_path_falls_back_to_home(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        assert resolve_default_path(str(f)) == os.path.expanduser("~")

    def test_home_unavailable_falls_back_to_root(self, monkeypatch):
        """主目录也不可用时回退根目录 /。"""
        monkeypatch.setattr(dir_browse.os.path, "expanduser", lambda p: "/nonexistent-home")
        assert resolve_default_path(None) == "/"
