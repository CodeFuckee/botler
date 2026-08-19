"""CHANGELOG.md 发布轮转机制测试（issue #289）。

背景：仓库 CHANGELOG.md 遵循 Keep a Changelog 约定，但此前没有任何发布/重置
机制，[Unreleased] 条目永远堆积（曾达 4500+ 行），版本节从未生成、历史也
不会归档。本测试断言 release_changelog() 具备完整发布轮转行为：
  - [Unreleased] 内容封版为版本节（## [x.y.z] - 日期）并重置为空白；
  - 超过 keep 保留数的旧版本节自动归档到 docs/CHANGELOG-archive.md；
  - 空 [Unreleased] / 版本已存在 / 文件缺失 / 缺少 Unreleased 节等
    异常场景给出明确错误且不破坏原文件。
修复前该模块不存在（ImportError），测试失败即复现「CHANGELOG 无法重置」缺陷。
"""

from pathlib import Path

import pytest

from botler.changelog_release import (
    ChangelogReleaseError,
    parse_changelog,
    release_changelog,
)

SAMPLE = """\
# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]
### Changed

- **修复定时暂停窗口全角兼容（issue #284）**：归一化后保存。
- **中断恢复机制 Phase 1（issue #281）**：会话 id 前置落库。

## [1.0.0] - 2026-01-01
### Added

- 初始版本。
"""


def write(tmp_path: Path, text: str = SAMPLE) -> Path:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---- 正常发布流程 ----

def test_release_moves_unreleased_to_versioned_section_and_resets(tmp_path):
    path = write(tmp_path)
    result = release_changelog(path, version="1.1.0", release_date="2026-02-01")
    text = path.read_text(encoding="utf-8")

    assert result.version == "1.1.0"
    assert result.entries_moved >= 2
    # [Unreleased] 被重置为空白（不再包含旧条目）
    assert "## [Unreleased]" in text
    assert "issue #284" not in text.split("## [1.1.0]")[0]
    # 新版本节包含原 [Unreleased] 全部条目，内容原样保留
    assert "## [1.1.0] - 2026-02-01" in text
    assert "**修复定时暂停窗口全角兼容（issue #284）**" in text
    assert "**中断恢复机制 Phase 1（issue #281）**" in text
    # 旧版本节仍在，且顺序为 Unreleased → 新版本 → 旧版本
    assert text.index("## [Unreleased]") < text.index("## [1.1.0] - 2026-02-01") < text.index("## [1.0.0] - 2026-01-01")


def test_release_preserves_preamble_and_subsection_headers(tmp_path):
    path = write(tmp_path)
    release_changelog(path, version="1.1.0", release_date="2026-02-01")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n\n本项目遵循 [Keep a Changelog]")
    assert "### Changed" in text
    assert "### Added" in text


def test_release_uses_default_version_from_version_file(tmp_path):
    path = write(tmp_path)
    version_file = tmp_path / "version.txt"
    version_file.write_text("2.3.4\n", encoding="utf-8")
    result = release_changelog(path, version_file=version_file, release_date="2026-02-01")
    assert result.version == "2.3.4"
    assert "## [2.3.4] - 2026-02-01" in path.read_text(encoding="utf-8")


def test_release_default_version_fallback(tmp_path):
    path = write(tmp_path, """\
# Changelog

## [Unreleased]
### Changed

- 仅有一条待发布条目。
""")
    result = release_changelog(path, release_date="2026-02-01")
    assert result.version == "1.0.0"
    assert "## [1.0.0] - 2026-02-01" in path.read_text(encoding="utf-8")


def test_release_repeated_calls_accumulate_versions_newest_first(tmp_path):
    path = write(tmp_path)
    release_changelog(path, version="1.1.0", release_date="2026-02-01")
    # 发布后 [Unreleased] 已重置，追加一条新条目后再发下一版
    current = path.read_text(encoding="utf-8")
    current = current.replace(
        "## [Unreleased]\n\n",
        "## [Unreleased]\n### Changed\n\n- 第二版新条目。\n\n",
    )
    path.write_text(current, encoding="utf-8")
    release_changelog(path, version="1.2.0", release_date="2026-03-01")
    text = path.read_text(encoding="utf-8")
    assert text.index("## [Unreleased]") < text.index("## [1.2.0] - 2026-03-01") < text.index("## [1.1.0] - 2026-02-01") < text.index("## [1.0.0] - 2026-01-01")
    assert "第二版新条目。" in text.split("## [1.2.0]")[1].split("## [1.1.0]")[0]


# ---- 归档轮转 ----

