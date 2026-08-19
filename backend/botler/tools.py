"""MCP 工具管理（issue #172）：工具页面的后端核心。

工具 = MCP server（Model Context Protocol），通过 ``mcpServers`` 配置
暴露给 agent（Claude Code 等）调用。来源四类：

- ``builtin``：内置工具市场（平台预置工具模板，一键安装入库）；
- ``url``：URL 导入——Git 仓库 URL（浅克隆后读取仓库内工具定义文件）
  或 JSON 定义文件 URL（单个 MCP server 定义 / ``mcpServers`` 多工具格式）；
- ``market``：远端市场索引（配置 JSON 索引地址，拉取候选工具清单，
  前端逐个安装）；
- ``custom``：自定义工具（页面表单手工编写，名称/描述/类型/命令参数）。

工具**全局生效**（issue #172 Q4）：启用中的工具由 executor 在任务执行
前注入仓库工作区根目录 ``.mcp.json``（Claude Code 项目级 MCP 配置，
``mcpServers`` 格式），并追加 ``.git/info/exclude`` 本地忽略防止误提交
（``.git/info/exclude`` 不入库、不影响他人）。

安全约束：
- 工具名只允许字母/数字/``-_``，禁止路径分隔符（MCP server 名约定）；
- stdio 类型必须提供 command；sse/http 类型必须提供 http(s) URL；
- args 必须是 JSON 字符串数组；env 必须是 JSON 字符串键值对；
- URL 下载仅允许 http/https，响应体 ≤ 1MB，超时 15s。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .database import Database

logger = logging.getLogger("botler.tools")

# ---- 类型与来源常量 ----

# MCP server 连接类型（与 Claude Code .mcp.json 的 server 定义对齐）
KIND_STDIO = "stdio"   # 本地子进程（command + args + env）
KIND_SSE = "sse"       # SSE 远程端点（url）
KIND_HTTP = "http"     # HTTP 流式远程端点（url）
KINDS = (KIND_STDIO, KIND_SSE, KIND_HTTP)

# 工具来源
SOURCE_BUILTIN = "builtin"   # 内置工具市场
SOURCE_URL = "url"           # URL 导入（Git 仓库 / JSON 文件）
SOURCE_MARKET = "market"     # 远端市场索引
SOURCE_CUSTOM = "custom"     # 页面自定义编写
SOURCES = (SOURCE_BUILTIN, SOURCE_URL, SOURCE_MARKET, SOURCE_CUSTOM)

# 工具页元信息键
META_MARKET_INDEX_URL = "market_index_url"

# 名称合法性（MCP server 名约定：字母数字 + - _，禁止路径分隔符）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
# 字段长度上限（防误操作写入超长文本）
MAX_NAME_LEN = 100
MAX_DESC_LEN = 500
MAX_FIELD_LEN = 2000
# URL 下载上限 / 超时（秒）
MAX_DOWNLOAD_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT = 15

# 工作区注入文件名（Claude Code 项目级 MCP 配置）
MCP_CONFIG_FILE = ".mcp.json"

# Git 仓库内工具定义文件候选（浅克隆后按序查找）
DEFINITION_FILES = (
    ".mcp.json", "mcp.json", ".mcp/mcp.json",
    "tool.json", "tools.json",
)

# 常见 Git 托管平台（地址栏复制的仓库页 URL 常无 .git 后缀，仍按 Git 仓库处理）
_GIT_PLATFORM_HOSTS = (
    "github.com", "gitlab.com", "gitlab.cn", "gitee.com", "gitcode.com",
    "bitbucket.org", "jihulab.com", "gitea.com",
)

# 仓库内可能存在的「模板」定义文件（复制改名即可用，识别后给出引导提示）
_EXAMPLE_DEFINITION_FILES = (
    ".mcp.json.example", "mcp.json.example",
    ".mcp/mcp.json.example", "tool.json.example", "tools.json.example",
)


def _url_scheme(url: str) -> str | None:
    """URL 协议（http/https 才允许）；非法返回 None。"""
    if not url:
        return None
    try:
        parsed = url.split("://", 1)
        if len(parsed) != 2:
            return None
        scheme, rest = parsed
        if scheme.lower() not in ("http", "https") or not rest:
            return None
        return scheme.lower()
    except Exception:
        return None


def _looks_like_git_url(url: str) -> bool:
    """判断 URL 是否为 Git 仓库地址。

    - ``.git`` 后缀 / ``git@`` / ``ssh://`` / ``git://``；
    - 常见 Git 托管平台（GitHub / GitLab / Gitee 等）的仓库页 URL：
      地址栏复制的仓库地址常不带 ``.git`` 后缀（issue #325），
      路径形如 ``https://<host>/<owner>/<repo>``，按 Git 仓库浅克隆处理；
      末尾是常见数据文件后缀（.json / .md 等）的不算仓库页，走 JSON 下载。
    """
    lowered = url.lower()
    if lowered.endswith(".git"):
        return True
    if lowered.startswith(("git@", "ssh://", "git://")):
        return True
    try:
        parsed = urllib.parse.urlsplit(lowered)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").lower() not in _GIT_PLATFORM_HOSTS:
        return False
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:  # 至少 owner/repo 两段
        return False
    last = segments[-1].lower()
    if last.endswith((".json", ".yaml", ".yml", ".toml", ".txt",
                      ".md", ".html", ".htm", ".xml", ".zip")):
        return False
    return True


def _normalize_args(args: Any) -> list[str]:
    """args 归一：必须是字符串数组（可空），否则抛 ValueError。"""
    if args is None:
        return []
    if not isinstance(args, list):
        raise ValueError("args 必须是字符串数组")
    out: list[str] = []
    for item in args:
        if not isinstance(item, str):
            raise ValueError("args 必须是字符串数组")
        if len(item) > MAX_FIELD_LEN:
            raise ValueError(f"args 单项过长（超过 {MAX_FIELD_LEN} 字符）")
        out.append(item)
    return out


def _normalize_env(env: Any) -> dict[str, str]:
    """env 归一：必须是字符串键值对（可空），否则抛 ValueError。"""
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env 必须是字符串键值对")
    out: dict[str, str] = {}
    for k, v in env.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("env 必须是字符串键值对")
        if len(k) > MAX_FIELD_LEN or len(v) > MAX_FIELD_LEN:
            raise ValueError(f"env 键值过长（超过 {MAX_FIELD_LEN} 字符）")
        out[k] = v
    return out


def validate_tool_def(definition: dict) -> None:
    """校验并归一化工具定义（就地写回 args/env 归一值）。

    非法定义抛 ValueError（调用方转 HTTP 400 / 前端提示）：
    - name 非空、仅字母数字_-、≤100 字符；
    - description ≤500 字符；
    - kind ∈ {stdio, sse, http}；
    - stdio 必须提供 command；sse/http 必须提供 http(s) url；
    - args 为字符串数组；env 为字符串键值对；字段长度上限。
    """
    name = definition.get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("工具名称不能为空")
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"工具名称过长（超过 {MAX_NAME_LEN} 字符）")
    if not _NAME_RE.match(name):
        raise ValueError(
            "工具名称只能包含字母、数字、下划线与连字符（如 my-tool）")
    definition["name"] = name

    description = definition.get("description", "") or ""
    if not isinstance(description, str):
        raise ValueError("工具描述必须是文本")
    if len(description) > MAX_DESC_LEN:
        raise ValueError(f"工具描述过长（超过 {MAX_DESC_LEN} 字符）")
    definition["description"] = description

    kind = definition.get("kind") or KIND_STDIO
    if kind not in KINDS:
        raise ValueError(f"工具类型必须是 {'/'.join(KINDS)} 之一")
    definition["kind"] = kind

    command = definition.get("command") or ""
    if not isinstance(command, str) or len(command) > MAX_FIELD_LEN:
        raise ValueError("command 必须是文本")
    definition["command"] = command.strip()

    url = definition.get("url") or ""
    if not isinstance(url, str) or len(url) > MAX_FIELD_LEN:
        raise ValueError("url 必须是文本")
    url = url.strip()
    definition["url"] = url

    if kind == KIND_STDIO:
        if not definition["command"]:
            raise ValueError("stdio 类型工具必须提供启动命令（command）")
    else:
        if _url_scheme(url) is None:
            raise ValueError("sse/http 类型工具必须提供 http(s) 服务地址（url）")

    definition["args"] = _normalize_args(definition.get("args"))
    definition["env"] = _normalize_env(definition.get("env"))


def _row_to_dict(row) -> dict:
    """数据库行 → API 视图 dict（args/env JSON 反序列化）。"""
    args, env = row["args"] or "[]", row["env"] or "{}"
    try:
        args_list = json.loads(args)
    except ValueError:
        args_list = []
    try:
        env_dict = json.loads(env)
    except ValueError:
        env_dict = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "kind": row["kind"],
        "command": row["command"] or "",
        "args": args_list if isinstance(args_list, list) else [],
        "env": env_dict if isinstance(env_dict, dict) else {},
        "url": row["url"] or "",
        "source": row["source"],
        "source_url": row["source_url"] or "",
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---- CRUD（基于 database.Database 的 tools 方法）----

def list_tools(db: Database) -> list[dict]:
    """全部工具视图（按安装顺序）。"""
    return [_row_to_dict(r) for r in db.list_tools()]


def get_tool(db: Database, tool_id: int) -> dict | None:
    """单个工具视图；不存在返回 None。"""
    row = db.get_tool(tool_id)
    return _row_to_dict(row) if row is not None else None


def create_tool(db: Database, definition: dict,
                source: str = SOURCE_CUSTOM,
                source_url: str = "") -> dict:
    """创建工具（校验 + 落库），返回视图。重名抛 ValueError。"""
    if source not in SOURCES:
        raise ValueError(f"工具来源必须是 {'/'.join(SOURCES)} 之一")
    validate_tool_def(definition)
    if db.get_tool_by_name(definition["name"]) is not None:
        raise ValueError(f"工具名称已存在: {definition['name']}")
    tool_id = db.create_tool(
        name=definition["name"],
        description=definition["description"],
        kind=definition["kind"],
        command=definition["command"],
        args=json.dumps(definition["args"], ensure_ascii=False),
        env=json.dumps(definition["env"], ensure_ascii=False),
        url=definition["url"],
        source=source,
        source_url=source_url or "",
    )
    return get_tool(db, tool_id)


def update_tool(db: Database, tool_id: int, patch: dict) -> dict:
    """按补丁更新工具（可更新任意定义字段 + enabled），返回视图。

    更新前用现有值合并出完整定义整体校验，避免只改 name 时其余
    字段非法逃过校验（如 stdio 工具被清空 command）。不存在抛
    ValueError（404 语义由调用方判定）。
    """
    current = get_tool(db, tool_id)
    if current is None:
        raise ValueError("工具不存在")
    merged = dict(current)
    for key in ("name", "description", "kind", "command", "url",
                "args", "env"):
        if key in patch:
            merged[key] = patch[key]
    validate_tool_def(merged)

    fields: dict = {}
    for key in ("name", "description", "kind", "command", "url",
                "source", "source_url"):
        if key in patch and patch[key] != current[key]:
            fields[key] = patch[key]
    if "args" in patch:
        fields["args"] = json.dumps(merged["args"], ensure_ascii=False)
    if "env" in patch:
        fields["env"] = json.dumps(merged["env"], ensure_ascii=False)
    if "enabled" in patch:
        fields["enabled"] = 1 if patch["enabled"] else 0

    # 重名冲突（改名且撞车）
    new_name = merged["name"]
    if new_name != current["name"]:
        existed = db.get_tool_by_name(new_name)
        if existed is not None and existed["id"] != tool_id:
            raise ValueError(f"工具名称已存在: {new_name}")

    db.update_tool(tool_id, **fields)
    return get_tool(db, tool_id)


def delete_tool(db: Database, tool_id: int) -> bool:
    """删除工具；不存在返回 False。"""
    return db.delete_tool(tool_id)


def set_tool_enabled(db: Database, tool_id: int, enabled: bool) -> dict:
    """启用/停用工具；不存在抛 ValueError（404 语义由调用方判定）。"""
    if not db.set_tool_enabled(tool_id, enabled):
        raise ValueError("工具不存在")
    return get_tool(db, tool_id)


# ---- 内置工具市场 ----

DEFAULT_MARKET_TOOLS: list[dict] = [
    {
        "name": "web-fetch",
        "description": "网页抓取工具：抓取 URL 并转换为 markdown 供 agent 阅读"
                       "（MCP 官方 fetch 参考服务器，需要网络与 Node.js/npx）",
        "kind": KIND_STDIO,
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "env": {},
        "url": "",
    },
    {
        "name": "filesystem",
        "description": "文件系统工具：安全的文件读写、目录列举与搜索"
                       "（MCP 官方 filesystem 参考服务器，默认根目录 /tmp 可在安装后编辑）",
        "kind": KIND_STDIO,
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "env": {},
        "url": "",
    },
    {
        "name": "everything",
        "description": "MCP 参考服务器：提供 echo / add 等示例工具，"
                       "用于验证 MCP 链路是否打通（官方 everything 服务器）",
        "kind": KIND_STDIO,
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "env": {},
        "url": "",
    },
    {
        "name": "http-bridge-demo",
        "description": "远程 HTTP MCP 端点示例（占位地址，安装后请编辑为实际"
                       "MCP 服务地址，如 SSE/HTTP 流式端点）",
        "kind": KIND_HTTP,
        "command": "",
        "args": [],
        "env": {},
        "url": "https://mcp.example.com/bridge",
    },
]


def market_tools() -> list[dict]:
    """内置工具市场清单（模板视图，安装后写入数据库可编辑）。"""
    return [dict(t) for t in DEFAULT_MARKET_TOOLS]


def install_builtin(db: Database, name: str) -> dict:
    """安装内置市场工具到库（source=builtin）；未知名称/重名抛 ValueError。"""
    market = {t["name"]: t for t in DEFAULT_MARKET_TOOLS}
    if name not in market:
        raise ValueError(f"内置市场不存在该工具: {name}")
    if db.get_tool_by_name(name) is not None:
        raise ValueError(f"工具已安装: {name}")
    return create_tool(db, market[name], source=SOURCE_BUILTIN)


# ---- URL 下载（JSON 文件 / Git 仓库）----

def _download_json(url: str) -> dict | list:
    """HTTP GET 下载 JSON（≤1MB、15s 超时）；失败抛 ValueError。"""
    scheme = _url_scheme(url)
    if scheme is None:
        raise ValueError("下载地址必须是 http(s) URL")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "botler-tools/1.0"})
        # scheme 已由上方 _url_scheme 白名单限定为 http/https，B310 误报
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:  # nosec B310
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except Exception as exc:
        raise ValueError(f"下载失败: {exc}") from None
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"下载内容过大（超过 {MAX_DOWNLOAD_BYTES // 1024}KB）")
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        raise ValueError("下载内容不是合法 JSON") from None


def _parse_server_def(name: str, raw: dict) -> dict:
    """把 mcpServers 里的单个 server 定义解析为工具定义（缺省补默认值）。

    支持 Claude Code 风格：``{command, args, env}`` 或 ``{url}``；
    名称缺省时用外层 key。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"工具定义必须是 JSON 对象: {name}")
    definition = {
        "name": name or (raw.get("name") or "").strip(),
        "description": (raw.get("description") or "")
        if isinstance(raw.get("description"), str) else "",
        "kind": raw.get("kind") or (
            KIND_STDIO if raw.get("command") else KIND_HTTP),
        "command": raw.get("command") or "",
        "args": raw.get("args") or [],
        "env": raw.get("env") or {},
        "url": raw.get("url") or "",
    }
    return definition


