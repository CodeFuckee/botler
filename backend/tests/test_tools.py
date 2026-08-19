"""MCP 工具管理核心测试（issue #172）。

覆盖：
- validate_tool_def：合法定义归一；非法 name（空/特殊字符/超长）、
  非法 kind、stdio 缺 command、sse/http 缺 http(s) url、args/env 类型
  错误 / 超长字段拒绝；
- CRUD：创建 / 更新（部分字段 / 重名冲突 / 合并校验）/ 删除 / 启停、
  名称唯一；
- 内置市场：market_tools 清单、install_builtin（成功 / 未知 / 重名）；
- URL 导入：JSON 定义文件（单定义 / mcpServers 多定义 / 非法 JSON /
  非 JSON / 404）、Git 仓库（含 .mcp.json 的定义文件）、失败跳过；
- 远端市场索引：tools 数组 / 直接数组 / 非法格式、非法条目过滤、
  索引地址保存读取；
- mcp_servers_json：stdio → command/args/env、sse/http → url、停用过滤；
- write_workspace_mcp_config：写入 .mcp.json + .git/info/exclude 去重、
  无启用工具清理残留、工作区不存在报错。
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from botler import tools
from botler.database import Database


@pytest.fixture
def db(tmp_path):
    """临时 SQLite 数据库（tools 表随迁移 v20 创建）。"""
    return Database(str(tmp_path / "test.db"))


def make_definition(**overrides):
    """合法工具定义（默认 stdio）；overrides 覆盖字段。"""
    definition = {
        "name": "my-tool",
        "description": "示例工具",
        "kind": "stdio",
        "command": "python3",
        "args": ["-m", "demo"],
        "env": {"TOKEN": "abc"},
        "url": "",
    }
    definition.update(overrides)
    return definition


# ---- 校验 ----

class TestValidate:
    def test_valid_stdio(self):
        definition = make_definition()
        tools.validate_tool_def(definition)
        assert definition["name"] == "my-tool"
        assert definition["args"] == ["-m", "demo"]

    def test_valid_http(self):
        definition = make_definition(kind="http", url="https://x.example/mcp",
                                     command="")
        tools.validate_tool_def(definition)
        assert definition["kind"] == "http"

    def test_empty_name(self):
        with pytest.raises(ValueError, match="不能为空"):
            tools.validate_tool_def(make_definition(name=""))

    def test_name_illegal_chars(self):
        for bad in ("../evil", "a/b", "a b", "a.b", "工具", "a" * 101):
            with pytest.raises(ValueError):
                tools.validate_tool_def(make_definition(name=bad))

    def test_name_legal_chars(self):
        for ok in ("web-fetch", "web_fetch", "web2", "A", "a-b_c9"):
            tools.validate_tool_def(make_definition(name=ok))

    def test_bad_kind(self):
        with pytest.raises(ValueError, match="类型"):
            tools.validate_tool_def(make_definition(kind="webrpc"))

    def test_stdio_requires_command(self):
        with pytest.raises(ValueError, match="command"):
            tools.validate_tool_def(make_definition(command=""))

    def test_remote_requires_url(self):
        with pytest.raises(ValueError, match="url"):
            tools.validate_tool_def(make_definition(kind="sse", url=""))
        with pytest.raises(ValueError, match="url"):
            tools.validate_tool_def(make_definition(kind="http", url="ftp://x"))

    def test_args_env_type_errors(self):
        with pytest.raises(ValueError, match="args"):
            tools.validate_tool_def(make_definition(args="not-list"))
        with pytest.raises(ValueError, match="args"):
            tools.validate_tool_def(make_definition(args=[1, 2]))
        with pytest.raises(ValueError, match="env"):
            tools.validate_tool_def(make_definition(env=["a"]))
        with pytest.raises(ValueError, match="env"):
            tools.validate_tool_def(make_definition(env={"k": 1}))

    def test_desc_too_long(self):
        with pytest.raises(ValueError, match="描述"):
            tools.validate_tool_def(make_definition(description="x" * 501))


# ---- CRUD ----

class TestCrud:
    def test_create_and_get(self, db):
        tool = tools.create_tool(db, make_definition())
        assert tool["id"] > 0
        assert tool["name"] == "my-tool"
        assert tool["kind"] == "stdio"
        assert tool["args"] == ["-m", "demo"]
        assert tool["env"] == {"TOKEN": "abc"}
        assert tool["source"] == "custom"
        assert tool["enabled"] is True

    def test_duplicate_name_rejected(self, db):
        tools.create_tool(db, make_definition())
        with pytest.raises(ValueError, match="已存在"):
            tools.create_tool(db, make_definition())

    def test_list_order(self, db):
        tools.create_tool(db, make_definition(name="aaa"))
        tools.create_tool(db, make_definition(name="bbb"))
        assert [t["name"] for t in tools.list_tools(db)] == ["aaa", "bbb"]

    def test_update_partial(self, db):
        tool = tools.create_tool(db, make_definition())
        updated = tools.update_tool(db, tool["id"], {"description": "新描述",
                                                     "enabled": False})
        assert updated["description"] == "新描述"
        assert updated["enabled"] is False
        assert updated["command"] == "python3"  # 未更新字段保留

    def test_update_rename_conflict(self, db):
        a = tools.create_tool(db, make_definition(name="aaa"))
        tools.create_tool(db, make_definition(name="bbb"))
        with pytest.raises(ValueError, match="已存在"):
            tools.update_tool(db, a["id"], {"name": "bbb"})

    def test_update_merge_validation(self, db):
        """更新只传 command='' 时应整体校验失败（stdio 需要 command）。"""
        tool = tools.create_tool(db, make_definition())
        with pytest.raises(ValueError, match="command"):
            tools.update_tool(db, tool["id"], {"command": ""})

    def test_update_missing(self, db):
        with pytest.raises(ValueError, match="不存在"):
            tools.update_tool(db, 999, {"description": "x"})

    def test_delete(self, db):
        tool = tools.create_tool(db, make_definition())
        assert tools.delete_tool(db, tool["id"]) is True
        assert tools.delete_tool(db, tool["id"]) is False
        assert tools.list_tools(db) == []

    def test_set_enabled(self, db):
        tool = tools.create_tool(db, make_definition())
        disabled = tools.set_tool_enabled(db, tool["id"], False)
        assert disabled["enabled"] is False
        with pytest.raises(ValueError, match="不存在"):
            tools.set_tool_enabled(db, 999, True)


# ---- 内置市场 ----

class TestMarket:
    def test_market_has_tools(self):
        market = tools.market_tools()
        assert len(market) >= 3
        assert {t["name"] for t in market} >= {"web-fetch", "filesystem"}

    def test_install_builtin(self, db):
        installed = tools.install_builtin(db, "web-fetch")
        assert installed["source"] == "builtin"
        assert installed["command"] == "npx"
        assert tools.list_tools(db)[0]["name"] == "web-fetch"

    def test_install_unknown(self, db):
        with pytest.raises(ValueError, match="不存在"):
            tools.install_builtin(db, "no-such-tool")

    def test_install_duplicate(self, db):
        tools.install_builtin(db, "web-fetch")
        with pytest.raises(ValueError, match="已安装"):
            tools.install_builtin(db, "web-fetch")
    def test_market_image_parse_entry(self):
        """内置市场应含 Image-Parse-MCP（issue #327）：stdio + uv 启动 +
        环境变量模板 + git_url 来源。"""
        entry = next(t for t in tools.market_tools() if t["name"] == "image-parse")
        assert entry["kind"] == "stdio"
        assert entry["command"] == "uv"
        assert entry["args"][0] == "run"
        assert "image-parse-mcp" in entry["args"]
        assert set(entry["env"]) >= {"IMAGE_PARSE_API_KEY",
                                     "IMAGE_PARSE_BASE_URL", "IMAGE_PARSE_MODEL"}
        assert entry["git_url"] == "https://github.com/1617110693/Image-Parse-MCP.git"

    def test_install_builtin_git_tool_clones(self, db, tmp_path):
        """Git 市场工具安装（issue #327）：自动浅克隆仓库到 tools_dir，
        args 中 {repo_dir} 占位符替换为实际克隆路径。"""
        repo = make_git_repo(tmp_path, "image-parse-src", {
            "pyproject.toml": '[project]\nname = "image-parse-mcp"\n',
            "src/image_parse/server.py": 'print("ok")\n',
        })
        market = {t["name"]: t for t in tools.DEFAULT_MARKET_TOOLS}
        orig = market["image-parse"]["git_url"]
        try:
            market["image-parse"]["git_url"] = f"file://{repo}"
            tools_dir = tmp_path / "tools"
            installed = tools.install_builtin(db, "image-parse",
                                              tools_dir=tools_dir)
            clone_dir = tools_dir / "image-parse"
            assert (clone_dir / ".git").is_dir(), "应浅克隆仓库到本地工具目录"
            assert (clone_dir / "pyproject.toml").is_file()
            assert installed["source"] == "builtin"
            assert installed["source_url"] == f"file://{repo}"
            assert installed["args"] == [
                "run", "--directory", str(clone_dir), "image-parse-mcp"]
        finally:
            market["image-parse"]["git_url"] = orig

    def test_install_builtin_git_tool_reuses_clone(self, db, tmp_path):
        """Git 工具重装：克隆目录已存在时复用，不重复拉取。"""
        repo = make_git_repo(tmp_path, "img-src", {"README.md": "# t"})
        market = {t["name"]: t for t in tools.DEFAULT_MARKET_TOOLS}
        orig = market["image-parse"]["git_url"]
        try:
            market["image-parse"]["git_url"] = f"file://{repo}"
            tools_dir = tmp_path / "tools"
            tools.install_builtin(db, "image-parse", tools_dir=tools_dir)
            clone_dir = tools_dir / "image-parse"
            tools.delete_tool(db, tools.list_tools(db)[0]["id"])
            tools.install_builtin(db, "image-parse", tools_dir=tools_dir)
            assert (clone_dir / ".git").is_dir(), "重装应复用已有克隆"
            assert (clone_dir / "README.md").is_file()
        finally:
            market["image-parse"]["git_url"] = orig

    def test_install_builtin_git_tool_clone_failure(self, db, tmp_path):
        """Git 工具克隆失败：抛错、不落库、清理半成品目录。"""
        market = {t["name"]: t for t in tools.DEFAULT_MARKET_TOOLS}
        orig = market["image-parse"]["git_url"]
        try:
            market["image-parse"]["git_url"] = "file:///nonexistent/image-parse-mcp.git"
            tools_dir = tmp_path / "tools"
            with pytest.raises(ValueError, match="克隆失败"):
                tools.install_builtin(db, "image-parse", tools_dir=tools_dir)
            assert tools.list_tools(db) == [], "克隆失败不应落库"
            assert not (tools_dir / "image-parse").exists(), "失败应清理半成品"
        finally:
            market["image-parse"]["git_url"] = orig

    def test_default_tools_dir_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BOTLER_DATA_DIR", str(tmp_path / "data"))
        assert tools.default_tools_dir() == tmp_path / "data" / "tools"

    def test_default_tools_dir_from_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BOTLER_DATA_DIR", raising=False)
        monkeypatch.setenv("BOTLER_CONFIG", str(tmp_path / "backend" / "config.yaml"))
        assert tools.default_tools_dir() == tmp_path / "backend" / "data" / "tools"



def make_git_repo(tmp_path, name, files):
    """构造本地 Git 仓库（issue #327 Git 市场工具安装测试复用）。"""
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
    return repo


