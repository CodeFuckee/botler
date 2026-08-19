"""统一日志脱敏工具（issue #259）。

平台明文规定凭据不进日志，但执行器日志包含 claude 子进程输出、GitLab API
请求细节，异常堆栈可能带上 URL 中的 token（如 git remote 内嵌凭据）、
Authorization 头等内容，存在意外泄露风险。本模块提供写入日志前的统一
脱敏入口：

- ``redact(text)``：对文本应用全部规则打码。打码保留前后 3 位便于定位
  （如 ``tok***abc``），中间以 ``***`` 掩掉；
- 内置正则规则：git remote URL userinfo、Authorization / Proxy-Authorization
  头、Bearer 令牌、GitLab PAT（``glpat-*``）、``${ENV}`` 引用名；
- ``register_secret(value)``：注册动态密钥（子串规则，带字符边界防误伤）；
- ``register_config_secrets(cfg)``：把配置中声明的凭据 Key（gitlab token /
  webhook secret / dsh api_key / sso client_secret / minio 凭据 /
  ai_providers[].api_key / repos URL 内嵌凭据等）自动纳入脱敏规则；
- ``RedactFilter``：logging.Filter，挂到 root logger 后所有日志输出统一脱敏。

使用方式：在应用入口 ``logging.getLogger().addFilter(RedactFilter())``，
任务日志落库入口（Database.add_log）与 GitLab API 错误构造处分别调用
``redact()``，配置加载后调用 ``register_config_secrets(settings)``。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Iterable

# 打码格式（issue #259）：保留前后 3 位便于定位，中间 *** 打码，如 tok***abc
_KEEP = 3
_MASK = "***"

# 动态密钥最短注册长度：过短的值（如 "abc"）注册后会把普通日志内容误伤，
# 而真实密钥（API Key / PAT / client_secret 等）通常远长于该阈值
_MIN_SECRET_LEN = 6


def _mask(value: str) -> str:
    """打码：保留前后 _KEEP 位，中间以 *** 掩掉；过短整体掩掉避免膨胀。"""
    if len(value) <= _KEEP * 2:
        return _MASK
    return f"{value[:_KEEP]}{_MASK}{value[-_KEEP:]}"


# ---- 内置正则规则（按精确到宽泛排列，替换互不二次命中） ----

# 1) git remote URL userinfo：scheme://user:password@host → 密码打码。
#    只处理带 scheme 的 URL（scp-like / ssh 形态原样保留，与
#    git_remote.mask_url_token 的判定一致）；userinfo 无冒号（只有用户名）
#    不误伤。裸 glpat 形式（https://glpat-xxx@host）由 glpat 规则覆盖。
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)"
    r"(?P<user>[^/@\s:]+):(?P<pw>[^@\s]+)@")


def _url_userinfo_replace(m: re.Match) -> str:
    return f"{m.group('scheme')}{m.group('user')}:{_mask(m.group('pw'))}@"


# 2) Authorization / Proxy-Authorization 头：值整体打码（可选吞掉
#    Bearer / Basic 前缀避免与第 3 条规则二次命中）。
_AUTH_HEADER_RE = re.compile(
    r"(?i)(?P<head>(?:authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer\s+|basic\s+)?)(?P<tok>[^\s,]+)")


def _auth_header_replace(m: re.Match) -> str:
    return f"{m.group('head')}{_mask(m.group('tok'))}"


# 3) 正文中的 Bearer 令牌（无头前缀）：至少 8 位；纯字母且不足 20 位视为
#    常见英文短语（如 "Bearer authentication"）不误伤，真实令牌要么含
#    数字/符号、要么足够长。
_BEARER_RE = re.compile(
    r"(?i)(?P<prefix>bearer\s+)(?P<tok>[A-Za-z0-9._~+/=-]{8,})")


def _bearer_replace(m: re.Match) -> str:
    tok = m.group("tok")
    if tok.isalpha() and len(tok) < 20:
        return m.group(0)
    return f"{m.group('prefix')}{_mask(tok)}"


# 4) GitLab PAT：glpat- 前缀是强信号，令牌部分打码（保留 glpat- 便于定位）。
_GLPAT_RE = re.compile(r"\b(?P<prefix>glpat-)(?P<tok>[A-Za-z0-9_=-]{4,})")


def _glpat_replace(m: re.Match) -> str:
    return f"{m.group('prefix')}{_mask(m.group('tok'))}"


# 5) ${ENV} 引用名：配置 dump / 异常堆栈里出现 ${NAME} 引用时打码引用名
#    （凭据引用不落日志）；不带花括号的裸环境变量名（如「环境变量
#    GITLAB_BOT_TOKEN 未设置」的报错）不受影响，保持可排查性。
_ENV_REF_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")


def _env_ref_replace(m: re.Match) -> str:
    return f"${{{_mask(m.group('name'))}}}"


_PATTERN_RULES: tuple[tuple[re.Pattern[str], Any], ...] = (
    (_URL_USERINFO_RE, _url_userinfo_replace),
    (_AUTH_HEADER_RE, _auth_header_replace),
    (_BEARER_RE, _bearer_replace),
    (_GLPAT_RE, _glpat_replace),
    (_ENV_REF_RE, _env_ref_replace),
)

# ---- 动态密钥注册表（配置声明的 Key 自动纳入） ----

_secret_lock = threading.Lock()
_secrets: list[str] = []
_secret_re: re.Pattern[str] | None = None


def _rebuild_secret_re() -> None:
    """重建动态密钥合并正则。带字符边界（前后都不是字母/数字/下划线），
    避免把普通单词中间的片段误伤；密钥值 re.escape 防元字符。"""
    global _secret_re
    if not _secrets:
        _secret_re = None
        return
    pattern = "|".join(re.escape(s) for s in _secrets)
    _secret_re = re.compile(
        rf"(?<![A-Za-z0-9_])(?:{pattern})(?![A-Za-z0-9_])")


def register_secret(value: str) -> None:
    """注册一个动态密钥值（子串规则）。空值 / 过短值忽略。

    多次注册同一值幂等；注册后对后续 redact() 调用立即生效。
    """
    value = (value or "").strip()
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    with _secret_lock:
        if value in _secrets:
            return
        _secrets.append(value)
        _rebuild_secret_re()


def register_secrets(values: Iterable[str]) -> None:
    """批量注册密钥（一次性重建合并正则，配置加载场景比逐个注册高效）。"""
    added = False
    with _secret_lock:
        for value in values:
            value = (value or "").strip()
            if not value or len(value) < _MIN_SECRET_LEN or value in _secrets:
                continue
            _secrets.append(value)
            added = True
        if added:
            _rebuild_secret_re()


_URL_CRED_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@\s:]+:([^@\s]+)@")


def register_config_secrets(cfg: Any) -> None:
    """把配置中声明的凭据自动纳入脱敏规则（issue #259 怎样优化第 3 条）。

    覆盖：gitlab bot_token / owner_token / webhook_secret、webhook 推送
    authorization、dsh api_key、sso client_secret、minio access/secret
    key、ai_providers / image_models / vision_models 的 api_key，以及
    repos URL 内嵌凭据。${ENV} 引用在配置加载时已展开为明文值，注册
    展开后的值即可覆盖环境变量注入场景。
    """
    values: list[str] = [
        getattr(cfg, "gitlab_token", ""),
        getattr(cfg, "gitlab_owner_token", ""),
        getattr(cfg, "webhook_secret", ""),
        getattr(cfg, "webhook_authorization", ""),
        getattr(cfg, "dsh_api_key", ""),
        getattr(cfg, "sso_client_secret", ""),
        getattr(cfg, "minio_access_key", ""),
        getattr(cfg, "minio_secret_key", ""),
    ]
    for section in ("ai_providers", "image_models", "vision_models"):
        for item in getattr(cfg, section, None) or []:
            values.append(str(item.get("api_key") or ""))
    for repo in getattr(cfg, "repos", None) or []:
        url = str(getattr(repo, "url", "") or "")
        m = _URL_CRED_RE.match(url)
        if m:
            values.append(m.group(1))
    register_secrets(values)


# ---- 统一脱敏入口 ----

def redact(text: str | None) -> str:
    """对文本应用全部脱敏规则，返回打码后的文本。

    - 内置正则规则（URL userinfo / Authorization / Bearer / glpat / ${ENV}）
      先执行，再应用注册的动态密钥子串规则；
    - 无敏感内容时原样返回（正常日志不受影响）；
    - 重复调用幂等（打码结果不会再命中规则）。
    """
    if not text:
        return text or ""
    value = text
    for rule, replacer in _PATTERN_RULES:
        value = rule.sub(replacer, value)
    secret_re = _secret_re
    if secret_re is not None:
        value = secret_re.sub(lambda m: _mask(m.group(0)), value)
    return value


class RedactFilter(logging.Filter):
    """logging.Filter：对每条日志记录的 message 统一脱敏（issue #259）。

    挂到 root logger 后，全仓库所有 ``logger.*`` 输出（executor 子进程
    输出、GitLab API 请求细节、webhook 处理日志等）在写入前自动打码。
    脱敏失败（极端格式）不影响日志写入——脱敏是尽力而为的安全加固。

    用法：``logging.getLogger().addFilter(RedactFilter())``
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 消息格式化失败：原样放行
            return True
        try:
            record.msg = redact(message)
            record.args = ()
        except Exception:  # noqa: BLE001 脱敏失败：原样放行，不阻塞日志
            pass
        return True


def install_redact_filter() -> RedactFilter:
    """把 RedactFilter 挂到 root logger 及其全部 handler（issue #259）。

    注意：挂在 logger 上的 filter 只作用于该 logger 自身发出的记录（子
    logger 不继承父 logger 的 filter）；挂在 root 的 handler 上，则所有
    子 logger 传播到 root handler 的记录在写入前都会经过脱敏，这才是
    覆盖全仓库日志输出的可靠方式。uvicorn 等框架在应用导入后可能新增
    handler，应用启动（lifespan）时再次调用本函数可幂等补挂。
    """
    redact_filter = RedactFilter()
    root = logging.getLogger()
    if redact_filter not in root.filters:
        root.addFilter(redact_filter)
    for handler in root.handlers:
        if redact_filter not in handler.filters:
            handler.addFilter(redact_filter)
    # uvicorn 的 error/access logger 默认 propagate=False、自带 handler，
    # 一并补挂（access 日志的 URL 也可能携带 token 查询串）
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        if redact_filter not in uvicorn_logger.filters:
            uvicorn_logger.addFilter(redact_filter)
        for handler in uvicorn_logger.handlers:
            if redact_filter not in handler.filters:
                handler.addFilter(redact_filter)
    return redact_filter
