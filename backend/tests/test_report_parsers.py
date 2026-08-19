"""报告解析器单元测试（issue #337：概览页流水线详情查看静态分析与测试报告）。

parse_sast_sarif / parse_dependency_scanning / parse_junit_xml / parse_report：
把 CI 上传的 SARIF（bandit/semgrep/gitleaks）、GitLab 依赖扫描 JSON
（deps-python/deps-frontend）与 JUnit XML（pytest/node:test）解析为
统一的 {kind, summary, results} 结构，供前端流水线详情抽屉直接渲染。

解析器为纯函数（只依赖标准库），输入异常一律抛 ValueError，
由 API 层转换为 502 响应；字段缺失逐项兜底不崩溃。
"""

import pytest

from botler.report_parsers import (
    parse_sast_sarif,
    parse_dependency_scanning,
    parse_junit_xml,
    parse_report,
)

# ---- SARIF 样本（bandit-sarif-formatter / semgrep / gitleaks 通用结构） ----

BANDIT_SARIF = """{
  "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
  "version": "2.1.0",
  "runs": [
    {
      "tool": {"driver": {"name": "Bandit", "rules": []}},
      "results": [
        {
          "ruleId": "B101",
          "level": "error",
          "message": {"text": "Use of assert detected."},
          "locations": [{
            "physicalLocation": {
              "artifactLocation": {"uri": "botler/api/pipelines.py"},
              "region": {"startLine": 42, "startColumn": 8}
            }
          }]
        },
        {
          "ruleId": "B608",
          "level": "warning",
          "message": {"text": "Possible SQL injection."},
          "locations": [{
            "physicalLocation": {
              "artifactLocation": {"uri": "botler/db.py"},
              "region": {"startLine": 7}
            }
          }]
        },
        {
          "ruleId": "B105",
          "level": "note",
          "message": {"text": "Possible hardcoded password."},
          "locations": [{
            "physicalLocation": {
              "artifactLocation": {"uri": "botler/config.py"},
              "region": {"startLine": 3, "startColumn": 1}
            }
          }]
        }
      ]
    }
  ]
}"""

# ---- 依赖扫描 JSON 样本（ci/convert-audit-to-gitlab.py 转换产物结构） ----

DEPS_REPORT = """{
  "version": "15.0.0",
  "vulnerabilities": [
    {
      "id": "CVE-2023-1234",
      "name": "requests",
      "description": "ReDoS 漏洞描述",
      "severity": "High",
      "solution": "升级到修复版本 ['2.31.0']",
      "identifiers": [{"type": "cve", "name": "CVE-2023-1234",
                       "value": "CVE-2023-1234", "url": ""}],
      "links": [],
      "location": {
        "file": "backend/requirements.txt",
        "dependency": {"package": {"name": "requests"}, "version": "2.28.1"},
        "operating_system": "unknown"
      }
    },
    {
      "id": "GHSA-xxxx",
      "name": "react",
      "description": "XSS 漏洞描述",
      "severity": "Medium",
      "solution": "暂无修复版本",
      "identifiers": [{"type": "ghsa", "name": "GHSA-xxxx",
                       "value": "GHSA-xxxx",
                       "url": "https://github.com/advisories/GHSA-xxxx"}],
      "links": [],
      "location": {
        "file": "frontend/package-lock.json",
        "dependency": {"package": {"name": "react"}, "version": "18.0.0"},
        "operating_system": "unknown"
      }
    }
  ],
  "dependencies": []
}"""

# ---- JUnit XML 样本（pytest --junitxml 输出结构） ----

JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="1" skipped="1" tests="3" time="1.23">
    <testcase classname="tests.test_a" name="test_ok" time="0.1"/>
    <testcase classname="tests.test_a" name="test_fail" time="0.2">
      <failure message="assert 1 == 2">assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.test_a" name="test_skip" time="0.0">
      <skipped message="skip reason"/>
    </testcase>
  </testsuite>
