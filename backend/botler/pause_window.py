"""定时暂停窗口（issue #169）。

需求：可配置时间窗口（如 09:00-12:00、14:00-18:00），窗口内调度器
停止开始新任务；已经开始执行的任务继续执行；未开始执行的任务保留
在队列中，等到窗口结束后自动开始执行。

配置（config.yaml 的 worker 段，设置页「任务调度」卡片可编辑）：
- pause_windows: ["09:00-12:00", "14:00-18:00"]  窗口串 HH:MM-HH:MM
  （24 小时制，支持跨天如 22:00-02:00；空列表 = 不启用）
- pause_weekdays: [0, 1, 2, 3, 4]                 生效星期（0=周一…6=周日），
  空 = 每天都生效
- pause_timezone: "Asia/Shanghai"                 判断所用时区（IANA 名），
  空 = 服务器本地时区

防御策略：窗口串格式非法（手动编辑 config.yaml 写坏）时忽略该条，
全部非法 = 视为未配置（不暂停），保证调度服务可用性优先。

全角字符兼容（issue #284）：中文输入法/需求描述（issue #169 原文
"9:00—12:00"）常用全角冒号（：）、全角破折号（—）、en-dash（–）、
全角连字符（－）、波浪号（～）等，解析前统一归一化为半角，避免
窗口串被误判非法导致暂停窗口静默失效、调度器继续派发新任务。
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")

# 全角字符归一化表（issue #284）：中文标点/分隔符 → 半角，避免窗口串
# 解析失败。覆盖：全角冒号/分号/逗号/括号、全角破折号（—）、en-dash
# （–）、全角连字符（－）、全角/半角波浪号（～/~）、全角空格（U+3000）。
_FULLWIDTH_TRANS = str.maketrans({
    "：": ":", "；": ";", "，": ",", "（": "(", "）": ")",
    "—": "-", "–": "-", "－": "-", "～": "-", "~": "-",
    "　": " ",
})


def normalize_window(raw: str) -> str:
    """窗口串字符归一化：全角标点/分隔符 → 半角，其余字符原样保留。

    供 parse_window 解析前调用，也供保存侧（settings API / config 读取）
    把全角输入规范化为半角后落盘，保证 config.yaml 中窗口串格式统一。
    """
    return raw.translate(_FULLWIDTH_TRANS)


def parse_window(raw: str) -> tuple[int, int] | None:
    """解析窗口串 "HH:MM-HH:MM" → (start_minutes, end_minutes)。

    - 小时允许单个数字（9:00 与 09:00 等价），容忍首尾与分隔空白；
    - 跨天窗口（如 22:00-02:00）返回 end_minutes < start_minutes，
      由 in_pause_window 按跨天语义判断；
    - 格式非法 / 超出 0-23 时 / 分钟超出 0-59 时返回 None（防御坏配置）。
    """
    m = _WINDOW_RE.match(normalize_window(raw or ""))
    if m is None:
        return None
    sh, sm, eh, em = (int(g) for g in m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return sh * 60 + sm, eh * 60 + em


def _resolve_now(pause_timezone: str) -> datetime:
    """取当前时间：配置了时区用该时区，否则用服务器本地时区。"""
    if pause_timezone:
        try:
            return datetime.now(ZoneInfo(pause_timezone))
        except ZoneInfoNotFoundError:
            # 防御：config 里时区名非法（API 已拦截，手动编辑可能写坏）
            return datetime.now().astimezone()
    return datetime.now().astimezone()


def in_pause_window(settings, now: datetime | None = None) -> bool:
    """判断 now（缺省 = 当前时间）是否处于定时暂停窗口内。

    - 窗口含开始时刻、不含结束时刻（09:00-12:00：09:00 在窗口内，
      12:00 起恢复派发）；
    - 跨天窗口（22:00-02:00）：now >= 22:00 或 now < 02:00 均在窗口内；
    - pause_weekdays 非空时仅列出的星期生效（按 now 自身星期判断）；
    - 非法窗口串忽略；窗口列表为空或全部非法 = 不暂停。
    """
    windows = settings.pause_windows or []
    if not windows:
        return False
    if now is None:
        now = _resolve_now(settings.pause_timezone)
    elif settings.pause_timezone and now.tzinfo is not None:
        # 配置了判断时区且传入带时区时间（调度器 _now 返回本机时区）：
        # 先换算到目标时区再判断，保证服务器时区与配置时区不同时语义正确
        try:
            now = now.astimezone(ZoneInfo(settings.pause_timezone))
        except ZoneInfoNotFoundError:
            pass  # 时区名非法（手动编辑写坏）：沿用传入时间判断
    # 星期过滤：非空时当前星期不在列表内则窗口不生效
    if settings.pause_weekdays and now.weekday() not in settings.pause_weekdays:
        return False
    minutes = now.hour * 60 + now.minute
    for raw in windows:
        parsed = parse_window(raw)
        if parsed is None:
            continue  # 非法窗口串忽略（防御坏配置）
        start, end = parsed
        if start <= end:
            if start <= minutes < end:
                return True
        else:  # 跨天窗口：22:00-02:00 → minutes >= 22:00 或 minutes < 02:00
            if minutes >= start or minutes < end:
                return True
    return False
