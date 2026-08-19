"""仓库 logo 生成 API（issue #188）。

需求——在仓库管理（设置）页面，每个仓库的右侧增加一个「生成图标」按钮，
点击后让 agent 根据这个项目的 readme.md 来生成最适合这个项目的图标的
提示词，并使用这个提示词调用生图模型来生成 logo（要求简约美观大方）；
生成的 logo 显示在仓库页面每个仓库的最左侧，用户可点击放大并下载。

接口设计：
- POST /api/repos/{repo_id}/generate-logo（同步执行）：
  1. 校验仓库存在、未软删除（issue #323 起不再校验 enabled——未启用仓库
     同样允许生成图标，停用只影响任务调度不限制设置页操作）；
  2. 收集项目上下文（尽力而为，复用自省 API issue #187 的收集链路）：
     本地项目文件夹（local_path 优先，executor 工作区 clone 兜底）读
     README，无本地文件夹时回退 GitLab 仓库 API 读 README；README 缺失
     时仅基于仓库元信息（名称/项目 id/地址）继续生成（提示模型合理推断）；
  3. 调用 AI 对话模型（复用设置页「AI API 供应商」第一个启用且 Key 非空
     的项，与自省 issue #187 同一链路）生成 logo 生成提示词（prompt）；
  4. 用该 prompt 调用生图模型（复用设置页「生图模型」第一个启用且 Key
     非空的项，issue #135/#137 的 ImageModelClient 统一封装）生成 logo；
  5. 首张图片落盘 LOGO_DIR/<repo_id>.<ext>（LOGO_DIR 按持久数据目录
     解析：BOTLER_LOGO_DIR 显式覆盖 → BOTLER_DATA_DIR/backend/data/
     logos（pm2 部署持久目录，issue #309）→ 兜底 <backend>/data/logos
     （docker-compose 挂载持久化），并把 logo_path / logo_updated_at /
     logo_mime 写入 repos 表；
  6. 返回 ok=true + logo 元信息 + 生成提示词（前端刷新列表即可展示）。
  重复点击重新生成：同名文件覆盖，logo_updated_at 更新（前端按此参数
  击穿缓存刷新展示）。
- GET /api/repos/{repo_id}/logo：读取已生成 logo 图片字节返回
  （Content-Type 按 logo_mime）；?download=1 时附加 Content-Disposition
  attachment（前端「下载」按钮直接走浏览器下载）。
- POST /api/repos/{repo_id}/sync-logo（issue #297）：把已生成 logo 上传为
  GitLab 项目图标（头像）。读取本地 logo 文件，经 GitLab API
  PUT /projects/{id} 的 avatar 文件参数上传（multipart），身份复用
  issue 创建链路（per-repo client——仓库 remote URL 内嵌 token 优先，
  无 token 回退全局 bot token）；成功返回 ok=true + 项目
  path_with_namespace + 新 avatar_url，前端展示「已同步到 GitLab」。
  上传前压缩 + 格式归一（issue #310/#318）：GitLab 项目头像上限 200KB
  且仅支持 png/jpeg/gif/bmp/tiff/vnd.microsoft.icon——logo 超限时先用
  Pillow 压缩到 200KB 以内再上传；logo 为不受支持格式（如 WebP/AVIF/
  SVG，GitLab 返回 400「file format is not supported」）时转码为支持
  格式（含透明 → PNG，无透明 → PNG/JPEG，禁止 WebP）。本地 logo 文件
  保持原始质量，仅为上传生成压缩/转码副本；mime 与文件名同步调整。

错误映射：仓库不存在 → 404；仓库已删除 → 400；未配置 AI 对话
模型 → 400（引导设置页配置）；未配置生图模型 → 400（引导设置页配置）；
AI 生成 prompt 失败/为空 → 502；生图模型调用失败/未返回图片 → 502。
README 收集失败不阻塞：仅基于仓库元信息生成（提示模型如实说明）。
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# 路由与仓库管理共用 /repos 前缀，避免前端新增第二种资源前缀（与
# api/introspection.py issue #187 同约定）
router = APIRouter(prefix="/repos", tags=["repos-logo"])

# logo 图片目录：见 _resolve_logo_dir（issue #309 起优先解析到持久数据
# 目录，避免 pm2 构建目录轮换导致 logo 文件丢失）。目录不存在自动创建。
_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _resolve_logo_dir() -> Path:
    """logo 图片目录解析（issue #309）：保证 logo 落在持久数据目录。

    pm2 部署形态（deploy/botler.config.cjs）运行在 gitlab-runner 构建
    目录（如 /home/ckd/builds/<hash>/chenkaidi/botler），backend/data
    随构建目录轮换被清理——若 logo 落 <backend>/data/logos，部署后
    repos 表仍有 logo_path 元信息但图片文件丢失，仓库设置页「生成图标」
    生成的 logo 在其他设备/新部署上看不到。优先级：
    1. BOTLER_LOGO_DIR（显式覆盖；测试注入临时目录用）；
    2. BOTLER_DATA_DIR/backend/data/logos（pm2/systemd 部署注入的
       持久数据目录，与 botler.db 同根，备份/迁移一并处理）；
    3. 兜底 <backend>/data/logos（docker-compose 形态 backend/data
       由 data 卷挂载，天然持久）。
    """
    explicit = os.environ.get("BOTLER_LOGO_DIR")
    if explicit:
        return Path(explicit)
    data_dir = os.environ.get("BOTLER_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "backend" / "data" / "logos"
    return _BACKEND_DIR / "data" / "logos"


LOGO_DIR = _resolve_logo_dir()

# AI 生成提示词 + 生图请求超时（秒）：比自省 120s 再放宽——生图模型接口
# 本身耗时较长（Gemini/OpenAI 图片生成通常 10~60s）
GENERATE_LOGO_TIMEOUT = 180.0

# MIME → 文件扩展名映射（生图结果落盘用；未知类型兜底 .png）
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/vnd.microsoft.icon": ".ico",
}


def _mime_ext(mime: str) -> str:
    """MIME → 扩展名（取映射值，未知类型兜底 .png）。"""
    return _MIME_EXT.get((mime or "").strip().lower(), ".png")


# AI 生成 logo 提示词（prompt）的系统提示词：直接输出可用于生图模型的
# prompt，不输出解释；风格要求简约/美观/大方，符合品牌 logo 设计规范。
GENERATE_PROMPT_SYSTEM = (
    "你是 Botler 平台的「项目 logo 设计」agent。任务：根据给定仓库的 "
    "README.md 与项目信息，为该软件项目设计一个 logo 的生成提示词 "
    "（prompt），供生图模型直接使用。\n"
    "要求：\n"
    "1. 直接输出可用于生图模型的英文 prompt（生图模型对英文指令理解更"
    "好），不要输出任何解释、前后缀、Markdown 代码块或引号包裹；\n"
    "2. 风格要求「简约、美观、大方」，适合作为现代软件项目的品牌 logo；\n"
    "3. 推荐扁平化/极简几何风格、单一主色或双色配色、无文字或极简文字、"
    "避免复杂细节与多余装饰，确保小尺寸下仍清晰可辨；\n"
    "4. 紧扣项目定位与核心功能（从 README 提取项目名、领域、技术栈、"
    "解决的问题等）设计视觉元素；\n"
    "5. prompt 控制在 200 词以内；README 信息不足时基于仓库名称、项目"
    "定位合理推断设计方向，不编造 README 中不存在的事实。"
)


def _resolve_image_model(settings) -> dict | None:
    """解析生图模型配置（issue #188）：设置页「生图模型」列表第一个
    enabled 且 api_key 非空的项（与 resolve_chat_provider 同模式）。"""
    for m in getattr(settings, "image_models", None) or []:
        if (isinstance(m, dict)
                and bool(m.get("enabled", True))
                and str(m.get("api_key") or "").strip()):
            return m
    return None


def _now_utc_str() -> str:
    """当前 UTC 时间串（与 SQLite datetime('now') 同格式，前端按 UTC 解析）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _collect_readme(repo: dict, client) -> tuple[str | None, bool]:
    """收集项目 README（尽力而为）：本地文件夹优先，GitLab 仓库 API 兜底。

    复用自省 API（issue #187）的收集链路；返回 (readme 文本或 None,
    是否来自本地文件夹)。两项都拿不到返回 (None, False)；收集异常由
    调用方统一兜底（不阻塞生成流程）。
    """
    from .introspection import (
        _collect_gitlab_context, _collect_local_context, _project_root,
    )

    root = _project_root(repo)
    if root is not None:
        local_ctx = _collect_local_context(root)
        readme = (local_ctx.get("readme") or "").strip() or None
        if readme:
            return readme, True
    gitlab_ctx = _collect_gitlab_context(client, repo["gitlab_project_id"])
    readme = (gitlab_ctx.get("readme") or "").strip() or None
    return readme, False


