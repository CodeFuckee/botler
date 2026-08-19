"""报告解析器（issue #337：概览页流水线详情查看静态分析与测试报告）。

把 CI 上传的三种报告原始文件解析为统一结构，供后端 /api/pipelines/
{repo_id}/report 接口返回、前端流水线详情抽屉直接渲染：

- SARIF 2.1.0（file_type=sast）：bandit / semgrep / gitleaks 输出
  → {kind:'sast', tool, summary:{total, by_severity}, results:[{rule,
    severity, message, file, line, column}]}
- GitLab 依赖扫描 JSON（file_type=dependency_scanning）：deps-python /
  deps-frontend 转换产物（ci/convert-audit-to-gitlab.py）
  → {kind:'deps', summary, results:[{id, name, severity, package,
    version, file, solution, identifiers}]}
- JUnit XML（file_type=junit）：pytest --junitxml / node:test junit 输出
  → {kind:'test', summary:{tests, failures, errors, skipped, time},
    results:[{name, classname, status, time, message}]}

约定：
- 纯函数、只依赖标准库，便于单测；不依赖网络。
- 输入不是对应格式时抛 ValueError（API 层转为 502），字段缺失逐项
  兜底为 None / 空串 / 0，绝不崩溃。
- SARIF level → 中文语义严重级别归一化：error→high、warning→medium、
  note→low、none→info、缺失/未知→unknown（bandit 用 error/warning 表达
  中高危，符合 GitLab 安全页语义）。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

# SARIF level → 归一化严重级别
_SAST_LEVEL_SEVERITY = {
    "error": "high",
    "warning": "medium",
    "note": "low",
    "none": "info",
}

# GitLab 依赖扫描报告的合法 severity 枚举（与 ci/convert-audit-to-gitlab.py 一致）
_DEPS_SEVERITIES = ("Critical", "High", "Medium", "Low", "Info", "Unknown")


def _norm_sast_severity(level) -> str:
    """SARIF level → 严重级别；空/未知 → unknown。"""
    if not level:
        return "unknown"
    return _SAST_LEVEL_SEVERITY.get(str(level).lower(), "unknown")


def _sast_message(result: dict) -> str:
    """SARIF result.message：dict（{text}）或字符串两种形态兼容。"""
    msg = result.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        text = msg.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _first_location(result: dict) -> dict | None:
    """取 result 第一个物理位置：{file, line, column}；缺字段逐项兜底。"""
    locations = result.get("locations")
    if not isinstance(locations, list):
        return None
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        physical = loc.get("physicalLocation")
        if not isinstance(physical, dict):
            continue
        artifact = physical.get("artifactLocation")
        region = physical.get("region")
        return {
            "file": artifact.get("uri") if isinstance(artifact, dict) else None,
            "line": region.get("startLine") if isinstance(region, dict) else None,
            "column": region.get("startColumn") if isinstance(region, dict) else None,
        }
    return None


def parse_sast_sarif(content: str) -> dict:
    """解析 SARIF 2.1.0 JSON → {kind, tool, summary, results}。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("SARIF 报告不是有效 JSON")
    if not isinstance(data, dict):
        raise ValueError("SARIF 报告结构异常（应为对象）")

    by_severity = {"high": 0, "medium": 0, "low": 0, "info": 0, "unknown": 0}
    results: list[dict] = []
    tool = None
    runs = data.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            tool_obj = run.get("tool")
            if isinstance(tool_obj, dict):
                driver = tool_obj.get("driver")
                if isinstance(driver, dict) and not tool:
                    tool = driver.get("name")
            for res in run.get("results") or []:
                if not isinstance(res, dict):
                    continue
                severity = _norm_sast_severity(res.get("level"))
                by_severity[severity] += 1
                loc = _first_location(res)
                results.append({
                    "rule": res.get("ruleId"),
                    "severity": severity,
                    "message": _sast_message(res),
                    "file": loc["file"] if loc else None,
                    "line": loc["line"] if loc else None,
                    "column": loc["column"] if loc else None,
                })
    return {
        "kind": "sast",
        "tool": tool,
        "summary": {"total": len(results), "by_severity": by_severity},
        "results": results,
    }


