#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 pip-audit / npm audit 的 JSON 输出转换为 GitLab 依赖扫描报告格式。

由 CI 的 security:deps-python / security:deps-frontend job 调用，转换产物经
artifacts:reports:dependency_scanning 上传 GitLab，可在
项目 → 安全 → 漏洞报告（依赖扫描）页面可视化查看（issue #86）。

用法：
    python3 convert-audit-to-gitlab.py pip <审计JSON> <输出报告.json>
    python3 convert-audit-to-gitlab.py npm <审计JSON> <输出报告.json>

退出码（调用方据此决定阻断/重试/放行）：
    0 = 转换成功，且无高危/中危漏洞（低危/Info 仅记录进报告，不阻断）
    1 = 转换成功，且存在高危/中危漏洞（流水线据此阻断）
    2 = 输入 JSON 无效（审计服务不可用/输出为空），调用方可切换
        备用数据源重试，重试仍失败时按基础设施故障处理

阻断策略（高危/中危漏洞中止流水线，issue #86 需求）：
    - npm audit 输出自带 severity（info/low/moderate/high/critical），
      按 GitLab 枚举归一化后 moderate 及以上（Medium/High/Critical）阻断；
    - pip-audit 输出不含 severity 字段（PyPI/OSV 数据源均不提供），
      其报告的均为已公开的已知漏洞（PYSEC/GHSA/CVE 编号），故采取
      保守策略：任何已公开 CVE 一律按中危处理并阻断，避免「无法定级」
      变成「静默放行」。可人工评估后通过环境变量豁免（见下）。

豁免机制（CI/CD 变量，逗号分隔）：
    - PIP_AUDIT_IGNORE：按漏洞 ID 精确豁免（如 PYSEC-2024-38,GHSA-xxxx）
    - NPM_AUDIT_IGNORE：按包名豁免该包的全部漏洞（如 react,esbuild）
"""

import json
import sys
from datetime import datetime, timezone

# GitLab 依赖扫描报告的合法 severity 枚举（大小写敏感）
GITLAB_SEVERITIES = ("Critical", "High", "Medium", "Low", "Info", "Unknown")

# 阻断流水线的级别：高危（Critical/High）+ 中危（Medium）
BLOCKING_SEVERITIES = ("Critical", "High", "Medium")

# 各审计工具输出的 severity 字符串 → GitLab 枚举归一化
SEVERITY_MAP = {
    # npm audit
    "critical": "Critical",
    "high": "High",
    "moderate": "Medium",
    "medium": "Medium",
    "low": "Low",
    "info": "Info",
    # bandit 风格（防御性兼容，实际输入为 npm/pip 格式）
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
    "UNDEFINED": "Unknown",
}


def _norm_severity(raw):
    """归一化 severity 字符串到 GitLab 合法枚举，未知值返回 Unknown。"""
    if not raw:
        return "Unknown"
    sev = SEVERITY_MAP.get(raw, SEVERITY_MAP.get(raw.strip().upper(), "Unknown"))
    return sev if sev in GITLAB_SEVERITIES else "Unknown"


def _identifier_type(vuln_id):
    """按编号前缀区分标识符类型（ghsa / cve），其余归为 other。"""
    if not vuln_id:
        return "other"
    if vuln_id.startswith("GHSA-"):
        return "ghsa"
    if vuln_id.startswith("CVE-"):
        return "cve"
    return "other"


def _now_iso():
    """当前 UTC 时间（ISO 8601，GitLab 报告要求的时间格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _emit_scan_block(tool_id, tool_url, version):
    """生成 GitLab 报告头部的 scan 块。"""
    return {
        "analyzer": {
            "id": tool_id,
            "name": tool_id,
            "version": version,
            "vendor": {"name": "Botler CI"},
        },
        "scanner": {
            "id": tool_id,
            "name": tool_id,
            "url": tool_url,
            "version": version,
            "vendor": {"name": "Botler CI"},
        },
        "type": "dependency_scanning",
        "start_time": _now_iso(),
        "end_time": _now_iso(),
        "status": "success",
    }


