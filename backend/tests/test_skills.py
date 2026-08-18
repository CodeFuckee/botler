"""技能目录管理核心测试（issue #282）。

覆盖：
- engine_skills_roots：内置引擎（claude / hermes / dsh）部署机惯例路径
  解析（Path.home / HERMES_HOME / DSH_HOME），外部插件 skills_dir 声明
  （str / list），未声明返回空；
- iter_skills：含 SKILL.md 的目录即技能（扁平 + 嵌套），frontmatter
  description 解析，字典序排序，根不存在返回空；
- list_md_files：递归枚举 md 文件（相对路径，排序）；
- safe_md_path：合法路径解析；../ 穿越 / 绝对路径 / 非 md / 符号链接
  逃逸 / 空路径拒绝；
- read/write_md_file：读写往返、父目录自动创建、缺文件抛
  FileNotFoundError、超大文件拒绝；
- resolve_skill_dir：合法技能解析、非法名（/ \\ ..）/ 不存在返回 None。
"""

import os
from pathlib import Path

import pytest

from botler import skills
from botler.plugins import ExecutorPlugin


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """固定 Path.home() 到临时目录，避免污染真实 HOME。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def make_skill(root: Path, name: str, description: str = "",
               extra_md: list[str] | None = None) -> Path:
    """在技能根下建一个技能目录（SKILL.md + 可选附加 md 文件）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {Path(name).name}\ndescription: {description}\n---\n"
    (d / "SKILL.md").write_text(front + f"# {name}\n", encoding="utf-8")
    for rel in extra_md or []:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n", encoding="utf-8")
    return d


