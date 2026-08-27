"""执行引擎健康探测与状态展示（issue #236）。

背景：worker.engine 配置一个执行引擎（claude / hermes / dsh），任务失败后
最多重试 max_retries 次且重试仍用同一引擎——引擎本身坏了（claude CLI 未
安装、DeepSeek API Key 失效、dsh 运行时损坏）时重试多少次都会失败，任务
只能 bot-failed。本模块为引擎提供轻量健康探测：

- **claude**：执行 ``claude --version``（短超时），命令缺失 / 非零退出 /
  超时 = 不可用（探测命令与 executor 实际执行路径一致）；
- **hermes**：检查 hermes-agent SDK 可加载（``run_agent`` 模块，与
  HermesSdkRunner.start 同语义，见 hermes_sdk_runner.py）；
- **dsh**：检查 deepseek-harness SDK 可导入（``deepseek_harness`` 模块，
  与 DshRunner.start 同语义，见 dsh_runner.py）；
- **zcode**：执行 ``zcode --version``（与 claude 同模式，ZCode CLI）；
- **其他**（外部加载的引擎插件）：无内置探测实现，返回 unknown（不误报）。

探测结果经进程内注册表缓存（TTL 默认 30s，避免每次任务 / 页面刷新都拉起
``claude --version`` 子进程），供设置页「任务调度」卡片与插件管理页展示
引擎状态徽章（GET /api/settings worker.engine_health、GET /api/plugins
engine_health）；executor 每次任务尝试开始前调用 :func:`probe_engine` 做
实时探测（不走缓存），引擎不可用时自动降级到 worker.fallback_engines 配置
的备用引擎执行（见 executor.run_task，issue #236）。
"""

from __future__ import annotations

import subprocess
import threading
import time
from importlib.util import find_spec
from typing import Any, Callable

# 探测状态常量（落库 / 展示值，保持稳定）
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

# claude --version 探测超时（秒）：CLI 冷启动 1s 量级，10s 足够；
# 超时视为不可用（避免坏 CLI 挂起任务/页面请求）
PROBE_TIMEOUT = 10.0

# 探测结果缓存 TTL（秒）：设置页/插件页展示用，避免高频页面刷新反复
# 拉起子进程；executor 任务侧探测不走缓存（每次尝试开始前实时探测）
DEFAULT_TTL = 30.0


def probe_claude(cfg: Any) -> dict:
    """claude 引擎探测：执行 ``claude --version``。

    命令缺失（FileNotFoundError）/ 超时 / 非零退出 / 空输出 = 不可用。
    :param cfg: Settings 实例（取 claude_command）
    :return: {"status": "ok"|"fail", "detail": ...}
    """
    cmd = [cfg.claude_command, "--version"]
    try:
        # 命令来自部署机配置白名单，与 executor 同源（S603 禁用）
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except FileNotFoundError:
        return {"status": STATUS_FAIL,
                "detail": f"找不到 claude 命令: {cfg.claude_command}（请先 npm install -g @anthropic-ai/claude-code）"}
    except subprocess.TimeoutExpired:
        return {"status": STATUS_FAIL,
                "detail": f"claude --version 探测超时（>{PROBE_TIMEOUT:g}s）"}
    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        return {"status": STATUS_OK, "detail": out.splitlines()[-1][:120]}
    err = (proc.stderr or "").strip().splitlines()
    tail = err[-1][:120] if err else f"退出码 {proc.returncode}"
    return {"status": STATUS_FAIL, "detail": f"claude --version 失败（{tail}）"}


def probe_hermes(cfg: Any) -> dict:
    """hermes 引擎探测：检查 hermes-agent SDK 可加载（run_agent 模块）。

    与 HermesSdkRunner.start 的安装判定同语义（见 hermes_sdk_runner.py）。
    """
    if find_spec("run_agent") is not None:
        return {"status": STATUS_OK, "detail": "hermes-agent SDK 可加载（run_agent）"}
    return {"status": STATUS_FAIL,
            "detail": "hermes-agent SDK 未安装（run_agent 模块缺失，请按 docs/hermes-engine-deployment.md 部署）"}


def probe_dsh(cfg: Any) -> dict:
    """dsh 引擎探测：检查 deepseek-harness SDK 可导入（deepseek_harness 模块）。

    与 DshRunner.start 的安装判定同语义（见 dsh_runner.py）。
    """
    if find_spec("deepseek_harness") is not None:
        return {"status": STATUS_OK, "detail": "deepseek-harness SDK 可导入"}
    return {"status": STATUS_FAIL,
            "detail": "deepseek-harness SDK 未安装（deepseek_harness 模块缺失，请按 docs/dsh-engine-deployment.md 部署）"}