def _parse_tool_document(data: Any) -> list[dict]:
    """解析工具定义文档为工具定义列表（两种格式）。

    - ``mcpServers`` 格式：``{"mcpServers": {name: {...}}}`` → 每个 server
      一个工具（来源保留，kind 按 command/url 推断）；
    - 单定义格式：``{name?, description?, kind?, command?, args?, env?, url?}``
      → 单工具（name 缺省报错）。
    """
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        out: list[dict] = []
        for name, raw in data["mcpServers"].items():
            out.append(_parse_server_def(name, raw))
        return out
    if isinstance(data, dict):
        definition = _parse_server_def("", data)
        if not definition["name"]:
            raise ValueError("单工具定义必须包含 name 字段")
        return [definition]
    raise ValueError("工具定义文档格式不正确（需要 mcpServers 对象或工具定义对象）")


def _git_clone(url: str) -> str:
    """浅克隆 Git 仓库到临时目录，返回临时目录路径；失败抛 ValueError。"""
    tmp = tempfile.mkdtemp(prefix="botler-tools-")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, tmp],
            capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT * 4)
        if result.returncode != 0:
            raise ValueError(
                f"Git 仓库克隆失败: {(result.stderr or result.stdout).strip()[-300:]}")
        return tmp
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("Git 仓库克隆超时") from None


