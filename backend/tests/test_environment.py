"""本地环境检测模块测试（issue #22）：工具安装检测 / 版本解析 / 最新版本查询。"""

from types import SimpleNamespace

import pytest

from botler import environment


class TestParseVersion:
    """版本号解析：从 --version 输出中提取首个 x.y.z。"""

    def test_plain_version(self):
        assert environment.parse_version("1.2.3") == "1.2.3"

    def test_v_prefix(self):
        assert environment.parse_version("v22.0.0") == "22.0.0"

    def test_text_prefix(self):
        assert environment.parse_version("Python 3.12.4") == "3.12.4"

    def test_git_style(self):
        assert environment.parse_version("git version 2.43.0") == "2.43.0"

    def test_docker_style(self):
        assert environment.parse_version("Docker version 27.0.0, build abc123") == "27.0.0"

    def test_uv_style(self):
        assert environment.parse_version("uv 0.4.0") == "0.4.0"

    def test_multi_line(self):
        assert environment.parse_version("foo\nclaude version 1.2.3\n") == "1.2.3"

    def test_no_version(self):
        assert environment.parse_version("unknown command") is None

    def test_empty_and_none(self):
        assert environment.parse_version("") is None
        assert environment.parse_version(None) is None

    def test_four_segments_takes_first_three(self):
        assert environment.parse_version("1.2.3.4") == "1.2.3"


def _tool(key="claude", name="Claude Code", command="claude"):
    return {
        "key": key, "name": name, "command": command,
        "version_args": ["--version"], "latest_source": None,
    }


class TestDetectTool:
    """单工具安装检测：which 定位 + --version 读取。"""

    def test_installed_with_version(self, monkeypatch):
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="claude 1.2.3\n", stderr=""),
        )
        result = environment.detect_tool(_tool())
        assert result["installed"] is True
        assert result["version"] == "1.2.3"

    def test_not_installed(self, monkeypatch):
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: None)
        result = environment.detect_tool(_tool())
        assert result["installed"] is False
        assert result["version"] is None

    @pytest.mark.parametrize("exc", [
        SimpleNamespace(returncode=1, stdout="", stderr="boom"),      # 非零退出
        TimeoutError(),                                                # 命令超时
        OSError("exec failed"),                                        # 执行异常
    ])
    def test_version_failure_keeps_installed(self, monkeypatch, exc):
        """which 找到但版本读取失败 → 已安装但版本未知，不抛异常。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: "/usr/bin/claude")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: (_ for _ in ()).throw(exc) if isinstance(exc, Exception) else exc,
        )
        result = environment.detect_tool(_tool())
        assert result["installed"] is True
        assert result["version"] is None

    def test_version_timeout(self, monkeypatch):
        """subprocess.TimeoutExpired → 已安装但版本未知。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: "/usr/bin/claude")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: (_ for _ in ()).throw(subprocess_timeout_expired()),
        )
        result = environment.detect_tool(_tool())
        assert result["installed"] is True
        assert result["version"] is None


def subprocess_timeout_expired():
    import subprocess
    return subprocess.TimeoutExpired(cmd=["claude"], timeout=10)


class TestHermesTool:
    """hermes 检测（issue #48）：TOOLS 清单配置 + 真实版本输出格式解析。"""

    def _hermes_tool(self):
        return next(t for t in environment.TOOLS if t["key"] == "hermes")

    def test_hermes_in_tools_list(self):
        """TOOLS 清单包含 hermes 项且配置正确。"""
        tool = self._hermes_tool()
        assert tool["name"] == "Hermes Agent"
        assert tool["command"] == "hermes"
        assert tool["version_args"] == ["--version"]
        # hermes 为 git 安装的内部工具，无 npm/GitHub 公开发布源
        assert tool["latest_source"] is None

    def test_hermes_real_version_output(self):
        """hermes --version 真实输出 "Hermes Agent v0.20.0 (2026.8.3)" →
        提取 "0.20.0"（而非括号内的日期 "2026.8.3"）。"""
        assert environment.parse_version("Hermes Agent v0.20.0 (2026.8.3)") == "0.20.0"

    def test_hermes_detect_installed(self, monkeypatch):
        """which 找到 hermes → 已安装且版本解析正确。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="Hermes Agent v0.20.0 (2026.8.3)\n", stderr=""),
        )
        result = environment.detect_tool(self._hermes_tool())
        assert result["installed"] is True
        assert result["version"] == "0.20.0"

    def test_hermes_not_installed(self, monkeypatch):
        """which 找不到 hermes → 未安装。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: None)
        result = environment.detect_tool(self._hermes_tool())
        assert result["installed"] is False
        assert result["version"] is None

    def test_hermes_no_latest_source(self, monkeypatch):
        """hermes 无发布源 → 不查网络，latest/up_to_date 为 None。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="Hermes Agent v0.20.0\n", stderr=""),
        )
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: pytest.fail("不应发请求"))
        result = environment._inspect_tool(self._hermes_tool(), cmd_timeout=10, net_timeout=8)
        assert result["latest"] is None
        assert result["up_to_date"] is None

    def test_full_flow_includes_hermes(self, monkeypatch):
        """整体检测结果包含 hermes 项。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="Hermes Agent v0.20.0\n", stderr=""),
        )
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"version": "2.0.0"}))
        tools = {t["key"]: t for t in environment.detect_local_environment()["tools"]}
        assert "hermes" in tools
        assert tools["hermes"]["installed"] is True
        assert tools["hermes"]["version"] == "0.20.0"


