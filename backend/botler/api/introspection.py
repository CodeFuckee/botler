"""概览页「自省」API（issue #187）。

需求：概览页每个仓库卡片右上角新增「自省」按钮，点击后调用 AI agent
审查该仓库的功能与实现情况，对项目的改进提出建议，并把建议写到对应
仓库的 GitLab issue 里，分配人选择仓库的 owner。

接口设计：
- POST /api/repos/{repo_id}/introspect（同步执行）：
  1. 校验仓库存在、未软删除、已启用；
  2. 收集项目上下文（尽力而为）：本地项目文件夹（local_path 优先，
     executor 工作区 clone 兜底）扫描文件树 + README + 关键清单文件；
     无本地文件夹时回退 GitLab 仓库 API（文件树 + README）；
  3. 调用 AI 对话模型（复用设置页「AI API 供应商」第一个启用且 Key
     非空的项，与灵感对话 issue #166 同一链路）生成审查报告；
  4. 在仓库创建 GitLab issue：标题带【自省】前缀，描述为审查报告，
     标签 optimize（改进建议语义），分配人 = 仓库 owner（GitLab 项目
     owner 优先，仓库 remote 用户名兜底，解析失败不指定分配人）；
  5. 创建成功后清空概览缓存，前端刷新即可看到新 issue。写 issue 与
     概览页其他 issue 编辑一致（_issue_edit_call）：必须使用 owner
     token，绝不回退 bot token。

错误映射：仓库不存在 → 404；仓库已删除/未启用 → 400；未配置 AI 对话
模型 → 400（引导设置页配置）；AI 调用失败/回复为空 → 502；GitLab
创建 issue 失败 → 502；网络错误 → 502。项目上下文收集失败不阻塞：
仅基于仓库元信息（名称/地址/项目 id）继续审查（提示模型如实说明）。
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..gitlab_client import GITLAB_ISSUE_TITLE_MAX_LEN, GitLabError
from .issues import (
    _issue_create_client, _issue_edit_call, _trim_issue, clear_issue_cache,
)

logger = logging.getLogger(__name__)

# 路由与仓库管理共用 /repos 前缀，避免前端新增第二种资源前缀
router = APIRouter(prefix="/repos", tags=["repos-introspection"])

# 自省 issue 默认标签（issue #187）：改进建议语义，属于平台有效标签池
INTROSPECT_LABELS = ("optimize",)

# AI 审查请求超时（秒）：审查上下文比灵感对话大，放宽到 120s
INTROSPECT_TIMEOUT = 120.0

# 项目上下文收集上限：文件树条目数 / 目录深度 / README 与清单文件长度，
# 防止大仓库（botler 本体含 node_modules 等）把上下文撑爆模型窗口
MAX_TREE_ENTRIES = 300
MAX_TREE_DEPTH = 3
MAX_README_CHARS = 8000
MAX_MANIFEST_CHARS = 4000

# 文件树扫描跳过的目录（构建产物 / 依赖 / 缓存 / 数据等，与 .gitignore
# 语义对齐；本地与 GitLab 兜底共用同一规则集）
SKIP_DIR_NAMES = {
    ".git", ".github", "node_modules", "dist", "build", "__pycache__",
    ".venv", "venv", ".pytest_cache", "coverage", "test-results",
    "playwright-report", ".hvigor", ".uv-cache", ".idea", ".vscode",
    "cache", "data", "workspace", "minio",
}

# 关键清单文件：存在时读取进审查上下文（按文件名精确匹配，均截断）
MANIFEST_FILES = (
    "package.json", "pyproject.toml", "requirements.txt",
    "docker-compose.yml", ".gitlab-ci.yml", "go.mod", "Cargo.toml",
    "pubspec.yaml",
)

# AI 审查系统提示词：角色 + 输出结构（要求具体可落地、不编造细节）
INTROSPECT_SYSTEM_PROMPT = (
    "你是 Botler 平台的「项目自省」审查 agent。任务：审查给定仓库的功能"
    "与实现情况，对项目的改进提出建议。请用中文以 Markdown 输出审查报告，"
    "包含三部分：\n"
    "1. 项目概览：根据仓库信息与文件结构梳理项目定位、主要功能模块与技术栈；\n"
    "2. 实现情况评估：分析代码组织、架构设计、依赖管理、测试覆盖、文档"
    "完备性、CI/CD 配置等方面做得好的地方与存在的问题；\n"
    "3. 改进建议：按优先级（高/中/低）列出 5~10 条具体可落地的改进建议，"
    "每条包含【问题】现状描述、【建议】具体做法、【收益】期望效果。\n"
    "要求：建议具体可执行，避免空泛套话；信息不足时如实说明，不编造代码细节。"
)


def _project_root(repo: dict) -> Path | None:
    """定位项目本地根目录（issue #187）：local_path 优先（用户本机直接
    开发的仓库），其次 executor 工作区 clone（backend/workspace/<name>，
    与 executor.prepare_workspace 同一目录约定）；都不存在返回 None。"""
    local = (repo.get("local_path") or "").strip()
    if local:
        p = Path(local)
        if p.is_dir():
            return p
    name = (repo.get("name") or "").strip()
    if name:
        # introspection.py 位于 backend/botler/api/，parents[2] = backend/
        p = Path(__file__).resolve().parents[2] / "workspace" / name
        if p.is_dir():
            return p
    return None


def _walk_tree(root: Path) -> list[str]:
    """扫描项目文件树（相对路径列表）：深度受限、跳过依赖/产物/缓存目录、
    条目数封顶；目录名排序保证输出确定性（审查结果可复现）。"""
    tree: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        # 深度达到上限：不再下钻子目录（os.walk 置空 dirnames 剪枝）
        if depth >= MAX_TREE_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIR_NAMES and not d.startswith("."))
        for f in sorted(filenames):
            if f.startswith(".") or len(tree) >= MAX_TREE_ENTRIES:
                continue
            tree.append(str(rel / f) if str(rel) != "." else f)
        if len(tree) >= MAX_TREE_ENTRIES:
            break
    return tree


def _read_truncated(path: Path, limit: int) -> str | None:
    """读取文本文件并截断到 limit 字符；读取失败返回 None（尽力而为）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def _collect_local_context(root: Path) -> dict:
    """从本地项目文件夹收集审查上下文（issue #187）：文件树 + README +
    关键清单文件。任何单项读取失败都降级跳过，不中断整体收集。"""
    tree = _walk_tree(root)
    # README：根目录下 README*（.md / 无扩展名均可）
    readme = None
    try:
        for f in sorted(root.iterdir()):
            if f.is_file() and f.name.upper().startswith("README"):
                readme = _read_truncated(f, MAX_README_CHARS)
                if readme:
                    break
    except OSError:
        pass
    manifests: dict[str, str] = {}
    for name in MANIFEST_FILES:
        p = root / name
        if p.is_file():
            content = _read_truncated(p, MAX_MANIFEST_CHARS)
            if content:
                manifests[name] = content
    return {"tree": tree, "readme": readme, "manifests": manifests}


