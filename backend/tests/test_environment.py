"""本地环境检测模块测试（issue #22）：工具安装检测 / 版本解析 / 最新版本查询。"""

import subprocess
import sys
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
    """hermes 检测（issue #48/#171）：SDK module 检测（run_agent 可导入）。

    issue #171 起 hermes 集成为 hermes-agent SDK（源码 editable 安装进
    botler venv，进程内调用 run_agent.AIAgent），检测与 dsh 引擎同模式
    （module 检测），不再走 hermes CLI which/--version。
    """

    def _hermes_tool(self):
        return next(t for t in environment.TOOLS if t["key"] == "hermes")

    def test_hermes_in_tools_list(self):
        """TOOLS 清单包含 hermes 项且配置正确（module 检测）。"""
        tool = self._hermes_tool()
        assert tool["name"] == "Hermes Agent SDK"
        assert tool["module"] == "run_agent"
        assert tool["pkg"] == "hermes-agent"
        # 无 npm/GitHub 公开发布源，不查最新版本（前端显示 "—"）
        assert tool["latest_source"] is None

    def test_hermes_detect_installed(self, monkeypatch):
        """run_agent 可导入 + pkg 版本 → 已安装且版本解析正确。"""
        monkeypatch.setattr("botler.environment.find_spec",
                            lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: "0.20.0")
        result = environment.detect_tool(self._hermes_tool())
        assert result["installed"] is True
        assert result["version"] == "0.20.0"  # parse_version 取 x.y.z

    def test_hermes_not_installed(self, monkeypatch):
        """run_agent 不可导入 → 未安装。"""
        monkeypatch.setattr("botler.environment.find_spec", lambda name: None)
        result = environment.detect_tool(self._hermes_tool())
        assert result["installed"] is False
        assert result["version"] is None

    def test_hermes_version_read_failure_keeps_installed(self, monkeypatch):
        """模块已装但版本读取失败 → installed=True 且 version=None。"""
        monkeypatch.setattr("botler.environment.find_spec",
                            lambda name: object())
        monkeypatch.setattr(
            "botler.environment.pkg_version",
            lambda name: (_ for _ in ()).throw(
                ModuleNotFoundError("no metadata")))
        result = environment.detect_tool(self._hermes_tool())
        assert result["installed"] is True
        assert result["version"] is None

    def test_hermes_no_latest_source(self, monkeypatch):
        """hermes 无发布源 → 不查网络，latest/up_to_date 为 None。"""
        monkeypatch.setattr("botler.environment.find_spec",
                            lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: "0.20.0")
        monkeypatch.setattr("botler.environment.httpx.get",
                            lambda *a, **k: pytest.fail("不应发请求"))
        result = environment._inspect_tool(self._hermes_tool(),
                                           cmd_timeout=10, net_timeout=8)
        assert result["installed"] is True
        assert result["latest"] is None
        assert result["up_to_date"] is None

    def test_full_flow_includes_hermes(self, monkeypatch):
        """整体检测结果包含 hermes 项（module 检测）。"""
        monkeypatch.setattr("botler.environment.find_spec",
                            lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: "0.20.0")
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



    def test_installable_flag_per_source(self, monkeypatch):
        """检测结果含 installable 标记：有发布源可安装，无发布源不可安装。"""
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: None if cmd == "docker" else f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="1.0.0\n", stderr=""))
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda *a, **k: SimpleNamespace(
                status_code=200, json=lambda: {"version": "2.0.0"}))
        tools = {t["key"]: t
                 for t in environment.detect_local_environment()["tools"]}
        # npm / pypi / github 发布源 → 可自动安装
        assert tools["claude"]["installable"] is True
        assert tools["dsh"]["installable"] is True
        assert tools["gh"]["installable"] is True
        # 无发布源 → 不可自动安装（docker/git/uv/hermes）
        assert tools["docker"]["installable"] is False
        assert tools["git"]["installable"] is False
        assert tools["uv"]["installable"] is False
        assert tools["hermes"]["installable"] is False


