#!/usr/bin/env python3
"""发版脚本（issue #294）：中间版本号 +1 时自动发布新版本并重置 CHANGELOG。

集成到 CI/CD（.gitlab-ci.yml 的 release:auto job），每次 main 分支 push
流水线成功后运行：读取本次构建版本（frontend/dist/version.json）→ 对比
最近发布版本（git tag vX.Y.Z）→ 中间版本号进位（如 1.3.99 → 1.4.0）或
首次发版时执行发版：
  1. 封版 CHANGELOG [Unreleased] 为版本节并重置（issue #289 轮转机制）；
  2. 提交 CHANGELOG.md 到主分支（chore: 发布 vX.Y.Z 并重置 CHANGELOG（issue #294））；
  3. 打 git tag vX.Y.Z 标记里程碑；
  4. 推送主分支 + tag 到远端。

用法:
    python3 scripts/release.py [--version 1.4.0] [--version-json frontend/dist/version.json]
                               [--version-file data/version.txt] [--keep 10]
                               [--tag-prefix v] [--push-url https://...] [--no-push]
                               [--dry-run] [--force]
    # --version 显式指定版本；缺省按 --version-json > --version-file 顺序读取
    #   （CI 用 --version-json 指向 frontend:build 产物；本地可缺省读 data/version.txt）
    # --push-url 推送地址（CI 用 GITLAB_BOT_TOKEN 构造；缺省用 origin 远端）
    # --no-push 只封版/提交/打 tag 不推送；--dry-run 仅预览不写盘
    # --force 跳过触发条件强制发版（版本校验仍生效）

示例:
    python3 scripts/release.py --dry-run
    python3 scripts/release.py --version-json frontend/dist/version.json --no-push
"""
import argparse
import sys
from pathlib import Path

# 允许从仓库根目录直接运行（backend/ 加入 sys.path 以导入 botler 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from botler.release import ReleaseError, run_release  # noqa: E402


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="自动发版（issue #294）：中间版本号 +1 时发布新版本并重置 CHANGELOG")
    parser.add_argument("--version", help="发布版本号 x.y.z（缺省按 version-json > version-file 读取）")
    parser.add_argument("--version-json", help="构建产物版本文件（如 frontend/dist/version.json，CI 用）")
    parser.add_argument("--version-file", help="纯文本版本文件（默认 <仓库>/data/version.txt）")
    parser.add_argument("--changelog", default=str(repo_root / "CHANGELOG.md"), help="CHANGELOG.md 路径")
    parser.add_argument("--archive", default=None, help="归档文件路径（默认 <仓库>/docs/CHANGELOG-archive.md）")
    parser.add_argument("--keep", type=int, default=10, help="CHANGELOG.md 保留最近 N 个版本节，更早的归档（默认 10）")
    parser.add_argument("--tag-prefix", default="v", help="版本 tag 前缀（默认 v）")
    parser.add_argument("--repo-root", default=str(repo_root), help="仓库根目录（git 操作 cwd）")
    parser.add_argument("--push-url", default=None, help="推送地址（缺省用 origin；CI 用 GITLAB_BOT_TOKEN 构造）")
    parser.add_argument("--no-push", action="store_true", help="只封版/提交/打 tag，不推送")
    parser.add_argument("--dry-run", action="store_true", help="仅判定/预览，不写盘、不提交、不打 tag、不推送")
    parser.add_argument("--force", action="store_true", help="跳过触发条件强制发版（版本校验仍生效）")
    args = parser.parse_args()

    try:
        outcome = run_release(
            changelog_path=args.changelog,
            version=args.version,
            version_json=args.version_json,
            version_file=args.version_file,
            keep=args.keep,
            archive_path=args.archive,
            tag_prefix=args.tag_prefix,
            repo_root=args.repo_root,
            push_url=args.push_url,
            dry_run=args.dry_run,
            no_push=args.no_push,
            force=args.force,
        )
    except ReleaseError as exc:
        print(f"✗ 发版失败：{exc}", file=sys.stderr)
        return 1

    if outcome.action == "skipped":
        print("⏭️  跳过发版（无需发布）：")
        print(f"   {outcome.reason}")
        return 0

    mode = "（dry-run 预览）" if outcome.dry_run else ""
    print(f"✓ 发版完成{mode}")
    print(f"  版本: {outcome.version}")
    print(f"  最近发布: {outcome.last_released if outcome.last_released else '（无，首次发版）'}")
    print(f"  判定: {outcome.reason}")
    print(f"  封版条目数: {outcome.entries_moved}")
    if outcome.created_tag:
        print(f"  创建 tag: {outcome.created_tag}")
    if outcome.commit_sha:
        print(f"  发版提交: {outcome.commit_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
