"""任务失败原因自动分类与处理建议（issue #274）。

任务失败时详情页只显示错误信息原文与日志，用户要自己判断失败类型：
是环境问题（token 失效/网络/超时/磁盘）、引擎问题（claude 命令缺失/
API 密钥失效），还是代码问题（agent 无法解决）。本模块在任务收尾时
对失败原因做规则分类（正则/关键字匹配错误信息），结果落库
tasks.failure_category，任务详情页展示分类徽章 + 处理建议文案，失败
评论同步带分类前缀，统计看板按分类聚合失败原因 Top 分布（与统计页
联动）。

分类：
- env：网络/认证/超时/磁盘
- engine：命令缺失/SDK 错误/API key 无效
- unsolvable：agent 明确报告无法解决/自测未过
- unknown：兜底（分类错误时不报错）

规则可配置扩展：默认规则在 DEFAULT_RULES（代码常量）；config.yaml 的
failure_classify.rules 可整体覆盖（见 config.example.yaml 示例与
config.py Settings.failure_classify_rules 读取）。
"""

from __future__ import annotations

import re

# ---- 分类常量（落库值，保持稳定） ----
CATEGORY_ENV = "env"
CATEGORY_ENGINE = "engine"
CATEGORY_UNSOLVABLE = "unsolvable"
CATEGORY_UNKNOWN = "unknown"

VALID_CATEGORIES = (CATEGORY_ENV, CATEGORY_ENGINE, CATEGORY_UNSOLVABLE, CATEGORY_UNKNOWN)

# 分类展示名（详情页徽章文案）
CATEGORY_LABELS = {
    CATEGORY_ENV: "环境类",
    CATEGORY_ENGINE: "引擎类",
    CATEGORY_UNSOLVABLE: "无法解决类",
    CATEGORY_UNKNOWN: "未知",
}

# 分类处理建议（详情页展示 + 失败评论前缀文案）
CATEGORY_ADVICE = {
    CATEGORY_ENV: "环境类：请检查仓库 token 与网络配置后点重试。",
    CATEGORY_ENGINE: "引擎类：请检查执行引擎配置（命令 / API Key）后点重试，或切换引擎。",
    CATEGORY_UNSOLVABLE: "无法解决类：请查看评论评估是否需要调整 issue 描述或拆分任务。",
    CATEGORY_UNKNOWN: "未知类：请查看错误日志与详细原因，判断后手动处理或点重试。",
}

# 默认规则（代码常量，可被 config failure_classify.rules 覆盖）。
# 顺序敏感：先匹配 unsolvable（agent 明确报告无法解决/自测未过），
# 再 env（网络/认证/超时/磁盘），再 engine（命令缺失/SDK/API key 无效），
# 全部未命中返回 unknown。每条规则为正则字符串（re.IGNORECASE 匹配）。
DEFAULT_RULES: dict[str, list[str]] = {
    CATEGORY_UNSOLVABLE: [
        r"无法解决", r"不能解决", r"无法完成", r"无法继续", r"不能继续",
        r"自测未过", r"测试未通过", r"自测没有通过",
        r"unable to (resolve|complete|solve)",
        r"cannot (resolve|complete|solve)",
        r"需要用户决策", r"等待用户回复",
        # 模板认领规则误判（issue #417 任务 #608）：agent 因执行环境绑定
        # 账号（项目机器人）与 assignee 不一致误判「越权」而自终止
        r"认领校验未通过", r"越权防护",
    ],
    CATEGORY_ENV: [
        r"超时", r"timeout", r"timed\s*out", r"time\s*out",
        r"网络", r"network", r"connection", r"connect\b", r"socket",
        r"temporary failure", r"name resolution", r"连接失败", r"连接被拒绝",
        r"getaddrinfo", r"resolve host", r"host not found", r"unreachable",
        r"401", r"403", r"unauthorized", r"authentication", r"认证失败",
        r"token[^。\n]{0,40}(invalid|expired|revoked|失效|无效|已过期)",
        r"(invalid|expired|revoked)[^。\n]{0,40}token",
        r"no space left", r"磁盘", r"disk full", r"quota", r"ENOSPC",
        r"任务执行前预检失败", r"预检失败", r"不可克隆", r"无法克隆", r"仓库不可达",
    ],
    CATEGORY_ENGINE: [
        r"command not found", r"找不到.*命令", r"请先 npm install",
        r"no such file or directory",
        r"api[ _-]?key", r"API 密钥",
        r"api[ _-]?key[^。\n]{0,40}(invalid|无效|失效|expired)",
        r"anthropic", r"claude[^。\n]{0,30}(error|失败|不可用|unavailable|not found)",
        # 429 限流：只匹配独立 429（前后不能是数字/小数点）——避免错误详情里
        # 的数值（如 Windows 磁盘剩余 "4294967.3 MB" / "429.2 MB"）误命中
        # （issue #481 流水线 #1429：backend:test 在 Windows runner 上因此
        # 把预检失败任务误分类为 engine）
        r"(?<!\d)429(?![\d.])", r"rate limit", r"usage\s+limits?",
        r"sdk[^。\n]{0,30}(error|失败|exception)",
        r"引擎错误", r"引擎不可用", r"engine (error|failed|unavailable)",
    ],
}