class TestDshDetection:
    """dsh 检测（issue #84）：pip 包检测（module 存在 + 版本 + PyPI 最新版）。"""

    def _dsh_tool(self):
        return next(t for t in environment.TOOLS if t["key"] == "dsh")

    def test_dsh_in_tools_list(self):
        """TOOLS 清单包含 dsh 项且配置正确（module 检测 + pypi 最新源）。"""
        tool = self._dsh_tool()
        assert tool["name"] == "DeepSeek Harness SDK"
        assert tool["module"] == "deepseek_harness"
        assert tool["latest_source"] == "pypi"
        assert tool["latest_pkg"] == "deepseek-harness-sdk"

    def test_dsh_detect_installed(self, monkeypatch):
        monkeypatch.setattr("botler.environment.find_spec", lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: "0.1.0rc6")
        result = environment.detect_tool(self._dsh_tool())
        assert result["installed"] is True
        # issue #470：pip 包版本保留完整预发布版本号（不截断 rc 后缀）
        assert result["version"] == "0.1.0rc6"

    def test_dsh_not_installed(self, monkeypatch):
        monkeypatch.setattr("botler.environment.find_spec", lambda name: None)
        result = environment.detect_tool(self._dsh_tool())
        assert result["installed"] is False
        assert result["version"] is None

    def test_dsh_version_read_failure_keeps_installed(self, monkeypatch):
        """模块已装但版本读取失败 → installed=True 且 version=None。"""
        monkeypatch.setattr("botler.environment.find_spec", lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: (_ for _ in ()).throw(
                                ModuleNotFoundError("no metadata")))
        result = environment.detect_tool(self._dsh_tool())
        assert result["installed"] is True
        assert result["version"] is None

    def test_dsh_latest_from_pypi(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get", lambda url, **kw: SimpleNamespace(
            status_code=200, json=lambda: {"info": {"version": "0.1.0"}}))
        assert environment.fetch_latest(self._dsh_tool()) == "0.1.0"

    def test_dsh_latest_network_failure_none(self, monkeypatch):
        monkeypatch.setattr("botler.environment.httpx.get",
                            lambda url, **kw: (_ for _ in ()).throw(OSError("timeout")))
        assert environment.fetch_latest(self._dsh_tool()) is None


