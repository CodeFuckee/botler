# -*- coding: utf-8 -*-
"""GitLab Wiki 到 GitHub Wiki 同步流水线的配置回归测试（issue #175）。

测试只检查可审计的 CI 合约：同步作业仅由 main push 触发，使用 CI
Job Token 读取 GitLab Wiki、使用 GitHub PAT 写入 GitHub Wiki，并以普通
提交推送，避免强制覆盖目标 Wiki 历史。
"""
from pathlib import Path


CI_FILE = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"



def test_wiki_sync_job_uses_main_push_and_sync_stage():
    """Wiki 同步作业应作为 main push 流水线的一部分执行。"""
    text = CI_FILE.read_text(encoding="utf-8")
    assert "sync_wiki_to_github:" in text
    start = text.index("sync_wiki_to_github:")
    body = text[start:text.index("release:auto:", start)]
    assert "stage: sync" in body
    assert '$CI_COMMIT_BRANCH == "main"' in body
    assert '$CI_PIPELINE_SOURCE == "push"' in body


def test_wiki_sync_job_copies_gitlab_wiki_to_github_wiki_without_force_push():
    """作业必须使用两个 Wiki Git 端点，并用普通提交保留 GitHub 历史。"""
    text = CI_FILE.read_text(encoding="utf-8")
    start = text.index("sync_wiki_to_github:")
    body = text[start:text.index("release:auto:", start)]
    assert "gitlab-ci-token:${CI_JOB_TOKEN}" in body
    assert "${CI_SERVER_URL}/${CI_PROJECT_PATH}.wiki.git" in body
    assert "CI_SERVER_HOST" not in body
    assert "CodeFuckee/botler.wiki.git" in body
    assert "GITHUB_PUSH_TOKEN" in body
    assert 'git -C "${TARGET_DIR}" rm -r --ignore-unmatch -- .' in body
    assert 'git -C "${TARGET_DIR}" commit --quiet -m "docs: 同步 GitLab Wiki"' in body
    assert 'git -C "${TARGET_DIR}" push --quiet origin HEAD:master' in body
    assert "git push --quiet --force" not in body
