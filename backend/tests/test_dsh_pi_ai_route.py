"""issue #729/#730 复现测试：硅基流动等 OpenAI 兼容端点工具调用空名 bug。

根因（已在真实 SDK + 硅基流动抓包实证）：SDK runtime 默认 Cordis 组合
只挂 llm-deepseek 适配器，其 translate 的守卫
`if (call.function?.name !== void 0) block.name = call.function.name`
对硅基流动流式续片的**显式空串** name（而非缺省 undefined）成立 → 首块
已写入的工具名被续片覆盖成 "" → dsh-tools 派发 `unknown tool ""` →
任务 #729/#730 全部工具调用失败。Web 正常是因为硅基流动注册在 llm-pi-ai
（api: openai-completions），其 translate 只在 toolcall_start 记录
id/name，续片缺失/空名时不覆盖。

修复（方案 A）：base_url 非空（第三方 OpenAI 兼容端点）时 botler 派生出
挂载 llm-pi-ai + provider（baseURL / apiKeyEnv / models）的 Cordis 文件，
并改走 pi-ai provider 路由——与 Web 的 llm-pi-ai 注册同构；官方 DeepSeek
（base_url 为空 / api.deepseek.com）维持 deepseek-official 现状。

本文件先在文本层复现两种 translate 语义（确定性、不联网），再断言
executor 路由决策（base_url 非空 → pi-ai provider + 派生 cordis +
apiKeyEnv 注入环境；官方 → deepseek-official 现状）。
"""

import json
from pathlib import Path

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer


# ---- 文本层：两种 translate 语义（从运行时二进制抓包片段忠实移植） ----

# 硅基流动 SSE 对一次工具调用的实际分片（/tmp/dsh-diag 抓包精简）：
# 第 1 片带完整 name，续片是**显式空串** name（且 id/type 为 null）
_SILICONFLOW_TOOL_CALL_CHUNKS = [
    {"index": 0, "id": None, "type": None,
     "function": {"name": "bash", "arguments": ""}},
    {"index": 0, "id": None, "type": None,
     "function": {"name": "", "arguments": '{"command":"ls -la"}'}},
]


def _deepseek_translate(chunks):
    """llm-deepseek translate 语义移植（translate.ts 真实守卫）。

    原始代码：`if (call.function?.name !== void 0) block.name = ...`。
    `!== void 0` 对显式空串成立（"" !== undefined），因此续片把名字覆盖。
    """
    block = {"id": "", "name": "", "arguments": ""}
    for call in chunks:
        fn = call.get("function") or {}
        if fn.get("name") is not None:      # 守卫：!== void 0
            block["name"] = fn["name"]
        if fn.get("arguments") is not None:
            block["arguments"] = fn["arguments"]
        block["id"] = call.get("id") or ""
    return block


def _pi_ai_translate(chunks):
    """llm-pi-ai translate 语义移植（stream.ts 真实行为）。

    toolcall_start 记录 id/name；toolcall_delta 仅当 name 非空才发；
    续片缺失/空名不覆盖首块写入的名字。
    """
    known = {"id": "", "name": ""}
    for call in chunks:
        fn = call.get("function") or {}
        name = fn.get("name")
        # 真实代码：known.name 来自 toolcall_start（首片），后续 delta 仅
        # `if (known.name.length > 0)` 时才发 name——显式空串被跳过
        if known["name"] == "" and name:    # 首片（或首个非空名）记录
            known["name"] = name
        known["id"] = known["id"] or (call.get("id") or "")
    return {"id": known["id"], "name": known["name"],
            "arguments": chunks[-1].get("function", {}).get("arguments", "")}


class TestTranslateSemantics:
    """两种适配器对硅基流动分片的差异（根因的确定性文本级复现）。"""

    def test_deepseek_guard_loses_tool_name_on_empty_continuation(self):
        """llm-deepseek 守卫把续片显式空串 name 视为有效 → 名字被覆盖成空。"""
        block = _deepseek_translate(_SILICONFLOW_TOOL_CALL_CHUNKS)
        assert block["name"] == ""          # 即 `unknown tool ""` 来源
        assert block["arguments"] == '{"command":"ls -la"}'

    def test_pi_ai_preserves_tool_name_across_chunks(self):
        """llm-pi-ai 只在首片记录名字、续片空名不覆盖 → 名字保留。"""
        block = _pi_ai_translate(_SILICONFLOW_TOOL_CALL_CHUNKS)
        assert block["name"] == "bash"
        assert block["arguments"] == '{"command":"ls -la"}'


# ---- Cordis 派生（方案 A 的构建单元） ----

def _build_pi_ai_cordis_text():
    from botler.dsh_runner import build_pi_ai_cordis_text  # 惰性导入
    return build_pi_ai_cordis_text(
        "- id: llm-deepseek\n  name: '@deepseek-ai/dsh-llm-deepseek'\n",
        "botler-pi-ai",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="BOTLER_DSH_PI_AI_KEY",
        model="deepseek-ai/DeepSeek-V4-Flash")


