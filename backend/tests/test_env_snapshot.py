"""任务执行环境快照模块测试（issue #276）：快照采集与序列化。

覆盖：config 关键项 hash 稳定性 / 平台版本读取 / 起始提交与分支采集 /
引擎版本检测 / 模型解析（dsh/hermes/claude）/ 序列化往返 / 采集失败
不影响任务执行（尽力而为，不抛异常）。
"""

import json
import subprocess
from types import SimpleNamespace


from botler import env_snapshot
from botler.env_snapshot import (
    SNAPSHOT_ERROR_MARKER, collect_env_snapshot, error_snapshot,
    parse_snapshot, serialize_snapshot,
)


def _fake_cfg(**overrides):
    """最小化 config 假对象（缺省字段按 None，与 ConfigManager 缺省行为对齐）。"""
    cfg = SimpleNamespace(
        engine="claude", claude_command="claude", claude_args=["-p"],
        task_timeout_seconds=1800, max_retries=2, max_concurrent_repos=3,
        issue_priority_labels=["bug", "test", "feature"],
        dsh_provider="deepseek-official", dsh_model="deepseek-v4-flash",
        dsh_reasoning_effort="", dsh_max_tokens=None,
        default_template="", resume_template="",
        pause_windows=[], pause_weekdays=[],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestConfigHash:
    """config 关键项 hash：确定性 + 配置变化时 hash 变化。"""

    def test_stable_across_calls(self):
        cfg = _fake_cfg()
        assert env_snapshot._config_hash(cfg) == env_snapshot._config_hash(cfg)

    def test_changes_when_key_changes(self):
        base = _fake_cfg()
        changed = _fake_cfg(engine="dsh")
        assert env_snapshot._config_hash(base) != env_snapshot._config_hash(changed)

    def test_missing_key_treated_as_none(self):
        """字段缺失不崩溃（getattr 缺省 None 参与 hash）。"""
        cfg = SimpleNamespace(engine="claude")
        h = env_snapshot._config_hash(cfg)
        assert isinstance(h, str) and len(h) == 64

    def test_hex_format(self):
        assert all(c in "0123456789abcdef" for c in env_snapshot._config_hash(_fake_cfg()))


class TestPlatformVersion:
    """平台版本读取（与前端 VersionBadge 同源 version.json）。"""

    def test_reads_dist_version_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BOTLER_DATA_DIR", raising=False)
        dist = tmp_path / "frontend" / "dist"
        dist.mkdir(parents=True)
        (dist / "version.json").write_text(
            json.dumps({"version": "1.2.3", "buildTime": "x"}), encoding="utf-8")
        monkeypatch.setattr(env_snapshot, "_PROJECT_ROOT", tmp_path)
        assert env_snapshot._platform_version() == "1.2.3"

    def test_reads_version_txt(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BOTLER_DATA_DIR", raising=False)
        data = tmp_path / "data"
        data.mkdir(parents=True)
        (data / "version.txt").write_text("2.0.5\n", encoding="utf-8")
        monkeypatch.setattr(env_snapshot, "_PROJECT_ROOT", tmp_path)
        assert env_snapshot._platform_version() == "2.0.5"

    def test_botler_data_dir_wins(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "persist"
        data_dir.mkdir()
        (data_dir / "version.txt").write_text("9.9.9\n", encoding="utf-8")
        monkeypatch.setattr(env_snapshot, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("BOTLER_DATA_DIR", str(data_dir))
        assert env_snapshot._platform_version() == "9.9.9"

    def test_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BOTLER_DATA_DIR", raising=False)
        monkeypatch.setattr(env_snapshot, "_PROJECT_ROOT", tmp_path)
        assert env_snapshot._platform_version() is None

    def test_invalid_json_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BOTLER_DATA_DIR", raising=False)
        dist = tmp_path / "frontend" / "dist"
        dist.mkdir(parents=True)
        (dist / "version.json").write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(env_snapshot, "_PROJECT_ROOT", tmp_path)
        assert env_snapshot._platform_version() is None


class TestGitInfo:
    """起始提交与分支采集（真实 git 仓库）。"""

    def test_real_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty",
                        "-m", "init"], check=True)
        info = env_snapshot._git_info(repo)
        assert "commit_sha" in info and len(info["commit_sha"]) == 40
        assert "branch" in info and info["branch"]  # master / main

    def test_commit_sha_matches_head(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty",
                        "-m", "init"], check=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True)
        assert env_snapshot._git_info(repo)["commit_sha"] == head.stdout.strip()

    def test_non_git_dir_returns_empty(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert env_snapshot._git_info(plain) == {}


class TestEngineVersion:
    """引擎版本检测（复用 environment.detect_tool，无网络查询）。"""

    def test_known_engine(self, monkeypatch):
        monkeypatch.setattr(
            "botler.env_snapshot.detect_tool",
            lambda tool, timeout: {"key": "claude", "version": "2.1.226",
                                   "installed": True})
        assert env_snapshot._engine_version("claude") == "2.1.226"

    def test_unknown_engine(self, monkeypatch):
        monkeypatch.setattr("botler.env_snapshot.detect_tool", lambda *a, **k: {})
        assert env_snapshot._engine_version("unknown-engine") is None

    def test_detect_failure_returns_none(self, monkeypatch):
        def boom(tool, timeout):
            raise RuntimeError("cli 不存在")
        monkeypatch.setattr("botler.env_snapshot.detect_tool", boom)
        assert env_snapshot._engine_version("claude") is None


class TestModelInfo:
    """实际模型解析：dsh 走平台配置，hermes/claude 走本机配置文件。"""

    def test_dsh_from_cfg(self):
        cfg = _fake_cfg(engine="dsh", dsh_model="deepseek-v4-flash",
                        dsh_provider="deepseek-official")
        info = env_snapshot._model_info("dsh", cfg)
        assert info == {"name": "deepseek-v4-flash", "provider": "deepseek-official"}

    def test_dsh_missing_model_empty(self):
        cfg = _fake_cfg(dsh_model="", dsh_provider="")
        assert env_snapshot._model_info("dsh", cfg) == {}

    def test_hermes_from_config_yaml(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".hermes").mkdir(parents=True)
        (home / ".hermes" / "config.yaml").write_text(
            "model:\n  default: deepseek-v4-flash\n  provider: deepseek\n",
            encoding="utf-8")
        monkeypatch.setattr("botler.env_snapshot.Path.home", lambda: home)
        assert env_snapshot._model_info("hermes", _fake_cfg()) == {
            "name": "deepseek-v4-flash", "provider": "deepseek"}

    def test_hermes_missing_config_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("botler.env_snapshot.Path.home",
                            lambda: tmp_path / "nohome")
        assert env_snapshot._model_info("hermes", _fake_cfg()) == {}

    def test_claude_from_settings_env(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_MODEL": "deepseek-v4-pro"}}),
            encoding="utf-8")
        monkeypatch.setattr("botler.env_snapshot.Path.home", lambda: home)
        assert env_snapshot._model_info("claude", _fake_cfg()) == {
            "name": "deepseek-v4-pro"}

    def test_claude_top_level_model(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4"}), encoding="utf-8")
        monkeypatch.setattr("botler.env_snapshot.Path.home", lambda: home)
        assert env_snapshot._model_info("claude", _fake_cfg()) == {
            "name": "claude-sonnet-4"}

    def test_claude_missing_settings_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("botler.env_snapshot.Path.home",
                            lambda: tmp_path / "nohome")
        assert env_snapshot._model_info("claude", _fake_cfg()) == {}


class TestSerializeParse:
    """快照序列化与反序列化。"""

    def test_round_trip(self):
        snap = {"engine": {"name": "claude", "version": "2.1.226"},
                "git": {"branch": "main", "commit_sha": "a" * 40},
                "config_hash": "abc"}
        assert parse_snapshot(serialize_snapshot(snap)) == snap

    def test_parse_empty_returns_none(self):
        assert parse_snapshot(None) is None
        assert parse_snapshot("") is None

    def test_parse_invalid_returns_none(self):
        assert parse_snapshot("{broken") is None
        assert parse_snapshot("[1,2]") is None  # 非 dict 返回 None

    def test_serialize_utf8(self):
        snap = {"error": "环境快照获取失败"}
        text = serialize_snapshot(snap)
        assert "环境快照获取失败" in text  # ensure_ascii=False，中文可读


class TestCollectSnapshot:
    """整体采集：组合子模块 + 失败容忍（不抛异常）。"""

    def test_full_snapshot(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty",
                        "-m", "init"], check=True)
        monkeypatch.setattr(
            "botler.env_snapshot.detect_tool",
            lambda tool, timeout: {"version": "1.0.0", "installed": True})
        monkeypatch.setattr("botler.env_snapshot._platform_version",
                            lambda: "1.0.289")
        cfg = _fake_cfg()
        snap = collect_env_snapshot("claude", repo, cfg)
        assert snap["engine"] == {"name": "claude", "version": "1.0.0"}
        assert snap["git"]["branch"]
        assert len(snap["git"]["commit_sha"]) == 40
        assert snap["platform"] == {"version": "1.0.289"}
        assert snap["config_hash"] == env_snapshot._config_hash(cfg)
        assert snap["captured_at"]

    def test_partial_failure_no_raise(self, tmp_path, monkeypatch):
        """单项采集失败只影响对应字段，不抛异常（尽力而为）。"""
        def boom(tool, timeout):
            raise RuntimeError("detect 失败")
        monkeypatch.setattr("botler.env_snapshot.detect_tool", boom)
        monkeypatch.setattr("botler.env_snapshot._platform_version",
                            lambda: (_ for _ in ()).throw(RuntimeError("读版本失败")))
        snap = collect_env_snapshot("claude", tmp_path / "no-such-dir", _fake_cfg())
        assert isinstance(snap, dict)
        assert snap["engine"] == {"name": "claude"}  # 版本缺省，不抛
        assert "config_hash" in snap  # 其余字段照常采集

    def test_git_failure_empty(self, tmp_path):
        snap = collect_env_snapshot("claude", tmp_path / "no-such-dir", _fake_cfg())
        assert "git" not in snap  # 目录不存在 → git 字段缺省，不抛异常

    def test_unknown_engine(self, tmp_path):
        snap = collect_env_snapshot("weird", tmp_path, _fake_cfg())
        assert snap["engine"] == {"name": "weird"}


class TestErrorSnapshot:
    """采集失败标记（前端显示「环境快照获取失败」）。"""

    def test_marker_and_timestamp(self):
        snap = error_snapshot()
        assert snap["error"] == SNAPSHOT_ERROR_MARKER
        assert snap["captured_at"]

    def test_serializable(self):
        text = serialize_snapshot(error_snapshot())
        assert parse_snapshot(text)["error"] == SNAPSHOT_ERROR_MARKER
