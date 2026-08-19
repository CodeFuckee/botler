"""自动发版机制（issue #294）。

背景：仓库版本号由 frontend/scripts/gen-version.mjs 每次构建自增 patch 位
（逢百进位，issue #179/#283：1.3.99 → 1.4.0），持久化在 data/version.txt
（gitignored，构建产物 version.json 与之一致）。用户要求新增发版机制：
**每次中间版本号 +1（minor 进位）时自动发布新版本并重置 CHANGELOG.md**
（例如 1.3.99 → 1.4.0 发布 v1.4.0），发版脚本集成到 CI/CD 流程。

本模块提供发版判定与执行（脚本入口 scripts/release.py，CI 入口
.gitlab-ci.yml 的 release:auto job）：
  - should_release()：判定「当前版本中间位（minor）> 最近发布版本」或
    「尚无任何版本 tag（首次发版）」→ 满足发版条件；patch 自增 → 跳过；
  - run_release()：满足条件时执行发版——
      1. 复用 issue #289 的 release_changelog() 把 [Unreleased] 封版为
         版本节 `## [x.y.z] - 日期`、重置 [Unreleased]、归档超龄版本；
      2. 提交 CHANGELOG.md 变更到主分支（chore: 发布 vX.Y.Z 并重置
         CHANGELOG（issue #294），全角括号引用、不触发 autoclose）；
      3. 打 git tag vX.Y.Z 标记里程碑（发版后可据此回溯版本）；
      4. 推送主分支 + tag 到远端（--no-push 可跳过，本地演练/测试用）。

安全约束：版本号非法 / 版本倒退 / 需要推送但缺少凭据 / 无 [Unreleased]
内容（由 release_changelog 校验）等场景抛 ReleaseError 且不改动任何文件；
支持 dry_run 预览（不写盘、不提交、不打 tag、不推送）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from botler.changelog_release import ChangelogReleaseError, release_changelog

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(Exception):
    """发版失败（版本号非法 / 版本倒退 / 缺少推送凭据等），文件保持原样。"""


@dataclass
class ReleaseOutcome:
    """一次发版判定的结果摘要。

    action: released（已发版）/ skipped（触发条件不满足，跳过）
    version: 当前版本号
    last_released: 最近一次发布版本号（无 tag 时为 None）
    reason: 判定说明（跳过原因 / 发版原因）
    created_tag: 创建的 tag 名（跳过/dry-run 时为 None）
    commit_sha: 发版提交短 SHA（跳过/dry-run 时为 None）
    entries_moved: 封版条目数
    dry_run: 是否为预览模式
    """

    action: str
    version: str | None = None
    last_released: str | None = None
    reason: str = ""
    created_tag: str | None = None
    commit_sha: str | None = None
    entries_moved: int = 0
    dry_run: bool = False


# ---- 版本解析与比较 ----

def parse_version(version: str) -> tuple[int, int, int]:
    """严格解析版本号 x.y.z → (major, minor, patch)；非法格式抛 ReleaseError。"""
    value = version.strip()
    if not _VERSION_RE.match(value):
        raise ReleaseError(f"版本号格式非法（应为 x.y.z）: {version}")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def latest_released_version(tags: Iterable[str], tag_prefix: str = "v") -> str | None:
    """从 git tag 列表中找到最近一次发布的版本（语义版本号最大者）。

    只认 `{tag_prefix}x.y.z` 形态（默认 v 前缀），其余 tag（如 release-1.2、
    无前缀版本号、非法版本）一律忽略；无匹配时返回 None（首次发版）。
    """
    versions: list[tuple[int, int, int]] = []
    for tag in tags:
        if not tag.startswith(tag_prefix):
            continue
        raw = tag[len(tag_prefix):]
        try:
            versions.append(parse_version(raw))
        except ReleaseError:
            continue
    if not versions:
        return None
    best = max(versions)
    return f"{tag_prefix}{best[0]}.{best[1]}.{best[2]}"


def should_release(current_version: str, last_released_version: str | None) -> tuple[bool, str]:
    """发版触发判定：中间版本号 +1（minor 进位）时发版（issue #294）。

    规则：
      - 尚无任何版本 tag → 首次发版，发布当前版本；
      - 当前 (major, minor) > 最近发布 (major, minor) → 发版
        （1.3.99 → 1.4.0；major 进位 2.0.0 同样触发）；
      - 其余（仅 patch 自增，如 1.3.61 → 1.3.62）→ 跳过，保留到下次进位；
      - 当前版本低于最近发布版本 → 抛 ReleaseError（版本号异常，不误发版）。

    返回 (是否发版, 判定说明)。
    """
    current = parse_version(current_version)
    if last_released_version is None:
        return True, f"首次发版（尚无任何版本 tag），发布 {current_version}"
    last = parse_version(last_released_version)
    if current < last:
        raise ReleaseError(
            f"当前版本 {current_version} 低于最近发布版本 {last_released_version}，"
            "版本号异常（不应倒退），请检查版本文件/构建流程"
        )
    if (current[0], current[1]) > (last[0], last[1]):
        return True, (
            f"中间版本号进位（最近发布 {last_released_version} → 当前 "
            f"{current_version}），满足发版条件"
        )
    return False, (
        f"中间版本号未变化（最近发布 {last_released_version}，当前 "
        f"{current_version}），跳过发版（发版保留到下次 minor 进位）"
    )


def read_current_version(
    version_json: str | Path | None = None,
    version_file: str | Path | None = None,
) -> str | None:
    """读取当前版本号：version.json（CI 构建产物，含 version 字段）优先，
    回退 version.txt（data/version.txt 持久化文件）；均不可用时返回 None。
    容错：文件缺失 / 非法 JSON / 字段为空一律静默降级。
    """
    if version_json is not None:
        path = Path(version_json)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                value = str(data.get("version", "")).strip()
                if value:
                    return value
    if version_file is not None:
        path = Path(version_file)
        if path.is_file():
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value:
                return value
    return None


# ---- git 辅助 ----

def _git(repo_root: str | Path, *args: str) -> str:
    """在仓库根目录执行 git 命令，失败抛 ReleaseError（带 stderr 摘要）。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "未知错误"
        raise ReleaseError(f"git {' '.join(args)} 失败: {detail}")
    return result.stdout


