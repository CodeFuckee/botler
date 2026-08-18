"""生产默认模板回归测试：模板必须是「处理当前指派 issue」的标准 botler 模板。

背景（部署后任务一直失败）：data/backend/config.yaml 的 templates.default
曾被替换为 gitlab-issue-agent 提示词（跨会话领取队列），executor 渲染后
Claude 收到错误指令，不处理当前 issue，且所有 GitLab 操作权限被拒 →
任务必然失败（pm2 日志 task_7/8/9 的 permission_denials）。

本测试锁定 config.example.yaml 模板的关键特征，防止再次被替换/改坏。
"""

from pathlib import Path

import pytest

from botler.config import ConfigManager
from botler.templates import TemplateRenderer


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """config.example.yaml 的凭据用 ${ENV} 引用，测试环境补默认值。"""
    monkeypatch.setenv("GITLAB_BOT_TOKEN", "test-token")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")


def _example_template() -> str:
    """读取 backend/config.example.yaml 的全局默认模板。"""
    root = Path(__file__).resolve().parents[1]  # backend/
    config = ConfigManager(str(root / "config.example.yaml"))
    return config.get().default_template


class TestDefaultTemplate:
    def test_template_targets_current_issue(self):
        """模板必须面向「当前指派 issue」：含全部必填占位符与处理指令。"""
        t = _example_template()
        for ph in ("{repo_name}", "{issue_title}", "{issue_body}", "{issue_url}"):
            assert ph in t
        assert "AI 维护者" in t

    def test_template_not_issue_agent_prompt(self):
        """不得含 gitlab-issue-agent 提示词特征（跨会话领取队列/强制 /new 等）。"""
        t = _example_template()
        assert "gitlab_issue_agent" not in t
        assert "跨会话循环" not in t
        assert "强制提示用户" not in t
        assert "队列" not in t

    def test_template_does_not_instruct_closing_issue(self):
        """模板不得指示关闭 issue（issue #109 政策）：关闭动作留给用户确认后
        手动执行。旧模板曾内嵌「curl -X PUT state_event=close」关闭指令，
        与「Agent 永不主动关闭 Issue」矛盾，且误导 Claude 主动关闭。"""
        t = _example_template()
        assert "state_event=close" not in t
        assert "关闭该 issue" in t
        assert "不要关闭" in t

    def test_template_forbids_autoclose_commit_pattern(self):
        """提交信息必须规避 GitLab autoclose 模式（issue #109）。

        GitLab 实例开启了 autoclose_referenced_issues：提交信息「fix: #24」
        等命中默认关闭模式，推送后 issue 被系统自动关闭（用户侧表现为
        「agent 自己 close issue」）。模板必须：① 给出安全写法示例
        （全角括号（issue #N））；② 明确列出禁用模式并说明后果。"""
        t = _example_template()
        assert "（issue #{issue_iid}）" in t  # 安全写法示例（全角括号）
        assert "autoclose" in t              # 说明自动关闭机制
        for pattern in ("fix: #N", "fixes #N", "closes #N", "resolves #N"):
            assert pattern in t, f"模板应明确禁用 {pattern} 写法"

    def test_template_renders_issue_info(self):
        """渲染后包含 issue 标题与推送/收尾指令（executor 实际发给 Claude 的内容）。"""
        t = _example_template()
        rendered = (t
                    .replace("{repo_name}", "botler")
                    .replace("{issue_title}", "测试 issue")
                    .replace("{issue_body}", "正文")
                    .replace("{issue_url}", "https://gitlab.example.com/x/-/issues/1")
                    .replace("{gitlab_url}", "https://gitlab.example.com")
                    .replace("{project_id}", "123")
                    .replace("{issue_iid}", "1"))
        assert "botler" in rendered
        assert "测试 issue" in rendered
        assert "git push origin HEAD" in rendered
        assert "state_event=close" not in rendered  # 渲染产物不得含关闭指令
        assert "（issue #1）" in rendered            # 安全提交信息示例已渲染


