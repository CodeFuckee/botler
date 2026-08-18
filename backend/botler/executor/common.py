"""executor 包公共工具与常量（issue #192 拆分）。

由原 executor.py 拆分而来：日志器、异常、通用行/JSON 读取与重试/日志
等共享常量。其余职责见 workspace.py（git 工作区）、process.py（引擎
子进程执行）、session.py（会话文件解析）、prompt.py（提示词渲染）。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("botler.executor")


class ExecutorError(Exception):
    pass


def _row_get(row, key, default=None):
    """兼容 sqlite3.Row 与 dict 的字段读取。

    database 层返回的是 sqlite3.Row（无 .get() 方法，issue #11）；
    调用方传 dict（如测试）时同样可用。键不存在返回 default。
    """
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError):
        return default
def _load_json_output(output: str) -> dict | None:
    """从 claude 输出中解析首个 JSON 对象，失败返回 None。

    容错两类污染：
    - 前缀：claude 无 stdin 时 stderr 先打印 "Warning: no stdin data
      received..."（executor 把 stderr 合并进 stdout），整串 json.loads
      必失败，导致 session_id 永不落库（断点续跑失效）、错误提取落空；
    - 尾随：同一次执行里 stderr 可能继续混入后续行。
    用 JSONDecoder.raw_decode 只取首个完整 JSON 对象，忽略其余内容。
    """
    if not output:
        return None
    start = output.find("{")
    if start == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(output[start:])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# 日志保留行数（落盘 + 失败评论摘要）
LOG_TAIL_LINES = 400
COMMENT_TAIL_CHARS = 3000

# 手动停止约定退出码（issue #35）：读循环检测到停止标记时返回，
# 区别于 124（超时）与其他环境失败，run_task 据此走停止收尾
STOP_EXIT_CODE = 125

# issue #280：任务启动阶段拉取 issue 遇 GitLab 瞬时故障（网关 502/限流/
# 网络抖动）时不立即判失败，按指数退避重试——08-17 生产事故：GitLab 短暂
# 不可用返回 502，44 个排队任务 get_issue 一次 502 即全部打成 failed，且
# 失败评论同样发不出，issue 上「没有任何回复评论」。重试耗尽后才判失败。
ISSUE_FETCH_MAX_ATTEMPTS = 5
ISSUE_FETCH_BASE_DELAY = 5.0
ISSUE_FETCH_MAX_DELAY = 60.0

# 收尾评论/标签尽力重试（同 issue #280）：GitLab 恢复后仍要保证用户能
# 收到失败反馈，不能只试一次就放弃。
FINISH_RETRY_ATTEMPTS = 5
FINISH_RETRY_BASE_DELAY = 5.0
FINISH_RETRY_MAX_DELAY = 60.0
