"""技能（skill）目录管理（issue #282）：技能页面的后端核心。

Botler 的三个内置执行引擎（claude / hermes / dsh）各自维护一份
「技能」目录——每个技能是一个包含 SKILL.md 的目录（SKILL.md 为技能
说明文档，可附带其他 *.md 文档，如 README.md / API.md / RECIPES.md）。
本模块负责：

- 引擎 → 技能目录根解析：内置引擎按部署机惯例路径（~/.claude/skills、
  $HERMES_HOME/skills、$DSH_HOME/skills / ~/.agents/skills），外部
  执行引擎插件可声明 ``skills_dir`` 属性（str 或 list[str]）覆盖；
- 技能枚举：递归查找技能根下含 SKILL.md 的目录，读取 frontmatter 的
  description 作为技能说明；
- 技能目录内 md 文件枚举 / 读取 / 写入：安全约束——仅允许
  md / markdown 文件、禁止路径穿越（``..`` / 绝对路径）、仅限技能
  目录内（见 :func:`safe_md_path`）。

技能页 API（api/skills.py）与 Web UI 基于本模块实现；写入仅支持
编辑/新增 md 文件，不提供删除（删除会直接影响引擎侧技能行为，
保留由人工通过文件系统操作）。

技能同步（issue #328）：:func:`sync_skills_to_engines` 合并全部执行引擎
的技能（同名保留引擎注册顺序第一个版本）去重后，复制到每个引擎的全部
技能根目录（目标已存在同名技能跳过、缺失根目录自动创建），技能页顶部
「同步所有 agent 技能」按钮触发。
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from .plugins import ExecutorPlugin, PluginKind, list_plugins

logger = __import__("logging").getLogger("botler.skills")

# 技能说明文件（各引擎惯例：技能目录内必须含 SKILL.md）
SKILL_MD = "SKILL.md"

# 可编辑文档扩展名（技能页仅支持查看/编辑 markdown 文档）
MD_EXTENSIONS = (".md", ".markdown")

# 单文件写入上限（2MB，防误操作写入超大文件撑爆磁盘）
MAX_MD_BYTES = 2 * 1024 * 1024


def _hermes_home() -> Path:
    """hermes 数据目录：$HERMES_HOME → ~/.hermes（与部署文档一致）。"""
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def _dsh_home() -> Path:
    """deepseek-harness 数据目录：$DSH_HOME → ~/.dsh（SDK 解析顺序）。"""
    env = os.environ.get("DSH_HOME")
    return Path(env) if env else Path.home() / ".dsh"


def _builtin_roots(engine_name: str) -> list[Path]:
    """内置执行引擎的技能目录根（部署机惯例路径，按引擎名解析）。

    - claude：~/.claude/skills（Claude Code CLI 技能目录）；
    - hermes：$HERMES_HOME/skills（默认 ~/.hermes/skills，见
      docs/hermes-engine-deployment.md 的 skills 数据目录说明）；
    - dsh：$DSH_HOME/skills + ~/.agents/skills（deepseek-harness
      runtime 按 user-dsh / user-agents 两层发现技能）；
    - zcode：~/.zcode/skills（ZCode CLI，与 Claude Code 同源惯例路径）。

    返回的路径可能不存在（如本机未启用某引擎）；调用方按 exists 展示。
    """
    if engine_name == "claude":
        return [Path.home() / ".claude" / "skills"]
    if engine_name == "hermes":
        return [_hermes_home() / "skills"]
    if engine_name == "dsh":
        return [_dsh_home() / "skills", Path.home() / ".agents" / "skills"]
    if engine_name == "zcode":
        return [Path.home() / ".zcode" / "skills"]
    return []


def _plugin_roots(plugin: ExecutorPlugin | None) -> list[Path]:
    """外部执行引擎插件的技能目录根（声明式 ``skills_dir`` 属性）。

    ``skills_dir`` 可为单个路径字符串或路径列表；支持 ``~`` 展开。
    未声明返回空列表（页面展示「该引擎未配置技能目录」）。
    """
    if plugin is None:
        return []
    raw = getattr(plugin, "skills_dir", None)
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    roots: list[Path] = []
    for v in values:
        if isinstance(v, str) and v.strip():
            roots.append(Path(v).expanduser())
    return roots


def engine_skills_roots(engine_name: str,
                        plugin: ExecutorPlugin | None = None) -> list[Path]:
    """解析指定执行引擎的技能目录根列表（可能不存在，调用方按 exists 展示）。

    ``engine_name`` 为执行引擎插件名（如 claude / hermes / dsh）；内置
    引擎走部署机惯例路径，外部引擎插件走 ``skills_dir`` 声明。
    """
    return _builtin_roots(engine_name) or _plugin_roots(plugin)


def _frontmatter_description(skill_md: Path) -> str:
    """SKILL.md 的 YAML frontmatter description 字段；无则返回空串。

    只解析 frontmatter 第一段（``---`` 到 ``---`` 之间）里单行
    description 键（各引擎技能文件的实际写法），多行折叠 YAML 不展开。
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    front = text[3:end]
    for line in front.splitlines():
        stripped = line.strip()
        if stripped.startswith("description:"):
            value = stripped[len("description:"):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def iter_skills(root: Path) -> list[dict]:
    """枚举技能根目录下的全部技能（含 SKILL.md 的目录即为一个技能）。

    返回 ``[{name, path, description}]``：name 为相对技能根目录的路径
    （``/`` 分隔，嵌套技能如 ``software-development/spike``），按名称
    字典序排序；root 不存在返回空列表。
    """
    out: list[dict] = []
    if not root.is_dir():
        return out
    for entry in root.rglob("*"):
        if not entry.is_dir():
            continue
        if (entry / SKILL_MD).is_file():
            out.append({
                "name": entry.relative_to(root).as_posix(),
                "path": str(entry),
                "description": _frontmatter_description(entry / SKILL_MD),
            })
    out.sort(key=lambda s: s["name"])
    return out


def list_md_files(skill_dir: Path) -> list[str]:
    """列出技能目录内全部 md 文件（递归，相对路径，按路径字典序）。"""
    out: list[str] = []
    if not skill_dir.is_dir():
        return out
    for p in skill_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in MD_EXTENSIONS:
            out.append(p.relative_to(skill_dir).as_posix())
    out.sort()
    return out


def safe_md_path(skill_dir: Path, rel: str) -> Path:
    """把技能目录内相对路径解析为绝对路径；非法输入抛 ValueError。

    安全规则（技能页文件读写统一入口）：
    - rel 非空、必须为相对路径（绝对路径拒绝）；
    - 路径任一组件不得为 ``..`` / ``.``（防穿越技能目录）；
    - resolve 后必须仍位于 skill_dir 内（防符号链接逃逸）；
    - 扩展名必须为 .md / .markdown（技能页仅编辑 markdown 文档）。
    """
    if not rel or "\x00" in rel:
        raise ValueError("文件路径不能为空")
    p = Path(rel)
    if p.is_absolute():
        raise ValueError("文件路径必须是相对路径")
    for part in p.parts:
        if part in ("..", ".") or "/" in part or "\\" in part:
            raise ValueError(f"文件路径含非法组件: {part!r}")
    if p.suffix.lower() not in MD_EXTENSIONS:
        raise ValueError("仅支持查看/编辑 md / markdown 文件")
    base = skill_dir.resolve()
    target = (base / p).resolve()
    if target != base and base not in target.parents:
        raise ValueError("文件路径超出技能目录")
    return target


def read_md_file(skill_dir: Path, rel: str) -> str:
    """读取技能目录内 md 文件内容（UTF-8，损坏字节以替换符容错）。"""
    target = safe_md_path(skill_dir, rel)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {rel}")
    return target.read_text(encoding="utf-8", errors="replace")


def write_md_file(skill_dir: Path, rel: str, content: str) -> int:
    """写入技能目录内 md 文件（父目录自动创建），返回写入字节数。

    超过 :data:`MAX_MD_BYTES` 抛 ValueError 拒绝写入。
    """
    target = safe_md_path(skill_dir, rel)
    data = content.encode("utf-8")
    if len(data) > MAX_MD_BYTES:
        raise ValueError(f"文件过大（超过 {MAX_MD_BYTES // 1024}KB，拒绝写入）")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)


