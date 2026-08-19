"""仓库级任务参数覆盖解析测试（issue #237）。

effective_task_params 按「仓库级 > 全局」解析任务的超时/重试/引擎，
是调度器/执行器与任务列表/详情展示共用的唯一口径：
- 仓库字段留空（None/空串）→ 继承全局，行为与现状完全一致；
- 仓库字段非空 → 覆盖全局，不污染其他仓库；
- settings_with_overrides 生成 frozen Settings 副本（测试 SimpleNamespace 兜底）。
"""

from types import SimpleNamespace

import pytest

from botler.repo_params import (
    SOURCE_GLOBAL, SOURCE_REPO,
    effective_task_params, settings_with_overrides,
)

GLOBAL = SimpleNamespace(task_timeout_seconds=1800, max_retries=2, engine="claude")


class TestEffectiveTaskParams:
    """按「仓库级 > 全局」解析。"""

    def test_repo_none_falls_back_to_global(self):
        """repo 为 None（仓库未知/已删除）→ 全部继承全局。"""
        eff = effective_task_params(None, GLOBAL)
        assert eff["timeout_seconds"] == 1800 and eff["timeout_source"] == SOURCE_GLOBAL
        assert eff["max_retries"] == 2 and eff["max_retries_source"] == SOURCE_GLOBAL
        assert eff["engine"] == "claude" and eff["engine_source"] == SOURCE_GLOBAL

    def test_empty_repo_dict_falls_back_to_global(self):
        """空 dict / 缺字段 repo → 全部继承全局（兼容旧调用方只传 name/url）。"""
        eff = effective_task_params({"name": "demo", "url": "https://x/demo.git"}, GLOBAL)
        assert eff["timeout_seconds"] == 1800 and eff["timeout_source"] == SOURCE_GLOBAL
        assert eff["engine"] == "claude" and eff["engine_source"] == SOURCE_GLOBAL

    def test_full_override(self):
        """仓库三字段全配置 → 全部仓库级覆盖。"""
        repo = {"timeout_seconds": 600, "max_retries": 5, "engine": "dsh"}
        eff = effective_task_params(repo, GLOBAL)
        assert eff["timeout_seconds"] == 600 and eff["timeout_source"] == SOURCE_REPO
        assert eff["max_retries"] == 5 and eff["max_retries_source"] == SOURCE_REPO
        assert eff["engine"] == "dsh" and eff["engine_source"] == SOURCE_REPO

    def test_partial_override(self):
        """只配超时 → 仅超时覆盖，重试/引擎继承全局（不互相污染）。"""
        repo = {"timeout_seconds": 300}
        eff = effective_task_params(repo, GLOBAL)
        assert eff["timeout_seconds"] == 300 and eff["timeout_source"] == SOURCE_REPO
        assert eff["max_retries"] == 2 and eff["max_retries_source"] == SOURCE_GLOBAL
        assert eff["engine"] == "claude" and eff["engine_source"] == SOURCE_GLOBAL

    def test_null_fields_inherit_global(self):
        """仓库字段显式 NULL（清空后落库）→ 继承全局。"""
        repo = {"timeout_seconds": None, "max_retries": None, "engine": None}
        eff = effective_task_params(repo, GLOBAL)
        assert eff["timeout_source"] == SOURCE_GLOBAL
        assert eff["max_retries_source"] == SOURCE_GLOBAL
        assert eff["engine_source"] == SOURCE_GLOBAL

    def test_empty_string_engine_inherits_global(self):
        """engine 空串 → 继承全局（与 NULL 同语义）。"""
        repo = {"engine": ""}
        eff = effective_task_params(repo, GLOBAL)
        assert eff["engine"] == "claude" and eff["engine_source"] == SOURCE_GLOBAL

    def test_engine_normalized_to_lowercase(self):
        """仓库 engine 大写/带空白 → 归一化小写。"""
        repo = {"engine": "  DSH  "}
        eff = effective_task_params(repo, GLOBAL)
        assert eff["engine"] == "dsh" and eff["engine_source"] == SOURCE_REPO

    def test_max_retries_zero_is_explicit_override(self):
        """max_retries=0（明确不重试）是有效覆盖，不是「留空」。"""
        repo = {"max_retries": 0}
        eff = effective_task_params(repo, GLOBAL)
        assert eff["max_retries"] == 0 and eff["max_retries_source"] == SOURCE_REPO

    def test_sqlite_row_repo_supported(self):
        """sqlite3.Row 入参（executor 路径）与 dict 行为一致。"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row  # Database._conn 同款 row_factory
        conn.execute("CREATE TABLE t (timeout_seconds INTEGER, max_retries INTEGER, engine TEXT)")
        conn.execute("INSERT INTO t VALUES (900, 4, 'hermes')")
        row = conn.execute("SELECT * FROM t").fetchone()
        eff = effective_task_params(row, GLOBAL)
        assert eff["timeout_seconds"] == 900 and eff["timeout_source"] == SOURCE_REPO
        assert eff["max_retries"] == 4 and eff["max_retries_source"] == SOURCE_REPO
        assert eff["engine"] == "hermes" and eff["engine_source"] == SOURCE_REPO

    def test_global_engine_empty_falls_back_claude(self):
        """全局 engine 为空串时生效引擎兜底 claude（与 executor._engine 同口径）。"""
        global_empty = SimpleNamespace(task_timeout_seconds=1800, max_retries=2, engine="")
        eff = effective_task_params(None, global_empty)
        assert eff["engine"] == "claude"


class TestSettingsWithOverrides:
    """生效配置副本生成。"""

    def test_returns_copy_with_overrides(self):
        """返回新对象且覆盖字段生效，原对象不变（frozen Settings 语义）。"""
        eff_cfg = settings_with_overrides(
            GLOBAL, timeout_seconds=600, max_retries=0, engine="dsh")
        assert eff_cfg.task_timeout_seconds == 600
        assert eff_cfg.max_retries == 0
        assert eff_cfg.engine == "dsh"
        assert GLOBAL.task_timeout_seconds == 1800, "原全局配置不应被修改"

    def test_simple_namespace_fallback(self):
        """测试 monkeypatch 的 SimpleNamespace 配置同样可生成副本。"""
        ns = SimpleNamespace(task_timeout_seconds=1800, max_retries=2, engine="claude")
        eff_cfg = settings_with_overrides(
            ns, timeout_seconds=300, max_retries=1, engine="hermes")
        assert eff_cfg.task_timeout_seconds == 300
        assert eff_cfg.max_retries == 1
        assert eff_cfg.engine == "hermes"
        assert ns.task_timeout_seconds == 1800, "原 mock 配置不应被修改"
