#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Botler 鸿蒙端工程结构校验脚本（CI harmony:build 第 0 步 + pytest 回归测试共用）

背景：鸿蒙端（harmony/）是「系统 Web 组件（WebView）套壳」工程，CI 在
harmony:build 作业中用 hvigorw 做真实编译。本脚本在编译前做快速结构校验，
把「配置缺失/引用断裂」这类错误前置到秒级反馈，并在 backend 全量测试中
作为回归防线（backend/tests/test_harmony_project.py 复用本模块）。

校验项：
  1. 必需文件/目录齐全（AppScope、entry 模块、构建配置等）；
  2. AppScope/app.json5：bundleName / versionCode / versionName / icon / label；
  3. entry/src/main/module.json5：INTERNET 权限、mainElement、EntryAbility 配置；
  4. entry/src/main/ets/common/AppConfig.ets：WEB_URL 已定义且为 http(s) 地址；
  5. entry/src/main/ets/pages/Index.ets：使用 Web 组件并开启 javaScript/domStorage；
  6. 根 build-profile.json5：product 配置 targetSdkVersion / compatibleSdkVersion，
     且包含 entry 模块；
  7. hvigor/hvigor-config.json5：modelVersion 已声明；
  8. main_pages.json 路由表包含 pages/Index。

退出码：0 = 全部通过；1 = 存在错误。纯标准库实现（JSON5 解析用内置迷你解析器），
不依赖第三方包，可在任意 python3 环境直接运行。
"""

import json
import re
import sys
from pathlib import Path


# ============================================================
# 迷你 JSON5 解析器（标准库实现，支持注释/尾逗号/单引号/无引号键）
# ============================================================

def _strip_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释（字符串字面量内的注释符保留）。"""
    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        c = text[i]
        if quote is not None:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _to_strict_json(text: str) -> str:
    """把 JSON5 规整为严格 JSON：单引号转双引号、无引号键补引号、去尾逗号。"""
    # 单引号字符串转双引号（含转义处理）
    def _sq(m: re.Match[str]) -> str:
        inner = m.group(1)
        inner = inner.replace('\\"', '"').replace("\\'", "'")
        return '"' + inner.replace('"', '\\"') + '"'

    text = re.sub(r"'((?:[^'\\]|\\.)*)'", _sq, text)
    # 无引号键补引号：{ 或 , 之后、: 之前的标识符
    text = re.sub(r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)', r'\1"\2"\3', text)
    # 去掉尾逗号
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    return text


def parse_json5(text: str) -> dict | list:
    """解析 JSON5 文本为 Python 对象；解析失败抛出 ValueError。"""
    cleaned = _strip_comments(text)
    strict = _to_strict_json(cleaned)
    try:
        return json.loads(strict)
    except json.JSONDecodeError as exc:
        raise ValueError(f'JSON5 解析失败: {exc}') from exc


# ============================================================
# 校验实现
# ============================================================

REQUIRED_FILES = [
    'build-profile.json5',
    'hvigorfile.ts',
    'hvigor/hvigor-config.json5',
    'oh-package.json5',
    'AppScope/app.json5',
    'AppScope/resources/base/element/string.json',
    'AppScope/resources/base/media/app_icon.png',
    'entry/build-profile.json5',
    'entry/hvigorfile.ts',
    'entry/oh-package.json5',
    'entry/obfuscation-rules.txt',
    'entry/src/main/module.json5',
    'entry/src/main/ets/common/AppConfig.ets',
    'entry/src/main/ets/entryability/EntryAbility.ets',
    'entry/src/main/ets/pages/Index.ets',
    'entry/src/main/resources/base/element/string.json',
    'entry/src/main/resources/base/element/color.json',
    'entry/src/main/resources/base/media/icon.png',
    'entry/src/main/resources/base/profile/main_pages.json',
]


def check_required_files(project: Path) -> list[str]:
    """校验必需文件是否存在。"""
    errors: list[str] = []
    for rel in REQUIRED_FILES:
        if not (project / rel).is_file():
            errors.append(f'缺少必需文件: {rel}')
    return errors


