"""执行引擎健康探测测试（issue #236）。

覆盖：claude 探测（命令缺失/非零退出/超时/正常）、hermes 与 dsh 的 SDK
可加载探测、未知引擎兜底 unknown、异常兜底 fail、注册表缓存 TTL 与失效、
快照结构（ok 布尔 + checked_at）。
"""

import subprocess
from types import SimpleNamespace

from botler.engine_health import (
    EngineHealthRegistry,
    engine_health_snapshot,
    probe_claude,
    probe_dsh,
    probe_engine,
    probe_hermes,
    registry,
)


def _cfg(claude_command: str = "claude"):
    return SimpleNamespace(claude_command=claude_command)


class TestProbeClaude:
    def test_ok_returns_version_line(self, monkeypatch):
        """claude --version 正常：返回 ok 与版本号。"""
        monkeypatch.setattr(
            "botler.engine_health.subprocess.run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="2.1.0\n", stderr=""))
        result = probe_claude(_cfg())
        assert result["status"] == "ok"
        assert "2.1.0" in result["detail"]

    def test_command_missing_fails(self, monkeypatch):
        """claude 命令缺失（FileNotFoundError）→ fail（验收：命令缺失被探测到）。"""
        def boom(*a, **k):
            raise FileNotFoundError("no such claude")
        monkeypatch.setattr("botler.engine_health.subprocess.run", boom)
        result = probe_claude(_cfg())
        assert result["status"] == "fail"
        assert "找不到 claude 命令" in result["detail"]

    def test_nonzero_exit_fails(self, monkeypatch):
        """claude --version 非零退出 → fail。"""
        monkeypatch.setattr(
            "botler.engine_health.subprocess.run",
            lambda *a, **k: SimpleNamespace(
                returncode=1, stdout="", stderr="boom\n"))
        result = probe_claude(_cfg())
        assert result["status"] == "fail"
        assert "boom" in result["detail"]

    def test_empty_output_fails(self, monkeypatch):
        """claude --version 退出码 0 但无输出 → fail（坏 CLI 占位）。"""
        monkeypatch.setattr(
            "botler.engine_health.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
        result = probe_claude(_cfg())
        assert result["status"] == "fail"

    def test_timeout_fails(self, monkeypatch):
        """claude --version 超时 → fail。"""
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=10)
        monkeypatch.setattr("botler.engine_health.subprocess.run", boom)
        result = probe_claude(_cfg())
        assert result["status"] == "fail"
        assert "超时" in result["detail"]


class TestProbeSdk:
    def test_hermes_ok(self, monkeypatch):
        """hermes：run_agent 模块可加载 → ok。"""
        monkeypatch.setattr(
            "botler.engine_health.find_spec",
            lambda name: name == "run_agent" and SimpleNamespace())
        assert probe_hermes(_cfg())["status"] == "ok"

    def test_hermes_missing(self, monkeypatch):
        """hermes：run_agent 缺失 → fail（runner 不可加载）。"""
        monkeypatch.setattr(
            "botler.engine_health.find_spec",
            lambda name: None)
        result = probe_hermes(_cfg())
        assert result["status"] == "fail"
        assert "run_agent" in result["detail"]

    def test_dsh_ok(self, monkeypatch):
        """dsh：deepseek_harness 模块可导入 → ok。"""
        monkeypatch.setattr(
            "botler.engine_health.find_spec",
            lambda name: name == "deepseek_harness" and SimpleNamespace())
        assert probe_dsh(_cfg())["status"] == "ok"

    def test_dsh_missing(self, monkeypatch):
        """dsh：deepseek_harness 缺失 → fail（SDK 导入失败）。"""
        monkeypatch.setattr(
            "botler.engine_health.find_spec",
            lambda name: None)
        result = probe_dsh(_cfg())
        assert result["status"] == "fail"
        assert "deepseek_harness" in result["detail"]


class TestProbeEngine:
    def test_unknown_engine_returns_unknown(self):
        """未注册/外部插件引擎 → unknown（不误报 ok/fail）。"""
        result = probe_engine("some_external_engine", _cfg())
        assert result["status"] == "unknown"

    def test_probe_exception_become_fail(self, monkeypatch):
        """探测函数抛异常 → fail 兜底（不向上抛，不阻塞任务/页面）。"""
        def boom(cfg):
            raise RuntimeError("probe crash")
        monkeypatch.setitem(probe_engine.__globals__["_PROBERS"], "claude", boom)
        result = probe_engine("claude", _cfg())
        assert result["status"] == "fail"
        assert "probe crash" in result["detail"]


class TestRegistry:
    def test_cache_hit_within_ttl(self, monkeypatch):
        """TTL 内重复 check 命中缓存，探测函数只执行一次。"""
        calls = []
        def fake_probe(engine, cfg):
            calls.append(engine)
            return {"status": "ok", "detail": "x"}
        monkeypatch.setattr("botler.engine_health.probe_engine", fake_probe)
        reg = EngineHealthRegistry(ttl=60)
        reg.check("claude", _cfg())
        reg.check("claude", _cfg())
        assert len(calls) == 1

    def test_cache_expired_reprobes(self, monkeypatch):
        """TTL 过期后重新探测。"""
        calls = []
        def fake_probe(engine, cfg):
            calls.append(engine)
            return {"status": "ok", "detail": "x"}
        monkeypatch.setattr("botler.engine_health.probe_engine", fake_probe)
        reg = EngineHealthRegistry(ttl=0)
        reg.check("claude", _cfg())
        reg.check("claude", _cfg())
        assert len(calls) == 2

    def test_invalidate_clears(self, monkeypatch):
        """invalidate 后强制重新探测。"""
        calls = []
        def fake_probe(engine, cfg):
            calls.append(engine)
            return {"status": "ok", "detail": "x"}
        monkeypatch.setattr("botler.engine_health.probe_engine", fake_probe)
        reg = EngineHealthRegistry(ttl=60)
        reg.check("claude", _cfg())
        reg.invalidate("claude")
        reg.check("claude", _cfg())
        assert len(calls) == 2

    def test_snapshot_structure(self, monkeypatch):
        """快照含 engine/status/ok/detail/checked_at，按引擎名排序。"""
        monkeypatch.setattr(
            "botler.engine_health.probe_engine",
            lambda engine, cfg: {"status": "ok" if engine == "dsh" else "fail",
                                 "detail": f"d-{engine}"})
        # 清空全局注册表缓存：API 测试可能已用真实探测结果缓存（TTL 30s），
        # 避免命中旧缓存导致本测试拿不到 monkeypatch 后的探测结果
        registry.invalidate()
        snap = engine_health_snapshot(_cfg(), engines=["dsh", "claude"])
        assert [s["engine"] for s in snap] == ["claude", "dsh"]
        by_name = {s["engine"]: s for s in snap}
        assert by_name["dsh"]["ok"] is True
        assert by_name["claude"]["ok"] is False
        assert by_name["claude"]["status"] == "fail"
        assert by_name["claude"]["detail"] == "d-claude"
        assert all("checked_at" in s for s in snap)

    def test_global_registry_singleton(self):
        """模块级 registry 为 EngineHealthRegistry 实例。"""
        assert isinstance(registry, EngineHealthRegistry)