def _read_gitlab_file(client, project_id: int, path: str) -> str | None:
    """读取 GitLab 仓库文件内容（files API 返回 base64 content）。"""
    try:
        entry = client._request(
            "GET", f"/projects/{project_id}/repository/files/{path}",
            params={"ref": "HEAD"})
    except GitLabError:
        return None
    if not isinstance(entry, dict) or not entry.get("content"):
        return None
    try:
        raw = base64.b64decode(entry["content"]).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 base64 解码失败视为不可读
        return None
    raw = (raw or "").strip()
    return raw[:MAX_README_CHARS] if raw else None


def _collect_gitlab_context(client, project_id: int) -> dict:
    """GitLab 仓库 API 兜底收集上下文（无本地文件夹时）：递归文件树 +
    README（清单文件仅本地收集，API 逐个读文件开销大且 README 已覆盖
    主要信息）。"""
    tree: list[str] = []
    try:
        entries = client._request(
            "GET", f"/projects/{project_id}/repository/tree",
            params={"recursive": True, "per_page": 100})
    except GitLabError as e:
        logger.warning("自省：读取仓库文件树失败（project=%s）: %s",
                       project_id, e)
        entries = None
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        path = str(e.get("path") or "").strip()
        if not path:
            continue
        parts = path.split("/")
        # 跳过依赖/产物/缓存目录与隐藏文件（与本地规则集一致）
        if any(p in SKIP_DIR_NAMES or p.startswith(".") for p in parts[:-1]):
            continue
        tree.append(path)
        if len(tree) >= MAX_TREE_ENTRIES:
            break
    readme = None
    for name in ("README.md", "README.MD", "readme.md", "README"):
        readme = _read_gitlab_file(client, project_id, name)
        if readme:
            break
    return {"tree": tree, "readme": readme, "manifests": {}}


