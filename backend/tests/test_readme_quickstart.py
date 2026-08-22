"""README 快速上手文档的基础契约测试。"""

from pathlib import Path


README_PATH = Path(__file__).resolve().parents[2] / "README.md"


def test_readme_contains_new_user_quickstart_contract():
    """首次部署所需的定位、配置、启动与参与信息均应可在 README 找到。"""
    content = README_PATH.read_text(encoding="utf-8")

    required_phrases = (
        "## 定位与核心能力",
        "## 典型使用流程",
        "## 运行环境与前置条件",
        "## 最小可运行示例",
        "GITLAB_BOT_TOKEN",
        "WEBHOOK_SECRET",
        "gitlab.bot_token",
        "## 参与开发",
        "## License",
    )

    missing_phrases = [phrase for phrase in required_phrases if phrase not in content]
    assert not missing_phrases, f"README 缺少首次上手必要内容：{', '.join(missing_phrases)}"