def parse_pip(data):
    """解析 pip-audit JSON，返回 (vulnerabilities, dependencies)。

    pip-audit 输出的每个 vuln 不含 severity 字段，统一标 Unknown
    （GitLab 页面如实展示），阻断判定由调用方按保守策略处理。
    """
    if not isinstance(data, dict) or "dependencies" not in data:
        return None  # 审计服务不可用（输出缺少关键字段）

    vulnerabilities = []
    dependencies = []
    ignore_ids = {
        x.strip() for x in (__import__("os").environ.get("PIP_AUDIT_IGNORE", "") or "").split(",") if x.strip()
    }

    for dep in data.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        name = dep.get("name", "?")
        version = dep.get("version", "unknown")
        dependencies.append({"name": name, "version": version})
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            vuln_id = vuln.get("id") or "unknown"
            if vuln_id in ignore_ids:
                continue
            fix_versions = vuln.get("fix_versions") or []
            solution = f"升级到修复版本 {fix_versions}" if fix_versions else "暂无修复版本"
            vulnerabilities.append(
                {
                    "id": vuln_id,
                    "name": vuln_id,
                    "description": vuln.get("description", ""),
                    "severity": "Unknown",
                    "solution": solution,
                    "identifiers": [
                        {
                            "type": _identifier_type(vuln_id),
                            "name": vuln_id,
                            "value": vuln_id,
                            "url": f"https://github.com/advisories/{vuln_id}" if vuln_id.startswith("GHSA-") else "",
                        }
                    ],
                    "links": [],
                    "location": {
                        "file": "backend/requirements.txt",
                        "dependency": {
                            "package": {"name": name},
                            "version": version,
                        },
                        "operating_system": "unknown",
                    },
                }
            )
    return vulnerabilities, dependencies