def resolve_skill_dir(engine_name: str, skill_name: str,
                      plugin: ExecutorPlugin | None = None) -> Path | None:
    """按引擎 + 技能名解析技能目录（跨该引擎全部技能根查找）。

    ``skill_name`` 为技能相对路径（``/`` 分隔，可嵌套，如
    ``software-development/spike``）；必须为非空相对路径、任一组件不得
    为 ``..`` / ``.``、不含反斜杠；对应目录内存在 SKILL.md 才返回。
    未命中返回 None。
    """
    if not skill_name or skill_name in (".", ".."):
        return None
    p = Path(skill_name)
    if p.is_absolute():
        return None
    for part in p.parts:
        if part in ("..", ".") or "/" in part or "\\" in part:
            return None
    for root in engine_skills_roots(engine_name, plugin):
        candidate = root.joinpath(*p.parts)
        if candidate.is_dir() and (candidate / SKILL_MD).is_file():
            return candidate
    return None


# ---- 技能同步（issue #328）：合并全部执行引擎技能去重后复制到各引擎技能根目录 ----

# 同步互斥锁：防止并发触发同步时相互覆盖（FastAPI 写接口并发调用场景）
_sync_lock = threading.Lock()


def _collect_skills_pool(engines: list) -> tuple[list[dict], list[dict]]:
    """按引擎注册顺序收集全部执行引擎技能，同名去重保留第一个版本。

    返回 ``(merged, deduped)``：merged 为去重后的技能清单（dict 保持
    插入顺序 = 引擎注册顺序 → 根目录顺序 → 根内名称字典序，同名取第一个），
    deduped 为被去重跳过的重复技能记录。每条记录为
    ``{name, engine, root, path}``（name 为技能相对路径，path 为源技能
    目录绝对路径）。
    """
    merged: dict[str, dict] = {}
    deduped: list[dict] = []
    for plugin in engines:
        for root in engine_skills_roots(plugin.name, plugin):
            for skill in iter_skills(root):
                record = {
                    "name": skill["name"],
                    "engine": plugin.name,
                    "root": str(root),
                    "path": skill["path"],
                }
                if record["name"] in merged:
                    deduped.append(record)
                else:
                    merged[record["name"]] = record
    return list(merged.values()), deduped


