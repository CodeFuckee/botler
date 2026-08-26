"""任务失败原因自动分类单元测试（issue #274）。

覆盖：验收标准 1（典型失败：超时/401/无法解决分类正确）、验收标准 3
（分类错误时兜底 unknown 不报错：未知文本/空输入/非法正则/非法分类）、
规则可配置扩展（config failure_classify.rules 覆盖内置默认）。
"""

import pytest

from botler.failure_classify import (
    CATEGORY_ENGINE,
    CATEGORY_ENV,
    CATEGORY_UNKNOWN,
    CATEGORY_UNSOLVABLE,
    category_advice,
    category_label,
    classify_failure,
)


class TestTypicalFailures:
    """验收标准 1：典型失败（超时/401/无法解决）分类正确。"""

    @pytest.mark.parametrize("text", [
        "任务超时（>1800s），已强制终止进程组",
        "CI 流水线超时未完成，任务视为失败",
        "timed out after 30 seconds",
        "Request timed out",
    ])
    def test_timeout_is_env(self, text):
        assert classify_failure(text) == CATEGORY_ENV

    @pytest.mark.parametrize("text", [
        "获取 issue 123#274 失败: 401 Unauthorized",
        "GitLab API 返回 401，token 已失效",
        "authentication failed: invalid credentials",
        "认证失败：bot token 已过期",
    ])
    def test_401_auth_is_env(self, text):
        assert classify_failure(text) == CATEGORY_ENV

    @pytest.mark.parametrize("text", [
        "连接失败: network unreachable",
        "temporary failure in name resolution",
        "getaddrinfo failed: Name or service not known",
        "no space left on device（磁盘不足）",
        "磁盘空间不足: ENOSPC",
    ])
    def test_network_disk_is_env(self, text):
        assert classify_failure(text) == CATEGORY_ENV

    @pytest.mark.parametrize("text", [
        "Claude Code 报告无法解决该 issue",
        "hermes 报告无法解决该 issue",
        "agent 明确报告无法解决，自测未过",
        "无法解决：该问题需要人工评估",
        "unable to resolve this issue",
        "cannot complete the task",
    ])
    def test_unresolvable_is_unsolvable(self, text):
        assert classify_failure(text) == CATEGORY_UNSOLVABLE

    @pytest.mark.parametrize("text", [
        "找不到 claude 命令: claude（请先 npm install -g @anthropic-ai/claude-code）",
        "claude: command not found",
        "bash: claude: command not found",
    ])
    def test_command_missing_is_engine(self, text):
        assert classify_failure(text) == CATEGORY_ENGINE

    @pytest.mark.parametrize("text", [
        "API key 无效: anthropic 401 invalid x-api-key",
        "Invalid API key provided: sk-ant-...",
        "anthropic SDK 报错：API 密钥已失效",
    ])
    def test_api_key_invalid_is_engine(self, text):
        assert classify_failure(text) == CATEGORY_ENGINE

    @pytest.mark.parametrize("text", [
        "The usage limit has been reached",
        "模型调用失败: usage limit exceeded",
        "DeepSeek API error: Usage limits exceeded",
    ])
    def test_usage_limit_is_engine(self, text):
        """issue #421：模型用量限制应归为引擎类，而非未知类。"""
        assert classify_failure(text) == CATEGORY_ENGINE

    def test_combined_texts(self):
        """多段文本综合分类：原因 + 错误详情 + 输出。"""
        # 原因无关键词，但错误详情里有 401 → env
        assert classify_failure(
            "重试耗尽（2 次）后仍失败，最后退出码 1",
            '{"attempt": 1, "error": "GitLab 401 Unauthorized"}') == CATEGORY_ENV
        # 原因明确无法解决，输出里的 401 不覆盖（unsolvable 优先级最高）
        assert classify_failure(
            "Claude Code 报告无法解决该 issue",
            "Traceback: requests 401") == CATEGORY_UNSOLVABLE
        # 无法解决仍高于用量限制；引擎类用量限制高于环境类网络错误。
        assert classify_failure(
            "无法解决：需要人工评估",
            "The usage limit has been reached") == CATEGORY_UNSOLVABLE
        assert classify_failure(
            "network unreachable",
            "The usage limit has been reached") == CATEGORY_ENGINE

    @pytest.mark.parametrize("text", [
        # Windows 磁盘剩余空间数值中恰好包含 "429" 数字串（如 4294967.3 MB /
        # 429.2 MB），与引擎类限流规则 r"429" 撞车——修复前误判 engine
        # （issue #481 流水线 #1429 实测：backend:test 在 Windows runner 上
        # 该数值抖动导致预检失败任务分类 engine，测试随机失败）。
        "磁盘剩余 4294967.3 MB ≥ 阈值 2048 MB（C:\\data）",
        "磁盘剩余 429.2 MB < 阈值 2048 MB（C:\\data）",
        "任务执行前预检失败：git ls-remote file:///tmp/x.git 失败（exit 128）: "
        "fatal: 'C:/Windows/TEMP/x.git' does not appear to be a git repository；"
        "磁盘剩余 4290.5 MB ≥ 阈值 2048 MB（C:\\data）",
    ])
    def test_bare_429_in_number_is_not_engine(self, text):
        """数字串里的 429 不应命中引擎类限流规则：环境类文本仍归 env。"""
        assert classify_failure(text) == CATEGORY_ENV

    @pytest.mark.parametrize("text", [
        "HTTP 429 Too Many Requests",
        "API 返回 429（rate limit exceeded）",
        "DeepSeek API error: 429 usage limits exceeded",
    ])
    def test_rate_limit_429_still_engine(self, text):
        """真正的 429 限流错误仍归引擎类（回归保护，修复不削弱原规则）。"""
        assert classify_failure(text) == CATEGORY_ENGINE

    @pytest.mark.parametrize("text", [
        "任务认领校验未通过，已停止处理：glab api user 返回当前绑定账号为 "
        "project_123_bot_xxx，但 Issue #414 分配给 @agent",
        "越权防护规则触发：Issue 未分配给绑定的 Agent 账号，已按越权防护规则终止本次任务",
    ])
    def test_claim_check_misjudge_is_unsolvable(self, text):
        """模板认领规则误判（issue #417 任务 #608）：agent 因账号名不一致
        误判「越权」秒退——agent 明确报告停止处理，归 unsolvable。"""
        assert classify_failure(text) == CATEGORY_UNSOLVABLE