def parse_npm(data):
    """解析 npm audit JSON，返回 (vulnerabilities, dependencies)。

    npm audit 输出的 severity 取自 via 条目（info/low/moderate/high/
    critical），归一化后 moderate 及以上阻断。via 中的字符串条目是
    传递依赖引用（非漏洞详情），跳过。
    """
    if not isinstance(data, dict) or "vulnerabilities" not in data:
        return None  # audit 端点不可用（输出缺少关键字段）

    vulnerabilities = []
    dependencies = []
    ignore_pkgs = {
        x.strip() for x in (__import__("os").environ.get("NPM_AUDIT_IGNORE", "") or "").split(",") if x.strip()
    }

    for pkg_name, info in (data.get("vulnerabilities") or {}).items():
        if not isinstance(info, dict):
            continue
        if pkg_name in ignore_pkgs:
            continue
        severity = _norm_severity(info.get("severity"))
        # 漏洞详情（via 数组中的 dict 条目，str 条目为传递依赖引用）
        via_details = [v for v in (info.get("via") or []) if isinstance(v, dict)]
        # 描述：优先取 via 中 title 最全的一条；CWE 拼接为补充信息
        best = max(via_details, key=lambda v: len(v.get("title") or ""), default=None)
        description = ""
        if best:
            title = best.get("title") or ""
            cwes = best.get("cwe") or []
            description = f"{title}（CWE: {', '.join(cwes)}）" if title and cwes else title
        # 漏洞编号：via 中 GHSA-/CVE- 前缀的 id 与 url 提取
        ids = []
        for v in via_details:
            vuln_id = v.get("url", "").rsplit("/", 1)[-1] or v.get("name", "")
            if vuln_id and (vuln_id.startswith("GHSA-") or vuln_id.startswith("CVE-")):
                ids.append(vuln_id)
        vuln_id = ids[0] if ids else f"npm-{pkg_name}"
        identifiers = [
            {
                "type": _identifier_type(i),
                "name": i,
                "value": i,
                "url": f"https://github.com/advisories/{i}" if i.startswith("GHSA-") else "",
            }
            for i in ids[:5]
        ] or [
            {"type": "other", "name": vuln_id, "value": vuln_id, "url": ""}
        ]
        # 受影响版本范围（npm audit 不直接给已装版本，用 range 表达）
        version_range = info.get("range") or best.get("range") if best else info.get("range")
        fix = info.get("fixAvailable")
        solution = "升级依赖或关注官方公告"
        if isinstance(fix, dict):
            solution = "npm audit fix 可自动修复（详见官方公告）"
        elif fix is False:
            solution = "暂无修复版本，关注官方公告"
        vulnerabilities.append(
            {
                "id": vuln_id,
                "name": pkg_name,
                "description": description,
                "severity": severity,
                "solution": solution,
                "identifiers": identifiers,
                "links": [{"url": v.get("url")} for v in via_details if v.get("url")],
                "location": {
                    "file": "frontend/package-lock.json",
                    "dependency": {
                        "package": {"name": pkg_name},
                        "version": version_range or "unknown",
                    },
                    "operating_system": "unknown",
                },
            }
        )
        dependencies.append({"name": pkg_name, "version": version_range or "unknown"})
    return vulnerabilities, dependencies


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in ("pip", "npm"):
        print("用法: convert-audit-to-gitlab.py <pip|npm> <审计JSON> <输出报告.json>", file=sys.stderr)
        return 2

    tool = sys.argv[1]
    audit_path, report_path = sys.argv[2], sys.argv[3]

    # 1. 读取审计 JSON（空文件/无效 JSON → 审计服务不可用）
    try:
        with open(audit_path, encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            raise ValueError("审计输出为空")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        print(f"✗ 无法解析审计输出 {audit_path}（JSON 无效或为空，可能审计服务不可用）: {exc}", file=sys.stderr)
        return 2

    # 2. 按工具解析（缺关键字段 → exit 2，调用方切换备用数据源重试）
    if tool == "pip":
        parsed = parse_pip(data)
        if parsed is None:
            print("✗ pip-audit 输出缺少 dependencies 字段（可能审计服务不可用）", file=sys.stderr)
            return 2
        tool_id, tool_url, dep_file = "pip-audit", "https://pypi.org/project/pip-audit/", "backend/requirements.txt"
    else:
        parsed = parse_npm(data)
        if parsed is None:
            print("✗ npm audit 输出缺少 vulnerabilities 字段（可能 audit 端点不可用）", file=sys.stderr)
            return 2
        tool_id, tool_url, dep_file = "npm-audit", "https://docs.npmjs.com/cli/commands/npm-audit", "frontend/package-lock.json"
    vulnerabilities, dependencies = parsed

    # 3. 阻断统计：pip 模式任何已知漏洞保守按中危阻断（无 severity 信息）
    blocking = 0
    for v in vulnerabilities:
        sev = v["severity"]
        if sev in BLOCKING_SEVERITIES or (tool == "pip" and sev == "Unknown"):
            blocking += 1

    # 4. 组装 GitLab 依赖扫描报告（15.0.0 schema）
    report = {
        "version": "15.0.0",
        "scan": _emit_scan_block(tool_id, tool_url, "latest"),
        "vulnerabilities": vulnerabilities,
        "dependency_files": [
            {
                "path": dep_file,
                "package_manager": "pip" if tool == "pip" else "npm",
                "dependencies": dependencies,
            }
        ],
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 5. 输出摘要并返回退出码
    if not vulnerabilities:
        print("✓ 未发现依赖漏洞")
        return 0
    total = len(vulnerabilities)
    print(f"发现 {total} 个依赖漏洞（其中高危/中危 {blocking} 个阻断级）")
    for v in vulnerabilities:
        pkg = v["location"]["dependency"]["package"]["name"]
        print(f"  [{v['severity']}] {v['id']} ({pkg})")
    print(f"✓ GitLab 依赖扫描报告已生成：{report_path}")
    return 1 if blocking > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
