"""SQLite 连接复用基准（issue #191）：同负载下 P95 延迟与每秒操作数。

用法：
    python scripts/benchmark_db.py [--ops 2000]

输出混合读写负载（任务列表 list_tasks + 日志写入 add_log——Issue 指明
的高频路径）的 P50/P95 延迟（毫秒）与每秒操作数。连接复用前每次操作
都要 connect + PRAGMA + close；复用后同线程只初始化一次、写事务串行化。
"""

import argparse
import os
import statistics
import tempfile
import time

from botler.database import Database


def run(ops: int) -> None:
    path = os.path.join(tempfile.mkdtemp(), "bench.db")
    db = Database(path)
    repo_id = db.upsert_repo(1, "bench", "https://example.com/bench.git")
    task_id = db.create_task(repo_id, 1, 1, "基准任务")
    assert task_id is not None
    db.add_log(task_id, "info", "warmup")

    # 预热：稳定文件系统缓存 / WAL，避免前几次操作计入统计
    for _ in range(100):
        db.list_tasks()
        db.add_log(task_id, "debug", "warmup")

    latencies: list[float] = []
    for _ in range(ops):
        t0 = time.perf_counter()
        db.list_tasks()                     # 高频读：任务列表
        db.add_log(task_id, "info", "bench")  # 高频写：日志写入
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95)]
    total_s = sum(latencies) / 1000
    print(f"ops={ops} total={total_s:.3f}s ops/sec={ops / total_s:.0f} "
          f"p50={p50:.4f}ms p95={p95:.4f}ms")
    db.close()
    os.remove(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ops", type=int, default=2000)
    args = parser.parse_args()
    run(args.ops)