class TestUpgradeTool:
    """工具升级（issue #465）：npm / pypi / gh 发布源分派与失败处理。"""

    def _npm_tool(self):
        tool = _tool()
        tool["latest_source"] = "npm"
        tool["latest_pkg"] = "@anthropic-ai/claude-code"
        return tool

    def test_npm_tool_upgrade_command(self, monkeypatch):
        """npm 工具：npm install -g <pkg>@latest。"""
        calls = {}
        monkeypatch.setattr("botler.environment.shutil.which",
                            lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: calls.update(cmd=cmd) or SimpleNamespace(
                returncode=0, stdout="added 1 package\n", stderr=""))
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": tool["key"],
                                      "installed": True, "version": "2.0.0"})
        result = environment.upgrade_tool("claude")
        assert result["upgraded"] is True
        assert result["version"] == "2.0.0"
        assert calls["cmd"] == [
            "/usr/bin/npm", "install", "-g", "@anthropic-ai/claude-code@latest"]

    def test_pypi_tool_upgrade_uses_current_interpreter(self, monkeypatch):
        """pypi 工具（dsh）：当前解释器 pip install -U --pre <pkg> -i <源>。"""
        calls = {}
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: calls.update(cmd=cmd) or SimpleNamespace(
                returncode=0, stdout="Successfully installed", stderr=""))
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "dsh",
                                      "installed": True, "version": "0.2.0"})
        # issue #470：已装版本落后于最新版本 → 继续执行升级命令
        monkeypatch.setattr("botler.environment.fetch_latest",
                            lambda tool, timeout=8: "9.9.9")
        result = environment.upgrade_tool("dsh")
        assert result["version"] == "0.2.0"
        assert calls["cmd"] == [
            __import__("sys").executable, "-m", "pip", "install", "-U",
            "--pre", "deepseek-harness-sdk",
            "-i", environment.DSH_INDEX_URL_DEFAULT]

    def test_npm_missing_bin_raises(self, monkeypatch):
        """npm 未安装 → UpgradeError（不执行任何命令）。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: None)
        with pytest.raises(environment.UpgradeError, match="未找到 npm"):
            environment.upgrade_tool("claude")

    def test_unknown_tool_raises(self):
        """未知工具 → UpgradeError。"""
        with pytest.raises(environment.UpgradeError, match="未知工具"):
            environment.upgrade_tool("no-such-tool")

    def test_no_upgrade_path_raises(self):
        """无发布源工具（git）→ UpgradeError。"""
        with pytest.raises(environment.UpgradeError, match="没有可用的自动升级途径"):
            environment.upgrade_tool("git")

    def test_nonzero_exit_raises(self, monkeypatch):
        """升级命令非零退出 → UpgradeError 携带退出码与输出。"""
        monkeypatch.setattr("botler.environment.shutil.which",
                            lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stdout="", stderr="EAI_AGAIN getaddrinfo"))
        with pytest.raises(environment.UpgradeError,
                           match="退出码 1.*EAI_AGAIN"):
            environment.upgrade_tool("claude")

    def test_timeout_raises(self, monkeypatch):
        """升级命令超时 → UpgradeError。"""
        monkeypatch.setattr("botler.environment.shutil.which",
                            lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd, 300)))
        with pytest.raises(environment.UpgradeError, match="超时"):
            environment.upgrade_tool("claude")

    def test_find_tool(self):
        """find_tool：命中返回清单项，未命中返回 None。"""
        assert environment.find_tool("claude")["latest_source"] == "npm"
        assert environment.find_tool("nope") is None


def _make_gh_tarball() -> bytes:
    """构造含 bin/gh 的最小 tar.gz（模拟 GitHub CLI release 资产）。"""
    import io as _io
    import tarfile as _tarfile
    buf = _io.BytesIO()
    data = b"fake-gh-binary"
    with _tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = _tarfile.TarInfo("gh_2.63.2_linux_amd64/bin/gh")
        info.size = len(data)
        tf.addfile(info, _io.BytesIO(data))
    return buf.getvalue()


class TestUpgradeGh:
    """gh 升级（issue #465）：GitHub release 二进制下载替换。"""

    def test_gh_binary_upgrade_replaces_executable(self, monkeypatch):
        """下载最新 release 二进制并原子替换当前 gh 可执行文件。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: "2.63.2")
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: "/usr/bin/gh" if cmd == "gh" else None)
        downloaded = {}
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda url, **kw: downloaded.update(url=url) or SimpleNamespace(
                status_code=200, content=_make_gh_tarball()))
        written = {}
        class _FakeFile:
            def __init__(self, path, mode):
                written["path"] = path
            def write(self, data):
                written["data"] = data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        monkeypatch.setattr("builtins.open", _FakeFile)
        monkeypatch.setattr("os.chmod", lambda *a, **k: None)
        replaced = {}
        monkeypatch.setattr(
            "os.replace", lambda src, dst: replaced.update(src=src, dst=dst))
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "gh",
                                      "installed": True, "version": "2.63.2"})
        result = environment.upgrade_tool("gh")
        assert result["version"] == "2.63.2"
        assert "gh_2.63.2_linux_amd64.tar.gz" in downloaded["url"]
        assert replaced["dst"] == "/usr/bin/gh"
        assert written["data"] == b"fake-gh-binary"

    def test_gh_non_linux_raises(self, monkeypatch):
        """非 Linux 平台 → UpgradeError（不下载不替换）。"""
        monkeypatch.setattr("botler.environment.sys.platform", "darwin")
        with pytest.raises(environment.UpgradeError, match="仅支持 Linux"):
            environment.upgrade_tool("gh")

    def test_gh_latest_query_failure_raises(self, monkeypatch):
        """查询 gh 最新版本失败 → UpgradeError。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: None)
        with pytest.raises(environment.UpgradeError, match="查询 gh 最新版本失败"):
            environment.upgrade_tool("gh")

    def test_gh_download_failure_raises(self, monkeypatch):
        """下载 gh 升级包 HTTP 错误 → UpgradeError。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: "2.63.2")
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: "/usr/bin/gh" if cmd == "gh" else None)
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda url, **kw: SimpleNamespace(status_code=404, content=b""))
        with pytest.raises(environment.UpgradeError, match="HTTP 404"):
            environment.upgrade_tool("gh")


class TestScheduleRestart:
    """延迟重启调度（issue #465）：进程内只调度一次。"""

    def test_schedule_once_per_process(self, monkeypatch):
        """同一进程内重复调度只生效一次（后续调用返回 False）。"""
        started = []
        class _FakeTimer:
            def __init__(self, delay, fn):
                self.delay = delay
                self.fn = fn
            def start(self):
                started.append((self.delay, self.fn))
        monkeypatch.setattr("botler.environment.threading.Timer", _FakeTimer)
        # 复位进程级全局标记，保证用例与执行顺序无关
        environment._restart_scheduled = False
        assert environment.schedule_restart(delay=2.0) is True
        assert environment.schedule_restart(delay=2.0) is False
        assert len(started) == 1
        assert started[0][0] == 2.0