def probe_zcode(cfg: Any) -> dict:
    """zcode 引擎探测：执行 ``zcode --version``（与 probe_claude 同模式）。

    命令缺失（FileNotFoundError）/ 超时 / 非零退出 / 空输出 = 不可用。
    :param cfg: Settings 实例（取 zcode_command）
    :return: {"status": "ok"|"fail", "detail": ...}
    """
    cmd = [cfg.zcode_command, "--version"]
    try:
        # 命令来自部署机配置白名单，与 executor 同源（S603 禁用）
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except FileNotFoundError:
        return {"status": STATUS_FAIL,
                "detail": f"找不到 zcode 命令: {cfg.zcode_command}（请先安装 ZCode CLI 并加入 PATH）"}
    except subprocess.TimeoutExpired:
        return {"status": STATUS_FAIL,
                "detail": f"zcode --version 探测超时（>{PROBE_TIMEOUT:g}s）"}
    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        return {"status": STATUS_OK, "detail": out.splitlines()[-1][:120]}
    err = (proc.stderr or "").strip().splitlines()
    tail = err[-1][:120] if err else f"退出码 {proc.returncode}"
    return {"status": STATUS_FAIL, "detail": f"zcode --version 失败（{tail}）"}


# 内置引擎探测实现表（引擎名 → 探测函数）；外部插件引擎不在表中 → unknown
_PROBERS: dict[str, Callable[[Any], dict]] = {
    "claude": probe_claude,
    "hermes": probe_hermes,
    "dsh": probe_dsh,
    "zcode": probe_zcode,
}


def probe_engine(engine: str, cfg: Any) -> dict:
    """对单个引擎执行健康探测（引擎名小写归一，未知引擎返回 unknown）。

    :param engine: 引擎名（claude / hermes / dsh / 外部插件引擎名）
    :param cfg:    Settings 实例
    :return: {"status": "ok"|"fail"|"unknown", "detail": ...}
    """
    probe = _PROBERS.get(str(engine or "").strip().lower())
    if probe is None:
        return {"status": STATUS_UNKNOWN, "detail": "该引擎无健康探测实现"}
    try:
        return probe(cfg)
    except Exception as exc:  # noqa: BLE001 探测兜底：任何异常视为不可用，不阻塞任务/页面
        return {"status": STATUS_FAIL, "detail": f"引擎探测异常: {exc}"}


class EngineHealthRegistry:
    """引擎健康状态注册表（进程内缓存，线程安全）。

    probe 结果按引擎名缓存 TTL 秒；超时后下次 check 重新探测。设置页 /
    插件页展示与 executor 任务侧探测共用：展示走缓存（check），任务侧每次
    尝试开始前直接调 :func:`probe_engine` 实时探测（不经缓存）。
    """

    def __init__(self, ttl: float = DEFAULT_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def check(self, engine: str, cfg: Any,
              ttl: float | None = None) -> dict:
        """取引擎健康状态：TTL 内命中缓存，否则实时探测并缓存。"""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(engine)
            if hit is not None and now - hit[0] < (self.ttl if ttl is None else ttl):
                return hit[1]
        result = probe_engine(engine, cfg)
        with self._lock:
            self._cache[engine] = (time.monotonic(), result)
        return result

    def invalidate(self, engine: str | None = None) -> None:
        """清空指定引擎缓存（None = 全部清空）；配置变更后调用。"""
        with self._lock:
            if engine is None:
                self._cache.clear()
            else:
                self._cache.pop(engine, None)


# 全局注册表（进程内单例，与 botler.plugins 注册表同模式）
registry = EngineHealthRegistry()


def engine_health_snapshot(cfg: Any, engines: list[str] | None = None) -> list[dict]:
    """引擎健康状态快照（设置页「任务调度」卡片 / 插件管理页徽章数据源）。

    :param cfg:     Settings 实例
    :param engines: 引擎名列表；None 时返回内置三引擎（调用方需显式传入
                    插件注册表 executor 分类的完整引擎名以覆盖外部引擎）
    :return: 按引擎名排序的列表，每项含 engine / status / detail / ok /
             checked_at（探测时间，本地时区）
    """
    names = engines or list(_PROBERS)
    snapshot = []
    for name in sorted(names):
        result = registry.check(name, cfg)
        snapshot.append({
            "engine": name,
            "status": result["status"],
            # 前端徽章直接可用的布尔标记（unknown 视为未确认，不算 ok）
            "ok": result["status"] == STATUS_OK,
            "detail": result.get("detail", ""),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return snapshot