def _build_prompt_user_content(repo: dict, readme: str | None) -> str:
    """组装用户侧上下文：仓库元信息 + README（缺失时如实说明）。"""
    lines = [
        "仓库信息：",
        f"- 仓库名：{(repo.get('name') or '').strip()}",
        f"- GitLab 项目 ID：{repo.get('gitlab_project_id')}",
        f"- 仓库地址：{(repo.get('url') or '').strip()}",
    ]
    if readme:
        lines.append("\n【README】")
        lines.append(readme)
    else:
        lines.append("\n（未能读取到该仓库的 README.md，请基于仓库名称与"
                     "项目定位合理推断设计方向，不要编造具体功能细节）")
    lines.append("\n请基于以上信息设计并输出该项目的 logo 生成提示词。")
    return "\n".join(lines)


def _save_logo(repo_id: int, data: bytes, mime: str) -> tuple[str, str]:
    """logo 图片落盘：<LOGO_DIR>/<repo_id>.<ext>，返回 (文件名, mime)。

    同名覆盖（重新生成时旧 logo 直接替换）；目录不存在自动创建。
    """
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{repo_id}{_mime_ext(mime)}"
    (LOGO_DIR / filename).write_bytes(data)
    return filename, (mime or "image/png")


# GitLab 项目头像大小上限（issue #310）：GitLab 官方限制
# PUT /projects/{id} 的 avatar 文件 ≤ 200KB，超限上传返回 4xx。压缩
# 目标预留 5% 余量，避免 multipart 封装/元数据膨胀后越过 GitLab 硬阈值。
GITLAB_AVATAR_LIMIT = 200 * 1024
GITLAB_AVATAR_TARGET = int(GITLAB_AVATAR_LIMIT * 0.95)