class TestInstallTool:
    """工具安装（issue #468）：npm / pypi / gh 发布源分派与失败处理。"""

    def _npm_tool(self):
        tool = _tool()
        tool["latest_source"] = "npm"
        tool["latest_pkg"] = "@anthropic-ai/claude-code"
        return tool

    def test_npm_tool_install_command(self, monkeypatch):
        """npm 工具：npm install -g <pkg>@latest（安装前检测未安装）。"""
        calls = {}
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd == "npm" else None)
        detections = iter([
            {"key": "claude", "installed": False, "version": None},   # 安装前：未安装
            {"key": "claude", "installed": True, "version": "2.0.0"},  # 安装后重新检测
        ])
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: next(detections))
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: calls.update(cmd=cmd) or SimpleNamespace(
                returncode=0, stdout="added 1 package\n", stderr=""))
        result = environment.install_tool("claude")
        assert result["installed"] is True
        assert result["version"] == "2.0.0"
        assert calls["cmd"] == [
            "/usr/bin/npm", "install", "-g", "@anthropic-ai/claude-code@latest"]

    def test_pypi_tool_install_uses_current_interpreter(self, monkeypatch):
        """pypi 工具（dsh）：当前解释器 pip install <pkg>。"""
        calls = {}
        detections = iter([
            {"key": "dsh", "installed": False, "version": None},
            {"key": "dsh", "installed": True, "version": "0.1.0"},
        ])
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: next(detections))
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: calls.update(cmd=cmd) or SimpleNamespace(
                returncode=0, stdout="Successfully installed", stderr=""))
        result = environment.install_tool("dsh")
        assert result["version"] == "0.1.0"
        assert calls["cmd"] == [
            __import__("sys").executable, "-m", "pip", "install", "--pre",
            "deepseek-harness-sdk", "-i", environment.DSH_INDEX_URL_DEFAULT]

    def test_already_installed_raises(self, monkeypatch):
        """工具已安装 → InstallError（不重复安装，不执行任何命令）。"""
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "claude",
                                      "installed": True, "version": "1.0.0"})
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: pytest.fail("已安装时不应执行命令"))
        with pytest.raises(environment.InstallError, match="已安装"):
            environment.install_tool("claude")

    def test_unknown_tool_raises(self):
        """未知工具 → InstallError。"""
        with pytest.raises(environment.InstallError, match="未知工具"):
            environment.install_tool("no-such-tool")

    def test_no_install_path_raises(self, monkeypatch):
        """无发布源工具（git）→ InstallError。"""
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "git",
                                      "installed": False, "version": None})
        with pytest.raises(environment.InstallError,
                           match="没有可用的自动安装途径"):
            environment.install_tool("git")

    def test_npm_missing_bin_raises(self, monkeypatch):
        """npm 未安装 → InstallError（不执行任何命令）。"""
        monkeypatch.setattr("botler.environment.shutil.which", lambda cmd: None)
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "claude",
                                      "installed": False, "version": None})
        with pytest.raises(environment.InstallError, match="未找到 npm"):
            environment.install_tool("claude")

    def test_nonzero_exit_raises(self, monkeypatch):
        """安装命令非零退出 → InstallError 携带退出码与输出。"""
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd == "npm" else None)
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "claude",
                                      "installed": False, "version": None})
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stdout="", stderr="EAI_AGAIN getaddrinfo"))
        with pytest.raises(environment.InstallError,
                           match="退出码 1.*EAI_AGAIN"):
            environment.install_tool("claude")

    def test_timeout_raises(self, monkeypatch):
        """安装命令超时 → InstallError。"""
        monkeypatch.setattr(
            "botler.environment.shutil.which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd == "npm" else None)
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "claude",
                                      "installed": False, "version": None})
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd, 300)))
        with pytest.raises(environment.InstallError, match="超时"):
            environment.install_tool("claude")