# 规则匹配顺序（unsolvable → engine → env；同分类内按列表顺序）。
# unsolvable 最优先：agent 明确报告无法解决/自测未过是最强信号；
# engine 次之：命令缺失/API key 无效/SDK 错误为明确的引擎信号（含 401
# 的 API key 报错也按 engine，避免被 env 的 401 认证规则抢先命中）；
# env 最后：网络/认证/超时/磁盘。全部未命中兜底 unknown。
_RULE_ORDER = (CATEGORY_UNSOLVABLE, CATEGORY_ENGINE, CATEGORY_ENV)


def _compile(rules: dict[str, list[str]] | None) -> dict[str, list[re.Pattern]]:
    """编译规则集；非法分类忽略，规则非法跳过（不抛错，兜底 unknown）。"""
    merged = DEFAULT_RULES if not rules else rules
    compiled: dict[str, list[re.Pattern]] = {}
    for category, patterns in merged.items():
        if category not in VALID_CATEGORIES:
            continue
        compiled[category] = []
        for p in patterns or []:
            try:
                compiled[category].append(
                    p if isinstance(p, re.Pattern) else re.compile(p, re.IGNORECASE))
            except re.error:
                # 用户配置了非法正则：跳过该条规则，不阻塞分类（兜底 unknown）
                continue
    return compiled


def classify_failure(*texts: str | None,
                     rules: dict[str, list[str]] | None = None) -> str:
    """对失败文本（失败原因/错误详情/执行输出，可多段）做规则分类。

    任一段命中即返回对应分类；全部未命中 / 输入为空返回 unknown
    （兜底不报错）。rules 为 None 时使用内置 DEFAULT_RULES
    （config failure_classify.rules 可覆盖，见 config.py）。
    """
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return CATEGORY_UNKNOWN
    compiled = _compile(rules)
    for category in _RULE_ORDER:
        for pattern in compiled.get(category, []):
            if pattern.search(blob):
                return category
    return CATEGORY_UNKNOWN


def category_label(category: str | None) -> str:
    """分类展示名（未知/空值返回「未知」，不抛错）。"""
    return CATEGORY_LABELS.get(category or "", CATEGORY_LABELS[CATEGORY_UNKNOWN])


def category_advice(category: str | None) -> str:
    """分类处理建议文案（未知/空值返回通用建议，不抛错）。"""
    return CATEGORY_ADVICE.get(category or "", CATEGORY_ADVICE[CATEGORY_UNKNOWN])