def test_release_archives_old_versions_beyond_keep(tmp_path):
    sample = """\
# Changelog

## [Unreleased]
### Changed

- 新功能条目。

## [1.3.0] - 2026-03-01
### Changed

- 1.3 条目。

## [1.2.0] - 2026-02-01
### Changed

- 1.2 条目。

## [1.1.0] - 2026-01-15
### Changed

- 1.1 条目。

## [1.0.0] - 2026-01-01
### Added

- 1.0 条目。
"""
    path = write(tmp_path, sample)
    result = release_changelog(path, version="1.4.0", release_date="2026-04-01", keep=2)
    text = path.read_text(encoding="utf-8")

    # keep=2：保留新版本 1.4.0 与最旧保留位的 1.3.0，其余归档
    assert "## [1.4.0] - 2026-04-01" in text
    assert "## [1.3.0] - 2026-03-01" in text
    assert "## [1.2.0] - 2026-02-01" not in text
    assert "## [1.1.0] - 2026-01-15" not in text
    assert "## [1.0.0] - 2026-01-01" not in text

    # 归档文件生成：按时间正序（旧版本在前）
    archive = tmp_path / "docs" / "CHANGELOG-archive.md"
    assert archive.exists()
    archive_text = archive.read_text(encoding="utf-8")
    assert archive_text.startswith("# Changelog 历史归档")
    assert archive_text.index("## [1.0.0] - 2026-01-01") < archive_text.index("## [1.1.0] - 2026-01-15") < archive_text.index("## [1.2.0] - 2026-02-01")
    assert "1.2 条目。" in archive_text
    assert result.archived == ["1.0.0", "1.1.0", "1.2.0"] or set(result.archived) == {"1.0.0", "1.1.0", "1.2.0"}


def test_release_appends_to_existing_archive(tmp_path):
    sample = """\
# Changelog

## [Unreleased]
### Changed

- 新条目。

## [1.3.0] - 2026-03-01
### Changed

- 1.3 条目。

## [1.2.0] - 2026-02-01
### Changed

- 1.2 条目。

## [1.1.0] - 2026-01-15
### Changed

- 1.1 条目。

## [1.0.0] - 2026-01-01
### Added

- 1.0 条目。
"""
    archive = tmp_path / "docs" / "CHANGELOG-archive.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("# Changelog 历史归档\n\n## [0.9.0] - 2025-12-01\n\n- 0.9 条目。\n", encoding="utf-8")
    path = write(tmp_path, sample)
    release_changelog(path, version="1.4.0", release_date="2026-04-01", keep=2)
    archive_text = archive.read_text(encoding="utf-8")
    # 旧归档内容保留，新归档内容追加在后
    assert "0.9 条目。" in archive_text
    assert "## [1.0.0] - 2026-01-01" in archive_text
    assert archive_text.index("0.9 条目。") < archive_text.index("## [1.0.0] - 2026-01-01")


def test_release_separates_version_sections_with_blank_line(tmp_path):
    path = write(tmp_path)
    release_changelog(path, version="1.1.0", release_date="2026-02-01")
    text = path.read_text(encoding="utf-8")
    assert "\n\n## [1.1.0] - 2026-02-01" in text
    assert "\n\n## [1.0.0] - 2026-01-01" in text


def test_release_keeps_all_versions_when_under_keep_limit(tmp_path):
    path = write(tmp_path)
    release_changelog(path, version="1.1.0", release_date="2026-02-01", keep=10)
    assert "## [1.0.0] - 2026-01-01" in path.read_text(encoding="utf-8")
    assert not (tmp_path / "docs" / "CHANGELOG-archive.md").exists()


# ---- 异常与安全 ----

def test_release_raises_when_unreleased_empty(tmp_path):
    path = write(tmp_path, """\
# Changelog

## [Unreleased]
### Changed
""")
    with pytest.raises(ChangelogReleaseError, match="没有可发布内容"):
        release_changelog(path, version="1.1.0", release_date="2026-02-01")
    assert "## [Unreleased]" in path.read_text(encoding="utf-8")


def test_release_raises_when_version_already_exists(tmp_path):
    path = write(tmp_path)
    with pytest.raises(ChangelogReleaseError, match="已存在"):
        release_changelog(path, version="1.0.0", release_date="2026-02-01")


def test_release_raises_when_changelog_missing(tmp_path):
    missing = tmp_path / "nope.md"
    with pytest.raises(ChangelogReleaseError, match="不存在"):
        release_changelog(missing, version="1.1.0", release_date="2026-02-01")


def test_release_raises_when_no_unreleased_section(tmp_path):
    path = write(tmp_path, """\
# Changelog

## [1.0.0] - 2026-01-01
### Added

- 初始版本。
""")
    with pytest.raises(ChangelogReleaseError, match="Unreleased"):
        release_changelog(path, version="1.1.0", release_date="2026-02-01")


def test_release_raises_on_invalid_version(tmp_path):
    path = write(tmp_path)
    with pytest.raises(ChangelogReleaseError, match="版本号"):
        release_changelog(path, version="abc", release_date="2026-02-01")


def test_release_failure_leaves_files_unchanged(tmp_path):
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ChangelogReleaseError):
        release_changelog(path, version="1.0.0", release_date="2026-02-01")
    assert path.read_text(encoding="utf-8") == before


def test_dry_run_does_not_modify_files(tmp_path):
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")
    result = release_changelog(path, version="1.1.0", release_date="2026-02-01", dry_run=True)
    assert result.version == "1.1.0"
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "docs" / "CHANGELOG-archive.md").exists()


# ---- 解析单元测试 ----

def test_parse_changelog_sections(tmp_path):
    preamble, sections = parse_changelog(SAMPLE)
    assert preamble.startswith("# Changelog")
    assert [s.title for s in sections] == ["Unreleased", "1.0.0"]
    assert sections[0].date is None
    assert sections[1].date == "2026-01-01"
    assert "issue #284" in sections[0].body