# GitLab 项目头像支持的文件格式（issue #318）：PUT /projects/{id} 的
# avatar 仅接受以下 MIME，其余格式（含 WebP/AVIF/SVG）一律返回 400
# 「file format is not supported」。压缩/转码候选只允许落在该集合内。
GITLAB_AVATAR_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff",
    "image/vnd.microsoft.icon",
})

# Pillow 探测出的真实图片格式 → MIME（issue #318）：直通判定与 mime
# 校正基于文件内容而非存储 mime，防止生图模型返回格式与记录不一致。
_PIL_FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "ICO": "image/vnd.microsoft.icon",
}


def _compress_image_for_gitlab(data: bytes, mime: str) -> tuple[bytes, str]:
    """把图片转为 GitLab 项目头像支持的格式并压缩到 200KB 以内（issue #310/#318）。

    仅在 sync-logo 上传前调用：本地 logo 文件保持原始质量，只为 GitLab
    上传生成上传副本。策略（Pillow；缺失或解码失败时回退原始字节）：
    1. 解码并探测真实格式（按文件内容而非存储 mime，防止模型返回格式
       与 mime 记录不一致）；
    2. 真实格式在 GitLab 支持集合内且原始字节 ≤ 压缩目标（200KB 的
       95%，留余量）→ 原样直通（字节不变），仅按真实格式校正 mime；
    3. 其余情况（超限，或格式不受支持如 WebP/AVIF/SVG）→ 转码为
       GitLab 支持格式：含透明通道 → PNG（保留 alpha；GitLab 头像不支持
       WebP，issue #318 不再用 WebP 保留透明）；无透明 → PNG/JPEG 候选
       （JPEG 有损、体积更小，超限兜底）；
    4. 仍超限 → 等比降分辨率（1.0→0.2 阶梯）重复 3；
    5. 全部候选失败或 Pillow 不可用/图片无法解码 → 返回原始字节与存储
       mime，交由 GitLab 报错兜底。
    返回 (压缩后字节, 压缩后 mime)；mime 可能变化（WebP → PNG/JPEG、
    PNG → JPEG），调用方需同步调整上传文件名扩展名。
    """
    stored_mime = (mime or "image/png").strip().lower()
    try:
        from PIL import Image
    except Exception:  # Pillow 未安装：跳过压缩/转码，让 GitLab 返回错误
        return data, stored_mime
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        img.load()
    except Exception:  # 无法解码（损坏/非图片）：原样上传，不崩溃
        return data, stored_mime

    # 真实格式判定（按文件内容）：直通仅当格式受 GitLab 支持（issue
    # #318）且未超限；mime 同时校正为真实值，避免存储 mime 与实际内容
    # 不一致导致 GitLab 按内容嗅探拒绝。
    actual_mime = _PIL_FORMAT_MIME.get(fmt)
    if actual_mime in GITLAB_AVATAR_MIMES and len(data) <= GITLAB_AVATAR_TARGET:
        return data, actual_mime

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)
    # 压缩/转码候选（仅 GitLab 支持格式，issue #318 禁止 WebP）：先按
    # 原格式重编码（JPEG 降质量 / 其余一律 PNG 无损，保留透明），无透明
    # 通道时追加 JPEG 有损降质候选——第一个压进目标大小的候选即「保质量
    # 最优」的结果。
    candidates: list[tuple[str, str, dict]] = []
    if fmt in ("JPEG", "JPG"):
        for q in (85, 70, 55, 40):
            candidates.append(("JPEG", "image/jpeg", {"quality": q, "optimize": True}))
    else:
        candidates.append(("PNG", "image/png", {"optimize": True, "compress_level": 9}))
    if not has_alpha:
        for q in (85, 70, 55, 40):
            candidates.append(("JPEG", "image/jpeg", {"quality": q, "optimize": True}))

    def _encode(im, img_fmt: str, save_kwargs: dict) -> bytes:
        """按候选格式编码；JPEG 不支持透明，先合成白底 RGB。"""
        buf = io.BytesIO()
        if img_fmt == "JPEG":
            if im.mode == "RGBA":
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[3])
                im = bg
            else:
                im = im.convert("RGB")
        im.save(buf, format=img_fmt, **save_kwargs)
        return buf.getvalue()

    best: tuple[bytes, str] | None = None
    # 分辨率阶梯：先原尺寸，逐级等比缩小到 0.2 倍（覆盖超大 logo）
    for scale in (1.0, 0.8, 0.6, 0.45, 0.3, 0.2):
        im = img if scale == 1.0 else img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS)
        for img_fmt, out_mime, save_kwargs in candidates:
            try:
                out = _encode(im, img_fmt, save_kwargs)
            except Exception:
                continue
            if best is None or len(out) < len(best[0]):
                best = (out, out_mime)
            if len(out) <= GITLAB_AVATAR_TARGET:
                return best
    return best if best is not None else (data, stored_mime)





