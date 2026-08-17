# -*- coding: utf-8 -*-
"""
Botler 鸿蒙端（Web 套壳）工程结构回归测试（issue #173）

背景：鸿蒙端（harmony/）是「系统 Web 组件（WebView）套壳」工程，CI 在
harmony:build 作业中用 hvigorw 做真实编译。本测试复用
harmony/scripts/validate_harmony.py 的结构校验逻辑，作为 backend 全量
测试中的回归防线，防止后续改动破坏鸿蒙工程的关键配置（网络权限 / Web
组件套壳 / 加载地址 / SDK 版本 / 路由表等）。

覆盖范围：
  - JSON5 迷你解析器（注释 / 尾逗号 / 单引号 / 无引号键）；
  - 真实 harmony 工程目录通过全部校验（防回退）；
  - 关键配置缺失能被检出：INTERNET 权限、Web 组件、WEB_URL 地址、
    targetSdkVersion、必需文件等。
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

# backend/tests/test_xxx.py -> parents[0]=tests, [1]=backend, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
HARMONY_DIR = REPO_ROOT / 'harmony'
VALIDATOR_PATH = HARMONY_DIR / 'scripts' / 'validate_harmony.py'

# 校验器复制工程时忽略的构建产物（避免 tmp 目录拷贝体积与噪音）
_IGNORE_PATTERNS = shutil.ignore_patterns('.hvigor', 'build', 'oh_modules', '.git', '__pycache__')


def load_validator():
    """动态加载 harmony/scripts/validate_harmony.py（独立脚本，不在包路径内）。"""
    spec = importlib.util.spec_from_file_location('validate_harmony', VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def vh():
    """校验器模块（整个测试模块只加载一次）。"""
    return load_validator()


def copy_project(tmp_path: Path) -> Path:
    """把真实 harmony 工程复制到临时目录（剔除构建产物），供破坏性测试使用。"""
    dest = tmp_path / 'harmony'
    shutil.copytree(HARMONY_DIR, dest, ignore=_IGNORE_PATTERNS)
    return dest


# ============================================================
# JSON5 迷你解析器
# ============================================================

class TestJson5Parser:
    def test_comments_trailing_commas_single_quotes_unquoted_keys(self, vh):
        """行/块注释、尾逗号、单引号字符串、无引号键均能正确解析。"""
        text = """
        {
          // 行注释
          "a": 1, /* 块注释 */
          'b': 'x',
          c: [1, 2,],
        }
        """
        assert vh.parse_json5(text) == {'a': 1, 'b': 'x', 'c': [1, 2]}

    def test_comment_like_text_inside_string_preserved(self, vh):
        """字符串字面量里的 // 与引号不应被当作注释/字符串边界。"""
        text = r'''{ "url": "http://10.0.0.122:8000", 'note': "it's ok" }'''
        assert vh.parse_json5(text) == {'url': 'http://10.0.0.122:8000', 'note': "it's ok"}

    def test_invalid_json5_raises(self, vh):
        """语法错误应抛出 ValueError 而非静默失败。"""
        with pytest.raises(ValueError):
            vh.parse_json5('{ "a": }')


# ============================================================
# 真实工程通过校验（防回退）
# ============================================================

class TestRealProject:
    def test_real_project_passes_all_checks(self, vh):
        """仓库内真实 harmony 工程必须通过全部结构校验。"""
        errors = vh.validate_project(HARMONY_DIR)
        assert errors == [], f'鸿蒙工程结构校验失败:\n' + '\n'.join(f'  - {e}' for e in errors)


# ============================================================
# 关键配置缺失可被检出（破坏性测试）
# ============================================================

class TestMissingConfigDetected:
    def test_missing_internet_permission(self, vh, tmp_path):
        """移除 INTERNET 权限必须被检出（Web 套壳无法联网）。"""
        proj = copy_project(tmp_path)
        target = proj / 'entry/src/main/module.json5'
        text = target.read_text(encoding='utf-8').replace(
            'ohos.permission.INTERNET', 'ohos.permission.SOME_OTHER')
        target.write_text(text, encoding='utf-8')
        errors = vh.validate_project(proj)
        assert any('INTERNET' in e for e in errors), errors

    def test_missing_web_component(self, vh, tmp_path):
        """Index.ets 移除 Web 组件调用必须被检出（不再是套壳）。"""
        proj = copy_project(tmp_path)
        target = proj / 'entry/src/main/ets/pages/Index.ets'
        text = target.read_text(encoding='utf-8').replace('Web({', 'Column({')
        target.write_text(text, encoding='utf-8')
        errors = vh.validate_project(proj)
        assert any('Web 组件' in e for e in errors), errors

    def test_missing_web_url(self, vh, tmp_path):
        """AppConfig.ets 移除 WEB_URL 必须被检出（无法确定加载地址）。"""
        proj = copy_project(tmp_path)
        target = proj / 'entry/src/main/ets/common/AppConfig.ets'
        text = target.read_text(encoding='utf-8').replace(
            "export const WEB_URL: string = 'http://10.0.0.122:8000';", '')
        target.write_text(text, encoding='utf-8')
        errors = vh.validate_project(proj)
        assert any('WEB_URL' in e for e in errors), errors

    def test_non_http_web_url_rejected(self, vh, tmp_path):
        """WEB_URL 使用非 http(s) 协议必须被检出。"""
        proj = copy_project(tmp_path)
        target = proj / 'entry/src/main/ets/common/AppConfig.ets'
        text = target.read_text(encoding='utf-8').replace(
            "'http://10.0.0.122:8000'", "'ftp://10.0.0.122:8000'")
        target.write_text(text, encoding='utf-8')
        errors = vh.validate_project(proj)
        assert any('http(s)' in e for e in errors), errors

    def test_missing_target_sdk_version(self, vh, tmp_path):
        """build-profile.json5 移除 targetSdkVersion 必须被检出（SDK 约束缺失）。"""
        proj = copy_project(tmp_path)
        target = proj / 'build-profile.json5'
        text = target.read_text(encoding='utf-8').replace(
            '"targetSdkVersion": "6.1.1(24)",', '')
        target.write_text(text, encoding='utf-8')
        errors = vh.validate_project(proj)
        assert any('targetSdkVersion' in e for e in errors), errors

    def test_missing_required_file(self, vh, tmp_path):
        """删除 AppScope/app.json5 必须被检出（工程不完整）。"""
        proj = copy_project(tmp_path)
        (proj / 'AppScope/app.json5').unlink()
        errors = vh.validate_project(proj)
        assert any('AppScope/app.json5' in e for e in errors), errors