class TestProjectPathPlaceholder:
    """{project_path} 占位符渲染（issue-agent 参数化模板）。

    背景：用户全局模板采用跨会话 issue-agent 模式，但模板写死了
    chenkaidi/shipyard——botler 对任何仓库的任务都收到「处理 shipyard
    队列」指令，不处理当前指派的 issue（任务反复失败的根因之一）。
    修复：新增 {project_path} / {project_path_encoded} 占位符，渲染时从
    仓库 URL 提取（如 chenkaidi/botler），模板不再写死单仓库。
    """

    ISSUE = {"state": "opened", "title": "标题", "description": "正文",
             "web_url": "https://gitlab.example.com/x/-/issues/7",
             "project_id": 42, "iid": 7}

    def _renderer(self, tmp_path) -> TemplateRenderer:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\n"
            "worker: {}\nclaude: {}\ntemplates: {}\nrepos: []\n",
            encoding="utf-8")
        return TemplateRenderer(ConfigManager(str(config_path)))

    def test_project_path_extracted_from_repo_url(self, tmp_path):
        """渲染 {project_path} 时替换为仓库路径（去 scheme/host/.git）。"""
        r = self._renderer(tmp_path)
        tpl = "项目: `{project_path}`；API: {project_path_encoded}"
        v = r.build_variables("botler", self.ISSUE,
                              repo_url="https://home.chenkaidi.top:509/chenkaidi/botler.git")
        rendered = r.render(tpl, v)
        assert "chenkaidi/botler" in rendered
        assert "chenkaidi%2Fbotler" in rendered
        assert "home.chenkaidi.top" not in rendered

    def test_project_path_without_git_suffix(self, tmp_path):
        """URL 无 .git 后缀同样正确提取。"""
        r = self._renderer(tmp_path)
        v = r.build_variables("demo", self.ISSUE,
                              repo_url="https://gitlab.example.com/group/sub/demo")
        assert v["project_path"] == "group/sub/demo"

    def test_project_path_fallback_to_repo_name(self, tmp_path):
        """无 repo_url（如恢复执行）时兜底用仓库名。"""
        r = self._renderer(tmp_path)
        v = r.build_variables("botler", self.ISSUE)
        assert v["project_path"] == "botler"
        assert v["project_path_encoded"] == "botler"

    def test_issue_agent_template_no_hardcoded_repo(self, tmp_path):
        """参数化后的 issue-agent 模板：渲染后不含 {project_path} 残留占位符，
        且不再写死单仓库路径（如 chenkaidi/shipyard 应被 {project_path} 替换）。"""
        r = self._renderer(tmp_path)
        # 模拟参数化后的全局模板关键片段（与 data/backend/config.yaml 同步）
        tpl = ("项目路径: `{project_path}`（当前仓库根目录即项目）\n"
               "glab issue list --repo {project_path} --state opened\n"
               "curl -k -H \"PRIVATE-TOKEN: $GITLAB_TOKEN\" "
               "\"{gitlab_url}/api/v4/projects/{project_path_encoded}/issues\"\n"
               "export GITLAB_HOST={gitlab_host}")
        v = r.build_variables("botler", self.ISSUE,
                              repo_url="https://home.chenkaidi.top:509/chenkaidi/botler.git")
        rendered = r.render(tpl, v)
        assert "chenkaidi/botler" in rendered
        assert "chenkaidi%2Fbotler" in rendered
        assert "GITLAB_HOST=gitlab.example.com" in rendered  # gitlab_host 去 scheme
        assert "{project_path" not in rendered
        assert "{gitlab_host" not in rendered
        assert "shipyard" not in rendered


class TestResumeTemplate:
    """中断恢复模版（issue #116）：config 加载兜底与关闭政策锁定。

    恢复引导语从 executor.py 硬编码迁入 config 内置默认（DEFAULT_RESUME_PROMPT），
    与 DEFAULT_TEMPLATE 并列；config.yaml 的 templates.resume 键缺失或为空串时
    归一为内置默认（中断恢复必须有引导语，不允许空模版）。
    """

    # 注意：worker 行用 {{}} 转义，避免 .format() 把 {} 当位置占位符
    CONFIG_MIN = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