@router.post("/{repo_id}/generate-logo", status_code=201)
def generate_repo_logo(request: Request, repo_id: int):
    """仓库管理页「生成图标」按钮（issue #188）：agent 基于 README 生成
    logo 提示词 → 调用生图模型生成 logo → 落盘并记录到 repos 表。

    同步执行（AI 生成提示词 + 生图一次请求完成，超时 180s）；返回
    ok=true + logo 元信息 + 生成提示词，前端刷新仓库列表展示 logo。
    issue #323：未启用仓库同样允许生成图标（仅校验存在与未软删除）。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    row = dict(row)  # sqlite3.Row → dict，统一按 dict 访问（issue #187）
    if row["deleted_at"] is not None:
        raise HTTPException(400, "仓库已删除")
    # issue #323：未启用仓库同样允许「生成图标」——停用只影响任务调度，
    # 不限制仓库设置页的 logo 生成/同步操作（sync-logo 本就未校验 enabled）

    # 1. AI 对话模型：复用设置页「AI API 供应商」第一个启用且 Key 非空的
    #    项（与自省 issue #187 / 灵感 issue #166 同一链路）
    from ..chat_models import ChatModelClient, ChatModelError, resolve_chat_provider
    settings = c.config.get()
    provider_cfg = resolve_chat_provider(settings)
    if provider_cfg is None:
        raise HTTPException(
            400, "未配置 AI 对话模型：请先在设置页「AI API 供应商」添加"
                 "并启用一个供应商（需填写 API Key）")

    # 2. 生图模型：设置页「生图模型」第一个启用且 Key 非空的项
    image_cfg = _resolve_image_model(settings)
    if image_cfg is None:
        raise HTTPException(
            400, "未配置生图模型：请先在设置页「生图模型」添加并启用一个"
                 "供应商（需填写 API Key）")

    # 3. 收集 README（尽力而为：本地文件夹优先，GitLab API 兜底）
    from ..gitlab_client import GitLabError
    from .introspection import _issue_create_client
    client = _issue_create_client(c, row)
    try:
        readme, _ = _collect_readme(row, client)
    except (GitLabError, httpx.HTTPError, OSError) as e:
        logger.warning("生成图标：README 收集失败（repo=%s），基于元信息继续: %s",
                       row["id"], e)
        readme = None

    # 4. 调用 AI 生成 logo 提示词
    try:
        chat = ChatModelClient(
            name=str(provider_cfg.get("name") or "AI 供应商"),
            provider=str(provider_cfg.get("provider") or "custom").strip(),
            base_url=str(provider_cfg.get("base_url") or "").strip(),
            api_key=str(provider_cfg.get("api_key") or "").strip(),
            model=str(provider_cfg.get("model") or "").strip(),
            timeout=GENERATE_LOGO_TIMEOUT,
            verify_ssl=getattr(settings, "verify_ssl", True))
        logo_prompt = chat.chat([
            {"role": "system", "content": GENERATE_PROMPT_SYSTEM},
            {"role": "user", "content": _build_prompt_user_content(row, readme)},
        ])
    except ChatModelError as e:
        raise HTTPException(502, f"AI 生成提示词失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"AI 生成提示词网络错误: {str(e)[:200]}") from e
    logo_prompt = (logo_prompt or "").strip()
    if not logo_prompt:
        raise HTTPException(502, "AI 生成的提示词为空，请稍后重试")

    # 5. 调用生图模型生成 logo
    from ..image_models import ImageModelClient, ImageModelError
    try:
        image_client = ImageModelClient(
            name=str(image_cfg.get("name") or "生图模型"),
            provider=str(image_cfg.get("provider") or "custom").strip(),
            base_url=str(image_cfg.get("base_url") or "").strip(),
            api_key=str(image_cfg.get("api_key") or "").strip(),
            model=str(image_cfg.get("model") or "").strip(),
            timeout=GENERATE_LOGO_TIMEOUT,
            verify_ssl=getattr(settings, "verify_ssl", True))
        results = image_client.generate(logo_prompt)
    except ImageModelError as e:
        raise HTTPException(502, f"生图模型调用失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"生图模型网络错误: {str(e)[:200]}") from e
    if not results:
        raise HTTPException(502, "生图模型未返回图片数据，请稍后重试")

    # 6. 首张图片落盘 + 写 repos 表
    image = results[0]
    filename, mime = _save_logo(row["id"], image.data, image.mime_type)
    updated_at = _now_utc_str()
    c.db.update_repo(
        row["id"],
        logo_path=filename,
        logo_updated_at=updated_at,
        logo_mime=mime,
    )
    return {
        "ok": True,
        "logo_path": filename,
        "logo_mime": mime,
        "logo_updated_at": updated_at,
        "size": len(image.data),
        "logo_prompt": logo_prompt,
    }


@router.post("/{repo_id}/sync-logo")
def sync_repo_logo(request: Request, repo_id: int):
    """同步仓库 logo 到 GitLab 作为项目图标（issue #297）。

    前置：仓库已有生成的 logo（「生成图标」issue #188 的产物）。读取
    本地 logo 文件，通过 GitLab API PUT /projects/{id} 的 avatar 文件
    参数上传，把 GitLab 项目头像设置为该 logo。身份与 issue 创建链路
    一致：per-repo client（仓库 remote URL 内嵌 token）优先，无 token
    回退全局 bot token（均需 Maintainer 及以上角色才能改项目头像）。

    错误映射：仓库不存在 → 404；已软删除 → 400；尚未生成 logo → 400；
    logo 文件缺失 → 404；读取文件失败 → 500；GitLab API 调用失败 →
    502（透传 GitLab 错误信息，如权限不足/图片格式不支持）。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    row = dict(row)  # sqlite3.Row → dict，统一按 dict 访问（issue #187）
    if row["deleted_at"] is not None:
        raise HTTPException(400, "仓库已删除")
    if not row["logo_path"]:
        raise HTTPException(400, "该仓库尚未生成 logo，请先点击「生成图标」")
    path = LOGO_DIR / row["logo_path"]
    if not path.is_file():
        raise HTTPException(404, "logo 文件不存在，请重新点击「生成图标」")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"读取 logo 文件失败: {e}") from e
    mime = row["logo_mime"] or "image/png"

    # issue #310/#318：GitLab 项目头像上限 200KB 且仅支持
    # png/jpeg/gif/bmp/tiff/vnd.microsoft.icon——上传前先压缩超限图片、
    # 把不支持格式（如 WebP，GitLab 返回 400）转码为支持格式（本地
    # logo 文件保持原始质量，仅为上传生成副本）；mime 变化时同步修正
    # 上传文件名扩展名。
    data, mime = _compress_image_for_gitlab(data, mime)
    filename = path.name
    if mime != (row["logo_mime"] or "image/png"):
        filename = f"{path.stem}{_mime_ext(mime)}"

    # 身份：per-repo client（仓库自身 token）优先，回退全局 bot token
    # （与生成 logo 的 README 收集 / issue 创建同一链路）
    from .introspection import _issue_create_client
    from ..gitlab_client import GitLabError
    client = _issue_create_client(c, row)
    try:
        project = client.update_project_avatar(
            row["gitlab_project_id"], filename, data, mime)
    except GitLabError as e:
        raise HTTPException(502, f"同步到 GitLab 失败: {e}") from e
    project = project or {}
    return {
        "ok": True,
        "project": project.get("path_with_namespace"),
        "avatar_url": project.get("avatar_url"),
    }


