"""Docker 部署 hermes-agent SDK 安装校验（issue #171）。

hermes 引擎（issue #171 起）为 hermes agent SDK 进程内集成：hermes-agent
以源码 editable 安装进 botler 自身 venv。Docker 构建期无法访问宿主机
hermes-agent 源码（源码在 NAS 上经 compose 只读挂载），SDK 安装放在
容器启动时由 docker-entrypoint.sh 幂等执行。本测试静态校验部署产物
（docker-entrypoint.sh / Dockerfile / 部署文档）防回退：

- entrypoint 脚本存在且可执行；
- Dockerfile COPY 脚本并设为 ENTRYPOINT（CMD 仍为 uvicorn）；
- entrypoint 检测挂载源码（默认 /opt/hermes/hermes-agent，
  HERMES_SOURCE_DIR 可覆盖）才安装，幂等 + import 校验（fail fast）；
- 未挂载源码时跳过并告警（不阻塞其他引擎启动）；
- 部署文档与 compose 挂载说明同步覆盖。
"""


import pytest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]  # botler/ 仓库根
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
COMPOSE = ROOT / "docker-compose.yml"
HERMES_DOC = ROOT / "docs" / "hermes-engine-deployment.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少文件: {path}"
    return path.read_text(encoding="utf-8")


class TestEntrypoint:
    """docker-entrypoint.sh：存在 / 可执行 / 幂等安装 / fail fast。"""
    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 无 exec 权限位，entrypoint 可执行性断言仅适用 POSIX（issue #469）")

    def test_entrypoint_exists_and_executable(self):
        assert ENTRYPOINT.is_file(), "缺少 docker-entrypoint.sh"
        assert ENTRYPOINT.stat().st_mode & 0o111, "entrypoint 无执行权限"

    def test_entrypoint_source_dir_overridable(self):
        """源码路径默认 /opt/hermes/hermes-agent，HERMES_SOURCE_DIR 可覆盖。"""
        script = _read(ENTRYPOINT)
        assert "HERMES_SOURCE_DIR" in script
        assert "/opt/hermes/hermes-agent" in script

    def test_entrypoint_installs_editable_into_opt_venv(self):
        """editable 安装进 /opt/venv（后端运行时 venv）。"""
        script = _read(ENTRYPOINT)
        assert "/opt/venv/bin/pip install" in script
        assert " -e " in script

    def test_entrypoint_idempotent(self):
        """已安装直接跳过；安装后 import 校验（fail fast）。"""
        script = _read(ENTRYPOINT)
        assert "跳过" in script
        assert "assert importlib.util.find_spec(\"run_agent\")" in script

    def test_entrypoint_skips_when_source_missing(self):
        """未挂载源码时跳过安装并告警（不阻塞启动，其他引擎不受影响）。"""
        script = _read(ENTRYPOINT)
        assert "pyproject.toml" in script
        assert "未挂载" in script


class TestDockerfile:
    """Dockerfile：entrypoint 集成。"""

    def test_dockerfile_copies_entrypoint(self):
        dockerfile = _read(DOCKERFILE)
        assert "COPY docker-entrypoint.sh" in dockerfile

    def test_dockerfile_sets_entrypoint(self):
        dockerfile = _read(DOCKERFILE)
        assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in dockerfile

    def test_dockerfile_cmd_unchanged(self):
        """CMD 仍为 uvicorn 启动命令（entrypoint exec 透传）。"""
        dockerfile = _read(DOCKERFILE)
        assert "uvicorn" in dockerfile


class TestComposeAndDocs:
    """docker-compose 挂载说明与部署文档同步。"""

    def test_compose_documents_hermes_mounts(self):
        compose = _read(COMPOSE)
        assert "hermes-agent" in compose
        assert "HERMES_SOURCE_DIR" in compose

    def test_deployment_doc_docker_section(self):
        doc = _read(HERMES_DOC)
        assert "docker-entrypoint.sh" in doc
        assert "HERMES_SOURCE_DIR" in doc
