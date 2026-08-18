"""CHANGELOG.md 发布/轮转机制（issue #289）。

背景：仓库 CHANGELOG.md 声明遵循 Keep a Changelog 约定，但此前没有任何
发布/重置机制——所有条目永远堆积在 [Unreleased] 下（曾达 4500+ 行），
版本节从未生成、历史记录也不会归档，文件无限膨胀。

本模块提供发布轮转工具 release_changelog()，用于「发版」时：
  1. 把当前 [Unreleased] 的全部内容封版为版本节 `## [x.y.z] - 日期`；
  2. 重置 [Unreleased] 为空白，供下一轮开发继续累积；
  3. 按 keep 保留最近 N 个版本节在 CHANGELOG.md 中，更早的版本节
     自动归档到 docs/CHANGELOG-archive.md（时间正序追加），
     使主文件体积可控。

安全约束：所有校验（文件存在 / 存在 Unreleased 节 / 有可发布内容 /
版本号合法 / 版本未重复）通过后才写盘；任一校验失败即抛
ChangelogReleaseError 且不改动任何文件；支持 dry_run 预览。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECTION_HEADER_RE = re.compile(r"^##\s+\[(?P<title>[^\]]+)\]\s*(?:-\s*(?P<date>\S+))?\s*$")
_ARCHIVE_HEADER = "# Changelog 历史归档\n\n本文件存放 CHANGELOG.md 轮转下来的历史版本记录；最新版本与 [Unreleased]\n见仓库根目录 CHANGELOG.md（Keep a Changelog 约定）。\n\n"


class ChangelogReleaseError(Exception):
    """发布操作失败（文件缺失 / 无可发布内容 / 版本已存在等），文件保持原样。"""


@dataclass
class Section:
    """CHANGELOG 的一个 `## [标题] - 日期` 节。

    title: 节标题（Unreleased 或版本号）
    date:  版本节日期（Unreleased 为 None）
    body:  节正文（不含标题行，保留原始换行）
    """

    title: str
    date: str | None
    body: str


@dataclass
class ReleaseResult:
    """一次发布操作的执行结果摘要。"""

    version: str
    release_date: str
    entries_moved: int
    archived: list[str]
    changelog_path: str
    archive_path: str
    dry_run: bool


def parse_changelog(text: str) -> tuple[str, list[Section]]:
    """解析 Keep a Changelog 文本，返回（前言, 节列表）。

    前言 = 首个 `## ` 标题之前的内容（# Changelog 与简介）；
    节列表按文件顺序，正文为标题行之后到下一个 `## ` 标题前的原文。
    """
    lines = text.splitlines(keepends=True)
    header_indices = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not header_indices:
        return text, []
    preamble = "".join(lines[: header_indices[0]])
    sections: list[Section] = []
    for idx, start in enumerate(header_indices):
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        header_line = lines[start]
        match = _SECTION_HEADER_RE.match(header_line.strip())
        title = match.group("title").strip() if match else header_line.strip().lstrip("#").strip()
        date = match.group("date").strip() if match and match.group("date") else None
        body = "".join(lines[start + 1 : end])
        sections.append(Section(title=title, date=date, body=body))
    return preamble, sections


def _has_substantive_content(body: str) -> bool:
    """判断节正文是否包含实质条目（忽略空行与 ### 小节标题）。"""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return True
    return False


def _count_entries(body: str) -> int:
    """统计节正文中的实质条目行数（忽略空行与 ### 小节标题）。"""
    count = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count += 1
    return count


def _format_section(section: Section) -> str:
    header = f"## [{section.title}]"
    if section.date:
        header += f" - {section.date}"
    body = section.body
    if body and not body.endswith("\n"):
        body += "\n"
    return header + "\n" + body


def read_current_version(version_file: str | Path | None, default: str = "1.0.0") -> str:
    """读取版本文件中的当前版本号；文件缺失/内容非法时返回 default。"""
    if version_file is not None:
        path = Path(version_file)
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if _VERSION_RE.match(raw):
                return raw
    return default


def _version_key(title: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in title.split("."))  # type: ignore[return-value]