def _find_definition_files(repo_dir: str) -> list[Path]:
    """在克隆的仓库中查找工具定义文件（按候选顺序，返回存在的文件）。"""
    found: list[Path] = []
    for rel in DEFINITION_FILES:
        p = Path(repo_dir) / rel
        if p.is_file():
            found.append(p)
    return found


def _no_definition_error(repo_dir: str) -> ValueError:
    """仓库未找到工具定义文件时，构造可操作的诊断报错（issue #325）。

    原报错只列了部分候选文件名，用户无法判断「是仓库没有定义文件，还是
    识别漏了」；此处补三类可操作信息：
    - 列出全部已查找的候选文件（含 .mcp/mcp.json / tools.json）；
    - 仓库内有 .example 模板文件 → 提示复制改名后重新导入；
    - FastMCP / uv 风格 Python MCP 项目（pyproject.toml + [project.scripts]
      + mcp 依赖，如 Image-Parse-MCP 这类仓库）→ 说明仓库本身不内置定义
      文件，引导补 .mcp.json 或改用「自定义工具」手动配置。
    """
    searched = "、".join(DEFINITION_FILES)
    base = f"Git 仓库中未找到工具定义文件（已查找：{searched}）"
    # 1) 模板文件提示（复制改名即可）
    for example in _EXAMPLE_DEFINITION_FILES:
        if (Path(repo_dir) / example).is_file():
            target = example[:-len(".example")]
            return ValueError(
                f"{base}。检测到模板文件 {example}，请先在仓库中复制为 "
                f"{target} 并补充实际配置，再重新导入。")
    # 2) FastMCP / uv 风格 Python MCP 项目提示
    pyproject = Path(repo_dir) / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "[project.scripts]" in text and re.search(
                r'"mcp(?:[>=<~]|")|fastmcp', text.lower()):
            return ValueError(
                f"{base}。该仓库是 MCP server 源码项目（FastMCP/uv 风格），"
                "仓库内未内置工具定义文件，且此类工具通常需要 API Key 等"
                "环境变量、无法从仓库自动推断：请在仓库根目录添加 .mcp.json"
                "（mcpServers 格式）后重新导入，或在工具页面使用「自定义工具」"
                "手动填写启动命令与参数。")
    return ValueError(f"{base}。请确认该仓库确实包含 MCP 工具定义文件。")


