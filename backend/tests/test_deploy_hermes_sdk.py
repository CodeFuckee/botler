"""pm2 部署 hermes-agent SDK 自动安装校验（issue #171 跟进）。

hermes 引擎（issue #171 起）改为 hermes agent SDK 进程内集成：hermes-agent
以源码 editable 安装进 botler 自身 venv（对齐 dsh 引擎 SDK 方式，issue #84）。
pm2 部署由 `deploy/install-hermes-agent.sh` 一键安装（单一事实来源），CI
deploy_to_code01 在主依赖安装后自动调用；Docker 部署由 docker-entrypoint.sh
在容器启动时对挂载源码安装。本测试静态校验部署产物防回退：

- 脚本存在且可执行，默认装入 backend/.venv（pm2 后端 venv）；
- 源码目录默认 ~/.hermes/hermes-agent，HERMES_SOURCE_DIR 可覆盖；
- editable 安装（hermes-agent 以源码分发、PyPI 无 wheel，只能 -e）；
- 优先 venv 内 pip（--ignore-requires-python，适配 pm2 Python 3.14——
  上游 pyproject requires-python <3.14 封顶已过时，cp314 wheel 实测可用），
  无 pip 时回退 uv（CI venv 由 uv 创建）；
- 安装后 import 校验（装不上立即失败，fail fast）；
- 幂等：run_agent 已可导入直接跳过；
- CI deploy_to_code01 job 在主依赖安装后调用脚本；
- 部署文档 / README / runner 安装指引同步指向脚本。
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]  # botler/ 仓库根
INSTALL_SH = ROOT / "deploy" / "install-hermes-agent.sh"
CI_FILE = ROOT / ".gitlab-ci.yml"
HERMES_DOC = ROOT / "docs" / "hermes-engine-deployment.md"
HERMES_RUNNER = ROOT / "backend" / "botler" / "hermes_sdk_runner.py"
README = ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少文件: {path}"
    return path.read_text(encoding="utf-8")


def _ci_deploy_job_section() -> str:
    """提取 .gitlab-ci.yml 中 deploy_to_code01 job 段落（到下一个顶级 key）。"""
    ci = _read(CI_FILE)
    lines = ci.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("deploy_to_code01:"):
            start = i
            break
    assert start is not None, "缺少 deploy_to_code01 job"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i
            break
    return "\n".join(lines[start:end])


class TestInstallScriptBasics:
    """deploy/install-hermes-agent.sh：脚本存在性 / 默认 venv / 源码目录。"""

    def test_install_script_exists_and_executable(self):
        """一键脚本存在且带执行权限（可直接 ./ 或 bash 执行）。"""
        assert INSTALL_SH.is_file(), "缺少 deploy/install-hermes-agent.sh"
        assert INSTALL_SH.stat().st_mode & 0o111, "脚本无执行权限"

    def test_script_defaults_to_backend_venv(self):
        """默认装进 backend/.venv（pm2 后端运行时 venv）。"""
        script = _read(INSTALL_SH)
        assert "backend/.venv" in script

    def test_script_source_dir_overridable(self):
        """源码目录默认 ~/.hermes/hermes-agent，HERMES_SOURCE_DIR 可覆盖。"""
        script = _read(INSTALL_SH)
        assert "HERMES_SOURCE_DIR" in script
        assert ".hermes/hermes-agent" in script

    def test_script_editable_install(self):
        """hermes-agent 以源码分发（PyPI 无 wheel），必须 editable 安装。"""
        script = _read(INSTALL_SH)
        assert "pip install" in script
        assert " -e " in script or "--editable" in script


class TestInstallScriptRobustness:
    """幂等 / fail fast / pip 优先（--ignore-requires-python）+ uv 回退。"""

    def test_script_idempotent(self):
        """已安装直接跳过（重复部署/重跑不重复安装）。"""
        script = _read(INSTALL_SH)
        assert "跳过" in script
        # 检测走 importlib.util.find_spec（uv 创建的 venv 无 pip 也可检测）
        assert "find_spec" in script

    def test_script_import_check_fail_fast(self):
        """安装后 import 校验：装不上立即 exit 1（部署失败而非带病运行）。"""
        script = _read(INSTALL_SH)
        assert "assert importlib.util.find_spec(\"run_agent\")" in script

    def test_script_prefers_pip_with_ignore_requires_python(self):
        """优先 venv 内 pip，且带 --ignore-requires-python（适配 Python 3.14）。"""
        script = _read(INSTALL_SH)
        assert "--ignore-requires-python" in script
        assert "pip --version" in script

    def test_script_uv_fallback(self):
        """无 pip 时回退 uv（CI venv 由 uv 创建、无 pip seed）。"""
        script = _read(INSTALL_SH)
        assert "uv pip install" in script


class TestCiPm2DeployAutoInstallsSdk:
    """CI deploy_to_code01（pm2 部署）：主依赖安装后自动调用脚本。"""

    def test_deploy_job_calls_install_script(self):
        job = _ci_deploy_job_section()
        assert "install-hermes-agent.sh" in job

    def test_deploy_job_installs_sdk_into_backend_venv(self):
        """脚本参数指向 backend/.venv（与 pm2 启动的 venv 一致）。"""
        job = _ci_deploy_job_section()
        assert re.search(r"install-hermes-agent\.sh\s+backend/\.venv", job)

    def test_deploy_job_script_failure_blocks_deploy(self):
        """脚本失败必须 exit 1（部署失败），不允许静默跳过。"""
        job = _ci_deploy_job_section()
        assert "exit 1" in job


class TestDocsSynced:
    """文档 / README / runner 安装指引同步。"""

    def test_deployment_doc_mentions_script(self):
        """部署文档：pm2 自动安装 + 一键脚本。"""
        doc = _read(HERMES_DOC)
        assert "install-hermes-agent.sh" in doc
        assert "自动" in doc

    def test_deployment_doc_mentions_docker_entrypoint(self):
        """部署文档：Docker 形态由 entrypoint 启动时安装。"""
        doc = _read(HERMES_DOC)
        assert "docker-entrypoint.sh" in doc

    def test_runner_hint_points_to_script(self):
        """hermes_sdk_runner 安装指引指向一键脚本（而非裸 pip 命令）。"""
        runner = _read(HERMES_RUNNER)
        assert "install-hermes-agent.sh" in runner

    def test_readme_mentions_script(self):
        """README 部署说明含脚本（手动 pm2/systemd 部署补装 SDK）。"""
        readme = _read(README)
        assert "install-hermes-agent.sh" in readme or "hermes-engine-deployment" in readme