def parse_dependency_scanning(content: str) -> dict:
    """解析 GitLab 依赖扫描 JSON → {kind, summary, results}。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("依赖扫描报告不是有效 JSON")
    if not isinstance(data, dict):
        raise ValueError("依赖扫描报告结构异常（应为对象）")

    by_severity = {s: 0 for s in _DEPS_SEVERITIES}
    results: list[dict] = []
    vulns = data.get("vulnerabilities")
    if isinstance(vulns, list):
        for v in vulns:
            if not isinstance(v, dict):
                continue
            severity = v.get("severity") or "Unknown"
            if severity not in by_severity:
                severity = "Unknown"
            by_severity[severity] += 1
            location = v.get("location")
            if not isinstance(location, dict):
                location = {}
            dep = location.get("dependency")
            if not isinstance(dep, dict):
                dep = {}
            pkg = dep.get("package")
            if not isinstance(pkg, dict):
                pkg = {}
            identifiers: list[dict] = []
            for ident in v.get("identifiers") or []:
                if isinstance(ident, dict):
                    identifiers.append({
                        "type": ident.get("type"),
                        "name": ident.get("name"),
                        "url": ident.get("url"),
                    })
            results.append({
                "id": v.get("id"),
                "name": v.get("name") or "",
                "severity": severity,
                "package": pkg.get("name"),
                "version": dep.get("version"),
                "file": location.get("file"),
                "solution": v.get("solution") or "",
                "identifiers": identifiers,
            })
    return {
        "kind": "deps",
        "summary": {"total": len(results), "by_severity": by_severity},
        "results": results,
    }


def _int_attr(attrs: dict[str, str], key: str) -> int:
    """testsuite 计数属性 → int，缺失/非法为 0。"""
    try:
        return int(float(attrs.get(key, "0")))
    except (TypeError, ValueError):
        return 0


def _float_attr(value: str | None) -> float | None:
    """time 属性 → float，缺失/非法为 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_junit_xml(content: str) -> dict:
    """解析 JUnit XML → {kind, summary, results}。兼容 <testsuites> 与单 <testsuite> 根。"""
    if not content or not content.strip():
        raise ValueError("JUnit 报告为空")
    try:
        # nosec B314：JUnit 报告来自自家 CI 上传的产物（pytest/node:test/
        # playwright 生成），非外部不可信输入；ET.fromstring 不解析外部实体
        # （不联网，无 XXE 外带风险），内部实体膨胀风险由 GitLab 产物大小
        # 限制兜底。与项目既有 B608/B310 的 nosec 模式一致（issue #337）
        root = ET.fromstring(content)  # nosec B314
    except ET.ParseError:
        raise ValueError("JUnit 报告不是有效 XML")
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError("JUnit 报告根节点异常")

    if root.tag == "testsuite":
        suites = [root]
        direct_cases: list = []
    else:
        # <testsuites>：优先取 testsuite 子节点；node --test 的 junit
        # reporter 不输出 testsuite 包装、testcase 直接挂在 testsuites 下
        # （实测 node v22 junit.xml），此时按直接 testcase 汇总
        suites = root.findall("testsuite") or []
        direct_cases = root.findall("testcase") or []

    total = failures = errors = skipped = 0
    time_sum = 0.0
    has_time = False
    results: list[dict] = []
    for suite in suites:
        # tests 属性缺失（极简 JUnit 输出）时回退统计 testcase 数量
        total += _int_attr(suite.attrib, "tests") or len(suite.findall("testcase") or [])
        failures += _int_attr(suite.attrib, "failures")
        errors += _int_attr(suite.attrib, "errors")
        skipped += _int_attr(suite.attrib, "skipped")
        t = _float_attr(suite.attrib.get("time"))
        if t is not None:
            has_time = True
            time_sum += t
    if not suites and direct_cases:
        # node --test junit reporter：无 testsuite 汇总属性，按用例计数
        total = len(direct_cases)
        failures = sum(1 for tc in direct_cases if tc.find("failure") is not None)
        errors = sum(1 for tc in direct_cases if tc.find("error") is not None)
        skipped = sum(1 for tc in direct_cases if tc.find("skipped") is not None)
        has_time = any(_float_attr(tc.get("time")) is not None for tc in direct_cases)
        time_sum = sum(_float_attr(tc.get("time")) or 0.0 for tc in direct_cases)

    for tc in (direct_cases if not suites else
               [c for suite in suites for c in suite.findall("testcase") or []]):
        status = "passed"
        message = ""
        fail = tc.find("failure")
        err = tc.find("error")
        skip = tc.find("skipped")
        if fail is not None:
            status = "failed"
            message = (fail.get("message") or "").strip() or (fail.text or "").strip()
        elif err is not None:
            status = "error"
            message = (err.get("message") or "").strip() or (err.text or "").strip()
        elif skip is not None:
            status = "skipped"
            message = (skip.get("message") or "").strip()
        results.append({
            "name": tc.get("name"),
            "classname": tc.get("classname"),
            "status": status,
            "time": _float_attr(tc.get("time")),
            "message": message,
        })
    return {
        "kind": "test",
        "summary": {
            "tests": total,
            "failures": failures,
            "errors": errors,
            "skipped": skipped,
            "time": round(time_sum, 2) if has_time else None,
        },
        "results": results,
    }


def parse_report(file_type: str, content: str) -> dict:
    """按 GitLab 产物 file_type 分派解析；未知类型抛 ValueError。"""
    if file_type == "sast":
        return parse_sast_sarif(content)
    if file_type == "dependency_scanning":
        return parse_dependency_scanning(content)
    if file_type == "junit":
        return parse_junit_xml(content)
    raise ValueError(f"不支持的报告类型：{file_type or '空'}")
