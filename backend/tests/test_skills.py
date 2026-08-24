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


import sys
from pathlib import Path

import pytest

from botler import skills
from botler.plugins import ExecutorPlugin


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """固定 Path.home() 到临时目录，并清除 HERMES_HOME / DSH_HOME 环境变量，
    避免污染真实 HOME 与真实部署机技能目录（_hermes_home / _dsh_home
    优先读环境变量，未清除会把技能根解析到真实 ~/.hermes、~/.dsh）。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("DSH_HOME", raising=False)
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
    @pytest.mark.skipif(sys.platform == "win32", reason="Windows Path.expanduser 用 USERPROFILE 而非 HOME，~ 展开语义不同（issue #469）")

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


class TestSyncSkills:
    """技能同步（issue #328）：合并全部执行引擎技能去重后，复制到各引擎技能根目录。

    规则（用户已确认）：
    - 合并所有 executor 引擎技能，同名保留引擎注册顺序第一个版本，其余去重；
    - 去重后复制到每个引擎的全部技能根目录（方案A）；
    - 目标已存在同名技能跳过不覆盖；缺失技能根目录自动创建。
    """

    @staticmethod
    def _engines():
        """构造参与同步的内置三引擎插件（注入式，避免污染全局插件注册表）。"""
        return [ExecutorPlugin(name="claude"), ExecutorPlugin(name="hermes"),
                ExecutorPlugin(name="dsh")]

    def test_merge_dedupe_and_copy(self, fake_home):
        # claude：animate；hermes：animate（同名不同内容，应去重）+ grill-me；dsh 根缺失
        make_skill(fake_home / ".claude" / "skills", "animate", "claude 版")
        make_skill(fake_home / ".hermes" / "skills", "animate", "hermes 版")
        make_skill(fake_home / ".hermes" / "skills", "grill-me", "拷问设计")
        result = skills.sync_skills_to_engines(self._engines())
        s = result["summary"]
        assert s["merged"] == 2     # animate（claude 版）+ grill-me（hermes）
        assert s["deduped"] == 1    # hermes 的 animate 重复被去重
        assert s["copied"] == 5     # claude 根 +1、dsh 两个缺失根各 +2
        assert s["skipped"] == 3    # claude 根 animate、hermes 根两个
        assert s["failed"] == 0
        # 去重保留引擎注册顺序第一个版本（claude 的 animate）
        assert [m["name"] for m in result["merged"]] == ["animate", "grill-me"]
        assert result["merged"][0]["engine"] == "claude"
        assert result["deduped"] == [{
            "name": "animate", "engine": "hermes",
            "root": str(fake_home / ".hermes" / "skills"),
            "path": str(fake_home / ".hermes" / "skills" / "animate")}]
        # dsh 缺失根自动创建，复制的是 claude 版 animate
        dsh_animate = fake_home / ".dsh" / "skills" / "animate" / "SKILL.md"
        assert dsh_animate.is_file()
        assert "claude 版" in dsh_animate.read_text(encoding="utf-8")
        # hermes 已有同名技能未覆盖（跳过）
        hermes_animate = fake_home / ".hermes" / "skills" / "animate" / "SKILL.md"
        assert "hermes 版" in hermes_animate.read_text(encoding="utf-8")

    def test_auto_create_missing_roots(self, fake_home):
        make_skill(fake_home / ".claude" / "skills", "animate")
        skills.sync_skills_to_engines(self._engines())
        for root in (fake_home / ".hermes" / "skills",
                     fake_home / ".dsh" / "skills",
                     fake_home / ".agents" / "skills"):
            assert (root / "animate" / "SKILL.md").is_file()

    def test_skip_existing_target_keeps_content(self, fake_home):
        make_skill(fake_home / ".claude" / "skills", "code-testing", "claude 版")
        make_skill(fake_home / ".hermes" / "skills", "code-testing", "hermes 版")
        result = skills.sync_skills_to_engines(self._engines())
        hermes = next(t for t in result["targets"] if t["engine"] == "hermes")
        assert "code-testing" in hermes["skipped"]
        assert "code-testing" not in hermes["added"]
        content = fake_home / ".hermes" / "skills" / "code-testing" / "SKILL.md"
        assert "hermes 版" in content.read_text(encoding="utf-8")

    def test_second_run_idempotent(self, fake_home):
        make_skill(fake_home / ".claude" / "skills", "animate")
        first = skills.sync_skills_to_engines(self._engines())
        second = skills.sync_skills_to_engines(self._engines())
        assert first["summary"]["copied"] > 0
        assert first["summary"]["skipped"] == 1   # 源引擎自身根已有 animate
        assert second["summary"]["copied"] == 0   # 已全部就位，重复调用零新增
        assert second["summary"]["skipped"] == 4  # 4 个技能根全部跳过

    def test_nested_skill_preserved(self, fake_home):
        make_skill(fake_home / ".claude" / "skills", "nested/group/spike")
        skills.sync_skills_to_engines(self._engines())
        assert (fake_home / ".hermes" / "skills" / "nested" / "group"
                / "spike" / "SKILL.md").is_file()

    def test_external_plugin_skills_dir_participates(self, fake_home):
        ext = ExecutorPlugin(name="ext_engine")
        ext.skills_dir = str(fake_home / "ext-skills")
        make_skill(fake_home / "ext-skills", "research")
        result = skills.sync_skills_to_engines(self._engines() + [ext])
        assert "research" in [m["name"] for m in result["merged"]]
        # research 复制到 claude 根；ext 自身根已存在跳过
        assert (fake_home / ".claude" / "skills" / "research"
                / "SKILL.md").is_file()
        ext_target = next(t for t in result["targets"]
                          if t["engine"] == "ext_engine")
        assert "research" in ext_target["skipped"]

    def test_plugin_without_skills_dir_ignored(self, fake_home):
        make_skill(fake_home / ".claude" / "skills", "animate")
        ext = ExecutorPlugin(name="ext_engine")
        result = skills.sync_skills_to_engines(self._engines() + [ext])
        # ext 无技能目录：不参与合并、也不产生同步目标
        assert all(t["engine"] != "ext_engine" for t in result["targets"])
        assert [m["name"] for m in result["merged"]] == ["animate"]

    def test_empty_pool(self, fake_home):
        result = skills.sync_skills_to_engines(self._engines())
        assert result["ok"] is True
        assert result["summary"]["merged"] == 0
        assert result["summary"]["copied"] == 0
        assert result["merged"] == []
        assert result["targets"] == []

    def test_copy_error_recorded(self, fake_home):
        # 目标路径被损坏符号链接占用 → copytree 失败记录 error，不中断整体同步
        make_skill(fake_home / ".claude" / "skills", "animate")
        bad = fake_home / ".hermes" / "skills" / "animate"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.symlink_to(fake_home / "no-such-target")
        result = skills.sync_skills_to_engines(self._engines())
        assert result["summary"]["failed"] == 1
        hermes = next(t for t in result["targets"] if t["engine"] == "hermes")
        assert hermes["errors"] and hermes["errors"][0]["name"] == "animate"