def _resolve_dsh_pi_ai_cordis(*args, **kwargs):
    from botler.dsh_runner import resolve_dsh_pi_ai_cordis  # 惰性导入
    return resolve_dsh_pi_ai_cordis(*args, **kwargs)


class TestPiAiCordisBuild:
    """build_pi_ai_cordis_text / resolve_dsh_pi_ai_cordis（方案 A 构建单元）。"""

    def test_build_pi_ai_cordis_text_mounts_llm_pi_ai(self):
        text = _build_pi_ai_cordis_text()
        assert "- id: llm-pi-ai" in text
        assert "name: '@deepseek-ai/dsh-llm-pi-ai'" in text
        assert "      botler-pi-ai:" in text
        assert "        api: openai-completions" in text
        assert "        baseURL: https://api.siliconflow.cn/v1" in text
        assert "        apiKeyEnv: BOTLER_DSH_PI_AI_KEY" in text
        assert "          - id: deepseek-ai/DeepSeek-V4-Flash" in text
        # 原 llm-deepseek 条目保留（自定义组合基底不被破坏）
        assert "- id: llm-deepseek" in text

    def test_resolve_pi_ai_cordis_returns_cached_file(self, tmp_path):
        bundled = "- id: llm-deepseek\n  name: '@deepseek-ai/dsh-llm-deepseek'\n"

        def bundled_text_provider():
            return bundled

        out = _resolve_dsh_pi_ai_cordis(
            None, "botler-pi-ai",
            base_url="https://api.siliconflow.cn/v1",
            api_key_env="BOTLER_DSH_PI_AI_KEY",
            model="deepseek-ai/DeepSeek-V4-Flash",
            bundled_text_provider=bundled_text_provider)
        assert out.endswith(".yml")
        assert Path(out).is_file()
        assert "- id: llm-pi-ai" in Path(out).read_text(encoding="utf-8")
        # 同参数幂等：缓存复用同一文件
        out2 = _resolve_dsh_pi_ai_cordis(
            None, "botler-pi-ai",
            base_url="https://api.siliconflow.cn/v1",
            api_key_env="BOTLER_DSH_PI_AI_KEY",
            model="deepseek-ai/DeepSeek-V4-Flash",
            bundled_text_provider=bundled_text_provider)
        assert out2 == out

    def test_resolve_pi_ai_cordis_custom_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            _resolve_dsh_pi_ai_cordis(
                "/no/such/cordis.yml", "botler-pi-ai",
                base_url="https://api.siliconflow.cn/v1",
                api_key_env="BOTLER_DSH_PI_AI_KEY",
                model="deepseek-ai/DeepSeek-V4-Flash")


# ---- executor 路由决策（复现测试：修复前应失败） ----

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {worker}
claude: {{}}
hermes: {{}}
dsh: {dsh}
ai_providers: {ai_providers}
templates: {{}}
repos: []
"""

_REPO = {"name": "demo", "prompt_template": None}

_ISSUE = {"state": "opened", "title": "标题", "description": "正文",
          "web_url": "https://gitlab.example.com/x/-/issues/7",
          "project_id": 42, "iid": 7}

_RESULT_LINE = json.dumps({"final_response": "已修复并推送，issue #7 处理完成",
                           "finish_reason": "completed",
                           "session_id": "dsh-sess-1"}, ensure_ascii=False)

# 设置页「AI 供应商」硅基流动项（config.yaml 实际启用项同构）
_AI_PROVIDERS_SILICONFLOW = """
- name: 硅基流动
  provider: custom
  base_url: https://api.siliconflow.cn/v1
  api_key: sk-siliconflow-key
  model: deepseek-ai/DeepSeek-V4-Flash
  enabled: true