class TestUnknownFallback:
    """验收标准 3：分类错误时兜底 unknown 不报错。"""

    def test_unknown_text(self):
        assert classify_failure("重试耗尽（2 次）后仍失败，最后退出码 1") == CATEGORY_UNKNOWN

    def test_empty_input(self):
        assert classify_failure("") == CATEGORY_UNKNOWN
        assert classify_failure(None) == CATEGORY_UNKNOWN
        assert classify_failure("", None, "") == CATEGORY_UNKNOWN

    def test_no_args(self):
        assert classify_failure() == CATEGORY_UNKNOWN

    def test_unknown_never_raises(self):
        """任意文本都不抛异常（兜底 unknown）。"""
        for text in ["", "随便什么内容", "⚠️ 特殊字符 [()] {}\\", "null", "None", "  "]:
            assert classify_failure(text) in {
                CATEGORY_ENV, CATEGORY_ENGINE, CATEGORY_UNSOLVABLE, CATEGORY_UNKNOWN}


class TestCustomRules:
    """验收标准：分类规则可配置扩展（config 或代码常量）。"""

    def test_custom_rules_override_defaults(self):
        custom = {"engine": [r"custom-engine-marker"]}
        assert classify_failure("custom-engine-marker hit", rules=custom) == CATEGORY_ENGINE
        # 覆盖后内置默认规则不再生效
        assert classify_failure("任务超时", rules=custom) == CATEGORY_UNKNOWN

    def test_invalid_regex_skipped(self):
        """非法正则被忽略，不抛错，其余规则仍生效。"""
        custom = {"env": [r"(", r"ok-marker"], "unknown": [r"should-ignore"]}
        assert classify_failure("ok-marker", rules=custom) == CATEGORY_ENV

    def test_invalid_category_ignored(self):
        custom = {"badcat": [r"x"], "env": [r"valid"]}
        assert classify_failure("valid", rules=custom) == CATEGORY_ENV

    def test_empty_rules_falls_back_to_default(self):
        assert classify_failure("任务超时", rules={}) == CATEGORY_ENV
        assert classify_failure("任务超时", rules=None) == CATEGORY_ENV


class TestLabelsAndAdvice:
    def test_labels(self):
        assert category_label(CATEGORY_ENV) == "环境类"
        assert category_label(CATEGORY_ENGINE) == "引擎类"
        assert category_label(CATEGORY_UNSOLVABLE) == "无法解决类"
        assert category_label(CATEGORY_UNKNOWN) == "未知"
        # 空/未知分类兜底「未知」，不抛错
        assert category_label(None) == "未知"
        assert category_label("") == "未知"
        assert category_label("bad") == "未知"

    def test_advice(self):
        assert "检查" in category_advice(CATEGORY_ENV)
        assert "引擎" in category_advice(CATEGORY_ENGINE)
        assert "issue" in category_advice(CATEGORY_UNSOLVABLE)
        assert category_advice(None) == category_advice(CATEGORY_UNKNOWN)
        assert category_advice("bad") == category_advice(CATEGORY_UNKNOWN)


class TestNotFound404:
    """issue #498：目标项目/issue 不存在（404「资源不存在」）应归为环境类。

    生产日志（任务 723，tender_document_spider 项目 89 已删除）：
    「任务失败: 获取 issue 89#19 失败: 资源不存在（404）: /projects/89/issues/19
    （失败分类：未知）」——404 未命中任何规则兜底 unknown，用户看不到
    明确分类。项目/issue 不存在属环境/配置问题（仓库配置的
    gitlab_project_id 失效、项目被删除/转移、token 无权限），应归 env。
    """

    @pytest.mark.parametrize("text", [
        "获取 issue 89#19 失败: 资源不存在（404）: /projects/89/issues/19",
        "GitLab API 错误 404: Project Not Found",
        "项目不可达（404）：项目不存在或为私有且 bot 账号无权限",
        "资源不存在（404）: /projects/89/issues/19/notes",
        "资源不存在（404）: /projects/89/issues",
    ])
    def test_404_not_found_is_env(self, text):
        assert classify_failure(text) == CATEGORY_ENV

    @pytest.mark.parametrize("text", [
        # 数字串里的 404 不应命中（借鉴 issue #481 的 429 教训）
        "磁盘剩余 4041.5 MB ≥ 阈值 2048 MB（C:\\data）",
        "磁盘剩余 40496.2 MB ≥ 阈值 2048 MB（C:\\data）",
    ])
    def test_bare_404_in_number_is_not_misclassified(self, text):
        """数字串里的 404 不应命中 404 规则：环境类文本仍归 env（不落 engine）。"""
        assert classify_failure(text) == CATEGORY_ENV
