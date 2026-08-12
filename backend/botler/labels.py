"""标记库（issue #29）：默认标签清单 + 自定义标签校验。

标记库规范见 docs/labels.md。默认 13 个标签为内置清单（不可删除），
用户可通过 Web UI「标记库」页 / /api/labels 添加/删除自定义标签。
"""

from __future__ import annotations

import re

# 默认标签清单（与 docs/labels.md / scripts/sync_labels.py 保持一致）
DEFAULT_LABELS = [
    # 类型标签
    {"name": "bug", "color": "#d9534f", "description": "缺陷修复"},
    {"name": "feature", "color": "#009966", "description": "新功能"},
    {"name": "optimize", "color": "#cd5b45", "description": "性能/体验优化"},
    {"name": "ui", "color": "#cc338b", "description": "界面相关改动"},
    {"name": "docs", "color": "#428bca", "description": "文档"},
    {"name": "test", "color": "#8e44ad", "description": "补充测试"},
    {"name": "gitlab-ci", "color": "#f0ad4e", "description": "CI/CD 配置相关"},
    {"name": "chore", "color": "#95a5a6", "description": "杂务（依赖升级、重构、构建等）"},
    # 流程/状态标签
    {"name": "in-progress", "color": "#6699cc", "description": "处理中（bot 领取 issue 时自动添加）"},
    {"name": "review", "color": "#ff9800", "description": "待人工审查确认"},
    {"name": "blocked", "color": "#607d8b", "description": "等待补充信息/被阻塞"},
    {"name": "bot-done", "color": "#6699cc", "description": "bot 已完成开发，待用户确认后关闭"},
    {"name": "bot-failed", "color": "#6699cc", "description": "bot 处理失败，需人工介入"},
]

# GitLab 标签名规则：字母/数字开头，可含字母、数字、空格、下划线、横线（≤ 60 字符）
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,59}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_COLOR = "#6699cc"


def validate_label(name: str, color: str | None = None) -> str | None:
    """校验标签名与颜色。合法返回 None，否则返回错误消息（API 400 detail）。"""
    if not name:
        return "标签名不能为空"
    if not _NAME_RE.match(name):
        return "标签名须以字母或数字开头，可含字母、数字、空格、下划线、横线（≤ 60 字符）"
    if color and not _COLOR_RE.match(color):
        return "颜色须为 #RRGGBB 格式（如 #d9534f）"
    return None