"""


def _mk_config(tmp_path, worker_extra="{precheck_enabled: false}", dsh_extra="{}",
               ai_providers_extra="[]") -> ConfigManager:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.format(worker=worker_extra, dsh=dsh_extra,
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


class _FakeRunner:
    """假 DshRunner：start 时同步回放 preset_lines（test_executor_dsh.py 同款）。"""

    instances: list["_FakeRunner"] = []
    preset_lines: list[str] = []
    preset_done: bool = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._done = _FakeRunner.preset_done
        self.result_lines = list(_FakeRunner.preset_lines)
        _FakeRunner.instances.append(self)

    def start(self):
        for line in self.result_lines:
            self.kwargs["on_line"](line)

    def done(self):
        return self._done

    def finish(self):
        return 0

    def stop(self):
        pass


@pytest.fixture
def fake_runner(monkeypatch):
    _FakeRunner.instances.clear()
    _FakeRunner.preset_lines = []
    _FakeRunner.preset_done = True
    monkeypatch.setattr("botler.executor.DshRunner", _FakeRunner)
    monkeypatch.setattr("botler.executor.DshSdkNotInstalledError",
                        type("DshSdkNotInstalledError", (Exception,), {}))
    return _FakeRunner


def _patch_workspace(monkeypatch, executor, tmp_path):
    """替换 prepare_workspace / _log_file，避免真实 git/磁盘交互。"""
    def fake_prepare(repo, resume=False):
        return tmp_path / "work", {"GIT_ASKPASS": "/askpass"}

    monkeypatch.setattr(executor, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(executor, "_log_file",
                        lambda tid: tmp_path / f"task_{tid}.log")
    return tmp_path / "work"


class TestRouteDecision:
    """issue #729/#730：base_url 非空（第三方 OpenAI 兼容端点）→ pi-ai 路由。
    修复前：provider 固定 deepseek-official、cordis 为空、无 apiKeyEnv → 失败。
    """

    def test_siliconflow_base_url_routes_pi_ai(
            self, tmp_path, monkeypatch, fake_runner):
        sessions = tmp_path / "dsh-sessions"
        sessions.mkdir(exist_ok=True)
        # 自定义 cordis 基底（避免测试依赖 SDK 内置组合文件）
        base_cordis = tmp_path / "cordis.yml"
        base_cordis.write_text(
            "- id: llm-deepseek\n  name: '@deepseek-ai/dsh-llm-deepseek'\n",
            encoding="utf-8")
        config = _mk_config(
            tmp_path,
            worker_extra="\n  engine: dsh\n  precheck_enabled: false",
            dsh_extra=f"\n  session_root: {sessions}"
                      f"\n  cordis: {base_cordis}",
            ai_providers_extra=_AI_PROVIDERS_SILICONFLOW)
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        code, output = ex._run_dsh_once(1, _REPO, _ISSUE)
        assert code == 0
        kwargs = fake_runner.instances[0].kwargs
        # 路由到 llm-pi-ai 提供商（修复核心：不再 deepseek-official）
        assert kwargs["provider"] == "botler-pi-ai"
        # 派生 cordis 文件挂载 llm-pi-ai + siliconflow provider 条目
        cordis = kwargs["cordis"]
        assert cordis and str(cordis).endswith(".yml")
        assert "- id: llm-pi-ai" in Path(cordis).read_text(encoding="utf-8")
        assert "baseURL: https://api.siliconflow.cn/v1" in Path(cordis).read_text(
            encoding="utf-8")
        # apiKeyEnv 变量已注入 runner 环境（运行时凭据缝读取）
        assert kwargs["env"]["BOTLER_DSH_PI_AI_KEY"] == "sk-siliconflow-key"
        # 凭据与模型透传（issue #115/#397 语义保持）
        assert kwargs["base_url"] == "https://api.siliconflow.cn/v1"
        assert kwargs["api_key"] == "sk-siliconflow-key"
        assert kwargs["model"] == "deepseek-ai/DeepSeek-V4-Flash"

    def test_official_deepseek_route_unchanged(self, tmp_path, monkeypatch, fake_runner):
        """官方 DeepSeek（base_url 空 / 无第三方端点）→ deepseek-official 现状。"""
        sessions = tmp_path / "dsh-sessions"
        sessions.mkdir(exist_ok=True)
        config = _mk_config(
            tmp_path,
            worker_extra="\n  engine: dsh\n  precheck_enabled: false",
            dsh_extra=f"\n  session_root: {sessions}")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        code, output = ex._run_dsh_once(1, _REPO, _ISSUE)
        assert code == 0
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["provider"] == "deepseek-official"
        assert kwargs["cordis"] is None
        assert "BOTLER_DSH_PI_AI_KEY" not in kwargs["env"]
        assert kwargs["api_key"] is None
        assert kwargs["base_url"] is None

    def test_official_deepseek_host_keeps_deepseek_route(
            self, tmp_path, monkeypatch, fake_runner):
        """ai_providers 官方 DeepSeek 域名（base_url 非空但主机官方）→ 不回退
        pi-ai 路由（llm-deepseek 对官方 API 正常，issue #115 语义不变）。"""
        sessions = tmp_path / "dsh-sessions"
        sessions.mkdir(exist_ok=True)
        config = _mk_config(
            tmp_path,
            worker_extra="\n  engine: dsh\n  precheck_enabled: false",
            dsh_extra=f"\n  session_root: {sessions}",
            ai_providers_extra="""
- name: deepseek
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key: sk-ai-provider-key
  model: deepseek-v4-flash
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        code, output = ex._run_dsh_once(1, _REPO, _ISSUE)
        assert code == 0
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["provider"] == "deepseek-official"
        assert kwargs["cordis"] is None
        assert kwargs["base_url"] == "https://api.deepseek.com/v1"
        assert kwargs["api_key"] == "sk-ai-provider-key"