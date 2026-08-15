# -*- coding: utf-8 -*-
"""ci/convert-audit-to-gitlab.py 的单元测试（issue #86）。

覆盖：pip/npm 正常转换、阻断判定（高危/中危中止）、豁免机制、
服务不可用（exit 2）与无效输入边界。由 CI 的 security:deps-python
job 在每次流水线中执行，防止转换脚本回归。

运行方式：
    python3 -m pytest ci/test_convert_audit_to_gitlab.py -v
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 脚本与测试文件同目录
SCRIPT = Path(__file__).resolve().parent / "convert-audit-to-gitlab.py"


def run_convert(tool, audit_json, tmp_path, env_extra=None):
    """执行转换脚本，返回 (exit_code, stdout, 报告dict或None)。"""
    audit_file = tmp_path / f"{tool}-audit.json"
    report_file = tmp_path / f"{tool}-report.json"
    audit_file.write_text(json.dumps(audit_json), encoding="utf-8")
    env = dict(os.environ)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), tool, str(audit_file), str(report_file)],
        capture_output=True, text=True, env=env,
    )
    report = None
    if report_file.exists():
        report = json.loads(report_file.read_text(encoding="utf-8"))
    return proc.returncode, proc.stdout, report


# ----------------------------------------------------------
# pip 模式（pip-audit 输出样例）
# ----------------------------------------------------------

PIP_AUDIT_SAMPLE = {
    "dependencies": [
        {
            "name": "fastapi",
            "version": "0.111.0",
            "vulns": [
                {
                    "id": "PYSEC-2024-38",
                    "fix_versions": ["0.111.1"],
                    "description": "FastAPI multipart 边界解析拒绝服务漏洞",
                }
            ],
        },
        {"name": "pytest", "version": "8.2.0", "vulns": []},
    ]
}


def test_pip_with_vuln_blocks_pipeline(tmp_path):
    """pip 模式：任何已知漏洞保守按中危阻断（exit 1）。"""
    code, out, report = run_convert("pip", PIP_AUDIT_SAMPLE, tmp_path)
    assert code == 1, out
    assert report["version"] == "15.0.0"
    assert report["scan"]["type"] == "dependency_scanning"
    vulns = report["vulnerabilities"]
    assert len(vulns) == 1
    v = vulns[0]
    assert v["id"] == "PYSEC-2024-38"
    # pip-audit 输出不含 severity → GitLab 页面如实显示 Unknown
    assert v["severity"] == "Unknown"
    assert v["location"]["file"] == "backend/requirements.txt"
    assert v["location"]["dependency"]["package"]["name"] == "fastapi"
    assert "0.111.1" in v["solution"]
    # 依赖清单含无漏洞依赖
    deps = report["dependency_files"][0]["dependencies"]
    assert {d["name"] for d in deps} == {"fastapi", "pytest"}


def test_pip_ignore_by_env(tmp_path):
    """pip 模式：PIP_AUDIT_IGNORE 按漏洞 ID 豁免后放行（exit 0）。"""
    code, _, report = run_convert(
        "pip", PIP_AUDIT_SAMPLE, tmp_path,
        env_extra={"PIP_AUDIT_IGNORE": "PYSEC-2024-38"},
    )
    assert code == 0
    assert report["vulnerabilities"] == []


def test_pip_clean_no_vulns(tmp_path):
    """pip 模式：无漏洞时 exit 0 且报告漏洞列表为空。"""
    clean = {"dependencies": [{"name": "pytest", "version": "8.2.0", "vulns": []}]}
    code, out, report = run_convert("pip", clean, tmp_path)
    assert code == 0, out
    assert "未发现依赖漏洞" in out
    assert report["vulnerabilities"] == []


def test_pip_ghsa_identifier(tmp_path):
    """pip 模式：GHSA- 前缀识别为 ghsa 标识符并带 advisory 链接。"""
    sample = {
        "dependencies": [
            {
                "name": "starlette",
                "version": "0.37.0",
                "vulns": [
                    {"id": "GHSA-93gm-qmq6-w238", "fix_versions": ["0.38.0"], "description": "x"}
                ],
            }
        ]
    }
    code, _, report = run_convert("pip", sample, tmp_path)
    assert code == 1
    ident = report["vulnerabilities"][0]["identifiers"][0]
    assert ident["type"] == "ghsa"
    assert "advisories/GHSA-93gm-qmq6-w238" in ident["url"]


# ----------------------------------------------------------
# npm 模式（npm audit --json 输出样例）
# ----------------------------------------------------------

NPM_AUDIT_SAMPLE = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        # 高危：直接依赖，via 带漏洞详情
        "react": {
            "name": "react",
            "severity": "high",
            "isDirect": True,
            "via": [
                {
                    "source": 1099999,
                    "name": "react",
                    "dependency": "react",
                    "title": "React 原型污染漏洞",
                    "url": "https://github.com/advisories/GHSA-abcd-efgh-ijkl",
                    "severity": "high",
                    "cwe": ["CWE-915"],
                    "range": "<18.3.1",
                }
            ],
            "effects": [],
            "range": "<18.3.1",
            "nodes": [""],
            "fixAvailable": {"name": "react", "version": "18.3.1", "isSemVerMajor": False},
        },
        # 中危：moderate → 阻断
        "vite": {
            "name": "vite",
            "severity": "moderate",
            "isDirect": True,
            "via": [
                {
                    "source": 1099900,
                    "name": "vite",
                    "dependency": "vite",
                    "title": "Vite dev server 任意文件读取",
                    "url": "https://github.com/advisories/GHSA-qqqq-wwww-eeee",
                    "severity": "moderate",
                    "cwe": ["CWE-200"],
                    "range": "<=5.4.0",
                }
            ],
            "effects": [],
            "range": "<=5.4.0",
            "nodes": [""],
            "fixAvailable": {"name": "vite", "version": "5.4.1", "isSemVerMajor": False},
        },
        # 低危：low → 不阻断
        "esbuild": {
            "name": "esbuild",
            "severity": "low",
            "isDirect": True,
            "via": [
                {
                    "source": 1099800,
                    "name": "esbuild",
                    "dependency": "esbuild",
                    "title": "esbuild 开发服务器 CORS 配置缺陷",
                    "url": "https://github.com/advisories/GHSA-67mh-4wv8-2f99",
                    "severity": "low",
                    "cwe": ["CWE-346"],
                    "range": "<=0.24.0",
                }
            ],
            "effects": [],
            "range": "<=0.24.0",
            "nodes": [""],
            "fixAvailable": False,
        },
        # 传递依赖：via 中混有字符串引用（npm audit 的传递链表示）
        "string_decoder": {
            "name": "string_decoder",
            "severity": "critical",
            "isDirect": False,
            "via": ["safer-buffer", "string_decoder@0.10.31"],
            "effects": [],
            "range": "0.10.31",
            "nodes": ["safer-buffer>string_decoder"],
            "fixAvailable": False,
        },
    },
}


def test_npm_blocking_severity_blocks(tmp_path):
    """npm 模式：critical/high/moderate 阻断（exit 1），low 不阻断但记录。"""
    code, out, report = run_convert("npm", NPM_AUDIT_SAMPLE, tmp_path)
    assert code == 1, out
    vulns = report["vulnerabilities"]
    # 4 个漏洞全部记录（含 low 与传递依赖）
    assert len(vulns) == 4
    by_pkg = {v["location"]["dependency"]["package"]["name"]: v for v in vulns}
    assert by_pkg["react"]["severity"] == "High"
    assert by_pkg["vite"]["severity"] == "Medium"
    assert by_pkg["esbuild"]["severity"] == "Low"
    # 传递依赖 via 为字符串时 severity 取自条目本身（critical）
    assert by_pkg["string_decoder"]["severity"] == "Critical"
    # 报告元信息
    assert report["scan"]["analyzer"]["id"] == "npm-audit"
    assert report["dependency_files"][0]["package_manager"] == "npm"
    assert report["dependency_files"][0]["path"] == "frontend/package-lock.json"


def test_npm_low_only_does_not_block(tmp_path):
    """npm 模式：仅低危漏洞 → exit 0（记录但不阻断）。"""
    sample = {"vulnerabilities": {k: v for k, v in NPM_AUDIT_SAMPLE["vulnerabilities"].items() if k == "esbuild"}}
    code, out, report = run_convert("npm", sample, tmp_path)
    assert code == 0, out
    assert len(report["vulnerabilities"]) == 1


def test_npm_ignore_by_package(tmp_path):
    """npm 模式：NPM_AUDIT_IGNORE 按包名豁免该包全部漏洞。"""
    code, _, report = run_convert(
        "npm", NPM_AUDIT_SAMPLE, tmp_path,
        env_extra={"NPM_AUDIT_IGNORE": "react,vite,string_decoder"},
    )
    assert code == 0
    remaining = {v["location"]["dependency"]["package"]["name"] for v in report["vulnerabilities"]}
    assert remaining == {"esbuild"}


def test_npm_no_vulns(tmp_path):
    """npm 模式：vulnerabilities 为空 dict → exit 0。"""
    code, out, report = run_convert("npm", {"vulnerabilities": {}}, tmp_path)
    assert code == 0, out
    assert report["vulnerabilities"] == []


# ----------------------------------------------------------
# 无效输入 / 服务不可用（exit 2）
# ----------------------------------------------------------

def test_invalid_json_exits_2(tmp_path):
    """无效 JSON（服务不可用）→ exit 2。"""
    audit_file = tmp_path / "bad.json"
    report_file = tmp_path / "r.json"
    audit_file.write_text("{not-valid-json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "pip", str(audit_file), str(report_file)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "无法解析" in proc.stderr


def test_empty_audit_file_exits_2(tmp_path):
    """空审计输出 → exit 2。"""
    audit_file = tmp_path / "empty.json"
    report_file = tmp_path / "r.json"
    audit_file.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "npm", str(audit_file), str(report_file)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def test_pip_missing_dependencies_field_exits_2(tmp_path):
    """pip 输出缺 dependencies 字段（审计服务不可用）→ exit 2。"""
    code, _, report = run_convert("pip", {"foo": "bar"}, tmp_path)
    assert code == 2
    assert report is None


def test_npm_missing_vulnerabilities_field_exits_2(tmp_path):
    """npm 输出缺 vulnerabilities 字段（audit 端点不可用）→ exit 2。"""
    code, _, report = run_convert("npm", {"error": "registry down"}, tmp_path)
    assert code == 2
    assert report is None


def test_usage_error_exits_2(tmp_path):
    """参数不合法 → exit 2。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "cargo", "a.json", "b.json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "用法" in proc.stderr


def test_severity_normalization_edge_cases():
    """severity 归一化边界：None/未知值/大小写 → Unknown 或合法枚举。"""
    # 从文件路径直接加载模块（ci/ 不是包，避免 sys.path 副作用）
    spec = importlib.util.spec_from_file_location("convert_audit_to_gitlab", SCRIPT)
    conv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)

    assert conv._norm_severity(None) == "Unknown"
    assert conv._norm_severity("") == "Unknown"
    assert conv._norm_severity("weird-value") == "Unknown"
    assert conv._norm_severity("critical") == "Critical"
    assert conv._norm_severity("moderate") == "Medium"
    assert conv._norm_severity("MEDIUM") == "Medium"
    assert conv._identifier_type("GHSA-1234-abcd") == "ghsa"
    assert conv._identifier_type("CVE-2024-1234") == "cve"
    assert conv._identifier_type("PYSEC-2024-1") == "other"
