"""dsh 会话根目录压缩编码归一化（issue #302）。

deepseek-harness runtime 的 session-persistence-jsonl 插件默认以
zstd 压缩写会话文件（`session.jsonl.zstd`）。runtime 在加载/恢复任何
会话前会做根级编码检查（ensureRootEncoding）：只要会话根目录下存在
旧版 runtime / 旧部署遗留的明文 `session.jsonl`（压缩模式 none），
整个 runtime 拒绝启动，报

    session artifact ".../session.jsonl" uses .jsonl, but this backend
    is configured for compression "zstd"

任务 #415（tender_system issue #9）正是因此反复失败：旧部署写入的
明文会话文件残留，重启后所有重试/全新会话都在根级检查处直接失败。

本模块在 dsh 引擎启动前把会话根目录归一化到 zstd（与 runtime 默认
一致）：将遗留明文 `session.jsonl` 转换为 runtime 兼容的 zstd 分帧
格式（首帧恰为头部一行，后续帧按批压缩，解码后与明文逐字节一致），
并删除明文文件；若目录里已存在 zstd 副本则以 zstd 为准、直接删除
明文。归一化后根级编码检查通过，遗留会话仍可被 runtime 正常读取
（断点续跑不丢）。

结构约定（与 runtime 一致）：
    <root>/<project dir>/<session dir>/session.jsonl(.zstd)
"""

from __future__ import annotations

from pathlib import Path

# runtime logSuffix 对应的会话文件名（session-persistence-jsonl 插件）
SESSION_ARTIFACT_PLAIN = "session.jsonl"      # compression none
SESSION_ARTIFACT_ZSTD = "session.jsonl.zstd"  # compression zstd

# 单帧打包的字节数上限（与 runtime 按批写帧的语义一致，控制帧数量；
# 首帧固定为头部一行，不受此值影响）
_FRAME_BATCH_BYTES = 64 * 1024


def effective_session_root(cfg_session_root: str, workdir: Path) -> Path:
    """dsh 会话根目录：配置了 dsh_session_root 用之，否则 <workdir>/.sessions。

    与 SDK 行为对齐：SDK 仅在显式配置 session_root 时设置
    DSH_SESSION_ROOT 环境变量，否则 runtime 回退到进程 cwd 下的
    `./.sessions`（cwd = 任务工作区 workdir）。
    """
    root = (cfg_session_root or "").strip()
    return Path(root) if root else (Path(workdir) / ".sessions")


def _compress_zstd_frames(data: bytes) -> bytes:
    """把明文 JSONL 会话内容压缩为 runtime 兼容的 zstd 分帧字节流。

    格式约定（来自 runtime 源码）：
    - 首帧解码后必须恰为一行头部（assertZstdHeaderFrame 校验，
      首帧含且仅含一条以 \\n 结尾的记录）；
    - 其余内容按批打包为后续帧，全部帧解码拼接后与明文逐字节一致。
    """
    import zstandard  # 延迟导入：仅归一化路径需要（requirements 已声明）

    lines = data.splitlines(keepends=True)
    if not lines:
        return b""
    compressor = zstandard.ZstdCompressor()
    out = bytearray()
    out += compressor.compress(lines[0])
    batch: list[bytes] = []
    batch_size = 0
    for line in lines[1:]:
        batch.append(line)
        batch_size += len(line)
        if batch_size >= _FRAME_BATCH_BYTES:
            out += compressor.compress(b"".join(batch))
            batch.clear()
            batch_size = 0
    if batch:
        out += compressor.compress(b"".join(batch))
    return bytes(out)


def normalize_session_root_encoding(root: Path) -> int:
    """把会话根目录下遗留的明文 session.jsonl 归一化到 zstd。

    返回修复（转换或清理）的明文 artifact 数量；根目录不存在 / 无可
    修复项返回 0。单目录修复失败不抛出（跳过，由调用方记录日志），
    避免一个坏目录阻塞整个根目录的归一化。
    """
    if not root.is_dir():
        return 0
    fixed = 0
    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        for session_dir in sorted(p for p in project.iterdir() if p.is_dir()):
            plain = session_dir / SESSION_ARTIFACT_PLAIN
            if not plain.is_file():
                continue
            zstd = session_dir / SESSION_ARTIFACT_ZSTD
            try:
                if zstd.is_file():
                    # 已有 zstd 副本（runtime 格式），以 zstd 为准，删除明文
                    plain.unlink()
                else:
                    data = plain.read_bytes()
                    if data:
                        zstd.write_bytes(_compress_zstd_frames(data))
                    # 空明文文件无可恢复内容，直接删除（不生成空 artifact）
                    plain.unlink()
                fixed += 1
            except OSError:
                # 单目录修复失败不阻塞整体归一化
                continue
    return fixed
