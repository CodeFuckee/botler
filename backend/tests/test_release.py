"""自动发版机制测试（issue #294）。

背景：仓库版本号由 gen-version.mjs 每次构建自增 patch 位（逢百进位：
1.3.99 → 1.4.0），用户要求新增发版机制——**每次中间版本号 +1（minor
进位）时自动发布新版本并重置 CHANGELOG.md**，发版脚本集成到 CI/CD。
本测试断言：
  - 版本解析 / 最近发布版本识别（git tag 前缀过滤 + 语义版本最大）；
  - 发版触发判定：首次发版（无 tag）与 minor 进位触发，patch 自增跳过；
  - 版本号倒退 / 非法格式报错（不误发版）；
  - 版本来源读取优先级（version.json > version.txt > 缺省报错）；
  - run_release 全流程：封版 [Unreleased] → 重置 → 打 tag → 提交；
  - dry-run 只预览不改动任何文件；force 可跳过触发条件强制发版。
修复前模块不存在（ImportError），测试失败即复现「发版机制缺失」缺陷。
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "botler"))
from botler.release import (  # noqa: E402
    ReleaseError,
    latest_released_version,
    parse_version,
    read_current_version,
    run_release,
    should_release,
)

SAMPLE_CHANGELOG = """\
# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]
### Changed

- **发版机制（issue #294）**：中间版本号 +1 自动发版并重置 CHANGELOG。
- **示例条目（issue #999）**：占位。

## [1.0.0] - 2026-01-01
### Added