# ---- URL 下载辅助（本地 HTTP server）----

class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        body = route.encode("utf-8") if isinstance(route, str) else route
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默日志
        pass


@pytest.fixture
def http_server():
    """启动本地 HTTP server 返回 base URL（teardown 关闭）。"""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def route_json(base: str, path: str, data) -> str:
    """注册 JSON 路由，返回完整 URL。"""
    url = f"{base}{path}"
    _Handler.routes[path] = json.dumps(data, ensure_ascii=False)
    return url


# ---- URL 导入 ----

class TestImportFromUrl:
    def test_import_single_definition_file(self, db, http_server):
        url = route_json(http_server, "/tool.json", {
            "name": "from-file", "description": "文件导入",
            "kind": "stdio", "command": "python3", "args": ["-c", "print(1)"],
        })
        imported = tools.import_from_url(db, url)
        assert len(imported) == 1
        assert imported[0]["name"] == "from-file"
        assert imported[0]["source"] == "url"
        assert imported[0]["source_url"] == url

    def test_import_mcp_servers_multi(self, db, http_server):
        url = route_json(http_server, "/mcp.json", {
            "mcpServers": {
                "alpha": {"command": "python3", "args": ["a.py"]},
                "beta": {"url": "https://x.example/mcp"},
            },
        })
        imported = tools.import_from_url(db, url)
        assert {t["name"] for t in imported} == {"alpha", "beta"}
        alpha = next(t for t in imported if t["name"] == "alpha")
        assert alpha["kind"] == "stdio"
        beta = next(t for t in imported if t["name"] == "beta")
        assert beta["kind"] == "http"
        assert beta["url"] == "https://x.example/mcp"

    def test_import_http_404(self, db, http_server):
        with pytest.raises(ValueError, match="下载失败"):
            tools.import_from_url(db, f"{http_server}/missing.json")

    def test_import_invalid_json(self, db, http_server):
        _Handler.routes["/bad.json"] = "not-json{{"
        with pytest.raises(ValueError, match="不是合法 JSON"):
            tools.import_from_url(db, f"{http_server}/bad.json")

    def test_import_non_http_url(self, db):
        with pytest.raises(ValueError, match="http"):
            tools.import_from_url(db, "ftp://x/tool.json")

    def test_import_single_without_name(self, db, http_server):
        url = route_json(http_server, "/noname.json",
                         {"command": "python3"})
        with pytest.raises(ValueError, match="name"):
            tools.import_from_url(db, url)

    def test_import_from_git_repo(self, db, tmp_path):
        """浅克隆本地 Git 仓库（file:// 协议 + .git 后缀触发 git 分支）。"""
        repo = tmp_path / "tool-repo.git"
        repo.mkdir()
        (repo / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "repo-tool": {"command": "python3", "args": ["run.py"]},
            },
        }), encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", ".mcp.json"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)

        url = f"file://{repo}"
        imported = tools.import_from_url(db, url)
        assert len(imported) == 1
        assert imported[0]["name"] == "repo-tool"
        assert imported[0]["source"] == "url"
        assert imported[0]["source_url"] == url

    def test_import_git_repo_without_definition(self, db, tmp_path):
        repo = tmp_path / "empty-repo.git"
        repo.mkdir()
        (repo / "README.md").write_text("# no tools", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
        with pytest.raises(ValueError, match="未找到工具定义"):
            tools.import_from_url(db, f"file://{repo}")

    @staticmethod
    def _make_git_repo(tmp_path, name, files):
        """构造本地 Git 仓库（目录名带 .git 后缀触发 git 分支）。"""
        return make_git_repo(tmp_path, name, files)

    def test_import_git_repo_without_definition_lists_all_files(self, db, tmp_path):
        """无定义文件时，报错应列出全部候选文件（含 .mcp/mcp.json / tools.json）。"""
        repo = self._make_git_repo(tmp_path, "plain-repo.git",
                                   {"README.md": "# no tools"})
        with pytest.raises(ValueError) as exc:
            tools.import_from_url(db, f"file://{repo}")
        msg = str(exc.value)
        for f in (".mcp.json", "mcp.json", ".mcp/mcp.json", "tool.json", "tools.json"):
            assert f in msg, f"报错应列出候选文件 {f}，实际: {msg}"

    def test_import_git_repo_fastmcp_python_hint(self, db, tmp_path):
        """FastMCP / uv 风格 Python 项目（issue #325 用户仓库同构）应给出引导。"""
        repo = self._make_git_repo(tmp_path, "Image-Parse-MCP.git", {
            "pyproject.toml": (
                '[project]\nname = "image-parse-mcp"\n'
                'dependencies = ["mcp>=1.0.0"]\n\n'
                '[project.scripts]\nimage-parse-mcp = "image_parse.server:main"\n'
            ),
            "src/image_parse/server.py": (
                'from mcp.server.fastmcp import FastMCP\n'
                'mcp = FastMCP("image-parse")\n'
                '@mcp.tool()\ndef analyze_image(image_source: str) -> str:\n'
                '    """分析图片"""\n    return "ok"\n'
            ),
            "README.md": "# Image Parse MCP\n",
        })
        with pytest.raises(ValueError) as exc:
            tools.import_from_url(db, f"file://{repo}")
        msg = str(exc.value)
        assert "FastMCP" in msg, f"应提示 FastMCP 风格项目，实际: {msg}"
        assert "自定义工具" in msg, f"应引导使用自定义工具，实际: {msg}"

    def test_import_git_repo_example_template_hint(self, db, tmp_path):
        """仓库只有 .mcp.json.example 模板时，提示复制改名。"""
        repo = self._make_git_repo(tmp_path, "template-repo.git", {
            ".mcp.json.example": json.dumps({
                "mcpServers": {"demo": {"command": "python3"}},
            }, ensure_ascii=False),
        })
        with pytest.raises(ValueError) as exc:
            tools.import_from_url(db, f"file://{repo}")
        msg = str(exc.value)
        assert ".mcp.json.example" in msg, f"应提示模板文件，实际: {msg}"
        assert ".mcp.json" in msg, f"应提示复制为 .mcp.json，实际: {msg}"

    def test_looks_like_git_url_variants(self):
        """Git 仓库 URL 识别：.git 后缀 / 托管平台仓库页 / 数据文件排除。"""
        # .git 后缀
        assert tools._looks_like_git_url("https://github.com/a/b.git")
        # 托管平台仓库页（地址栏复制，无 .git 后缀，issue #325）
        assert tools._looks_like_git_url("https://github.com/1617110693/Image-Parse-MCP")
        assert tools._looks_like_git_url("https://gitlab.com/group/sub/repo")
        assert tools._looks_like_git_url("https://gitee.com/owner/repo")
        assert tools._looks_like_git_url("https://gitcode.com/owner/repo")
        assert tools._looks_like_git_url("https://bitbucket.org/owner/repo")
        # 非仓库 URL：数据文件 / 非托管平台 / 非 http(s)
        assert not tools._looks_like_git_url("https://github.com/a/b/tool.json")
        assert not tools._looks_like_git_url("https://github.com/a/b/README.md")
        assert not tools._looks_like_git_url("https://example.com/a/b")
        assert not tools._looks_like_git_url("ftp://github.com/a/b")
        assert not tools._looks_like_git_url("")

    def test_import_partial_failure_skips(self, db, http_server):
        """mcpServers 中一个定义非法时跳过，其余导入。"""
        url = route_json(http_server, "/partial.json", {
            "mcpServers": {
                "good": {"command": "python3"},
                "bad-kind": {"command": "python3", "kind": "webrpc"},
            },
        })
        imported = tools.import_from_url(db, url)
        assert [t["name"] for t in imported] == ["good"]


# ---- 远端市场索引 ----

class TestMarketIndex:
    def test_fetch_tools_array(self, http_server):
        url = route_json(http_server, "/index.json", {
            "tools": [
                {"name": "idx-a", "kind": "stdio", "command": "python3"},
                {"name": "idx-b", "kind": "http", "url": "https://x/mcp"},
            ],
        })
        candidates = tools.fetch_market_index(url)
        assert [c["name"] for c in candidates] == ["idx-a", "idx-b"]

    def test_fetch_plain_array(self, http_server):
        url = route_json(http_server, "/plain.json", [
            {"name": "plain-a", "command": "python3"},
        ])
        candidates = tools.fetch_market_index(url)
        assert candidates[0]["name"] == "plain-a"

    def test_fetch_invalid_entries_filtered(self, http_server):
        url = route_json(http_server, "/mixed.json", {
            "tools": [
                {"name": "ok", "command": "python3"},
                {"name": "../bad"},
                {"kind": "http", "url": "https://x"},
                "not-an-object",
            ],
        })
        candidates = tools.fetch_market_index(url)
        assert [c["name"] for c in candidates] == ["ok"]

    def test_fetch_bad_format(self, http_server):
        url = route_json(http_server, "/badfmt.json", {"foo": 1})
        with pytest.raises(ValueError, match="格式不正确"):
            tools.fetch_market_index(url)

    def test_save_and_get_url(self, db):
        assert tools.get_market_index_url(db) == ""
        tools.save_market_index_url(db, "  https://index.example/tools.json  ")
        assert tools.get_market_index_url(db) == "https://index.example/tools.json"


# ---- MCP 配置注入 ----

class TestMcpServersJson:
    def test_stdlib_entry(self, db):
        tools.create_tool(db, make_definition(
            name="srv", command="python3", args=["-m", "x"], env={"K": "v"}))
        config = tools.mcp_servers_json(db)
        assert config["mcpServers"]["srv"] == {
            "command": "python3", "args": ["-m", "x"], "env": {"K": "v"}}

    def test_remote_entry(self, db):
        tools.create_tool(db, make_definition(
            name="remote", kind="http", url="https://x/mcp", command=""))
        config = tools.mcp_servers_json(db)
        assert config["mcpServers"]["remote"] == {"url": "https://x/mcp"}

    def test_disabled_excluded(self, db):
        tool = tools.create_tool(db, make_definition(name="on"))
        tools.create_tool(db, make_definition(name="off"))
        tools.set_tool_enabled(db, tool["id"], False)
        config = tools.mcp_servers_json(db)
        assert list(config["mcpServers"]) == ["off"]

    def test_empty(self, db):
        assert tools.mcp_servers_json(db) == {"mcpServers": {}}


class TestWriteWorkspaceMcpConfig:
    def test_writes_mcp_json_and_exclude(self, db, tmp_path):
        workdir = tmp_path / "repo"
        (workdir / ".git" / "info").mkdir(parents=True)
        (workdir / ".git" / "info" / "exclude").write_text("# git ignore\n",
                                                           encoding="utf-8")
        tools.create_tool(db, make_definition(name="srv"))
        path = tools.write_workspace_mcp_config(db, workdir)
        assert path == workdir / ".mcp.json"
        payload = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
        assert payload["mcpServers"]["srv"]["command"] == "python3"
        exclude = (workdir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert ".mcp.json" in exclude

    def test_exclude_idempotent(self, db, tmp_path):
        workdir = tmp_path / "repo"
        (workdir / ".git" / "info").mkdir(parents=True)
        tools.create_tool(db, make_definition(name="srv"))
        tools.write_workspace_mcp_config(db, workdir)
        tools.write_workspace_mcp_config(db, workdir)
        exclude = (workdir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert exclude.count(".mcp.json") == 1

    def test_no_enabled_removes_stale(self, db, tmp_path):
        workdir = tmp_path / "repo"
        (workdir / ".git" / "info").mkdir(parents=True)
        tools.create_tool(db, make_definition(name="srv"))
        tools.write_workspace_mcp_config(db, workdir)
        assert (workdir / ".mcp.json").is_file()
        # 停用全部工具 → 清理残留
        srv = tools.get_tool(db, 1)
        tools.set_tool_enabled(db, srv["id"], False)
        assert tools.write_workspace_mcp_config(db, workdir) is None
        assert not (workdir / ".mcp.json").exists()

    def test_missing_workdir(self, db, tmp_path):
        with pytest.raises(ValueError, match="工作区不存在"):
            tools.write_workspace_mcp_config(db, tmp_path / "nope")