class TestEngineSkillsRoots:
    def test_claude_root(self, fake_home):
        assert skills.engine_skills_roots("claude") == [
            fake_home / ".claude" / "skills"]

    def test_hermes_root_default(self, fake_home, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        assert skills.engine_skills_roots("hermes") == [
            fake_home / ".hermes" / "skills"]

    def test_hermes_root_env_override(self, fake_home, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(fake_home / "custom-hermes"))
        assert skills.engine_skills_roots("hermes") == [
            fake_home / "custom-hermes" / "skills"]

    def test_dsh_roots(self, fake_home, monkeypatch):
        monkeypatch.delenv("DSH_HOME", raising=False)
        assert skills.engine_skills_roots("dsh") == [
            fake_home / ".dsh" / "skills", fake_home / ".agents" / "skills"]

    def test_dsh_roots_env_override(self, fake_home, monkeypatch):
        monkeypatch.setenv("DSH_HOME", str(fake_home / "dsh-custom"))
        assert skills.engine_skills_roots("dsh") == [
            fake_home / "dsh-custom" / "skills", fake_home / ".agents" / "skills"]

    def test_external_plugin_str_skills_dir(self, fake_home):
        plugin = ExecutorPlugin(name="my_engine",
                                description="外部执行引擎")
        plugin.skills_dir = str(fake_home / "my-skills")
        assert skills.engine_skills_roots("my_engine", plugin) == [
            fake_home / "my-skills"]

    def test_external_plugin_list_skills_dir(self, fake_home):
        plugin = ExecutorPlugin(name="my_engine")
        plugin.skills_dir = [str(fake_home / "a"), str(fake_home / "b")]
        assert skills.engine_skills_roots("my_engine", plugin) == [
            fake_home / "a", fake_home / "b"]

    def test_external_plugin_tilde_expand(self, fake_home, monkeypatch):
        monkeypatch.setenv("HOME", str(fake_home))
        plugin = ExecutorPlugin(name="my_engine")
        plugin.skills_dir = "~/skills"
        assert skills.engine_skills_roots("my_engine", plugin) == [
            fake_home / "skills"]

    def test_external_plugin_without_skills_dir(self):
        plugin = ExecutorPlugin(name="my_engine")
        assert skills.engine_skills_roots("my_engine", plugin) == []

    def test_unknown_builtin_empty(self):
        assert skills.engine_skills_roots("nope") == []


class TestIterSkills:
    def test_flat_and_nested(self, tmp_path):
        root = tmp_path / "skills"
        make_skill(root, "animate", "做动画")
        make_skill(root, "code-testing", "测试")
        make_skill(root, "nested/group/spike", "嵌套技能")
        out = skills.iter_skills(root)
        assert [s["name"] for s in out] == [
            "animate", "code-testing", "nested/group/spike"]
        assert out[0]["description"] == "做动画"
        assert out[2]["description"] == "嵌套技能"
        # 无 SKILL.md 的目录不算技能
        (root / "plain-dir").mkdir()
        assert len(skills.iter_skills(root)) == 3

    def test_missing_root_empty(self, tmp_path):
        assert skills.iter_skills(tmp_path / "no-such") == []

    def test_description_fallback_empty(self, tmp_path):
        root = tmp_path / "skills"
        d = root / "no-frontmatter"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# 无 frontmatter\n", encoding="utf-8")
        assert skills.iter_skills(root)[0]["description"] == ""

    def test_skill_paths_absolute(self, tmp_path):
        root = tmp_path / "skills"
        make_skill(root, "animate")
        (skill,) = skills.iter_skills(root)
        assert Path(skill["path"]).name == "animate"
        assert skill["path"].endswith("animate")


class TestListMdFiles:
    def test_recursive_sorted(self, tmp_path):
        root = tmp_path / "skills"
        d = make_skill(root, "demo", extra_md=[
            "README.md", "docs/guide.markdown", "docs/api.md", "notes.txt"])
        files = skills.list_md_files(d)
        assert files == ["README.md", "SKILL.md", "docs/api.md",
                         "docs/guide.markdown"]
        # 非 md 不出现
        assert "notes.txt" not in files

    def test_missing_dir_empty(self, tmp_path):
        assert skills.list_md_files(tmp_path / "no-such") == []


class TestSafeMdPath:
    def test_valid(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        target = skills.safe_md_path(skill, "docs/api.md")
        assert target == (skill / "docs/api.md").resolve()

    def test_traversal_rejected(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        for bad in ("../evil.md", "a/../../evil.md", "../demo2/x.md"):
            with pytest.raises(ValueError):
                skills.safe_md_path(skill, bad)

    def test_absolute_rejected(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        with pytest.raises(ValueError):
            skills.safe_md_path(skill, str(tmp_path / "x.md"))

    def test_non_md_rejected(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        for bad in ("script.py", "data.json", "image.png", "noext"):
            with pytest.raises(ValueError):
                skills.safe_md_path(skill, bad)

    def test_empty_rejected(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        with pytest.raises(ValueError):
            skills.safe_md_path(skill, "")

    def test_symlink_escape_rejected(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        outside = tmp_path / "outside.md"
        outside.write_text("x", encoding="utf-8")
        (skill / "link.md").symlink_to(outside)
        with pytest.raises(ValueError):
            skills.safe_md_path(skill, "link.md")


class TestReadWriteMdFile:
    def test_roundtrip(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        size = skills.write_md_file(skill, "docs/new.md", "你好，技能")
        assert size == len("你好，技能".encode("utf-8"))
        assert skills.read_md_file(skill, "docs/new.md") == "你好，技能"

    def test_write_creates_parents(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        skills.write_md_file(skill, "a/b/c/deep.md", "# deep")
        assert (skill / "a/b/c/deep.md").is_file()

    def test_read_missing_raises(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        with pytest.raises(FileNotFoundError):
            skills.read_md_file(skill, "no-such.md")

    def test_write_too_large_rejected(self, tmp_path):
        skill = make_skill(tmp_path, "demo")
        with pytest.raises(ValueError):
            skills.write_md_file(skill, "big.md", "x" * (skills.MAX_MD_BYTES + 1))


class TestResolveSkillDir:
    def test_resolve(self, fake_home):
        make_skill(fake_home / ".claude" / "skills", "animate")
        d = skills.resolve_skill_dir("claude", "animate")
        assert d == (fake_home / ".claude" / "skills" / "animate").resolve()

    def test_invalid_names(self, tmp_path, fake_home):
        make_skill(fake_home / ".claude" / "skills", "animate")
        for bad in ("", ".", "..", "a/b", "a\\b", "../x"):
            assert skills.resolve_skill_dir("claude", bad) is None

    def test_missing_skill(self, fake_home):
        assert skills.resolve_skill_dir("claude", "no-such") is None

    def test_missing_root_returns_none(self, tmp_path, fake_home):
        assert skills.resolve_skill_dir("claude", "animate") is None

    def test_resolve_with_hermes_env(self, tmp_path, fake_home, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        make_skill(tmp_path / "hh" / "skills", "grill-me")
        d = skills.resolve_skill_dir("hermes", "grill-me")
        assert d == (tmp_path / "hh" / "skills" / "grill-me").resolve()