- 初始版本。
"""


def git(tmp_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(tmp_path), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"git {' '.join(args)} 失败: {result.stderr}"
    return result.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test Bot")
    git(tmp_path, "config", "user.email", "test@botler.local")
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE_CHANGELOG, encoding="utf-8")
    git(tmp_path, "add", "CHANGELOG.md")
    git(tmp_path, "commit", "-q", "-m", "chore: 初始 CHANGELOG（测试）")
    return tmp_path


# ---- 版本解析 ----

def test_parse_version_valid():
    assert parse_version("1.4.0") == (1, 4, 0)
    assert parse_version("1.3.99") == (1, 3, 99)
    assert parse_version("100.0.0") == (100, 0, 0)
    assert parse_version(" 2.0.1 ") == (2, 0, 1)


def test_parse_version_invalid():
    for bad in ("1.4", "1.4.0.1", "v1.4.0", "1.a.0", "1..0", "", "一.四.零"):
        with pytest.raises(ReleaseError):
            parse_version(bad)


# ---- 最近发布版本（git tag）----

def test_latest_released_version_picks_highest_semver():
    tags = ["v1.3.61", "v1.4.0", "v1.4.2", "v2.0.0", "v1.3.99"]
    assert latest_released_version(tags) == "v2.0.0"


def test_latest_released_version_ignores_invalid_and_prefixless():
    tags = ["1.4.0", "release-1.2", "vabc", "v1.3.5", "v1.3.9"]
    assert latest_released_version(tags) == "v1.3.9"


def test_latest_released_version_empty():
    assert latest_released_version([]) is None
    assert latest_released_version(["foo", "bar"]) is None


def test_latest_released_version_custom_prefix():
    tags = ["rel-1.2.3", "rel-1.10.0", "v1.0.0"]
    assert latest_released_version(tags, tag_prefix="rel-") == "rel-1.10.0"


# ---- 发版触发判定（中间版本号 +1）----

def test_should_release_first_release_when_no_prior_tag():
    triggered, reason = should_release("1.3.61", None)
    assert triggered is True
    assert "首次发版" in reason


def test_should_release_on_minor_increment():
    # 1.3.99 → 1.4.0：中间版本号 +1，必须发版（issue 示例）
    triggered, reason = should_release("1.4.0", "1.3.99")
    assert triggered is True
    assert "1.3.99" in reason and "1.4.0" in reason
    triggered, _ = should_release("1.4.0", "1.3.61")
    assert triggered is True


def test_should_not_release_on_patch_increment():
    triggered, reason = should_release("1.3.62", "1.3.61")
    assert triggered is False
    assert "跳过" in reason
    triggered, _ = should_release("1.3.61", "1.3.61")
    assert triggered is False


def test_should_release_on_major_increment():
    triggered, _ = should_release("2.0.0", "1.99.99")
    assert triggered is True


def test_should_release_rejects_version_going_backwards():
    with pytest.raises(ReleaseError):
        should_release("1.3.0", "1.4.0")
    with pytest.raises(ReleaseError):
        should_release("1.2.9", "1.3.0")


def test_should_release_rejects_invalid_version():
    with pytest.raises(ReleaseError):
        should_release("1.4", "1.3.0")
    with pytest.raises(ReleaseError):
        should_release("1.4.0", "not-a-version")


# ---- 版本来源读取 ----

def test_read_current_version_prefers_json_over_txt(tmp_path):
    version_json = tmp_path / "version.json"
    version_txt = tmp_path / "version.txt"
    version_json.write_text('{"version": "1.4.0", "commit": "abc"}', encoding="utf-8")
    version_txt.write_text("1.3.99\n", encoding="utf-8")
    assert read_current_version(str(version_json), str(version_txt)) == "1.4.0"


def test_read_current_version_falls_back_to_txt(tmp_path):
    version_txt = tmp_path / "version.txt"
    version_txt.write_text("1.3.61\n", encoding="utf-8")
    assert read_current_version(None, str(version_txt)) == "1.3.61"


def test_read_current_version_missing_files_returns_none(tmp_path):
    assert read_current_version(str(tmp_path / "nope.json"), str(tmp_path / "nope.txt")) is None


def test_read_current_version_malformed_json_falls_back(tmp_path):
    version_json = tmp_path / "version.json"
    version_txt = tmp_path / "version.txt"
    version_json.write_text("{broken", encoding="utf-8")
    version_txt.write_text("1.4.2\n", encoding="utf-8")
    assert read_current_version(str(version_json), str(version_txt)) == "1.4.2"


# ---- run_release 全流程 ----

def test_run_release_first_release_seals_changelog_creates_tag_and_commits(tmp_path):
    repo = init_repo(tmp_path)
    outcome = run_release(
        changelog_path=str(repo / "CHANGELOG.md"),
        version="1.3.61",
        repo_root=str(repo),
        no_push=True,
    )
    assert outcome.action == "released"
    assert outcome.version == "1.3.61"
    assert outcome.created_tag == "v1.3.61"
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # [Unreleased] 已重置（不再包含旧条目）
    assert "issue #294" not in text.split("## [1.3.61]")[0]
    # 新版本节包含原 [Unreleased] 内容
    assert "## [1.3.61] - " in text
    assert "发版机制（issue #294）" in text
    # tag 指向最新提交（发版提交）
    tag_sha = git(repo, "rev-parse", "v1.3.61")
    head_sha = git(repo, "rev-parse", "HEAD")
    assert tag_sha == head_sha
    # 发版提交信息符合规范（chore: + 全角括号 issue 引用）
    commit_msg = git(repo, "log", "-1", "--format=%s")
    assert commit_msg == "chore: 发布 v1.3.61 并重置 CHANGELOG（issue #294）"


def test_run_release_releases_on_minor_increment_and_skips_patch(tmp_path):
    repo = init_repo(tmp_path)
    run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.3.61", repo_root=str(repo), no_push=True)
    # 下一轮构建：patch 自增到 1.3.62 → 不应发版
    skipped = run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.3.62", repo_root=str(repo), no_push=True)
    assert skipped.action == "skipped"
    assert "跳过" in skipped.reason
    assert git(repo, "tag", "-l") == "v1.3.61"  # 没有新增 tag
    # 再下一轮：1.4.0（minor 进位）→ 应发版
    # 先给 [Unreleased] 加一条新条目（否则空节无法封版）
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    text = text.replace("## [Unreleased]\n", "## [Unreleased]\n### Changed\n\n- 1.4.0 新条目。\n\n", 1)
    (repo / "CHANGELOG.md").write_text(text, encoding="utf-8")
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "feat: 1.4.0 新功能（测试）")
    released = run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.4.0", repo_root=str(repo), no_push=True)
    assert released.action == "released"
    assert released.created_tag == "v1.4.0"
    assert "1.4.0 新条目。" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert set(git(repo, "tag", "-l").splitlines()) == {"v1.3.61", "v1.4.0"}


def test_run_release_dry_run_changes_nothing(tmp_path):
    repo = init_repo(tmp_path)
    before = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    outcome = run_release(
        changelog_path=str(repo / "CHANGELOG.md"),
        version="1.3.61",
        repo_root=str(repo),
        dry_run=True,
        no_push=True,
    )
    assert outcome.action == "released"
    assert outcome.dry_run is True
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert git(repo, "tag", "-l") == ""
    assert git(repo, "log", "-1", "--format=%s") == "chore: 初始 CHANGELOG（测试）"


def test_run_release_force_bypasses_trigger(tmp_path):
    repo = init_repo(tmp_path)
    run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.3.61", repo_root=str(repo), no_push=True)
    # patch 自增本应跳过，force 强制发版
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    text = text.replace("## [Unreleased]\n", "## [Unreleased]\n### Changed\n\n- 强制发版条目。\n\n", 1)
    (repo / "CHANGELOG.md").write_text(text, encoding="utf-8")
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "chore: 补条目（测试）")
    outcome = run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.3.62", repo_root=str(repo), no_push=True, force=True)
    assert outcome.action == "released"
    assert outcome.created_tag == "v1.3.62"


def test_run_release_rejects_backwards_version(tmp_path):
    repo = init_repo(tmp_path)
    run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.3.61", repo_root=str(repo), no_push=True)
    with pytest.raises(ReleaseError):
        run_release(changelog_path=str(repo / "CHANGELOG.md"), version="1.3.0", repo_root=str(repo), no_push=True)


def test_run_release_requires_push_credentials_when_push_needed(tmp_path):
    repo = init_repo(tmp_path)
    # --push-url 为空串 = 需要推送但缺少凭据 → 必须报错，不能静默跳过
    with pytest.raises(ReleaseError):
        run_release(
            changelog_path=str(repo / "CHANGELOG.md"),
            version="1.3.61",
            repo_root=str(repo),
            push_url="",
        )


def test_run_release_missing_version_raises(tmp_path):
    repo = init_repo(tmp_path)
    with pytest.raises(ReleaseError):
        run_release(
            changelog_path=str(repo / "CHANGELOG.md"),
            version=None,
            version_json=None,
            version_file=str(repo / "no-such-version.txt"),
            repo_root=str(repo),
            no_push=True,
        )
