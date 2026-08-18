"""dsh 会话根目录压缩编码归一化测试（issue #302）。

背景：deepseek-harness runtime 的 session-persistence-jsonl 插件默认
zstd 压缩；runtime 根级编码检查（ensureRootEncoding）遇到旧版部署
遗留的明文 session.jsonl 会拒绝启动，任务 #415 反复失败的根因。
本模块负责在 dsh 执行前把会话根目录归一化到 zstd。

覆盖：明文→zstd 转换（内容逐字节一致、首帧恰为头部一行）、已有 zstd
副本时删明文、空明文清理、缺失目录 / 无遗留 / 无关文件不动、多目录
批量、effective_session_root 解析。
"""

import zstandard
from pathlib import Path

from botler import dsh_sessions

HEADER = b'{"type":"session","version":0,"id":"s-1","createdAt":1,"cwd":"/x","delegationDepth":0}\n'
EVENT = b'{"type":"agent/inbox/spliced","seq":0,"time":1,"data":{}}\n'
PLAIN_TEXT = HEADER + EVENT + EVENT


def _decompress_zstd(path: Path) -> bytes:
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as f:
        return dctx.stream_reader(f).read()


def _mk_session(root: Path, project: str, session: str,
                plain: bytes | None = None, zstd: bytes | None = None) -> Path:
    """构造 <root>/<project>/<session>/ 会话目录，可选预置明文/zstd artifact。"""
    session_dir = root / project / session
    session_dir.mkdir(parents=True, exist_ok=True)
    if plain is not None:
        (session_dir / dsh_sessions.SESSION_ARTIFACT_PLAIN).write_bytes(plain)
    if zstd is not None:
        (session_dir / dsh_sessions.SESSION_ARTIFACT_ZSTD).write_bytes(zstd)
    return session_dir


class TestNormalize:
    def test_converts_plain_to_zstd_roundtrip(self, tmp_path):
        """明文 session.jsonl → zstd：明文删除、zstd 生成、解码逐字节一致。"""
        sd = _mk_session(tmp_path, "proj-a", "sess-1", plain=PLAIN_TEXT)
        fixed = dsh_sessions.normalize_session_root_encoding(tmp_path)
        assert fixed == 1
        assert not (sd / "session.jsonl").exists()
        zstd_path = sd / "session.jsonl.zstd"
        assert zstd_path.is_file()
        assert _decompress_zstd(zstd_path) == PLAIN_TEXT

    def test_first_frame_is_exactly_header_line(self, tmp_path):
        """首帧解码后必须恰为一行头部（runtime assertZstdHeaderFrame 契约）。"""
        sd = _mk_session(tmp_path, "proj-a", "sess-1", plain=PLAIN_TEXT)
        dsh_sessions.normalize_session_root_encoding(tmp_path)
        zstd_path = sd / "session.jsonl.zstd"
        first_frame = zstandard.ZstdCompressor().compress(HEADER)
        assert zstd_path.read_bytes().startswith(first_frame)

    def test_plain_with_zstd_sibling_removes_plain(self, tmp_path):
        """已有 zstd 副本（runtime 格式）时以 zstd 为准，仅删除明文。"""
        zstd_bytes = zstandard.ZstdCompressor().compress(PLAIN_TEXT)
        sd = _mk_session(tmp_path, "proj-a", "sess-1",
                         plain=PLAIN_TEXT, zstd=zstd_bytes)
        fixed = dsh_sessions.normalize_session_root_encoding(tmp_path)
        assert fixed == 1
        assert not (sd / "session.jsonl").exists()
        assert (sd / "session.jsonl.zstd").read_bytes() == zstd_bytes

    def test_empty_plain_removed_without_zstd(self, tmp_path):
        """空明文文件无可恢复内容：删除明文，不生成空 zstd artifact。"""
        sd = _mk_session(tmp_path, "proj-a", "sess-1", plain=b"")
        fixed = dsh_sessions.normalize_session_root_encoding(tmp_path)
        assert fixed == 1
        assert not (sd / "session.jsonl").exists()
        assert not (sd / "session.jsonl.zstd").exists()

    def test_missing_root_returns_zero(self, tmp_path):
        assert dsh_sessions.normalize_session_root_encoding(
            tmp_path / "nope") == 0

    def test_zstd_only_session_untouched(self, tmp_path):
        """无明文遗留：返回 0，zstd artifact 原样保留。"""
        zstd_bytes = zstandard.ZstdCompressor().compress(PLAIN_TEXT)
        sd = _mk_session(tmp_path, "proj-a", "sess-1", zstd=zstd_bytes)
        assert dsh_sessions.normalize_session_root_encoding(tmp_path) == 0
        assert (sd / "session.jsonl.zstd").read_bytes() == zstd_bytes

    def test_unrelated_files_untouched(self, tmp_path):
        """非 session.jsonl 的文件（含目录名伪装）不受影响。"""
        other = tmp_path / "proj-a" / "notes.txt"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"hello")
        fake_dir = tmp_path / "proj-a" / "session.jsonl"  # 同名目录
        fake_dir.mkdir()
        assert dsh_sessions.normalize_session_root_encoding(tmp_path) == 0
        assert other.read_bytes() == b"hello"
        assert fake_dir.is_dir()

    def test_multiple_projects_and_sessions(self, tmp_path):
        """多项目/多会话批量修复：全部明文转换并计数准确。"""
        sd1 = _mk_session(tmp_path, "proj-a", "s1", plain=PLAIN_TEXT)
        sd2 = _mk_session(tmp_path, "proj-a", "s2", plain=PLAIN_TEXT)
        sd3 = _mk_session(tmp_path, "proj-b", "s3", plain=HEADER)
        fixed = dsh_sessions.normalize_session_root_encoding(tmp_path)
        assert fixed == 3
        for sd in (sd1, sd2, sd3):
            assert not (sd / "session.jsonl").exists()
            assert (sd / "session.jsonl.zstd").is_file()

    def test_relative_root_accepted(self, tmp_path):
        """相对路径根目录同样工作（runtime 用相对路径时的一致性）。"""
        sd = _mk_session(tmp_path, "proj-a", "sess-1", plain=PLAIN_TEXT)
        assert dsh_sessions.normalize_session_root_encoding(
            Path(".")) == 0  # 当前目录无遗留，不影响 tmp_path 用例语义
        # 用真实相对路径再验一次
        import os
        old = os.getcwd()
        os.chdir(tmp_path)
        try:
            assert dsh_sessions.normalize_session_root_encoding(Path(".")) == 1
            assert not (sd / "session.jsonl").exists()
        finally:
            os.chdir(old)


class TestEffectiveSessionRoot:
    def test_default_is_workdir_dot_sessions(self, tmp_path):
        assert dsh_sessions.effective_session_root(
            "", tmp_path / "work") == tmp_path / "work" / ".sessions"

    def test_configured_root_wins(self, tmp_path):
        assert dsh_sessions.effective_session_root(
            str(tmp_path / "sroot"), tmp_path / "work") == tmp_path / "sroot"

    def test_configured_root_strips_whitespace(self, tmp_path):
        assert dsh_sessions.effective_session_root(
            f"  {tmp_path / 'sroot'}  ", tmp_path / "work") == tmp_path / "sroot"