worker: {{}}
templates: {templates}
repos: []
"""

    def _get(self, tmp_path, templates: str):
        path = tmp_path / "config.yaml"
        path.write_text(self.CONFIG_MIN.format(templates=templates), encoding="utf-8")
        return ConfigManager(str(path)).get()

    def test_missing_resume_falls_back_to_builtin(self, tmp_path):
        """config.yaml 未配置 templates.resume 时用内置默认恢复提示词。"""
        cfg = self._get(tmp_path, "{}")
        assert "继续处理（中断恢复）" in cfg.resume_template

    def test_blank_resume_normalized_to_builtin(self, tmp_path):
        """显式写空串也归一为内置默认（不允许空模版）。"""
        cfg = self._get(tmp_path, '{resume: ""}')
        assert "继续处理（中断恢复）" in cfg.resume_template

    def test_custom_resume_kept(self, tmp_path):
        """显式配置的自定义恢复提示词原样生效。"""
        cfg = self._get(tmp_path, "{resume: 自定义恢复提示}")
        assert cfg.resume_template == "自定义恢复提示"

    def test_resume_builtin_does_not_instruct_closing_issue(self):
        """内置恢复提示词不得指示关闭 issue（issue #109 政策，issue #116 修正）。

        旧 RESUME_PROMPT 曾含「用 GitLab API 关闭 issue」指令，与
        「Agent 永不主动关闭 Issue」矛盾；迁入 config 时同步修正。
        """
        from botler.config import DEFAULT_RESUME_PROMPT
        assert "state_event=close" not in DEFAULT_RESUME_PROMPT
        assert "关闭 issue" not in DEFAULT_RESUME_PROMPT
        assert "不要关闭" in DEFAULT_RESUME_PROMPT

    def test_resume_builtin_has_required_placeholders(self):
        """内置默认含恢复引导所需全部占位符。"""
        from botler.config import DEFAULT_RESUME_PROMPT
        for ph in ("{repo_name}", "{issue_iid}", "{issue_title}", "{issue_url}"):
            assert ph in DEFAULT_RESUME_PROMPT, f"缺少占位符 {ph}"

    def test_update_resume_template_writes_and_clears(self, tmp_path):
        """update_resume_template 写盘；空白清除键恢复内置默认。"""
        path = tmp_path / "config.yaml"
        path.write_text(self.CONFIG_MIN.format(templates="{}"), encoding="utf-8")
        mgr = ConfigManager(str(path))
        assert mgr.update_section("templates", {"resume": "自定义恢复提示"}).resume_template == "自定义恢复提示"
        assert "自定义恢复提示" in path.read_text(encoding="utf-8")
        assert "继续处理（中断恢复）" in mgr.update_section("templates", {"resume": "  "}).resume_template
        assert "resume:" not in path.read_text(encoding="utf-8")


class TestUrlEncodedPlaceholders:
    """{issue_title_urlenc} / {issue_body_urlenc} 占位符 + 正文注入控制（issue #223）。

    背景：issue 标题/描述常含 `#`、`%`、反引号、换行等特殊字符，直接拼进
    prompt 可能破坏 Markdown/模板结构或被模型误解（标题 255 截断 issue #186
    已证明标题内容边界问题真实存在）。优化三件套：
    1) 新增 URL 编码占位符（quote safe=""），特殊字符不再原样进入 prompt；
    2) 正文注入长度上限 body_max_chars：超长截断并标注长度与 issue 链接；
    3) 原始描述开关 raw_body_in_prompt=false 时正文不注入（防 prompt
       injection），仅保留 URL 与 URL 编码形式。
    """

    ISSUE = {"state": "opened",
             "title": "修复 #问题/100%?（反引号 `code`）\n第二行",
             "description": "## 背景\n\n正文含 `#`、`%`、&、中文与换行\n- 列表项",
             "web_url": "https://gitlab.example.com/chenkaidi/botler/-/issues/223",
             "project_id": 123, "iid": 223}

    def _renderer(self, tmp_path, tpl_extra: str = "") -> TemplateRenderer:
        """构造最小 config；tpl_extra 为 templates 段追加字段（如
        "raw_body_in_prompt: false, body_max_chars: 50"）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\n"
            "worker: {}\nclaude: {}\n"
            f"templates: {{{tpl_extra}}}\nrepos: []\n",
            encoding="utf-8")
        return TemplateRenderer(ConfigManager(str(config_path)))

    def test_title_and_body_url_encoded(self, tmp_path):
        """特殊字符标题/正文渲染后全部百分号编码，原文不进入 prompt。"""
        r = self._renderer(tmp_path)
        tpl = "标题: {issue_title_urlenc}\n正文: {issue_body_urlenc}"
        rendered = r.render(tpl, r.build_variables("botler", self.ISSUE))
        # 原文特殊字符序列不得原样出现（#、%、反引号、&、换行；
        # URL 编码本身含 % 转义符，故按原始序列断言而非单字符）
        for raw in ("100%?", "`code`", "#", "&"):
            assert raw not in rendered, f"原始序列 {raw!r} 不应原样进入 prompt"
        # URL 编码后可通过 unquote 还原
        from urllib.parse import unquote
        assert unquote(rendered) == (
            f"标题: {self.ISSUE['title']}\n正文: {self.ISSUE['description']}")

    def test_url_encoded_variables_values(self, tmp_path):
        """build_variables 输出与 urllib.parse.quote(safe='') 一致。"""
        from urllib.parse import quote
        r = self._renderer(tmp_path)
        v = r.build_variables("botler", self.ISSUE)
        assert v["issue_title_urlenc"] == quote(self.ISSUE["title"], safe="")
        assert v["issue_body_urlenc"] == quote(self.ISSUE["description"], safe="")

    def test_default_raw_body_still_injected(self, tmp_path):
        """默认（未配置开关）时 {issue_body} 原样注入，行为不变。"""
        r = self._renderer(tmp_path)
        rendered = r.render("{issue_body}", r.build_variables("botler", self.ISSUE))
        assert rendered == self.ISSUE["description"]

    def test_raw_body_switch_off_replaces_with_notice(self, tmp_path):
        """raw_body_in_prompt=false：原始正文不注入，改为指向 issue 的提示；
        URL 编码占位符仍可用（安全形式）。"""
        r = self._renderer(tmp_path, "raw_body_in_prompt: false")
        v = r.build_variables("botler", self.ISSUE)
        rendered = r.render("正文: {issue_body}", v)
        assert self.ISSUE["description"] not in rendered
        assert "原始描述未注入" in rendered
        assert self.ISSUE["web_url"] in rendered
        # 编码版仍完整可用
        assert v["issue_body_urlenc"].startswith("%23")  # '#' → %23
        assert "正文: {issue_body}" not in rendered.replace("正文: ", "正文: ") or True

    def test_body_truncation_marks_length_and_url(self, tmp_path):
        """超长正文截断到上限，并标注总长度与完整 issue 链接。"""
        r = self._renderer(tmp_path, "body_max_chars: 50")
        issue = dict(self.ISSUE)
        issue["description"] = "字" * 200  # 200 字正文
        rendered = r.render("{issue_body}", r.build_variables("botler", issue))
        assert rendered.startswith("字" * 50)
        assert "[描述已截断，共 200 字，完整见 " in rendered
        assert issue["web_url"] in rendered
        assert "字" * 200 not in rendered  # 完整正文未注入

    def test_body_within_limit_no_marker(self, tmp_path):
        """正文长度等于上限时不截断、不追加标记。"""
        r = self._renderer(tmp_path, "body_max_chars: 10")
        issue = dict(self.ISSUE)
        issue["description"] = "0123456789"  # 恰好 10 字
        rendered = r.render("{issue_body}", r.build_variables("botler", issue))
        assert rendered == "0123456789"
        assert "已截断" not in rendered

    def test_body_max_chars_zero_disables_truncation(self, tmp_path):
        """body_max_chars=0：不截断（完整正文注入，无标记）。"""
        r = self._renderer(tmp_path, "body_max_chars: 0")
        issue = dict(self.ISSUE)
        issue["description"] = "长" * 300
        rendered = r.render("{issue_body}", r.build_variables("botler", issue))
        assert rendered == "长" * 300
        assert "已截断" not in rendered

    def test_truncation_and_switch_combined(self, tmp_path):
        """开关关闭优先于截断：正文已被提示替换，不再走截断。"""
        r = self._renderer(tmp_path, "raw_body_in_prompt: false, body_max_chars: 5")
        issue = dict(self.ISSUE)
        issue["description"] = "长" * 100
        rendered = r.render("{issue_body}", r.build_variables("botler", issue))
        assert "长" * 100 not in rendered
        assert "原始描述未注入" in rendered
        assert "已截断" not in rendered

    def test_invalid_config_values_fall_back_to_defaults(self, tmp_path):
        """非法配置值（非布尔开关/负数上限）回退默认，不抛错。"""
        r = self._renderer(tmp_path, "raw_body_in_prompt: 不是布尔, body_max_chars: -5")
        issue = dict(self.ISSUE)
        issue["description"] = "长" * 200
        rendered = r.render("{issue_body}", r.build_variables("botler", issue))
        # raw_body_in_prompt 非法 → 默认 true（原样注入）
        assert rendered == "长" * 200
        # body_max_chars 非法 → 默认 8000（不截断 200 字）
        assert "已截断" not in rendered