class TestFetchLatest:
    """最新版本查询：npm registry / GitHub API，网络失败返回 None。"""

    def test_npm_success(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"version": "1.4.0"}))
        tool = _tool(); tool["latest_source"] = "npm"; tool["latest_pkg"] = "@anthropic-ai/claude-code"
        assert environment.fetch_latest(tool) == "1.4.0"

    def test_github_success_strips_v(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"tag_name": "v2.43.1"}))
        tool = _tool(key="gh", name="GitHub CLI", command="gh")
        tool["latest_source"] = "github"; tool["latest_repo"] = "cli/cli"
        assert environment.fetch_latest(tool) == "2.43.1"

    def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=404, json=lambda: {}))
        tool = _tool(); tool["latest_source"] = "npm"; tool["latest_pkg"] = "aider-chat"
        assert environment.fetch_latest(tool) is None

    def test_network_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError("timeout")))
        tool = _tool(); tool["latest_source"] = "npm"; tool["latest_pkg"] = "aider-chat"
        assert environment.fetch_latest(tool) is None

    def test_invalid_json_returns_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("bad json"))))
        tool = _tool(); tool["latest_source"] = "npm"; tool["latest_pkg"] = "aider-chat"
        assert environment.fetch_latest(tool) is None

    def test_missing_version_field_returns_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"foo": "bar"}))
        tool = _tool(); tool["latest_source"] = "npm"; tool["latest_pkg"] = "aider-chat"
        assert environment.fetch_latest(tool) is None

    def test_no_latest_source_returns_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: pytest.fail("不应发请求"))
        tool = _tool()  # latest_source=None
        assert environment.fetch_latest(tool) is None

    def test_unknown_source_returns_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: pytest.fail("不应发请求"))
        tool = _tool(); tool["latest_source"] = "unknown"
        assert environment.fetch_latest(tool) is None


class TestDetectEnvironment:
    """整体检测：并发检测全部工具，合并安装/版本/最新版本结果。"""

    def test_full_flow(self, monkeypatch):
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: None if cmd == "docker" else f"/usr/bin/{cmd}",
        )
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="1.0.0\n", stderr=""),
        )
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"version": "2.0.0"}))
        result = environment.detect_local_environment()
        tools = {t["key"]: t for t in result["tools"]}
        # 已安装 + 有最新版本（2.0.0 > 1.0.0）→ 落后
        assert tools["claude"]["installed"] is True
        assert tools["claude"]["version"] == "1.0.0"
        assert tools["claude"]["latest"] == "2.0.0"
        assert tools["claude"]["up_to_date"] is False
        # 未安装 → 不查最新版本
        assert tools["docker"]["installed"] is False
        assert tools["docker"]["version"] is None
        assert tools["docker"]["latest"] is None
        assert tools["docker"]["up_to_date"] is None
        # 基础工具（无最新版本源）→ latest 为 None
        assert tools["git"]["installed"] is True
        assert tools["git"]["latest"] is None
        assert tools["git"]["up_to_date"] is None
        # 元信息
        assert result["hostname"]
        assert result["platform"]
        assert result["detected_at"]

    def test_up_to_date_when_versions_match(self, monkeypatch):
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="1.2.3\n", stderr=""),
        )
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"version": "1.2.3"}))
        tools = {t["key"]: t for t in environment.detect_local_environment()["tools"]}
        assert tools["claude"]["up_to_date"] is True

    def test_network_failure_latest_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="1.2.3\n", stderr=""),
        )
        monkeypatch.setattr("botler.environment.httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError("timeout")))
        tools = {t["key"]: t for t in environment.detect_local_environment()["tools"]}
        assert tools["claude"]["latest"] is None
        assert tools["claude"]["up_to_date"] is None