def _build_review_prompt(repo: dict, local_ctx: dict | None,
                         gitlab_ctx: dict | None) -> str:
    """组装用户侧审查上下文：仓库元信息 + 文件树 + README + 清单文件。

    本地上下文优先（更完整，含清单文件）；本地为空时用 GitLab 兜底；
    两者都拿不到时仅保留仓库元信息，并提示模型如实说明信息不足。
    """
    ctx = local_ctx or gitlab_ctx or {}
    lines = [
        "仓库信息：",
        f"- 仓库名：{(repo.get('name') or '').strip()}",
        f"- GitLab 项目 ID：{repo.get('gitlab_project_id')}",
        f"- 仓库地址：{(repo.get('url') or '').strip()}",
    ]
    tree = ctx.get("tree") or []
    if tree:
        lines.append("\n【项目文件结构】")
        lines.append("\n".join(tree[:MAX_TREE_ENTRIES]))
    readme = (ctx.get("readme") or "").strip()
    if readme:
        lines.append("\n【README】")
        lines.append(readme)
    for name, content in (ctx.get("manifests") or {}).items():
        if content:
            lines.append(f"\n【{name}】")
            lines.append(content)
    if not tree and not readme:
        lines.append("\n（未能读取到仓库文件内容，请仅基于仓库元信息给出"
                     "通用审查与改进建议，并如实说明信息不足）")
    lines.append("\n请基于以上项目信息完成审查并输出报告。")
    return "\n".join(lines)


def _resolve_owner_assignee(c, repo: dict, client) -> int | None:
    """解析仓库 owner 作为自省 issue 的分配人（issue #187）。

    优先 GitLab 项目 owner（projects API 的 owner 字段，id 或 username
    解析为 GitLab 用户 id）；项目 owner 读取失败/缺失时兜底仓库 remote
    用户名（与灵感提交 issue 的分配人解析一致，issue #153）；仍无法
    解析返回 None（调用方不指定分配人，不阻塞 issue 创建）。
    """
    project_id = repo["gitlab_project_id"]
    try:
        proj = client.get_project(project_id)
        owner = (proj or {}).get("owner") or {}
        uid = owner.get("id")
        if uid is not None:
            try:
                return int(uid)
            except (TypeError, ValueError):
                pass
        username = (owner.get("username") or "").strip()
        if username:
            return client.get_user_id_by_username(username)
    except (GitLabError, httpx.HTTPError) as e:
        logger.warning("自省：读取项目 owner 失败（project=%s），尝试仓库用户兜底: %s",
                       project_id, e)
    username = (repo.get("remote_username") or "").strip()
    if username:
        try:
            return client.get_user_id_by_username(username)
        except (GitLabError, httpx.HTTPError) as e:
            logger.warning("自省：解析仓库用户 %s 失败，跳过分配人: %s",
                           username, e)
    return None


