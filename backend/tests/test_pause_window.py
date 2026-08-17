"""定时暂停窗口纯函数测试（issue #169）。

需求：可配置时间窗口（如 09:00-12:00、14:00-18:00），窗口内调度器
停止开始新任务；已经开始执行的任务继续执行；未开始执行的任务等到
窗口结束后开始执行。

本文件测试解析与判断纯函数（botler/pause_window.py）：
- parse_window：窗口串 "HH:MM-HH:MM" → (start_minutes, end_minutes)；
- in_pause_window：当前时间是否落在暂停窗口内（含跨天窗口与星期过滤）。

测试先行：实现前这些用例应全部失败（模块不存在）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from botler.config import Settings
from botler.pause_window import in_pause_window, parse_window


def _mk(now: str, tz="Asia/Shanghai",
        windows=None, weekdays=None, pause_tz="") -> Settings:
    """构造带暂停窗口配置的 Settings（now 为固定时刻，tz 为其时区）。"""
    return Settings(
        gitlab_url="https://gitlab.example.com",
        gitlab_token="t",
        webhook_secret="s",
        pause_windows=windows or [],
        pause_weekdays=weekdays or [],
        pause_timezone=pause_tz,
    )


def _now(iso: str, tz="Asia/Shanghai") -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(tz))


# ---- parse_window ----

class TestParseWindow:
    def test_parse_normal(self):
        """正常窗口：09:00-12:00 → (540, 720)。"""
        assert parse_window("09:00-12:00") == (540, 720)

    def test_parse_single_digit_hour(self):
        """小时允许单个数字：9:00-12:00 与 09:00-12:00 等价。"""
        assert parse_window("9:00-12:00") == (540, 720)

    def test_parse_cross_midnight(self):
        """跨天窗口：22:00-02:00 → (1320, 120)（结束分钟小于开始分钟）。"""
        assert parse_window("22:00-02:00") == (1320, 120)

    def test_parse_whitespace_tolerant(self):
        """容忍首尾与分隔空白。"""
        assert parse_window("  09:00 - 12:00  ") == (540, 720)

    @pytest.mark.parametrize("bad", [
        "", "abc", "09:00", "09:00-", "-12:00", "09:00-12:00-13:00",
        "25:00-12:00", "09:60-12:00", "09:00-24:00", "9:0-12:00",
        "09:00-12:0", "9-12", "09-12:00", "09:00-12", "a9:00-12:00",
    ])
    def test_parse_invalid(self, bad):
        """非法窗口串一律返回 None（防御坏配置）。"""
        assert parse_window(bad) is None


# ---- in_pause_window ----

class TestInPauseWindow:
    def test_empty_windows_never_pause(self):
        """未配置窗口 = 不暂停（保持现状，任何时间都允许派发）。"""
        s = _mk("2026-08-18T10:00:00", windows=[])
        assert in_pause_window(s, _now("2026-08-18T10:00:00")) is False

    def test_inside_window(self):
        """窗口内返回 True。"""
        s = _mk("2026-08-18T10:00:00", windows=["09:00-12:00"])
        assert in_pause_window(s, _now("2026-08-18T10:00:00")) is True

    def test_start_boundary_inclusive(self):
        """恰在开始时刻（09:00）视为窗口内。"""
        s = _mk("2026-08-18T09:00:00", windows=["09:00-12:00"])
        assert in_pause_window(s, _now("2026-08-18T09:00:00")) is True

    def test_end_boundary_exclusive(self):
        """恰在结束时刻（12:00）视为窗口外（12:00 起恢复派发）。"""
        s = _mk("2026-08-18T12:00:00", windows=["09:00-12:00"])
        assert in_pause_window(s, _now("2026-08-18T12:00:00")) is False

    def test_outside_window(self):
        """窗口外（08:59）返回 False。"""
        s = _mk("2026-08-18T08:59:00", windows=["09:00-12:00"])
        assert in_pause_window(s, _now("2026-08-18T08:59:00")) is False

    def test_multiple_windows(self):
        """多窗口：任一命中即暂停（14:00-18:00 命中，13:00 未命中）。"""
        s = _mk("2026-08-18T15:00:00",
                windows=["09:00-12:00", "14:00-18:00"])
        assert in_pause_window(s, _now("2026-08-18T15:00:00")) is True
        assert in_pause_window(s, _now("2026-08-18T13:00:00")) is False

    def test_cross_midnight_inside(self):
        """跨天窗口：23:30 与次日 01:00 都在窗口内。"""
        s = _mk("2026-08-18T23:30:00", windows=["22:00-02:00"])
        assert in_pause_window(s, _now("2026-08-18T23:30:00")) is True
        assert in_pause_window(s, _now("2026-08-19T01:00:00")) is True

    def test_cross_midnight_outside(self):
        """跨天窗口：白天 12:00 不在窗口内。"""
        s = _mk("2026-08-18T12:00:00", windows=["22:00-02:00"])
        assert in_pause_window(s, _now("2026-08-18T12:00:00")) is False

    def test_weekdays_filter_hit(self):
        """星期过滤：周一（weekday=0）在配置内，窗口生效。"""
        s = _mk("2026-08-17T10:00:00",  # 2026-08-17 是周一
                windows=["09:00-12:00"], weekdays=[0])
        assert in_pause_window(s, _now("2026-08-17T10:00:00")) is True

    def test_weekdays_filter_miss(self):
        """星期过滤：周六（weekday=5）不在配置内，窗口不生效。"""
        s = _mk("2026-08-22T10:00:00",  # 2026-08-22 是周六
                windows=["09:00-12:00"], weekdays=[0, 1, 2, 3, 4])
        assert in_pause_window(s, _now("2026-08-22T10:00:00")) is False

    def test_weekdays_empty_means_everyday(self):
        """星期配置为空 = 每天都生效。"""
        s = _mk("2026-08-22T10:00:00",  # 周六
                windows=["09:00-12:00"], weekdays=[])
        assert in_pause_window(s, _now("2026-08-22T10:00:00")) is True

    def test_timezone_affects_judgement(self):
        """时区影响判断：UTC 07:00 = Asia/Shanghai 15:00（在 14:00-18:00 内）。"""
        s = _mk("2026-08-18T15:00:00",
                windows=["14:00-18:00"], pause_tz="Asia/Shanghai")
        utc_now = datetime.fromisoformat("2026-08-18T07:00:00").replace(
            tzinfo=timezone.utc)
        assert in_pause_window(s, utc_now) is True

    def test_invalid_windows_fallback_to_no_pause(self):
        """全非法窗口串 = 防御回退不暂停（服务可用性优先）。"""
        s = _mk("2026-08-18T10:00:00", windows=["not-a-window", "25:99-99:99"])
        assert in_pause_window(s, _now("2026-08-18T10:00:00")) is False

    def test_partial_invalid_windows_ignored(self):
        """部分非法窗口被忽略，其余正常生效。"""
        s = _mk("2026-08-18T10:00:00",
                windows=["bad-format", "09:00-12:00"])
        assert in_pause_window(s, _now("2026-08-18T10:00:00")) is True

    def test_default_now_uses_current_time(self):
        """now 缺省时用当前时间判断（不抛异常，默认不在暂停状态时 False）。"""
        s = _mk("2026-08-18T10:00:00", windows=["09:00-12:00"])
        # 当前时间大概率不在窗口内；只断言不抛异常且返回布尔
        result = in_pause_window(s)
        assert isinstance(result, bool)

    def test_invalid_timezone_default_now_fallback_local(self):
        """配置了非法时区名（手动编辑写坏）且 now 缺省：回退本地时间不抛异常。"""
        s = _mk("2026-08-18T10:00:00", windows=["09:00-12:00"],
                pause_tz="Mars/Olympus")
        result = in_pause_window(s)
        assert isinstance(result, bool)

    def test_invalid_timezone_aware_now_keeps_original(self):
        """配置非法时区名 + 传入带时区 now：沿用原时间判断（不抛异常）。"""
        s = _mk("2026-08-18T10:00:00", windows=["09:00-12:00"],
                pause_tz="Mars/Olympus")
        now = _now("2026-08-18T10:00:00")  # Asia/Shanghai 10:00，窗口内
        assert in_pause_window(s, now) is True
