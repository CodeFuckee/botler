"""ClaudeExecutor dsh 引擎测试（issue #84：集成 deepseek-harness 方案B）。

覆盖：引擎分派（worker.engine=dsh 白名单）、_run_dsh_once 构造参数透传
（prompt 渲染 / 工作区 / 环境 / SDK 配置 / resume session_id）、停止与超时
（stop 强制终止运行时 → 125/124）、结果判定（_dsh_result 三态）、会话 id
落库（dsh_session_id）与断点续跑恢复、SSE 事件发布、SDK 未安装报错、
以及 claude/hermes 现有路径不受影响。

DshRunner 的真实行为（线程/停止/事件映射）在 test_dsh_runner.py 覆盖，
本文件用假 DshRunner 测 executor 侧的分派、判定与落库逻辑。
"""

import json

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {worker}
claude: {{}}
hermes: {hermes}
dsh: {dsh}
ai_providers: {ai_providers}
templates: {{}}
repos: []
"""

# 直接传 _run_once 的 repo 字典（_build_prompt 需 prompt_template 键）
_REPO = {"name": "demo", "prompt_template": None}

_ISSUE = {"state": "opened", "title": "标题", "description": "正文",
          "web_url": "https://gitlab.example.com/x/-/issues/7",
          "project_id": 42, "iid": 7}

# dsh runner 成功结果行样例
_RESULT_LINE = json.dumps({"final_response": "已修复并推送，issue #7 处理完成",
                           "finish_reason": "completed",
                           "session_id": "dsh-sess-1"}, ensure_ascii=False)

# 设置页「AI 供应商」里的 DeepSeek 项（issue #115：dsh 引擎凭据回退源）
_AI_PROVIDERS_DEEPSEEK = """
- name: deepseek
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key: sk-ai-provider-key
  model: deepseek-v4-flash
  enabled: true
"""


def _mk_config(tmp_path, worker_extra="{precheck_enabled: false}", dsh_extra="{}",
               hermes_extra="{}", ai_providers_extra="[]") -> ConfigManager:
    """worker_extra / dsh_extra / hermes_extra 为整段子键文本（非空时需自带前置换行）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.format(worker=worker_extra, dsh=dsh_extra,
                           hermes=hermes_extra,
                           ai_providers=ai_providers_extra),
        encoding="utf-8")
    return ConfigManager(str(config_path))


def _mk_executor(tmp_path, config: ConfigManager) -> ClaudeExecutor:
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token",
                          verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


@pytest.fixture
def executor(tmp_path):
    ex = _mk_executor(tmp_path, _mk_config(tmp_path))
    yield ex
    ex.db.close()  # 显式关闭 sqlite 连接，避免泄漏（unclosed database）


@pytest.fixture
def dsh_executor(tmp_path):
    """engine=dsh + dsh 段配置的 executor（session_root 指向 tmp_path 下
    真实目录——issue #281 §4.7 resume 校验要求 session_root 目录存在）。"""
    sessions = tmp_path / "dsh-sessions"
    sessions.mkdir(exist_ok=True)
    config = _mk_config(
        tmp_path, worker_extra="\n  engine: dsh\n  precheck_enabled: false",
        dsh_extra="\n  provider: deepseek-official"
                  f"\n  model: deepseek-v4-flash"
                  f"\n  max_tokens: 49152"
                  f"\n  session_root: {sessions}")
    ex = _mk_executor(tmp_path, config)
    yield ex
    ex.db.close()  # 显式关闭 sqlite 连接，避免泄漏（unclosed database）


