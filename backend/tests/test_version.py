"""平台版本信息读取与 /api/health 负载构建测试（issue #233）。

背景：前端无版本信息展示，用户无法确认当前部署版本。验收标准要求
「页面可见版本号（含 commit/时间）」「/api/health 版本一致」——
后端 /api/health 的版本号必须与前端构建产物（frontend/dist/version.json，
由 scripts/gen-version.mjs 生成）同源。本文件覆盖：
  1. read_version_info：version.json（版本 + 构建时间 + commit）解析、
     可选字段缺失/空值容错、非法 JSON / 缺失版本号回退 version.txt、
     双缺失返回 None；
  2. build_health_payload：有/无版本信息时的响应结构（version 字段
     永远存在，无信息时 0.0.0——健康检查本身可用）。
"""
import json

import pytest

from botler.version import build_health_payload, read_version_info


class TestReadVersionInfo:
    def test_version_json_full(self, tmp_path):
        """version.json 含版本 + 构建时间 + commit 全量解析。"""
        (tmp_path / "version.json").write_text(
            json.dumps({
                "version": "1.3.34",
                "buildTime": "2026-08-18 15:00:00",
                "commit": "abc12345",
            }),
            encoding="utf-8",
        )
        info = read_version_info(tmp_path / "version.json")
        assert info == {
            "version": "1.3.34",
            "buildTime": "2026-08-18 15:00:00",
            "commit": "abc12345",
        }

    def test_version_json_minimal(self, tmp_path):
        """version.json 只有 version 字段（旧版本产物）也能解析。"""
        (tmp_path / "version.json").write_text(
            json.dumps({"version": "1.3.33"}),
            encoding="utf-8",
        )
        assert read_version_info(tmp_path / "version.json") == {"version": "1.3.33"}

    def test_version_json_empty_optional_fields_omitted(self, tmp_path):
        """可选字段为空串/None 时从结果中剔除（前端降级隐藏）。"""
        (tmp_path / "version.json").write_text(
            json.dumps({"version": "1.3.34", "buildTime": "", "commit": None}),
            encoding="utf-8",
        )
        assert read_version_info(tmp_path / "version.json") == {"version": "1.3.34"}

    def test_version_json_malformed_falls_back_to_txt(self, tmp_path):
        """version.json 非法 JSON → 回退 version.txt。"""
        (tmp_path / "version.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "version.txt").write_text("9.9.9\n", encoding="utf-8")
        assert read_version_info(tmp_path / "version.json", tmp_path / "version.txt") == {
            "version": "9.9.9",
        }

    def test_version_json_missing_version_falls_back_to_txt(self, tmp_path):
        """version.json 缺 version 字段 → 回退 version.txt。"""
        (tmp_path / "version.json").write_text(
            json.dumps({"buildTime": "2026-08-18 15:00:00"}),
            encoding="utf-8",
        )
        (tmp_path / "version.txt").write_text("9.9.9\n", encoding="utf-8")
        assert read_version_info(tmp_path / "version.json", tmp_path / "version.txt") == {
            "version": "9.9.9",
        }

    def test_version_txt_fallback_only(self, tmp_path):
        """无 version.json 时读取 version.txt（本地开发/CI 测试兜底）。"""
        (tmp_path / "version.txt").write_text("1.2.3\n", encoding="utf-8")
        assert read_version_info(tmp_path / "none.json", tmp_path / "version.txt") == {
            "version": "1.2.3",
        }

    def test_none_when_both_missing(self, tmp_path):
        """双源均缺失 → None（健康检查仍可用，版本报 0.0.0）。"""
        assert read_version_info(tmp_path / "none.json", tmp_path / "none.txt") is None

    def test_none_when_version_txt_not_provided(self, tmp_path):
        """未提供 version.txt 回退路径且 version.json 缺失 → None。"""
        assert read_version_info(tmp_path / "none.json") is None

    def test_version_txt_blank_ignored(self, tmp_path):
        """version.txt 内容为空/空白 → None。"""
        (tmp_path / "version.txt").write_text("   \n", encoding="utf-8")
        assert read_version_info(tmp_path / "none.json", tmp_path / "version.txt") is None


class TestBuildHealthPayload:
    def test_with_version_info(self):
        """有版本信息：version 字段 + build 对象（buildTime/commit）+ 统计。"""
        payload = build_health_payload(
            {"version": "1.3.34", "buildTime": "2026-08-18 15:00:00", "commit": "abc12345"},
            scheduler_stats={"queued": 1, "running": 2},
            task_stats={"total": 5, "failed": 1},
        )
        assert payload["ok"] is True
        assert payload["version"] == "1.3.34"
        assert payload["build"] == {
            "version": "1.3.34",
            "buildTime": "2026-08-18 15:00:00",
            "commit": "abc12345",
        }
        assert payload["scheduler"] == {"queued": 1, "running": 2}
        assert payload["tasks"] == {"total": 5, "failed": 1}

    def test_without_version_info_reports_zero_version(self):
        """无版本信息（构建产物缺失）：ok 仍为 True，version 报 0.0.0。"""
        payload = build_health_payload(None, scheduler_stats={"queued": 0}, task_stats={"total": 0})
        assert payload["ok"] is True
        assert payload["version"] == "0.0.0"
        assert "build" not in payload

    def test_stats_optional(self):
        """scheduler/task 统计可缺省（不强制依赖 ctx 完整）。"""
        payload = build_health_payload({"version": "1.0.0"})
        assert payload["version"] == "1.0.0"
        assert "scheduler" not in payload
        assert "tasks" not in payload