@router.post("/{repo_id}/introspect", status_code=201)
def introspect_repo(request: Request, repo_id: int):
    """概览页「自省」按钮（issue #187）：调用 AI agent 审查仓库的功能与
    实现情况，把改进建议写入该仓库的 GitLab issue，分配人为仓库 owner。

    同步执行（AI 审查 + 创建 issue 一次请求完成，超时 120s）；返回
    创建的 issue 精简对象与审查报告全文，前端展示成功提示与跳转链接。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    row = dict(row)  # sqlite3.Row → dict，统一按 dict 访问（issue #187）
    if row["deleted_at"] is not None:
        raise HTTPException(400, "仓库已删除")
    if not row["enabled"]:
        raise HTTPException(400, "仓库未启用")

    # AI 审查模型：复用设置页「AI API 供应商」第一个启用且 Key 非空的项
    # （与灵感 AI 对话 issue #166 同一链路）
    from ..chat_models import ChatModelClient, ChatModelError, resolve_chat_provider
    settings = c.config.get()
    provider_cfg = resolve_chat_provider(settings)
    if provider_cfg is None:
        raise HTTPException(
            400, "未配置 AI 对话模型：请先在设置页「AI API 供应商」添加"
                 "并启用一个供应商（需填写 API Key）")

    # 收集项目上下文（尽力而为：本地文件夹优先，GitLab API 兜底）
    client = _issue_create_client(c, row)
    root = _project_root(row)
    local_ctx = _collect_local_context(root) if root else None
    gitlab_ctx = None
    if local_ctx is None or not local_ctx.get("tree"):
        try:
            gitlab_ctx = _collect_gitlab_context(
                client, row["gitlab_project_id"])
        except (GitLabError, httpx.HTTPError) as e:
            logger.warning("自省：GitLab 上下文兜底收集失败（repo=%s）: %s",
                           row["id"], e)
            gitlab_ctx = None

    user_content = _build_review_prompt(row, local_ctx, gitlab_ctx)
    try:
        chat = ChatModelClient(
            name=str(provider_cfg.get("name") or "AI 供应商"),
            provider=str(provider_cfg.get("provider") or "custom").strip(),
            base_url=str(provider_cfg.get("base_url") or "").strip(),
            api_key=str(provider_cfg.get("api_key") or "").strip(),
            model=str(provider_cfg.get("model") or "").strip(),
            timeout=INTROSPECT_TIMEOUT,
            verify_ssl=getattr(settings, "verify_ssl", True))
        review = chat.chat([
            {"role": "system", "content": INTROSPECT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ])
    except ChatModelError as e:
        raise HTTPException(502, f"AI 审查失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"AI 审查网络错误: {str(e)[:200]}") from e
    review = (review or "").strip()
    if not review:
        raise HTTPException(502, "AI 审查结果为空，请稍后重试")

    # 分配人 = 仓库 owner；解析失败（项目 owner 不可得、用户不存在等）
    # 不阻塞 issue 创建，与灵感提交 issue 的降级语义一致
    try:
        assignee_id = _resolve_owner_assignee(c, row, client)
    except Exception:  # noqa: BLE001 防御性兜底：分配人解析失败不阻塞
        logger.exception("自省：解析仓库 owner 分配人异常（repo=%s），跳过分配人",
                         row["id"])
        assignee_id = None

    repo_name = (row["name"] or "").strip() or f"项目{row['gitlab_project_id']}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"【自省】{repo_name} 项目审查与改进建议（{now}）"
    if len(title) > GITLAB_ISSUE_TITLE_MAX_LEN:
        title = title[:GITLAB_ISSUE_TITLE_MAX_LEN - 1] + "…"
    description = (
        f"本 issue 由 Botler 概览页「自省」按钮生成：调用 AI agent 审查仓库"
        f"「{repo_name}」的功能与实现情况，以下为改进建议。\n\n{review}")
    try:
        issue = _issue_edit_call(
            c, row,
            lambda cl: cl.create_issue(
                row["gitlab_project_id"], title,
                description=description,
                assignee_id=assignee_id,
                labels=list(INTROSPECT_LABELS)))
    except GitLabError as e:
        raise HTTPException(502, f"创建 issue 失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"创建 issue 网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    return {"issue": _trim_issue(issue, {}), "review": review}