@router.post("/{repo_id}/sync-logo-from-gitlab")
def sync_logo_from_gitlab(request: Request, repo_id: int):
    """把 GitLab 项目图标（头像）同步回仓库设置页（issue #320）。

    与 sync-logo（issue #297 本地 logo → GitLab 项目图标）方向相反：
    读取 GitLab 项目当前图标，落盘为本地 logo 并写 repos 表
    logo_path / logo_updated_at / logo_mime，仓库设置页最左侧即展示
    GitLab 图标（可放大/下载/继续「同步到 GitLab」回写，形成双向同步）。
    身份与 issue 创建链路一致：per-repo client（仓库 remote URL 内嵌
    token）优先，无 token 回退全局 bot token。

    错误映射：仓库不存在 → 404；已软删除 → 400；GitLab 侧未设置图标
    （avatar_url 为空）→ 400；GitLab 查询/下载失败 → 502（透传错误
    信息，如权限不足）；落盘失败 → 500。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    row = dict(row)  # sqlite3.Row → dict，统一按 dict 访问（issue #187）
    if row["deleted_at"] is not None:
        raise HTTPException(400, "仓库已删除")

    # 身份：per-repo client（仓库自身 token）优先，回退全局 bot token
    # （与 sync-logo / issue 创建同一链路）
    from .introspection import _issue_create_client
    from ..gitlab_client import GitLabError
    client = _issue_create_client(c, row)
    try:
        avatar = client.get_project_avatar(row["gitlab_project_id"])
    except GitLabError as e:
        raise HTTPException(502, f"从 GitLab 同步图标失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(
            502, f"从 GitLab 同步图标网络错误: {str(e)[:200]}") from e
    if not avatar:
        raise HTTPException(
            400, "GitLab 项目未设置图标（头像），请先在 GitLab 上传项目图标")

    data, mime, avatar_url = avatar
    filename, mime = _save_logo(row["id"], data, mime)
    updated_at = _now_utc_str()
    c.db.update_repo(
        row["id"],
        logo_path=filename,
        logo_updated_at=updated_at,
        logo_mime=mime,
    )
    return {
        "ok": True,
        "logo_path": filename,
        "logo_mime": mime,
        "logo_updated_at": updated_at,
        "size": len(data),
        "avatar_url": avatar_url,
    }


@router.get("/{repo_id}/logo")
def get_repo_logo(request: Request, repo_id: int, download: int = 0):
    """读取仓库已生成的 logo 图片（issue #188）。

    仓库管理页每个仓库最左侧的 <img> 直接以此接口作 src（前端按
    logo_updated_at 拼缓存击穿参数）；?download=1 时返回
    Content-Disposition: attachment（下载按钮走浏览器下载）。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    if not row["logo_path"]:
        raise HTTPException(404, "该仓库尚未生成 logo，请先点击「生成图标」")
    path = LOGO_DIR / row["logo_path"]
    if not path.is_file():
        raise HTTPException(404, "logo 文件不存在，请重新点击「生成图标」")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"读取 logo 文件失败: {e}") from e
    mime = row["logo_mime"] or "image/png"
    headers = {}
    if download:
        # 文件名做安全净化：仓库名可能含引号/换行等会破坏响应头字符
        name = re.sub(r'[\\"\r\n]', "_", (row["name"] or f"repo-{repo_id}").strip())
        filename = f"{name}-logo{path.suffix or '.png'}"
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(content=data, media_type=mime, headers=headers)