def release_changelog(
    changelog_path: str | Path,
    version: str | None = None,
    release_date: str | None = None,
    keep: int = 10,
    archive_path: str | Path | None = None,
    version_file: str | Path | None = None,
    dry_run: bool = False,
) -> ReleaseResult:
    """执行一次 CHANGELOG 发布轮转（issue #289）。

    参数:
        changelog_path: CHANGELOG.md 路径
        version: 发布版本号（x.y.z）；缺省时读取 version_file，再缺省为 1.0.0
        release_date: 发布日期（YYYY-MM-DD）；缺省为今天
        keep: CHANGELOG.md 中保留的最近版本节数量（含本次新版本），更早的归档
        archive_path: 归档文件路径；缺省为 changelog 同目录 docs/CHANGELOG-archive.md
        version_file: 版本文件路径（缺省读取 changelog 同目录 data/version.txt）
        dry_run: 只计算不写盘，返回结果供预览

    返回:
        ReleaseResult 结果摘要

    异常:
        ChangelogReleaseError: 文件缺失 / 缺少 Unreleased 节 / 无可发布内容 /
            版本号非法 / 版本已存在 / keep 非法。异常时文件保持原样。
    """
    path = Path(changelog_path)
    if not path.is_file():
        raise ChangelogReleaseError(f"CHANGELOG 文件不存在: {path}")
    if keep < 1:
        raise ChangelogReleaseError(f"keep 必须 ≥ 1，当前为 {keep}")

    text = path.read_text(encoding="utf-8")
    preamble, sections = parse_changelog(text)

    unreleased = next((s for s in sections if s.title == "Unreleased"), None)
    if unreleased is None:
        raise ChangelogReleaseError("CHANGELOG.md 缺少 [Unreleased] 节，无法执行发布")
    if not _has_substantive_content(unreleased.body):
        raise ChangelogReleaseError("[Unreleased] 没有可发布内容（空节无需封版）")

    # 版本号：显式参数 > 版本文件 > 1.0.0
    if version is None:
        version = read_current_version(
            version_file if version_file is not None else path.parent / "data" / "version.txt"
        )
    version = version.strip()
    if not _VERSION_RE.match(version):
        raise ChangelogReleaseError(f"版本号格式非法（应为 x.y.z）: {version}")
    if any(s.title == version for s in sections):
        raise ChangelogReleaseError(f"版本 {version} 已存在于 CHANGELOG.md，请确认版本号")

    # 发布日期：显式参数 > 今天
    if release_date is None:
        release_date = dt.date.today().isoformat()
    release_date = release_date.strip()
    if not _DATE_RE.match(release_date):
        raise ChangelogReleaseError(f"发布日期格式非法（应为 YYYY-MM-DD）: {release_date}")

    # 版本节排序（新 → 旧），保留最近 keep 个，更早的归档
    versioned = [s for s in sections if _VERSION_RE.match(s.title)]
    versioned.sort(key=lambda s: _version_key(s.title), reverse=True)
    new_section = Section(title=version, date=release_date, body=unreleased.body)
    kept = [new_section] + versioned[: keep - 1]
    archived_sections = versioned[keep - 1 :]
    archived_titles = [s.title for s in archived_sections]

    # 新 CHANGELOG：前言 + 空白 [Unreleased] + 保留的版本节（新 → 旧）
    entries_moved = _count_entries(unreleased.body)
    new_changelog = preamble.rstrip("\n") + "\n\n## [Unreleased]\n\n"
    new_changelog += "\n\n".join(_format_section(s).rstrip("\n") for s in kept)
    new_changelog += "\n"

    # 归档：时间正序（旧 → 新）追加到 docs/CHANGELOG-archive.md
    archive = Path(archive_path) if archive_path is not None else path.parent / "docs" / "CHANGELOG-archive.md"
    if archive.resolve() == path.resolve():
        raise ChangelogReleaseError("归档文件不能与 CHANGELOG.md 相同")
    new_archive: str | None = None
    if archived_sections:
        chronological = list(reversed(archived_sections))
        if archive.is_file():
            existing = archive.read_text(encoding="utf-8")
            new_archive = existing.rstrip("\n") + "\n\n"
            new_archive += "\n\n".join(_format_section(s).rstrip("\n") for s in chronological)
            new_archive += "\n"
        else:
            new_archive = _ARCHIVE_HEADER
            new_archive += "\n\n".join(_format_section(s).rstrip("\n") for s in chronological)
            new_archive += "\n"

    if dry_run:
        return ReleaseResult(
            version=version,
            release_date=release_date,
            entries_moved=entries_moved,
            archived=archived_titles,
            changelog_path=str(path),
            archive_path=str(archive),
            dry_run=True,
        )

    # 全部校验通过后写盘：先归档后主文件
    if new_archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(new_archive, encoding="utf-8")
    path.write_text(new_changelog, encoding="utf-8")

    return ReleaseResult(
        version=version,
        release_date=release_date,
        entries_moved=entries_moved,
        archived=archived_titles,
        changelog_path=str(path),
        archive_path=str(archive),
        dry_run=False,
    )