class TestInstallGh:
    """gh 安装（issue #468）：GitHub release 二进制下载安装到 PATH 目录。"""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="gh 二进制安装目标 /usr/local/bin 为 Linux 专属，Windows 路径语义（反斜杠）不同，跳过（issue #468）")
    def test_gh_binary_install_to_usr_local_bin(self, monkeypatch):
        """下载最新 release 二进制并原子写入 /usr/local/bin/gh。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: "2.63.2")
        detections = iter([
            {"key": "gh", "installed": False, "version": None},
            {"key": "gh", "installed": True, "version": "2.63.2"},
        ])
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: next(detections))
        downloaded = {}
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda url, **kw: downloaded.update(url=url) or SimpleNamespace(
                status_code=200, content=_make_gh_tarball()))
        monkeypatch.setattr("botler.environment.os.path.isdir",
                            lambda p: p == "/usr/local/bin")
        monkeypatch.setattr("botler.environment.os.access", lambda p, m: True)
        written = {}
        class _FakeFile:
            def __init__(self, path, mode):
                written["path"] = path
            def write(self, data):
                written["data"] = data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        monkeypatch.setattr("builtins.open", _FakeFile)
        monkeypatch.setattr("os.chmod", lambda *a, **k: None)
        replaced = {}
        monkeypatch.setattr(
            "os.replace", lambda src, dst: replaced.update(src=src, dst=dst))
        result = environment.install_tool("gh")
        assert result["installed"] is True
        assert result["version"] == "2.63.2"
        assert "gh_2.63.2_linux_amd64.tar.gz" in downloaded["url"]
        assert replaced["dst"] == "/usr/local/bin/gh"
        assert written["data"] == b"fake-gh-binary"

    def test_gh_non_linux_raises(self, monkeypatch):
        """非 Linux 平台 → InstallError（不下载不写入）。"""
        monkeypatch.setattr("botler.environment.sys.platform", "darwin")
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "gh",
                                      "installed": False, "version": None})
        with pytest.raises(environment.InstallError, match="仅支持 Linux"):
            environment.install_tool("gh")

    def test_gh_latest_query_failure_raises(self, monkeypatch):
        """查询 gh 最新版本失败 → InstallError。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: None)
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "gh",
                                      "installed": False, "version": None})
        with pytest.raises(environment.InstallError, match="查询 gh 最新版本失败"):
            environment.install_tool("gh")

    def test_gh_download_failure_raises(self, monkeypatch):
        """下载 gh 安装包 HTTP 错误 → InstallError。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: "2.63.2")
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "gh",
                                      "installed": False, "version": None})
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda url, **kw: SimpleNamespace(status_code=404, content=b""))
        with pytest.raises(environment.InstallError, match="HTTP 404"):
            environment.install_tool("gh")

    def test_gh_install_dir_not_writable_raises(self, monkeypatch):
        """安装目录不可写 → InstallError。"""
        monkeypatch.setattr("botler.environment.sys.platform", "linux")
        monkeypatch.setattr(
            "botler.environment.fetch_latest",
            lambda tool, timeout=8: "2.63.2")
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "gh",
                                      "installed": False, "version": None})
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda url, **kw: SimpleNamespace(
                status_code=200, content=_make_gh_tarball()))
        monkeypatch.setattr("botler.environment.os.path.isdir", lambda p: True)
        monkeypatch.setattr("botler.environment.os.access", lambda p, m: False)
        with pytest.raises(environment.InstallError, match="不可写"):
            environment.install_tool("gh")


class TestDshPrereleaseRegression:
    """dsh 升级假成功回归（issue #470）：rc 预发布版版本判定与升级命令。

    现象：设置页升级 dsh 显示「升级成功」，重启后版本号不变。
    根因链条：
      1. detect 版本被 parse_version 截断（0.1.1rc1 → 0.1.1），与
         fetch_latest 完整版本比较永远不等 → 前端永远显示「可升级」；
      2. pypi 升级/安装命令不带 --pre、不带镜像源（默认清华源不同步
         rc 版），pip 静默 "already satisfied" 退出码 0 → 假成功。
    """

    def _dsh_tool(self):
        return next(t for t in environment.TOOLS if t["key"] == "dsh")

    def test_dsh_detect_keeps_prerelease_version(self, monkeypatch):
        """pip 包检测版本应保留 rc 后缀（0.1.1rc1 不应截断为 0.1.1）。"""
        monkeypatch.setattr("botler.environment.find_spec", lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: "0.1.1rc1")
        result = environment.detect_tool(self._dsh_tool())
        assert result["installed"] is True
        assert result["version"] == "0.1.1rc1"

    def test_dsh_up_to_date_when_latest_is_prerelease(self, monkeypatch):
        """已装 rc 版本等于 PyPI 最新 rc 时 up_to_date 必须为 True。"""
        monkeypatch.setattr("botler.environment.find_spec", lambda name: object())
        monkeypatch.setattr("botler.environment.pkg_version",
                            lambda name: "0.1.1rc1")
        monkeypatch.setattr(
            "botler.environment.httpx.get",
            lambda url, **kw: SimpleNamespace(
                status_code=200,
                json=lambda: {"info": {"version": "0.1.1rc1"}}))
        result = environment._inspect_tool(
            self._dsh_tool(), cmd_timeout=10, net_timeout=8)
        assert result["version"] == "0.1.1rc1"
        assert result["up_to_date"] is True

    def test_pypi_upgrade_command_includes_pre_and_env_mirror(self, monkeypatch):
        """pypi 升级命令必须 --pre + 显式镜像源（DSH_INDEX_URL 优先）。"""
        monkeypatch.setenv("DSH_INDEX_URL", "https://mirrors.example.com/simple")
        cmd = environment._upgrade_command(self._dsh_tool())
        assert "--pre" in cmd, "rc 预发布版升级必须允许预发布版本"
        assert "-i" in cmd, "升级必须显式指定镜像源（默认源不同步 rc 版）"
        assert any("mirrors.example.com" in c for c in cmd)

    def test_pypi_upgrade_command_default_mirror_is_aliyun(self, monkeypatch):
        """未配置 DSH_INDEX_URL 时缺省走阿里源（与部署基线一致）。"""
        monkeypatch.delenv("DSH_INDEX_URL", raising=False)
        cmd = environment._upgrade_command(self._dsh_tool())
        assert "--pre" in cmd
        assert "-i" in cmd
        assert any("mirrors.aliyun.com" in c for c in cmd)

    def test_pypi_install_command_includes_pre_and_mirror(self, monkeypatch):
        """pypi 安装命令必须 --pre + 显式镜像源。"""
        monkeypatch.delenv("DSH_INDEX_URL", raising=False)
        cmd = environment._install_command(self._dsh_tool())
        assert "--pre" in cmd, "rc 预发布版安装必须允许预发布版本"
        assert "-i" in cmd
        assert any("mirrors.aliyun.com" in c for c in cmd)

    def test_upgrade_already_up_to_date_skips_pip(self, monkeypatch):
        """已装版本等于 PyPI 最新版 → 返回 already_up_to_date，不执行 pip。"""
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "dsh",
                                      "installed": True,
                                      "version": "0.1.1rc1"})
        monkeypatch.setattr("botler.environment.fetch_latest",
                            lambda tool, timeout=8: "0.1.1rc1")
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: pytest.fail("已是最新时不应执行 pip 命令"))
        result = environment.upgrade_tool("dsh")
        assert result["upgraded"] is False
        assert result["already_up_to_date"] is True
        assert result["version"] == "0.1.1rc1"

    def test_upgrade_latest_query_failure_still_upgrades(self, monkeypatch):
        """最新版本查询失败（网络异常）不阻断升级，继续执行 pip。"""
        calls = {}
        monkeypatch.setattr("botler.environment.fetch_latest",
                            lambda tool, timeout=8: None)
        monkeypatch.setattr(
            "botler.environment.detect_tool",
            lambda tool, timeout=10: {"key": "dsh",
                                      "installed": True, "version": "0.1.0rc6"})
        monkeypatch.setattr(
            "botler.environment.subprocess.run",
            lambda cmd, **kw: calls.update(cmd=cmd) or SimpleNamespace(
                returncode=0, stdout="Successfully installed", stderr=""))
        result = environment.upgrade_tool("dsh")
        assert result["upgraded"] is True
        assert "--pre" in calls["cmd"]