def import_from_url(db: Database, url: str) -> list[dict]:
    """从 URL 导入工具（落库），返回导入后的工具视图列表。

    - Git 仓库 URL（``.git`` 后缀 / ``git@`` / ``ssh://`` / ``git://``）：
      浅克隆后查找仓库内工具定义文件（.mcp.json / mcp.json /
      .mcp/mcp.json / tool.json / tools.json），解析全部工具落库；
    - 其他 URL：下载 JSON 定义文件（mcpServers 多工具格式或单定义格式）。

    单个定义解析失败跳过（不中断批量导入），全部失败抛 ValueError。
    """
    url = url.strip()
    if not url:
        raise ValueError("导入地址不能为空")

    definitions: list[dict] = []
    if _looks_like_git_url(url):
        repo_dir = _git_clone(url)
        try:
            files = _find_definition_files(repo_dir)
            if not files:
                raise _no_definition_error(repo_dir)
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                    definitions.extend(_parse_tool_document(data))
                except ValueError as exc:
                    logger.warning("工具定义文件 %s 解析失败，跳过: %s", f, exc)
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)
    else:
        data = _download_json(url)
        definitions = _parse_tool_document(data)

    if not definitions:
        raise ValueError("未解析到任何工具定义")

    imported: list[dict] = []
    for definition in definitions:
        try:
            imported.append(create_tool(db, definition, source=SOURCE_URL,
                                        source_url=url))
        except ValueError as exc:
            logger.warning("导入工具 %s 失败，跳过: %s",
                           definition.get("name"), exc)
    if not imported:
        raise ValueError("工具导入失败（名称重复或定义不合法，请检查）")
    return imported


