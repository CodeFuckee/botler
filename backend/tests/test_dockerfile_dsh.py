"""Docker 部署 deepseek-harness SDK 内置安装校验（issue #112）。

dsh 引擎 SDK（deepseek-harness-sdk==0.1.0rc6）原为可选依赖（issue #84：
requirements.txt 不声明、部署机手动安装）。issue #112 起 Docker 部署镜像
内置该 SDK，本测试静态校验部署产物（Dockerfile / verify-docker.sh / 文档）
的安装声明，防回退：

- SDK 显式全版本号（rc 预发布版必须写全，否则 pip 装不到）；
- 安装走阿里镜像（清华 pip 镜像未同步 rc 版，仅有 0.0.0.dev0 占位）；
- 镜像源可经 build arg 覆盖（内网/代理场景）；
- 安装进 /opt/venv（与后端运行时 venv 一致）；
- 构建期 import 校验（装不上立即构建失败，fail fast）；
- requirements.txt 不声明（主依赖仍走清华源，避免 rc 版解析失败
  阻塞全部依赖安装）；
- 部署冒烟脚本与部署文档同步覆盖。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # botler/ 仓库根
DOCKERFILE = ROOT / "Dockerfile"
VERIFY_SH = ROOT / "deploy" / "verify-docker.sh"
DSH_DOC = ROOT / "docs" / "dsh-engine-deployment.md"
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
README = ROOT / "README.md"

SDK_PIN = "deepseek-harness-sdk==0.1.0rc6"


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少文件: {path}"
    return path.read_text(encoding="utf-8")


class TestDockerfileInstallsDshSdk:
    """Dockerfile：镜像构建期安装 dsh SDK。"""

    def test_dockerfile_declares_sdk_pinned_full_version(self):
        """SDK 以显式全版本号声明（rc 预发布版缩写装不到）。"""
        dockerfile = _read(DOCKERFILE)
        assert SDK_PIN in dockerfile

    def test_sdk_install_uses_aliyun_mirror(self):
        """SDK 安装走阿里镜像（清华源未同步 rc 版）。"""
        dockerfile = _read(DOCKERFILE)
        # 与 SDK 安装同一条 RUN 内必须出现阿里镜像地址
        assert "mirrors.aliyun.com" in dockerfile

    def test_sdk_install_mirror_overridable_via_build_arg(self):
        """镜像源可经 DSH_INDEX_URL build arg 覆盖（内网代理场景）。"""
        dockerfile = _read(DOCKERFILE)
        assert "DSH_INDEX_URL" in dockerfile
        # ARG 默认值即阿里镜像，覆盖不影响开箱可用
        assert re.search(
            r"ARG\s+DSH_INDEX_URL=https://mirrors\.aliyun\.com/pypi/simple",
            dockerfile,
        )

    def test_sdk_installed_into_runtime_venv(self):
        """SDK 装入 /opt/venv（与后端运行时 venv 一致，非系统 pip）。"""
        dockerfile = _read(DOCKERFILE)
        assert "/opt/venv/bin/pip" in dockerfile

    def test_build_time_import_check(self):
        """构建期 import 校验：SDK 装不上时镜像构建直接失败（fail fast）。"""
        dockerfile = _read(DOCKERFILE)
        assert "from deepseek_harness import DeepSeekHarness" in dockerfile


class TestRequirementsKeepsSdkOptional:
    """requirements.txt 不声明 SDK：主依赖继续走清华源。"""

    def test_requirements_does_not_declare_sdk(self):
        requirements = _read(REQUIREMENTS)
        assert "deepseek-harness" not in requirements


class TestVerifyDockerSmokeChecksSdk:
    """deploy/verify-docker.sh 冒烟：容器内 SDK 可导入。"""

    def test_verify_script_checks_sdk_import(self):
        script = _read(VERIFY_SH)
        assert "deepseek_harness" in script
        assert "DeepSeekHarness" in script
        # 校验失败应阻断冒烟（die），而不是仅打印告警
        assert "die" in script


class TestDeploymentDocsSynced:
    """部署文档与 README 同步：Docker 已内置、pm2/systemd 仍手动。"""

    def test_dsh_deployment_doc_marks_docker_builtin(self):
        doc = _read(DSH_DOC)
        assert "Docker" in doc
        # Docker 部署无需手动安装
        assert ("已内置" in doc) or ("无需" in doc)

    def test_dsh_deployment_doc_keeps_manual_install_for_pm2(self):
        """pm2/systemd（非容器）部署路径保留手动安装指引。"""
        doc = _read(DSH_DOC)
        assert "pip install" in doc
        assert SDK_PIN in doc

    def test_readme_docker_section_mentions_sdk_builtin(self):
        readme = _read(README)
        assert "deepseek-harness" in readme