# ---- 发版执行 ----

def run_release(
    changelog_path: str | Path,
    version: str | None = None,
    version_json: str | Path | None = None,
    version_file: str | Path | None = None,
    keep: int = 10,
    archive_path: str | Path | None = None,
    tag_prefix: str = "v",
    repo_root: str | Path | None = None,
    push_url: str | None = None,
    dry_run: bool = False,
    no_push: bool = False,
    force: bool = False,
) -> ReleaseOutcome:
    """执行一次发版判定与（可选）发布。

    参数:
        changelog_path: CHANGELOG.md 路径
        version: 发布版本号（x.y.z）；缺省按 version_json > version_file 读取
        version_json: dist/version.json 等构建产物（CI 用，含 version 字段）
        version_file: data/version.txt 等纯文本版本文件
        keep: CHANGELOG.md 保留的最近版本节数量（更早归档，issue #289）
        archive_path: 归档文件路径（默认 <仓库>/docs/CHANGELOG-archive.md）
        tag_prefix: 版本 tag 前缀（默认 v → v1.4.0）
        repo_root: 仓库根目录（git 操作 cwd；默认 changelog 所在目录）
        push_url: 推送地址；None 用 origin 远端；空串 = 需要推送但缺少凭据
                  （发版时报错，避免静默不推）
        dry_run: 只判定/预览，不写盘、不提交、不打 tag、不推送
        no_push: 执行封版/提交/tag 但不推送（本地演练/测试）
        force:   跳过触发条件强制发版（版本校验仍生效）

    返回:
        ReleaseOutcome 结果摘要；触发条件不满足时 action="skipped"。

    异常:
        ReleaseError: 版本号非法 / 版本倒退 / 版本缺失 / 缺少推送凭据 /
            CHANGELOG 封版失败（空 [Unreleased] 等）。异常时文件保持原样。
    """
    changelog = Path(changelog_path)
    root = Path(repo_root) if repo_root is not None else changelog.resolve().parent
    if not root.is_dir():
        raise ReleaseError(f"仓库根目录不存在: {root}")

    # 1. 确定当前版本：显式参数 > version.json > version.txt
    #    均未显式指定时回退 <仓库>/data/version.txt（与 release_changelog
    #    的缺省版本文件一致，本地发版直接可跑）
    current = version
    if current is None:
        if version_json is None and version_file is None:
            version_file = root / "data" / "version.txt"
        current = read_current_version(version_json, version_file)
    if not current or not current.strip():
        raise ReleaseError(
            "无法确定当前版本：--version 未指定，且 version.json / version.txt "
            "均缺失或内容为空"
        )
    current = current.strip()

    # 2. 最近发布版本（git tag 列表；dry-run 也只读，安全）
    tags = _git(root, "tag", "-l").splitlines()
    last = latest_released_version(tags, tag_prefix=tag_prefix)

    # 3. 触发判定（force 可跳过触发条件，版本校验仍生效）
    #    latest_released_version 返回带前缀的 tag（v1.4.0），此处剥掉前缀
    #    再交给 should_release 做版本比较
    last_bare = last[len(tag_prefix):] if last and last.startswith(tag_prefix) else last
    triggered, reason = should_release(current, last_bare)
    if not (triggered or force):
        return ReleaseOutcome(
            action="skipped",
            version=current,
            last_released=last,
            reason=reason,
            dry_run=dry_run,
        )

    # 4. 封版 CHANGELOG：[Unreleased] → 版本节、重置、归档（issue #289）
    try:
        result = release_changelog(
            changelog_path=changelog,
            version=current,
            keep=keep,
            archive_path=archive_path,
            dry_run=dry_run,
        )
    except ChangelogReleaseError as exc:
        raise ReleaseError(f"发版封版 CHANGELOG 失败：{exc}") from exc

    if dry_run:
        return ReleaseOutcome(
            action="released",
            version=current,
            last_released=last,
            reason=reason,
            entries_moved=result.entries_moved,
            dry_run=True,
        )

    # 5. 提交 CHANGELOG 变更（+ 归档文件）+ 打 tag
    archive = Path(archive_path) if archive_path is not None else root / "docs" / "CHANGELOG-archive.md"
    files_to_add = [os.path.relpath(changelog, root)]
    if archive.is_file():
        files_to_add.append(os.path.relpath(archive, root))
    _git(root, "add", "--", *files_to_add)
    commit_msg = f"chore: 发布 v{current} 并重置 CHANGELOG（issue #294）"
    _git(root, "commit", "-m", commit_msg)
    tag_name = f"{tag_prefix}{current}"
    _git(root, "tag", tag_name)
    sha = _git(root, "rev-parse", "--short", "HEAD")

    # 6. 推送主分支 + tag（no_push 跳过）
    if not no_push:
        if push_url is not None and not push_url.strip():
            raise ReleaseError(
                "需要推送发版提交与 tag，但 --push-url 为空（缺少推送凭据，"
                "如 CI 变量 GITLAB_BOT_TOKEN）"
            )
        target = push_url.strip() if push_url and push_url.strip() else "origin"
        _git(root, "push", "-q", target, "HEAD:refs/heads/main")
        _git(root, "push", "-q", target, f"refs/tags/{tag_name}")

    return ReleaseOutcome(
        action="released",
        version=current,
        last_released=last,
        reason=reason,
        created_tag=tag_name,
        commit_sha=sha,
        entries_moved=result.entries_moved,
        dry_run=False,
    )


__all__ = [
    "ReleaseError",
    "ReleaseOutcome",
    "latest_released_version",
    "parse_version",
    "read_current_version",
    "run_release",
    "should_release",
]