# ---- 远端市场索引 ----

def fetch_market_index(url: str) -> list[dict]:
    """拉取远端市场索引：``{"tools": [...]}`` 或直接数组 → 候选清单。

    仅返回候选（不落库），前端展示后由用户逐个安装。每个候选按
    工具定义校验（非法条目跳过并在视图标记？——直接过滤非法条目，
    保证返回的都是可安装定义）。
    """
    url = url.strip()
    if not url:
        raise ValueError("市场索引地址不能为空")
    data = _download_json(url)
    if isinstance(data, dict) and isinstance(data.get("tools"), list):
        items = data["tools"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("市场索引格式不正确（需要 tools 数组或对象数组）")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            definition = _parse_server_def("", item)
            if not definition["name"]:
                definition["name"] = (item.get("name") or "").strip()
            validate_tool_def(definition)
        except ValueError:
            continue
        out.append(definition)
    return out


def get_market_index_url(db: Database) -> str:
    """已保存的远端市场索引地址（空串 = 未配置）。"""
    return db.get_tool_meta(META_MARKET_INDEX_URL)


def save_market_index_url(db: Database, url: str) -> str:
    """保存远端市场索引地址（返回 strip 后值）。"""
    url = url.strip()
    db.set_tool_meta(META_MARKET_INDEX_URL, url)
    return url


# ---- 给 agent 使用：MCP 配置注入 ----

def mcp_servers_json(db: Database) -> dict:
    """启用中工具的 ``mcpServers`` 配置（.mcp.json 的 mcpServers 字段）。

    stdio 工具 → ``{command, args, env}``；sse/http 工具 → ``{url}``。
    无启用工具返回 ``{"mcpServers": {}}``。
    """
    servers: dict[str, dict] = {}
    for row in db.list_tools():
        if not row["enabled"]:
            continue
        tool = _row_to_dict(row)
        if tool["kind"] == KIND_STDIO:
            entry: dict = {"command": tool["command"]}
            if tool["args"]:
                entry["args"] = tool["args"]
            if tool["env"]:
                entry["env"] = tool["env"]
        else:
            entry = {"url": tool["url"]}
        servers[tool["name"]] = entry
    return {"mcpServers": servers}


def write_workspace_mcp_config(db: Database, workdir) -> Path | None:
    """把启用中工具写入工作区 ``.mcp.json`` 并本地忽略，返回配置文件路径。

    - 无启用工具：不写入（返回 None），避免干扰任务仓库；
    - 有启用工具：写 ``<workdir>/.mcp.json``（UTF-8 美化 JSON），并把
      ``.mcp.json`` 追加到 ``<workdir>/.git/info/exclude``（去重），防止
      agent 执行 git add -A 时把注入配置提交进仓库（exclude 是本地
      git 配置，不入库）；下次 prepare_workspace 的 git clean -fd 会
      自然清掉该文件（不被 exclude 忽略，clean -fd 仅清理未跟踪文件，
      exclude 只影响 add/status，不影响 clean——注意：git clean -fd 会
      删除未跟踪文件，包括被 exclude 忽略的？不会——clean -fd 不删除
      被 .gitignore / exclude 忽略的文件，只有 -x 才删。所以注入的
      .mcp.json 会被 clean 保留下来！

      因此注入的文件下次 prepare 时不会被 clean 删除，会持续残留。
      处理：每次注入前先删除已有 .mcp.json（无论内容），再写新的，
      保证与当前启用工具一致；停用全部工具后不写（残留由人工清理，
      或下次注入时被覆盖）。
    """
    workdir = Path(workdir)
    if not workdir.is_dir():
        raise ValueError(f"工作区不存在: {workdir}")
    servers = mcp_servers_json(db)
    if not servers["mcpServers"]:
        # 无启用工具：删除上次注入的配置文件（若有），保持工作区干净
        target = workdir / MCP_CONFIG_FILE
        if target.is_file():
            try:
                target.unlink()
            except OSError:
                pass
        return None

    target = workdir / MCP_CONFIG_FILE
    payload = {
        "$schema": "https://json.schemastore.org/mcp.json",
        **servers,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # .git/info/exclude 本地忽略（去重；文件不存在则创建）
    git_dir = workdir / ".git"
    exclude = git_dir / "info" / "exclude"
    if not exclude.is_file():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(f"{MCP_CONFIG_FILE}\n", encoding="utf-8")
    else:
        existing = exclude.read_text(encoding="utf-8", errors="replace")
        if MCP_CONFIG_FILE not in existing.splitlines():
            exclude.write_text(
                existing.rstrip() + f"\n{MCP_CONFIG_FILE}\n",
                encoding="utf-8")
    return target