def check_app_json5(project: Path) -> list[str]:
    """校验 AppScope/app.json5 关键字段。"""
    errors: list[str] = []
    path = project / 'AppScope/app.json5'
    if not path.is_file():
        return errors  # 缺失已在 check_required_files 报出
    try:
        data = parse_json5(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        return [f'AppScope/app.json5 解析失败: {exc}']
    app = data.get('app', {}) if isinstance(data, dict) else {}
    for key in ('bundleName', 'versionCode', 'versionName', 'icon', 'label'):
        if not app.get(key):
            errors.append(f'AppScope/app.json5 缺少 app.{key}')
    if not app.get('bundleName'):
        pass
    elif not re.match(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$', str(app['bundleName'])):
        errors.append(f"bundleName 格式非法: {app['bundleName']}（应为反向域名，如 com.botler.app）")
    return errors


def check_module_json5(project: Path) -> list[str]:
    """校验 entry/src/main/module.json5：INTERNET 权限、EntryAbility、pages 路由。"""
    errors: list[str] = []
    path = project / 'entry/src/main/module.json5'
    if not path.is_file():
        return errors
    try:
        data = parse_json5(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        return [f'entry/src/main/module.json5 解析失败: {exc}']
    module = data.get('module', {}) if isinstance(data, dict) else {}
    perms = module.get('requestPermissions', []) or []
    perm_names = [p.get('name') for p in perms if isinstance(p, dict)]
    if 'ohos.permission.INTERNET' not in perm_names:
        errors.append('module.json5 缺少网络权限 ohos.permission.INTERNET（Web 套壳无法加载远程页面）')
    if module.get('mainElement') != 'EntryAbility':
        errors.append(f"module.json5 mainElement 应为 EntryAbility，实际: {module.get('mainElement')}")
    abilities = module.get('abilities', []) or []
    entry = next((a for a in abilities if isinstance(a, dict) and a.get('name') == 'EntryAbility'), None)
    if entry is None:
        errors.append('module.json5 缺少名为 EntryAbility 的 ability')
    else:
        src = entry.get('srcEntry')
        if src != './ets/entryability/EntryAbility.ets':
            errors.append(f'EntryAbility srcEntry 应为 ./ets/entryability/EntryAbility.ets，实际: {src}')
        if not (project / 'entry/src/main/ets/entryability/EntryAbility.ets').is_file():
            errors.append('EntryAbility.ets 文件不存在（srcEntry 引用断裂）')
    pages = module.get('pages')
    if pages != '$profile:main_pages':
        errors.append(f'module.json5 pages 应为 $profile:main_pages，实际: {pages}')
    return errors


def check_main_pages(project: Path) -> list[str]:
    """校验 main_pages.json 路由表包含 pages/Index。"""
    errors: list[str] = []
    path = project / 'entry/src/main/resources/base/profile/main_pages.json'
    if not path.is_file():
        return errors
    try:
        data = parse_json5(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        return [f'main_pages.json 解析失败: {exc}']
    src = data.get('src', []) if isinstance(data, dict) else []
    if 'pages/Index' not in src:
        errors.append(f'main_pages.json 路由表缺少 pages/Index，实际: {src}')
    return errors


def check_app_config(project: Path) -> list[str]:
    """校验 AppConfig.ets 定义了 http(s) 的 WEB_URL。"""
    errors: list[str] = []
    path = project / 'entry/src/main/ets/common/AppConfig.ets'
    if not path.is_file():
        return errors
    text = path.read_text(encoding='utf-8')
    match = re.search(r"WEB_URL\s*[:=][^'\"\n]*['\"]([^'\"]+)['\"]", text)
    if match is None:
        errors.append('AppConfig.ets 未找到 WEB_URL 常量定义')
        return errors
    url = match.group(1)
    if not url.startswith(('http://', 'https://')):
        errors.append(f'WEB_URL 应为 http(s) 地址，实际: {url}')
    return errors


def check_index_ets(project: Path) -> list[str]:
    """校验 Index.ets 使用 Web 组件并开启 javaScript/domStorage，引用 WEB_URL。"""
    errors: list[str] = []
    path = project / 'entry/src/main/ets/pages/Index.ets'
    if not path.is_file():
        return errors
    text = path.read_text(encoding='utf-8')
    if 'Web({' not in text:
        errors.append('Index.ets 未使用 Web 组件（缺少 Web({ ... }) 调用）')
    for attr in ('.javaScriptAccess(', '.domStorageAccess('):
        if attr not in text:
            errors.append(f'Index.ets 缺少 {attr} 配置（Web 套壳基础能力）')
    if 'WEB_URL' not in text:
        errors.append('Index.ets 未引用 WEB_URL（应加载 common/AppConfig.ets 中的地址）')
    return errors


def check_root_build_profile(project: Path) -> list[str]:
    """校验根 build-profile.json5：product 的 SDK 版本与 entry 模块。"""
    errors: list[str] = []
    path = project / 'build-profile.json5'
    if not path.is_file():
        return errors
    try:
        data = parse_json5(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        return [f'build-profile.json5 解析失败: {exc}']
    app = data.get('app', {}) if isinstance(data, dict) else {}
    products = app.get('products', []) or []
    if not products:
        errors.append('build-profile.json5 未配置 products')
    for product in products:
        if not isinstance(product, dict):
            continue
        if not product.get('targetSdkVersion'):
            errors.append('build-profile.json5 product 缺少 targetSdkVersion')
        if not product.get('compatibleSdkVersion'):
            errors.append('build-profile.json5 product 缺少 compatibleSdkVersion')
    modules = data.get('modules', []) if isinstance(data, dict) else []
    module_names = [m.get('name') for m in modules if isinstance(m, dict)]
    if 'entry' not in module_names:
        errors.append(f"build-profile.json5 modules 缺少 entry，实际: {module_names}")
    return errors


def check_hvigor_config(project: Path) -> list[str]:
    """校验 hvigor/hvigor-config.json5 声明了 modelVersion。"""
    errors: list[str] = []
    path = project / 'hvigor/hvigor-config.json5'
    if not path.is_file():
        return errors
    try:
        data = parse_json5(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        return [f'hvigor/hvigor-config.json5 解析失败: {exc}']
    if not isinstance(data, dict) or not data.get('modelVersion'):
        errors.append('hvigor/hvigor-config.json5 未声明 modelVersion')
    return errors


def validate_project(project: Path) -> list[str]:
    """对 harmony 工程目录执行全部结构校验，返回错误列表（空 = 通过）。"""
    project = Path(project)
    errors: list[str] = []
    for check in (
        check_required_files,
        check_app_json5,
        check_module_json5,
        check_main_pages,
        check_app_config,
        check_index_ets,
        check_root_build_profile,
        check_hvigor_config,
    ):
        errors.extend(check(project))
    return errors


# ============================================================
# CLI 入口
# ============================================================

def main(argv: list[str]) -> int:
    project_dir = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    print(f'===== Botler 鸿蒙端工程结构校验: {project_dir} =====')
    errors = validate_project(project_dir)
    if errors:
        print(f'❌ 校验未通过，共 {len(errors)} 项错误：')
        for err in errors:
            print(f'   - {err}')
        return 1
    print('✅ 校验全部通过（必需文件 / app.json5 / module.json5 / 路由表 /')
    print('   AppConfig / Index.ets / build-profile / hvigor-config）')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