def sync_skills_to_engines(plugins: list | None = None) -> dict:
    """同步所有执行引擎技能（issue #328）。

    合并全部 executor 执行引擎（注册顺序）的技能，同名保留注册顺序第一个
    版本（其余去重跳过），再把去重后的技能复制到**每个**引擎的全部技能
    根目录（方案A，用户已确认）：

    - 目标根目录不存在自动创建；
    - 目标已存在同名技能（目录或文件）跳过，不覆盖（保留引擎侧既有内容）；
    - 单技能复制失败记录到 ``errors``，不中断整体同步（保留现场便于排查）。

    ``plugins`` 可选指定参与同步的执行引擎插件列表（默认全局注册表的全部
    executor 插件；测试可注入自定义列表隔离全局注册表）。

    返回结果统计：``merged`` 去重后技能清单、``deduped`` 去重跳过的重复
    技能、``targets`` 各引擎根目录的 added / skipped / errors 明细、
    ``summary`` 汇总计数（merged / deduped / copied / skipped / failed）。
    """
    engines = list(plugins) if plugins is not None \
        else list_plugins(PluginKind.EXECUTOR)
    with _sync_lock:
        merged, deduped = _collect_skills_pool(engines)
        targets: list[dict] = []
        copied = skipped = failed = 0
        for plugin in engines:
            for root in engine_skills_roots(plugin.name, plugin):
                entry = {"engine": plugin.name, "root": str(root),
                         "added": [], "skipped": [], "errors": []}
                for skill in merged:
                    # 技能名来自 iter_skills（相对路径、无 .. / 绝对路径），
                    # 直接按组件拼接目标路径，结构保持不变
                    target = root.joinpath(*Path(skill["name"]).parts)
                    if target.exists():
                        entry["skipped"].append(skill["name"])
                        skipped += 1
                        continue
                    try:
                        root.mkdir(parents=True, exist_ok=True)
                        # symlinks=True 保留符号链接本身、不解析跟随，
                        # 避免把链接指向的外部文件内容复制进技能目录
                        shutil.copytree(Path(skill["path"]), target,
                                        symlinks=True)
                        entry["added"].append(skill["name"])
                        copied += 1
                    except OSError as e:
                        entry["errors"].append(
                            {"name": skill["name"], "message": str(e)})
                        failed += 1
                if entry["added"] or entry["skipped"] or entry["errors"]:
                    targets.append(entry)
        return {
            "ok": True,
            "summary": {
                "engines": len(engines),
                "merged": len(merged),
                "deduped": len(deduped),
                "copied": copied,
                "skipped": skipped,
                "failed": failed,
            },
            "merged": merged,
            "deduped": deduped,
            "targets": targets,
        }