</testsuites>"""


class TestParseSastSarif:
    """SARIF 解析：正常路径 + 严重级别归一化 + 字段缺失兜底。"""

    def test_bandit_normal(self):
        parsed = parse_sast_sarif(BANDIT_SARIF)
        assert parsed["kind"] == "sast"
        assert parsed["tool"] == "Bandit"
        assert parsed["summary"]["total"] == 3
        assert parsed["summary"]["by_severity"] == {
            "high": 1, "medium": 1, "low": 1, "info": 0, "unknown": 0}
        results = parsed["results"]
        assert results[0] == {
            "rule": "B101", "severity": "high",
            "message": "Use of assert detected.",
            "file": "botler/api/pipelines.py",
            "line": 42, "column": 8,
        }
        assert results[1]["severity"] == "medium", "warning → medium"
        assert results[2]["severity"] == "low", "note → low"

    def test_empty_results(self):
        parsed = parse_sast_sarif('{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "T"}}, "results": []}]}')
        assert parsed["summary"]["total"] == 0
        assert parsed["results"] == []
        assert parsed["summary"]["by_severity"]["unknown"] == 0

    def test_level_none_maps_unknown(self):
        sarif = ('{"version":"2.1.0","runs":[{"tool":{"driver":{"name":"T"}},'
                 '"results":[{"ruleId":"R1","message":{"text":"m"},'
                 '"locations":[{"physicalLocation":{"artifactLocation":{"uri":"a.py"},'
                 '"region":{"startLine":1}}}]}]}]}')
        parsed = parse_sast_sarif(sarif)
        assert parsed["results"][0]["severity"] == "unknown"

    def test_missing_locations_tolerated(self):
        sarif = ('{"version":"2.1.0","runs":[{"tool":{"driver":{"name":"T"}},'
                 '"results":[{"ruleId":"R1","level":"error","message":{"text":"m"}}]}]}')
        parsed = parse_sast_sarif(sarif)
        assert parsed["results"][0]["file"] is None
        assert parsed["results"][0]["line"] is None
        assert parsed["results"][0]["column"] is None

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            parse_sast_sarif("not json {")

    def test_non_dict_json_raises(self):
        with pytest.raises(ValueError):
            parse_sast_sarif("[1, 2, 3]")

    def test_missing_runs_returns_empty(self):
        parsed = parse_sast_sarif('{"version": "2.1.0"}')
        assert parsed["summary"]["total"] == 0
        assert parsed["results"] == []

    def test_no_message_text_tolerated(self):
        sarif = ('{"version":"2.1.0","runs":[{"tool":{"driver":{"name":"T"}},'
                 '"results":[{"ruleId":"R1","level":"warning"}]}]}')
        parsed = parse_sast_sarif(sarif)
        assert parsed["results"][0]["message"] == ""


class TestParseDependencyScanning:
    """GitLab 依赖扫描 JSON 解析。"""

    def test_normal(self):
        parsed = parse_dependency_scanning(DEPS_REPORT)
        assert parsed["kind"] == "deps"
        assert parsed["summary"]["total"] == 2
        assert parsed["summary"]["by_severity"] == {
            "Critical": 0, "High": 1, "Medium": 1, "Low": 0, "Info": 0, "Unknown": 0}
        r0 = parsed["results"][0]
        assert r0["id"] == "CVE-2023-1234"
        assert r0["name"] == "requests"
        assert r0["severity"] == "High"
        assert r0["package"] == "requests"
        assert r0["version"] == "2.28.1"
        assert r0["file"] == "backend/requirements.txt"
        assert r0["solution"].startswith("升级到修复版本")
        assert r0["identifiers"][0]["type"] == "cve"

    def test_empty_vulnerabilities(self):
        parsed = parse_dependency_scanning('{"version": "15.0.0", "vulnerabilities": []}')
        assert parsed["summary"]["total"] == 0
        assert parsed["results"] == []

    def test_missing_severity_defaults_unknown(self):
        data = ('{"version":"15.0.0","vulnerabilities":[{"id":"X","name":"pkg",'
                '"location":{"file":"f.txt","dependency":{"package":{"name":"pkg"}}}}]}')
        parsed = parse_dependency_scanning(data)
        assert parsed["results"][0]["severity"] == "Unknown"

    def test_missing_fields_tolerated(self):
        parsed = parse_dependency_scanning(
            '{"version":"15.0.0","vulnerabilities":[{"id":"X"}]}')
        r = parsed["results"][0]
        assert r["name"] == ""
        assert r["package"] is None
        assert r["version"] is None
        assert r["file"] is None
        assert r["solution"] == ""

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            parse_dependency_scanning("not json")

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            parse_dependency_scanning('"str"')

    def test_vulnerabilities_non_list_tolerated(self):
        parsed = parse_dependency_scanning('{"version":"15.0.0"}')
        assert parsed["summary"]["total"] == 0


class TestParseJunitXml:
    """JUnit XML 解析：汇总 + 用例明细 + 状态归一化。"""

    def test_normal(self):
        parsed = parse_junit_xml(JUNIT_XML)
        assert parsed["kind"] == "test"
        assert parsed["summary"] == {
            "tests": 3, "failures": 1, "errors": 0, "skipped": 1, "time": 1.23}
        results = parsed["results"]
        assert results[0]["status"] == "passed"
        assert results[0]["name"] == "test_ok"
        assert results[0]["classname"] == "tests.test_a"
        assert results[1]["status"] == "failed"
        assert results[1]["message"].startswith("assert 1 == 2")
        assert results[2]["status"] == "skipped"

    def test_single_testsuite_root(self):
        xml = ('<?xml version="1.0"?><testsuite name="pytest" tests="1" failures="0" '
               'errors="0" skipped="0" time="0.5"><testcase classname="c" '
               'name="t" time="0.5"/></testsuite>')
        parsed = parse_junit_xml(xml)
        assert parsed["summary"]["tests"] == 1
        assert parsed["results"][0]["status"] == "passed"

    def test_error_case_status(self):
        xml = ('<testsuite name="pytest" tests="1" failures="0" errors="1" '
               'skipped="0" time="0.1"><testcase classname="c" name="t" time="0.1">'
               '<error message="boom">boom</error></testcase></testsuite>')
        parsed = parse_junit_xml(xml)
        assert parsed["results"][0]["status"] == "error"
        assert parsed["results"][0]["message"] == "boom"

    def test_missing_attrs_default_zero(self):
        xml = '<testsuite name="pytest"><testcase name="t"/></testsuite>'
        parsed = parse_junit_xml(xml)
        assert parsed["summary"]["tests"] == 1
        assert parsed["summary"]["failures"] == 0
        assert parsed["results"][0]["time"] is None


    def test_node_test_reporter_no_testsuite_wrapper(self):
        """node --test 的 junit reporter：testcase 直接挂在 <testsuites> 下，
        无 <testsuite> 包装（实测 node v22 输出结构），应正确汇总。"""
        xml = ('<testsuites>'
               '<testcase name="测试A" time="0.01" classname="test"/>'
               '<testcase name="测试B" time="0.02" classname="test">'
               '<failure message="boom">boom</failure></testcase>'
               '<testcase name="测试C" time="0.03" classname="test"/>'
               '</testsuites>')
        parsed = parse_junit_xml(xml)
        assert parsed["summary"]["tests"] == 3
        assert parsed["summary"]["failures"] == 1
        assert parsed["summary"]["errors"] == 0
        assert parsed["summary"]["skipped"] == 0
        statuses = [r["status"] for r in parsed["results"]]
        assert statuses == ["passed", "failed", "passed"]
        assert parsed["results"][1]["message"] == "boom"
        assert parsed["summary"]["time"] is not None, "无 testsuite time 时按用例 time 汇总"

    def test_malformed_xml_raises(self):
        with pytest.raises(ValueError):
            parse_junit_xml("<testsuites><testsuite>")

    def test_non_xml_raises(self):
        with pytest.raises(ValueError):
            parse_junit_xml("plain text")

    def test_empty_document_raises(self):
        with pytest.raises(ValueError):
            parse_junit_xml("")


class TestParseReport:
    """parse_report 按 file_type 分派。"""

    def test_dispatch_sast(self):
        parsed = parse_report("sast", BANDIT_SARIF)
        assert parsed["kind"] == "sast"

    def test_dispatch_dependency_scanning(self):
        parsed = parse_report("dependency_scanning", DEPS_REPORT)
        assert parsed["kind"] == "deps"

    def test_dispatch_junit(self):
        parsed = parse_report("junit", JUNIT_XML)
        assert parsed["kind"] == "test"

    def test_unknown_file_type_raises(self):
        with pytest.raises(ValueError):
            parse_report("archive", "blob")

    def test_empty_file_type_raises(self):
        with pytest.raises(ValueError):
            parse_report("", "blob")