class _FakeRunner:
    """假 DshRunner：start 时同步回放 preset_lines（模拟 worker 输出）。

    实例由 _run_dsh_once 内部构造，测试通过类级 preset 预设行为：
    preset_lines = 新实例回放的行；preset_done = 新实例 done() 返回值。
    preset_queue = 行组队列（issue #291 降级重跑场景）：每实例化一次弹出
    一组行回放，队列空时回退 preset_lines；collision 降级会构造第二个
    实例（全新会话重跑），需要两组不同输出时用队列预设。
    """

    instances: list["_FakeRunner"] = []
    preset_lines: list[str] = []
    preset_done: bool = True
    preset_queue: list[list[str]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._exit = 0
        self._done = _FakeRunner.preset_done
        self.stop_calls = 0
        if _FakeRunner.preset_queue:
            self.result_lines = list(_FakeRunner.preset_queue.pop(0))
        else:
            self.result_lines = list(_FakeRunner.preset_lines)
        _FakeRunner.instances.append(self)

    def start(self):
        for line in self.result_lines:
            self.kwargs["on_line"](line)

    def done(self):
        return self._done

    def finish(self):
        return self._exit

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def fake_runner(monkeypatch):
    """注入假 DshRunner 到 executor 模块命名空间并重置 preset。"""
    _FakeRunner.instances.clear()
    _FakeRunner.preset_lines = []
    _FakeRunner.preset_done = True
    _FakeRunner.preset_queue = []
    monkeypatch.setattr("botler.executor.DshRunner", _FakeRunner)
    monkeypatch.setattr("botler.executor.DshSdkNotInstalledError",
                        type("DshSdkNotInstalledError", (Exception,), {}))
    return _FakeRunner


def _patch_workspace(monkeypatch, executor, tmp_path):
    """替换 prepare_workspace / _log_file，避免真实 git/磁盘交互。"""
    calls: dict = {}

    def fake_prepare(repo, resume=False):
        calls["resume"] = resume
        return tmp_path / "work", {"GIT_ASKPASS": "/askpass"}

    monkeypatch.setattr(executor, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(executor, "_log_file",
                        lambda tid: tmp_path / f"task_{tid}.log")
    return calls


def _mk_task(executor) -> int:
    """创建任务记录（session_id 落库需要真实 task 行）。"""
    db = executor.db
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    repo_id = db.get_repo_by_project_id(42)["id"]
    return db.create_task(repo_id, 42, 7, "标题")


def _fake_drain_proc(exit_code=0):
    """构造 claude/hermes 路径用的假 Popen 进程（立即 EOF + 退出）。"""
    return type("Proc", (), {
        "stdout": type("SO", (), {"readline": lambda s: ""})(),
        "stderr": None,
        "stdin": type("SI", (), {"write": lambda s, t: None,
                                 "close": lambda s: None})(),
        "poll": lambda s: exit_code,
        "wait": lambda s, timeout=None: exit_code,
        "pid": 1,
    })()


class TestEngine:
    """_engine：dsh 白名单与回退。"""

    def test_engine_dsh(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: dsh\n  precheck_enabled: false")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "dsh"

    def test_engine_uppercase_normalized(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: DSH\n  precheck_enabled: false")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "dsh"

    def test_default_engine_is_claude(self, executor):
        assert executor._engine(executor.config.get()) == "claude"


class TestDshCredentials:
    """_dsh_credentials：dsh 段显式配置 > ai_providers deepseek 项 > 环境变量（SDK 默认）。

    issue #115 复现：用户仅设置页「AI 供应商」配过 DeepSeek key，dsh 段
    未配且部署机环境无 DEEPSEEK_API_KEY → 任务 #194 #195 全部 401 失败。
    """

    def test_explicit_dsh_settings_win(self, tmp_path):
        config = _mk_config(
            tmp_path, dsh_extra="\n  api_key: sk-dsh\n  base_url: https://x/v1",
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        # dsh 段显式配置 key/base_url → 模型 None（调用方透传 dsh.model）
        assert ex._dsh_credentials(config.get()) == ("sk-dsh", "https://x/v1", None)

    def test_falls_back_to_ai_provider_deepseek(self, tmp_path):
        """dsh 段未配 → ai_providers 的 deepseek 项（enabled）回退。"""
        config = _mk_config(tmp_path, ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        # issue #397：dsh 段未显式配模型 → 模型跟随选中供应商（deepseek 项）
        assert ex._dsh_credentials(config.get()) == (
            "sk-ai-provider-key", "https://api.deepseek.com/v1",
            "deepseek-v4-flash")

    def test_ai_provider_fills_only_missing_fields(self, tmp_path):
        """dsh 段只配 base_url：api_key 回退、base_url 保留 dsh 段值。"""
        config = _mk_config(
            tmp_path, dsh_extra="\n  base_url: https://self-host/v1",
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (
            "sk-ai-provider-key", "https://self-host/v1",
            "deepseek-v4-flash")

    def test_disabled_ai_provider_skipped(self, tmp_path):
        """deepseek 项 enabled=false → 不回退（key 不可用）。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: deepseek
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key: sk-disabled
  model: deepseek-v4-flash
  enabled: false
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None, None)

    def test_non_openai_compat_provider_skipped(self, tmp_path):
        """（issue #395）非 OpenAI 兼容协议 provider（anthropic）不回退——
        dsh 引擎需 OpenAI 兼容 chat/completions 接口，anthropic 走 /messages。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: anthropic
  provider: anthropic
  base_url: https://api.anthropic.com/v1
  api_key: sk-anthropic
  model: claude-sonnet-5
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None, None)

    def test_none_when_no_source(self, tmp_path):
        """dsh 段与 ai_providers 均无 deepseek 凭据 → (None, None)（SDK 读环境）。"""
        config = _mk_config(tmp_path)
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None, None)



    def test_provider_model_follows_credentials_chain(self, tmp_path):
        """（issue #397）dsh 段未显式配置模型时，模型跟随凭据解析链选中的
        AI 供应商。此前模型固定 dsh_model 默认 deepseek-v4-flash，
        ai_providers 里配的模型（如越建科 deepseek-v4-pro）从未传给 dsh
        引擎——任务 #581/#397「配置了 v4 pro 最终却调用 v4 flash」根因。
        """
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: 越建科
  provider: custom
  base_url: https://new.s1.prod.gglohh.top/v1
  api_key: sk-yuejianke
  model: deepseek-v4-pro
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        api_key, base_url, model = ex._dsh_credentials(config.get())
        assert (api_key, base_url, model) == (
            "sk-yuejianke", "https://new.s1.prod.gglohh.top/v1",
            "deepseek-v4-pro")

    def test_explicit_dsh_model_wins_over_provider_model(self, tmp_path):
        """dsh 段显式配置模型 → 供应商模型不覆盖（显式优先）。"""
        config = _mk_config(
            tmp_path,
            dsh_extra="\n  model: deepseek-v4-flash",
            ai_providers_extra="""
- name: 越建科
  provider: custom
  base_url: https://new.s1.prod.gglohh.top/v1
  api_key: sk-yuejianke
  model: deepseek-v4-pro
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        api_key, base_url, model = ex._dsh_credentials(config.get())
        assert (api_key, base_url) == (
            "sk-yuejianke", "https://new.s1.prod.gglohh.top/v1")
        assert model is None  # dsh 段显式模型由调用方透传

class TestRunDshOnce:
    """_run_dsh_once：构造参数、工作区、环境、SDK 配置透传。"""

    def test_fresh_run_passes_prompt_and_workspace(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        calls = _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        code, output = dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        assert code == 0
        assert _RESULT_LINE in output
        assert calls["resume"] is False  # 全新执行：工作区重置
        kwargs = fake_runner.instances[0].kwargs
        assert "AI 维护者" in kwargs["prompt"]  # DEFAULT_TEMPLATE 渲染产物
        # issue #281 §4.7：fresh 任务在 runner 构造前预生成会话 id 并落库，
        # runner 以该 id 创建会话（SDK 支持指定 id 创建全新会话）
        assert kwargs["session_id"] is not None
        assert kwargs["session_id"].startswith("botler-1-")
        assert "进度上报约定" in kwargs["prompt"]  # 进度上报节已追加
        assert kwargs["cwd"] == str(tmp_path / "work")
        # git 凭据注入继承 _build_env（GIT_ASKPASS 为真实生成的脚本路径）
        assert kwargs["env"]["GIT_ASKPASS"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_sdk_config_from_dsh_settings(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["provider"] == "deepseek-official"
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["max_tokens"] == 49152
        assert kwargs["session_root"] == str(tmp_path / "dsh-sessions")

    def test_ai_provider_credentials_passed_to_runner(
            self, monkeypatch, tmp_path, fake_runner):
        """issue #115：dsh 段无 key 时，ai_providers 的 deepseek 凭据传给 runner。"""
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: dsh\n  precheck_enabled: false",
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        ex._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["api_key"] == "sk-ai-provider-key"
        assert kwargs["base_url"] == "https://api.deepseek.com/v1"


    def test_provider_model_passed_to_runner_when_not_explicit(
            self, monkeypatch, tmp_path, fake_runner):
        """issue #397：dsh 段未显式配置模型时，AI 供应商的模型名传给
        DshRunner（此前固定 dsh_model 默认 deepseek-v4-flash——任务
        #581/#397「配置了 deepseek-v4-pro 最终却调用 v4 flash」）。"""
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: dsh\n  precheck_enabled: false",
            ai_providers_extra="""
- name: 越建科
  provider: custom
  base_url: https://new.s1.prod.gglohh.top/v1
  api_key: sk-yuejianke
  model: deepseek-v4-pro
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        ex._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["api_key"] == "sk-yuejianke"
        assert kwargs["base_url"] == "https://new.s1.prod.gglohh.top/v1"
        assert kwargs["model"] == "deepseek-v4-pro"

    def test_ai_provider_credentials_none_passed_as_none(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """无任何凭据源时传 None（SDK 读环境 DEEPSEEK_API_KEY 兜底，行为不变）。"""
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["api_key"] is None
        assert kwargs["base_url"] is None

    def test_resume_passes_session_id_and_keeps_workspace(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        calls = _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE, resume_session="sess-9")
        assert calls["resume"] is True  # 恢复模式：工作区不清空
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["session_id"] == "sess-9"
        assert "继续处理" in kwargs["prompt"]  # RESUME_PROMPT 渲染

    def test_success_persists_dsh_session_id(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        row = dsh_executor.db.get_task(task_id)
        assert row["dsh_session_id"] == "dsh-sess-1"

    def test_multiline_output_parseable_success_and_session_persist(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """issue #119：多行事件输出拼接必须保留换行，否则结果行解析失败。

        复现：DshRunner 的 on_line 回调逐条收到事件行（行尾无换行符），
        _run_dsh_once 若用 ''.join 拼接，output 无换行分隔，_last_json_object
        按行扫描只拿到首个事件对象（finish_reason 缺失）→ _dsh_result 误判
        failed → 触发重试；且 _persist_dsh_session_id 同样解析不到 session_id
        → 断点续跑失效 → 每次重试都是全新会话（重复开发任务），重试耗尽后
        任务显示失败（任务 #198 #199 日志：引擎 exit 0、结果行 completed 仍失败）。
        """
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        # 事件行 + 结果行（真实 DshRunner 回调逐行 on_line 的真实形态）
        fake_runner.preset_lines = [
            json.dumps({"event": "raw", "type": "step/end"},
                       ensure_ascii=False),
            json.dumps({"event": "status", "message": "回合结束: completed"},
                       ensure_ascii=False),
            _RESULT_LINE,
        ]
        code, output = dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert code == 0
        # 结果行必须可解析：成功判定 + 会话 id 落库（断点续跑）
        assert dsh_executor._dsh_result(output) == "success"
        row = dsh_executor.db.get_task(task_id)
        assert row["dsh_session_id"] == "dsh-sess-1"

    def test_stop_returns_stop_exit_code_and_calls_stop(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_done = False  # 运行中（done 返回 False 进入轮询循环）
        dsh_executor.request_stop(1)
        try:
            code, _ = dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        finally:
            dsh_executor.clear_stop_request(1)
        assert code == 125
        assert fake_runner.instances[0].stop_calls == 1  # stop 被调用（终止运行时）

    def test_timeout_returns_124_and_calls_stop(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_done = False
        # 超时秒数设为 0：进入轮询循环第一轮即超时（get 触发 load 后改内存值）
        dsh_executor.config.get().task_timeout_seconds = 0
        try:
            code, _ = dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        finally:
            dsh_executor.config.get().task_timeout_seconds = 1800
        assert code == 124
        assert fake_runner.instances[0].stop_calls == 1

    def test_sdk_missing_raises_executor_error(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """SDK 未安装：DshSdkNotInstalledError → ExecutorError（run_task 捕获重试）。"""
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        from botler.executor import DshSdkNotInstalledError as _SDKErr

        class _AlwaysMissing(_FakeRunner):
            def start(self):
                raise _SDKErr("deepseek-harness-sdk 未安装")

        monkeypatch.setattr("botler.executor.DshRunner", _AlwaysMissing)
        from botler.executor import ExecutorError
        with pytest.raises(ExecutorError):
            dsh_executor._run_dsh_once(1, _REPO, _ISSUE)

    # ---- issue #291：SDK 会话 id collision 降级为全新会话 ----

    @staticmethod
    def _collision_lines(session_id: str) -> list[str]:
        """SDK 报告 id collision 的真实输出形态（任务 #390/#391 日志）。"""
        return [
            json.dumps({"event": "status",
                        "message": "dsh 会话状态: running"},
                       ensure_ascii=False),
            json.dumps({"event": "status",
                        "message": f"回合结束: error（session "
                                   f'"{session_id}" already has a persisted '
                                   f"log on disk that does not match this "
                                   f"live session (id collision)）"},
                       ensure_ascii=False),
            json.dumps({"event": "status",
                        "message": "dsh 会话状态: idle"},
                       ensure_ascii=False),
            json.dumps({"final_response": "", "finish_reason": "error",
                        "session_id": session_id}, ensure_ascii=False),
        ]

    def test_collision_downgrades_to_fresh_session(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """resume 撞 SDK id collision → 换新 id + 全新提示词重跑并成功。

        复现任务 #390/#391：resume 会话时 SDK 报「already has a persisted
        log on disk that does not match this live session (id collision)」，
        旧逻辑把该错误当普通失败交给重试循环，重试仍用同一落库 id →
        每次必撞 → 重试耗尽 failed。修复后 collision 检测命中即降级为
        全新会话（新 id + 全新提示词）重跑，任务得以完成。
        """
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(task_id, None, dsh_session_id="old-sid")
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_queue = [
            self._collision_lines("old-sid"),  # 第 1 轮：resume 撞 collision
            [_RESULT_LINE],                    # 第 2 轮：全新会话成功
        ]
        code, output = dsh_executor._run_dsh_once(
            task_id, _REPO, _ISSUE, resume_session="old-sid")
        assert code == 0
        assert dsh_executor._dsh_result(output) == "success"
        # 两个 runner 实例：第 1 个 resume 旧 id，第 2 个全新 id + 全新提示词
        assert len(fake_runner.instances) == 2
        first, second = fake_runner.instances
        assert first.kwargs["session_id"] == "old-sid"
        assert second.kwargs["session_id"] != "old-sid"
        assert second.kwargs["session_id"].startswith("botler-")
        assert "继续处理" not in second.kwargs["prompt"]  # 无恢复引导语（不声称对话保留）
        # 新 id 已落库（任务详情展示实际会话）
        row = dsh_executor.db.get_task(task_id)
        assert row["dsh_session_id"] == "dsh-sess-1"

    def test_collision_downgrade_prompt_carries_progress_ledger(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """降级全新会话仍携带进度账本交接单（issue #291 补充）：对话历史
        丢失但账本与工作区保留，新会话按账本接续，禁止重做 done 步骤，
        避免「平台重启 → 全新对话 → 从头重复实现」。
        """
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(task_id, None, dsh_session_id="old-sid")
        # 上次会话已落库的进度账本（跨会话持久化，与 dsh 会话无关）
        dsh_executor.db.record_task_progress(
            task_id, 1, "复现测试", "done", evidence="commit abc1234")
        dsh_executor.db.record_task_progress(task_id, 2, "修复实现", "doing")
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_queue = [
            self._collision_lines("old-sid"),
            [_RESULT_LINE],
        ]
        dsh_executor._run_dsh_once(
            task_id, _REPO, _ISSUE, resume_session="old-sid")
        second = fake_runner.instances[1]
        prompt = second.kwargs["prompt"]
        # 交接单携带账本内容：已完成步骤 + 证据 + 下一步 + 禁止重做
        assert "对话历史已丢失" in prompt  # 如实说明，不假装对话保留
        assert "步骤 1「复现测试」→ done" in prompt
        assert "commit abc1234" in prompt
        assert "步骤 2「修复实现」" in prompt
        assert "禁止重新检查/重做" in prompt

    def test_collision_downgrade_second_collision_fails_once(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """降级重跑再撞 collision → 直接失败返回，不无限降级（防死循环）。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_queue = [
            self._collision_lines("s-a"),
            self._collision_lines("s-b"),
        ]
        code, output = dsh_executor._run_dsh_once(
            task_id, _REPO, _ISSUE, resume_session="s-a")
        assert code == 0  # 退出码语义不变（失败由结果行判定）
        assert dsh_executor._dsh_result(output) == "failed"
        assert len(fake_runner.instances) == 2  # 只重跑一次，不递归降级

    def test_plain_error_no_downgrade(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """非 collision 的运行错误不触发降级（正常失败交重试循环）。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"final_response": "", "finish_reason": "error",
                        "session_id": "s-1"}, ensure_ascii=False),
        ]
        code, output = dsh_executor._run_dsh_once(
            task_id, _REPO, _ISSUE, resume_session="s-1")
        assert code == 0
        assert dsh_executor._dsh_result(output) == "failed"
        assert len(fake_runner.instances) == 1  # 无降级重跑

    def test_collision_detector(self, dsh_executor):
        """_dsh_collision：id collision 输出 → True；普通错误/成功 → False。"""
        assert dsh_executor._dsh_collision(
            "\n".join(self._collision_lines("x"))) is True
        assert dsh_executor._dsh_collision(
            json.dumps({"final_response": "", "finish_reason": "error",
                        "session_id": "x"}, ensure_ascii=False)) is False
        assert dsh_executor._dsh_collision(_RESULT_LINE) is False


    # ---- issue #401：dsh 会话损坏（tool 消息缺 callId）续跑降级 ----
    def _corrupt_lines(self, session_id: str) -> list[str]:
        """会话损坏续跑错误输出形态（任务 #581/#582 日志）：
        「回合结束: error（session event at seq N message must have tool
        source）」——会话文件里持久化了空 callId 的 tool 消息，runtime
        重放时无法还原工具来源，报 tool source 缺失。"""
        return [
            json.dumps({"event": "status", "message": "dsh 会话状态: running"}),
            json.dumps({"event": "raw", "type": "turn/start"}),
            json.dumps({"event": "status",
                        "message": "回合结束: error（session event at seq 94 "
                                   "message must have tool source）"}),
            json.dumps({"event": "status", "message": "dsh 会话状态: idle"}),
            json.dumps({"final_response": "", "finish_reason": "error",
                        "session_id": session_id}),
        ]

    def test_corrupted_session_downgrades_to_fresh_session(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """resume 撞会话损坏错误 → 换新 id + 全新提示词重跑并成功。

        复现任务 #581/#582：dsh 模型流式输出工具调用时后续 chunk 的
        name/id 为空串，runtime 合并后工具调用名称为空 → 全部工具调用
        报 `unknown tool ""` → agent 死循环 → 会话文件持久化空 callId
        的 tool 消息 → 断点续跑 runtime 重放报「message must have tool
        source」。旧逻辑把该错误当普通失败交重试循环，重试仍用同一落库
        id → 每次必报 → 重试耗尽 failed。修复后检测命中即降级全新会话
        （新 id + 全新提示词）重跑，任务得以完成。
        """
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(task_id, None, dsh_session_id="old-sid")
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_queue = [
            self._corrupt_lines("old-sid"),  # 第 1 轮：resume 撞会话损坏
            [_RESULT_LINE],                  # 第 2 轮：全新会话成功
        ]
        code, output = dsh_executor._run_dsh_once(
            task_id, _REPO, _ISSUE, resume_session="old-sid")
        assert code == 0
        assert dsh_executor._dsh_result(output) == "success"
        # 两个 runner 实例：第 1 个 resume 旧 id，第 2 个全新 id + 全新提示词
        assert len(fake_runner.instances) == 2
        first, second = fake_runner.instances
        assert first.kwargs["session_id"] == "old-sid"
        assert second.kwargs["session_id"] != "old-sid"
        assert second.kwargs["session_id"].startswith("botler-")
        # 降级提示词如实说明对话历史丢失（不假装保留），且带进度账本交接
        assert "对话历史已丢失" in second.kwargs["prompt"]
        # 新 id 已落库（任务详情展示实际会话）
        row = dsh_executor.db.get_task(task_id)
        assert row["dsh_session_id"] == "dsh-sess-1"

    def test_corrupted_session_detector(self, dsh_executor):
        """_dsh_corrupted_session：tool source 缺失输出 → True；
        普通错误/成功/collision → False（与 _dsh_collision 互斥）。"""
        assert dsh_executor._dsh_corrupted_session(
            "\n".join(self._corrupt_lines("x"))) is True
        assert dsh_executor._dsh_corrupted_session(
            json.dumps({"final_response": "", "finish_reason": "error",
                        "session_id": "x"}, ensure_ascii=False)) is False
        assert dsh_executor._dsh_corrupted_session(_RESULT_LINE) is False
        assert dsh_executor._dsh_corrupted_session(
            "\n".join(self._collision_lines("x"))) is False

    def test_sse_events_published_from_event_lines(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"event": "stream_delta", "text": "正在处理…"},
                       ensure_ascii=False),
            _RESULT_LINE,
        ]
        sub = dsh_executor.event_bus.subscribe(1, maxsize=10)
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        event = sub.get(timeout=2)
        assert event["kind"] == "text"
        assert event["text"] == "正在处理…"
        sub.close()

    def test_log_file_written_from_lines(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """事件行与结果行均落盘日志（SSE 回放数据源）。"""
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        log_text = (tmp_path / "task_1.log").read_text(encoding="utf-8")
        assert _RESULT_LINE in log_text

    def test_claude_engine_untouched(self, executor, monkeypatch, tmp_path,
                                     fake_runner):
        """engine=claude（默认）时 dsh runner 不被调用（回归保护）。"""
        _patch_workspace(monkeypatch, executor, tmp_path)
        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kw: _fake_drain_proc())
        code, _ = executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert fake_runner.instances == []  # 未走 dsh 路径

    def test_hermes_engine_untouched(self, monkeypatch, tmp_path, fake_runner):
        """engine=hermes 时 dsh runner 不被调用（回归保护）。"""
        class _FakeHermesRunner:
            def __init__(self, **kwargs):
                pass
            def start(self):
                pass
            def done(self):
                return True
            def finish(self):
                return 0
            def stop(self):
                pass

        monkeypatch.setattr("botler.executor.HermesSdkRunner", _FakeHermesRunner)
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: hermes\n  precheck_enabled: false")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        code, _ = ex._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert fake_runner.instances == []  # 未走 dsh 路径


class TestDshResult:
    """_dsh_result：成功 / 无法解决 / 失败重试（dsh 输出语义）。"""

    def _out(self, final_response, finish_reason="completed", error=None,
             session_id="s1"):
        data = {"final_response": final_response,
                "finish_reason": finish_reason,
                "session_id": session_id}
        if error:
            data["error"] = error
        return json.dumps(data, ensure_ascii=False)

    def test_completed_with_response_is_success(self, executor):
        assert executor._dsh_result(
            self._out("任务完成")) == "success"

    def test_unresolvable_detected(self, executor):
        assert executor._dsh_result(
            self._out("抱歉，无法解决该 issue：依赖缺失")) == "unresolvable"

    def test_max_tokens_is_failure(self, executor):
        """finish_reason=max-tokens（输出截断，未完成）→ 失败重试。"""
        assert executor._dsh_result(
            self._out("做了一半", finish_reason="max-tokens")) == "failed"

    def test_error_finish_reason_is_failure(self, executor):
        assert executor._dsh_result(
            self._out("", finish_reason="error")) == "failed"

    def test_none_finish_reason_is_failure(self, executor):
        """无回合结束（finish_reason=None）→ 失败重试。"""
        assert executor._dsh_result(
            self._out("文本", finish_reason=None)) == "failed"

    def test_error_field_marks_failure(self, executor):
        assert executor._dsh_result(
            self._out("", error="runtime crashed")) == "failed"

    def test_empty_final_response_is_failure(self, executor):
        assert executor._dsh_result(
            self._out("", finish_reason="completed")) == "failed"

    def test_non_json_output_is_failure(self, executor):
        assert executor._dsh_result("no json here") == "failed"

    def test_unknown_finish_reason_is_failure(self, executor):
        """SDK 版本演进产生未知 reason：不静默成功，按失败重试。"""
        assert executor._dsh_result(
            self._out("文本", finish_reason="mystery")) == "failed"


class TestDshSessionPersist:
    def test_persist_valid_session_id(self, executor):
        task_id = _mk_task(executor)
        executor._persist_dsh_session_id(task_id, _RESULT_LINE)
        assert executor.db.get_task(task_id)["dsh_session_id"] == "dsh-sess-1"

    def test_persist_invalid_output_keeps_value(self, executor):
        """输出无结果行（非 JSON）→ 不落库、不抛异常。"""
        task_id = _mk_task(executor)
        executor._persist_dsh_session_id(task_id, "garbage")
        assert executor.db.get_task(task_id)["dsh_session_id"] is None

    def test_persist_missing_session_id_keeps_value(self, executor):
        task_id = _mk_task(executor)
        executor.db.set_task_status(task_id, None, dsh_session_id="old")
        executor._persist_dsh_session_id(
            task_id, json.dumps({"final_response": "x",
                                 "finish_reason": "completed"}))
        assert executor.db.get_task(task_id)["dsh_session_id"] == "old"


class TestRunTaskResume:
    """run_task 断点续跑：dsh_session_id 恢复为 resume_session。"""

    def _run_task(self, dsh_executor, monkeypatch, captured):
        monkeypatch.setattr(dsh_executor, "_call_with_fallback",
                            lambda repo, fn, **kw: (_ISSUE, None))
        monkeypatch.setattr(dsh_executor, "_await_pipeline_and_finish_succeeded",
                            lambda *a, **kw: None)

        def fake_run_once(task_id, repo, issue, resume_session=None,
                          resume_history=None, engine=None):
            captured["resume_session"] = resume_session
            return 0, _RESULT_LINE

        monkeypatch.setattr(dsh_executor, "_run_once", fake_run_once)

    def test_run_task_resumes_from_dsh_session_id(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(task_id, None, dsh_session_id="sess-9")
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        captured = {}
        self._run_task(dsh_executor, monkeypatch, captured)
        dsh_executor.run_task(task_id)
        assert captured["resume_session"] == "sess-9"

    def test_run_task_fresh_when_no_session(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        captured = {}
        self._run_task(dsh_executor, monkeypatch, captured)
        dsh_executor.run_task(task_id)
        assert captured["resume_session"] is None


class TestDshSessionPrePersist:
    """issue #281 §4.7：会话 id 任务开始即落库 + resume 前可恢复性校验。

    覆盖：fresh 任务在 runner 构造前预生成 id 并原子落库（先落 id 再开跑）；
    前置落库失败 = 任务失败（不静默降级）；resume 时 session_root 目录缺失
    → 如实降级为全新会话（不假装「对话已保留」）。
    """

    def test_fresh_run_pre_persists_session_id_before_runner(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """fresh 执行：runner 构造时 dsh_session_id 已落库，且 runner 用同一 id。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        captured: dict = {}

        class _Probe(_FakeRunner):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                captured["runner_session_id"] = kwargs["session_id"]
                captured["db_before_start"] =                     dsh_executor.db.get_task(task_id)["dsh_session_id"]

        monkeypatch.setattr("botler.executor.DshRunner", _Probe)
        fake_runner.preset_lines = []  # 无结果行 → _persist_dsh_session_id 不改
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert captured["runner_session_id"].startswith("botler-1-")
        # 先落 id 再开跑：runner 构造时库中已是该 id
        assert captured["db_before_start"] == captured["runner_session_id"]
        assert dsh_executor.db.get_task(task_id)["dsh_session_id"] ==             captured["runner_session_id"]

    def test_session_id_pre_persist_failure_raises(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """前置落库失败 = 任务失败（ExecutorError），不静默降级继续跑。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        original = dsh_executor.db.set_task_status

        def boom(tid, status=None, **fields):
            if "dsh_session_id" in fields:
                raise RuntimeError("db down")
            return original(tid, status, **fields)

        monkeypatch.setattr(dsh_executor.db, "set_task_status", boom)
        from botler.executor import ExecutorError
        with pytest.raises(ExecutorError):
            dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert fake_runner.instances == []  # runner 未启动

    def test_resume_degrades_when_session_root_missing(
            self, tmp_path, monkeypatch, fake_runner):
        """session_root 目录不存在 → resume 降级全新会话（诚实降级）。"""
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: dsh\n  precheck_enabled: false",
            dsh_extra="\n  session_root: /nonexistent-dsh-sessions-xyz")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        task_id = _mk_task(ex)
        ex.db.set_task_status(task_id, None, dsh_session_id="sess-9")
        fake_runner.preset_lines = []
        code, _ = ex._run_dsh_once(task_id, _REPO, _ISSUE, resume_session="sess-9")
        assert code == 0
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["session_id"].startswith("botler-")  # 新预生成 id
        assert "AI 维护者" in kwargs["prompt"]  # 全新提示词（非恢复引导语）
        row = ex.db.get_task(task_id)
        assert row["dsh_session_id"] == kwargs["session_id"]  # 旧 id 已清除
        logs = ex.db.list_logs(task_id)
        assert any("会话目录已不存在" in l["message"] for l in logs)

    def test_resume_keeps_session_when_root_exists(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """session_root 目录存在 → resume 复用已落库 id（不降级）。"""
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(task_id, None, dsh_session_id="sess-9")
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE, resume_session="sess-9")
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["session_id"] == "sess-9"
        assert "继续处理" in kwargs["prompt"]


class TestProgressLedger:
    """issue #281 §4.1/§4.4：进度账本 [PROGRESS] 解析落库 + 交接单渲染。"""

    def test_progress_markers_persisted_to_ledger(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """assistant 输出含 [PROGRESS] 行 → 落库 task_progress（增量解析）。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        prog_line = json.dumps(
            {"event": "stream_delta",
             "text": "[PROGRESS] step=1 status=done desc=\"定位根因\" "
                     "evidence=\"阅读 src/a.py 确认根因\""},
            ensure_ascii=False)
        fake_runner.preset_lines = [prog_line, _RESULT_LINE]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        steps = dsh_executor.db.latest_task_progress(task_id)
        assert len(steps) == 1
        assert steps[0]["step_no"] == 1
        assert steps[0]["status"] == "done"
        assert "定位根因" in steps[0]["step_desc"]
        assert "src/a.py" in steps[0]["evidence"]

    def test_progress_snapshot_appends_latest_status(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """同一 step 多次上报为快照追加，latest_task_progress 取最新状态。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"event": "stream_delta",
                        "text": "[PROGRESS] step=1 status=pending desc=\"开始\""},
                       ensure_ascii=False),
            json.dumps({"event": "stream_delta",
                        "text": "[PROGRESS] step=1 status=done desc=\"完成\" "
                                "evidence=\"pytest -q 通过\""},
                       ensure_ascii=False),
            _RESULT_LINE,
        ]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        all_rows = dsh_executor.db.list_task_progress(task_id)
        assert len(all_rows) == 2  # 只增不改（快照式）
        latest = dsh_executor.db.latest_task_progress(task_id)
        assert len(latest) == 1
        assert latest[0]["status"] == "done"

    def test_resume_prompt_renders_handoff_from_ledger(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """账本有记录 → 恢复引导语渲染确定性交接单（已完成/证据/下一步）。"""
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.record_task_progress(
            task_id, 1, "定位根因", "done", evidence="pytest 通过")
        dsh_executor.db.record_task_progress(task_id, 2, "补充边界测试", "pending")
        prompt = dsh_executor._resume_prompt(_REPO, _ISSUE, task_id=task_id)
        assert "平台已记录以下确定性进度" in prompt
        assert "定位根因" in prompt
        assert "pytest 通过" in prompt
        assert "下一步：步骤 2「补充边界测试」" in prompt
        assert "禁止重新检查/重做已标记 done 的步骤" in prompt

    def test_resume_prompt_empty_ledger_honest_fallback(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """账本为空 → 如实说明无进度记录，不再声称「对话与改动已保留」。"""
        task_id = _mk_task(dsh_executor)
        prompt = dsh_executor._resume_prompt(_REPO, _ISSUE, task_id=task_id)
        assert "暂无任务进度账本记录" in prompt
        assert "你的对话与工作区改动已保留" not in prompt

    def test_progress_markers_malformed_tolerated(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """非法 [PROGRESS] 行整体跳过，不落库不抛异常。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"event": "stream_delta",
                        "text": "[PROGRESS] step=abc status=??"}, ensure_ascii=False),
            _RESULT_LINE,
        ]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert dsh_executor.db.list_task_progress(task_id) == []

    def test_resume_prompt_handoff_all_steps_done(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """账本全部步骤 done → 交接单提示收尾（无下一步）。"""
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.record_task_progress(task_id, 1, "全部完成", "done")
        prompt = dsh_executor._resume_prompt(_REPO, _ISSUE, task_id=task_id)
        assert "已完成全部记录步骤" in prompt
        assert "下一步：步骤" not in prompt

    def test_progress_ledger_write_failure_tolerated(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """账本写入失败 → 记 warn 不阻塞任务（账本尽力而为）。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)

        def boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(dsh_executor.db, "record_task_progress", boom)
        prog_line = json.dumps(
            {"event": "stream_delta",
             "text": "[PROGRESS] step=1 status=done desc=\"x\""},
            ensure_ascii=False)
        fake_runner.preset_lines = [prog_line, _RESULT_LINE]
        code, _ = dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert code == 0
        logs = dsh_executor.db.list_logs(task_id)
        assert any("[PROGRESS] 账本落库失败" in l["message"] for l in logs)


class TestDshTranscript:
    """dsh 引擎提示词持久化与聊天记录落库（issue #146）。

    复现背景：execution 接口只读 claude 会话文件（claude_session_id），
    dsh 引擎会话 id 存 dsh_session_id、提示词无处落库 → dsh 任务
    「查看提示词」显示「提示词未持久化」、聊天记录为空。修复方案：
    参照 hermes_history 落库模式，新增 dsh_transcript（prompt +
    messages JSON），执行中/结束后供 execution 接口读取。
    """

    def _run(self, dsh_executor, monkeypatch, tmp_path, fake_runner,
             preset_lines, resume_session=None):
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = preset_lines
        code, _ = dsh_executor._run_dsh_once(
            task_id, _REPO, _ISSUE, resume_session=resume_session)
        return task_id, code

    @staticmethod
    def _line(event: str, **extra) -> str:
        return json.dumps({"event": event, **extra}, ensure_ascii=False)

    def test_fresh_run_persists_prompt_and_assistant_messages(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """fresh 执行：提示词持久化，聊天记录含 user 提示词 + assistant 文本。"""
        task_id, code = self._run(
            dsh_executor, monkeypatch, tmp_path, fake_runner,
            [self._line("stream_delta", text="正在处理…"),
             self._line("stream_delta", text="已修复"),
             _RESULT_LINE])
        assert code == 0
        row = dsh_executor.db.get_task(task_id)
        data = json.loads(row["dsh_transcript"])
        # 提示词已持久化（渲染后的完整提示词，与 claude 会话文件首条 user 消息等价）
        assert "AI 维护者" in data["prompt"]
        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant"]
        assert "AI 维护者" in data["messages"][0]["text"]  # 聊天记录首条 = 提示词
        assert data["messages"][1]["text"] == "正在处理…已修复"  # 流式增量合并
        assert data["truncated"] is False

    def test_tool_start_appends_tool_message(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """tool_start 事件 → 工具调用消息（含 input），回复片段先收口。"""
        task_id, _ = self._run(
            dsh_executor, monkeypatch, tmp_path, fake_runner,
            [self._line("stream_delta", text="先看代码"),
             self._line("tool_start", tool="Bash", input={"command": "ls"}),
             _RESULT_LINE])
        row = dsh_executor.db.get_task(task_id)
        data = json.loads(row["dsh_transcript"])
        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant", "tool"]
        assert data["messages"][1]["text"] == "先看代码"
        assert data["messages"][2]["tool"] == "Bash"
        assert data["messages"][2]["input"] == {"command": "ls"}

    def test_thinking_status_not_in_chat(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """thinking/status 事件不进聊天记录（事件流 SSE 已展示，与 claude 对齐）。"""
        task_id, _ = self._run(
            dsh_executor, monkeypatch, tmp_path, fake_runner,
            [self._line("thinking", text="先思考"),
             self._line("stream_delta", text="回复正文"),
             self._line("status", message="回合结束: completed"),
             _RESULT_LINE])
        row = dsh_executor.db.get_task(task_id)
        data = json.loads(row["dsh_transcript"])
        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant"]  # 思考/状态不入列表
        assert data["messages"][1]["text"] == "回复正文"

    def test_stop_path_persists_partial_transcript(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """停止路径同样落库（flush 当前累积片段，不丢已产出文本）。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_done = False
        fake_runner.preset_lines = [self._line("stream_delta", text="做到一半")]
        dsh_executor.request_stop(task_id)
        try:
            code, _ = dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        finally:
            dsh_executor.clear_stop_request(task_id)
        assert code == 125
        row = dsh_executor.db.get_task(task_id)
        data = json.loads(row["dsh_transcript"])
        assert "AI 维护者" in data["messages"][0]["text"]
        assert data["messages"][1]["text"] == "做到一半"

    def test_resume_appends_to_previous_history(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """断点续跑：保留上次历史并追加本次 user 消息与回复（完整会话）。"""
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(
            task_id, None,
            dsh_transcript=json.dumps(
                {"prompt": "上次提示词",
                 "messages": [{"role": "user", "text": "上次提示词",
                               "ts": "2026-08-17T08:00:00Z", "truncated": False},
                              {"role": "assistant", "text": "上次回复",
                               "ts": "2026-08-17T08:01:00Z", "truncated": False}],
                 "truncated": False}, ensure_ascii=False))
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [
            self._line("stream_delta", text="继续修复"),
            _RESULT_LINE]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE,
                                   resume_session="sess-9")
        row = dsh_executor.db.get_task(task_id)
        data = json.loads(row["dsh_transcript"])
        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert data["messages"][2]["text"] == data["prompt"]  # 本次恢复引导语
        assert "继续处理" in data["messages"][2]["text"]
        assert data["messages"][3]["text"] == "继续修复"
        # 历史保留
        assert data["messages"][0]["text"] == "上次提示词"
        assert data["messages"][1]["text"] == "上次回复"

    def test_prompt_persisted_before_runner_starts(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """提示词在 runner 启动前已落库（运行中「查看提示词」即可用）。"""
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        prompt_seen: dict = {}

        class _CaptureRunner(_FakeRunner):
            def start(self):
                # runner 启动时（执行中）提示词应已在库
                row = dsh_executor.db.get_task(task_id)
                prompt_seen["stored"] = json.loads(
                    row["dsh_transcript"])["prompt"]
                super().start()

        monkeypatch.setattr("botler.executor.DshRunner", _CaptureRunner)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert "AI 维护者" in prompt_seen["stored"]

    def test_persist_invalid_output_keeps_value(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """无事件行输出（garbage）→ 不抛异常，落库至少含提示词消息。"""
        task_id, code = self._run(dsh_executor, monkeypatch, tmp_path,
                                  fake_runner, ["garbage"])
        assert code == 0  # 结果判定失败但执行不崩
        row = dsh_executor.db.get_task(task_id)
        data = json.loads(row["dsh_transcript"])
        assert data["messages"][0]["role"] == "user"


class TestDshCredentialsRelayFallback:
    """（issue #395）dsh 引擎回退第三方中转站（OpenAI 兼容）配置。

    用户复现：在设置页「AI API 供应商」配置第三方中转站的 base_url /
    api_key 后（provider 通常选 openai / custom 等 OpenAI 兼容类型），
    dsh 执行引擎不可用——任务 #569 失败日志：
    `llm-deepseek: no API key for provider route "deepseek-official"`。
    根因：_dsh_credentials 仅回退 provider=deepseek 的 ai_providers 项，
    openai/custom 等中转站项匹配不到 → SDK 无 key。中转站本身可用
    （其他消费 ai_providers 的功能正常），纯配置链路断裂。
    """

    def test_falls_back_to_openai_relay(self, tmp_path):
        """（bug 复现）仅配 provider=openai 的中转站 → 应回退其 key/base_url。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: 中转站
  provider: openai
  base_url: https://relay.example.com/v1
  api_key: sk-relay-12345
  model: deepseek-chat
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        # issue #397：中转站模型跟随凭据链
        assert ex._dsh_credentials(config.get()) == (
            "sk-relay-12345", "https://relay.example.com/v1", "deepseek-chat")

    def test_falls_back_to_custom_relay(self, tmp_path):
        """（bug 复现）provider=custom 的中转站同样回退。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: 中转站
  provider: custom
  base_url: https://relay.example.com/v1
  api_key: sk-relay-999
  model: deepseek-chat
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (
            "sk-relay-999", "https://relay.example.com/v1", "deepseek-chat")

    def test_deepseek_still_preferred_over_relay(self, tmp_path):
        """（issue #115 语义保持）deepseek 项与 openai 中转站并存 → 仍优先 deepseek。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK + """
- name: 中转站
  provider: openai
  base_url: https://relay.example.com/v1
  api_key: sk-relay-12345
  model: deepseek-chat
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        # deepseek 仍优先（issue #115 语义保持），模型取 deepseek 项
        assert ex._dsh_credentials(config.get()) == (
            "sk-ai-provider-key", "https://api.deepseek.com/v1",
            "deepseek-v4-flash")

    def test_disabled_relay_skipped(self, tmp_path):
        """中转站项 enabled=false → 不回退。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: 中转站
  provider: openai
  base_url: https://relay.example.com/v1
  api_key: sk-relay-12345
  model: deepseek-chat
  enabled: false
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None, None)

    def test_gemini_still_not_fallback(self, tmp_path):
        """（边界）非 OpenAI 兼容协议 provider（gemini）不回退——dsh 需要
        OpenAI 兼容 chat/completions 接口，gemini 走 generateContent。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: gemini
  provider: gemini
  base_url: https://generativelanguage.googleapis.com/v1beta
  api_key: sk-gemini
  model: gemini-2.5-pro
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None, None)


class TestDshResultAbortDetection:
    """issue #403：agent 明确报告任务终止/失败时不得判 success。

    任务 #585（issue #402）的 dsh 会话在 step 1 因「GitLab API 认证失败」
    （glab auth status 误报 Invalid token + GITLAB_TOKEN 被 dsh 运行时
    过滤不可见）主动终止，final_response 明确报告「任务已终止，未进行代码
    修改」，但 _dsh_result 仅看 finish_reason=completed + 非空回复即判
    success，平台误打 bot-done。本类测试锁定失败信号识别。
    """

    def _out(self, final_response, finish_reason="completed", error=None,
             session_id="s1"):
        data = {"final_response": final_response,
                "finish_reason": finish_reason,
                "session_id": session_id}
        if error:
            data["error"] = error
        return json.dumps(data, ensure_ascii=False)

    def test_task585_abort_report_is_not_success(self, executor):
        """任务585的真实失败报告（认证失败 + 任务已终止）→ 不得判 success。"""
        final = ('[PROGRESS] step=1 status=failed desc="仓库路径与主分支校验'
                 '通过，但 GitLab API 认证失败" evidence="GITLAB_TOKEN 未设置，'
                 'glab auth status 报 Invalid token provided"任务已终止，未进行'
                 '代码修改。\n\n原因：\n- 但 GitLab API 认证失败：`glab auth '
                 'status` 报告 `Invalid token provided`\n- 环境变量 '
                 '`GITLAB_TOKEN` 未设置\n\n根据认证失效即终止的约束，未读取 '
                 'Issue、未编写代码、未提交或推送任何改动，也未调用 Issue 关闭接口。')
        result = executor._dsh_result(self._out(final))
        assert result != "success"
        assert result == "failed"  # 认证/环境类失败 → 可重试

    def test_progress_failed_marker_is_failure(self, executor):
        """[PROGRESS] ... status=failed 里程碑标记 → 判失败。"""
        final = ('[PROGRESS] step=2 status=failed desc="定位根因失败" '
                 'evidence="测试未通过"\n任务终止。')
        assert executor._dsh_result(self._out(final)) == "failed"

    def test_terminated_without_changes_is_failure(self, executor):
        """「任务已终止，未进行代码修改」→ 判失败。"""
        final = '任务已终止，未进行代码修改。原因：环境异常。'
        assert executor._dsh_result(self._out(final)) == "failed"

    def test_blocked_session_terminated_is_failure(self, executor):
        """模板 §1.1 阻塞流程的「本会话终止，不再继续处理」→ 判失败（不判成功）。"""
        final = ('已评论缺失疑问，移除 in-progress，本会话终止，不再继续处理'
                 '该任务，等待用户补充信息。')
        assert executor._dsh_result(self._out(final)) == "failed"

    def test_success_report_with_auth_word_is_not_false_positive(self, executor):
        """正常完成报告中提及「认证失败」诊断不应误判失败。"""
        final = ('已定位根因：认证失败源于 glab 1.36 对 4 段式 PAT 的误报，'
                 '已修复并推送，测试全部通过，任务完成。')
        assert executor._dsh_result(self._out(final)) == "success"

    def test_hermes_abort_report_is_failure(self, executor):
        """hermes 引擎同样识别任务终止失败报告。"""
        final = '任务已终止，未进行代码修改。GitLab API 认证失败。'
        out = json.dumps({"final_response": final, "session_id": "h1"},
                         ensure_ascii=False)
        assert executor._hermes_result(out) == "failed"
