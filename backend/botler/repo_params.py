"""仓库级任务参数覆盖（issue #237）：按「仓库级 > 全局」优先级解析任务参数。

repos 表新增三个可选字段（NULL / 空串 = 继承全局，行为与现状完全一致）：
- timeout_seconds：任务超时（秒），覆盖 worker.task_timeout_seconds
- max_retries：任务最大重试次数，覆盖 worker.max_retries
- engine：执行引擎，覆盖 worker.engine（claude / hermes / dsh 等，未知回退 claude）

调度器/执行器与任务列表/详情统一经 :func:`effective_task_params` 解析，
保证「生效值」口径一致，避免各模块各自实现导致展示与执行不一致。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

# 参数来源标记（任务列表/详情展示「继承 or 仓库覆盖」用）
SOURCE_REPO = "repo"      # 仓库级覆盖
SOURCE_GLOBAL = "global"  # 继承全局

# 仓库级参数取值范围（与设置页全局校验一致：超时上限 7200s；
# 重试允许 0 = 不重试，上限 20 防手滑配置爆炸）
TIMEOUT_MIN = 1
TIMEOUT_MAX = 7200
MAX_RETRIES_MIN = 0
MAX_RETRIES_MAX = 20

_ENGINE_CHOICES: tuple[str, ...] | None = None


def engine_choices() -> tuple[str, ...]:
    """执行引擎白名单（与 settings.py ENGINE_CHOICES 同源：插件注册表）。"""
    global _ENGINE_CHOICES
    if _ENGINE_CHOICES is None:
        from .plugins import PluginKind, list_plugins
        _ENGINE_CHOICES = tuple(p.name for p in list_plugins(PluginKind.EXECUTOR))
    return _ENGINE_CHOICES


def normalize_engine(value) -> str | None:
    """归一化引擎名：strip + 小写；空串/None → None（继承全局）。

    不做白名单校验（executor._engine 对未知引擎回退 claude 属运行时
    防御），校验交给 API 层。
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    return s or None


def _as_repo_dict(repo) -> dict | None:
    """统一 repo 入参为 dict（支持 sqlite3.Row / dict / None）。

    executor 从 db.get_repo 拿到的是 sqlite3.Row（支持下标取列、不支持
    .get），API 层传的是 dict——统一转 dict 保证两种调用方行为一致。
    """
    if repo is None:
        return None
    if isinstance(repo, dict):
        return repo
    try:
        return dict(repo)
    except Exception:  # 无法转 dict（异常行）按无仓库处理，继承全局
        return None


def effective_task_params(repo, settings) -> dict:
    """按「仓库级 > 全局」解析任务参数。

    :param repo: repos 行 dict（可为 None——仓库未知/已删除时按全局）。
        三个覆盖字段（timeout_seconds / max_retries / engine）为
        None 或空串表示继承全局。
    :param settings: 全局 Settings（config.get()，worker 段）。

    :return: dict——
        - timeout_seconds / max_retries：生效整数值
        - engine：生效引擎（归一化小写；空回退 claude，与 executor._engine 同口径）
        - timeout_source / max_retries_source / engine_source：来源
          （SOURCE_REPO=仓库覆盖 / SOURCE_GLOBAL=继承全局）
    """
    repo = _as_repo_dict(repo)
    timeout = None
    max_retries = None
    engine = None
    if repo:
        timeout = repo.get("timeout_seconds")
        max_retries = repo.get("max_retries")
        engine = normalize_engine(repo.get("engine"))

    if timeout is not None:
        timeout_value = int(timeout)
        timeout_source = SOURCE_REPO
    else:
        timeout_value = settings.task_timeout_seconds
        timeout_source = SOURCE_GLOBAL

    if max_retries is not None:
        max_retries_value = int(max_retries)
        max_retries_source = SOURCE_REPO
    else:
        max_retries_value = settings.max_retries
        max_retries_source = SOURCE_GLOBAL

    if engine is not None:
        engine_value = engine
        engine_source = SOURCE_REPO
    else:
        engine_value = normalize_engine(settings.engine) or "claude"
        engine_source = SOURCE_GLOBAL

    return {
        "timeout_seconds": timeout_value,
        "timeout_source": timeout_source,
        "max_retries": max_retries_value,
        "max_retries_source": max_retries_source,
        "engine": engine_value,
        "engine_source": engine_source,
    }


def settings_with_overrides(settings, *, timeout_seconds: int,
                            max_retries: int, engine: str):
    """生成带仓库级覆盖的生效配置副本（issue #237）。

    生产配置（Settings）为 frozen dataclass，用 dataclasses.replace
    生成副本（原对象不可变、不污染全局）；测试 monkeypatch 传
    SimpleNamespace 时退化为普通属性复制（仅测试路径，行为一致）。
    """
    try:
        return replace(settings, task_timeout_seconds=timeout_seconds,
                       max_retries=max_retries, engine=engine)
    except TypeError:
        return SimpleNamespace(**{
            **vars(settings),
            "task_timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
            "engine": engine,
        })
