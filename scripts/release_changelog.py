#!/usr/bin/env python3
"""CHANGELOG.md 发布/轮转脚本（issue #289）。

在发版时把 [Unreleased] 封版为版本节、重置 [Unreleased]，并将超过
保留数量的历史版本节归档到 docs/CHANGELOG-archive.md，避免
CHANGELOG.md 无限累积（曾达 4500+ 行）。

用法:
    python3 scripts/release_changelog.py [--version 1.3.17] [--date 2026-08-18]
                                        [--keep 10] [--dry-run]
    # --version 缺省读取 data/version.txt（再缺省 1.0.0）
    # --date 缺省为今天
    # --keep 保留最近 N 个版本节在 CHANGELOG.md，更早的归档
    # --dry-run 仅预览不写盘

示例:
    python3 scripts/release_changelog.py --dry-run
    python3 scripts/release_changelog.py --version 1.3.17
"""
import argparse
import sys
from pathlib import Path

# 允许从仓库根目录直接运行（backend/ 加入 sys.path 以导入 botler 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from botler.changelog_release import ChangelogReleaseError, release_changelog  # noqa: E402


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="CHANGELOG.md 发布/轮转（issue #289）")
    parser.add_argument("--version", help="发布版本号 x.y.z（缺省读取 data/version.txt）")
    parser.add_argument("--date", help="发布日期 YYYY-MM-DD（缺省为今天）")
    parser.add_argument("--keep", type=int, default=10, help="保留最近 N 个版本节，更早的归档（默认 10）")
    parser.add_argument("--changelog", default=str(repo_root / "CHANGELOG.md"), help="CHANGELOG.md 路径")
    parser.add_argument("--archive", default=None, help="归档文件路径（默认 docs/CHANGELOG-archive.md）")
    parser.add_argument("--version-file", default=None, help="版本文件路径（默认 <仓库>/data/version.txt）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写盘")
    args = parser.parse_args()

    try:
        result = release_changelog(
            changelog_path=args.changelog,
            version=args.version,
            release_date=args.date,
            keep=args.keep,
            archive_path=args.archive,
            version_file=args.version_file,
            dry_run=args.dry_run,
        )
    except ChangelogReleaseError as exc:
        print(f"✗ 发布失败：{exc}", file=sys.stderr)
        return 1

    mode = "（dry-run 预览）" if result.dry_run else ""
    print(f"✓ CHANGELOG 发布完成{mode}")
    print(f"  版本: {result.version}，日期: {result.release_date}")
    print(f"  封版条目数: {result.entries_moved}")
    print(f"  归档版本: {result.archived if result.archived else '无'}")
    print(f"  CHANGELOG: {result.changelog_path}")
    print(f"  归档文件: {result.archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
