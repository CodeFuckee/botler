#!/usr/bin/env python3
"""批量同步标记库规范（docs/labels.md）到命名空间下全部 GitLab 项目。

用法:
    GITLAB_TOKEN=<token> python3 scripts/sync_labels.py \
        --host <host:port> --namespace <用户名或群组路径>

行为:
    - 枚举命名空间下全部项目
    - 对每个项目: 缺失的标签 -> 创建; 已存在但颜色/描述不同 -> 更新
    - 不删除任何已有标签（旧标签如 `ci` 保留，仅新工作统一使用新规范）
自托管实例使用自签名证书时自动跳过 TLS 校验（-k 等价）。
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# 标记库规范：与 docs/labels.md 保持一致
LABELS = [
    # 类型标签
    {"name": "bug",       "color": "#d9534f", "description": "缺陷修复"},
    {"name": "feature",   "color": "#009966", "description": "新功能"},
    {"name": "optimize",  "color": "#cd5b45", "description": "性能/体验优化"},
    {"name": "ui",        "color": "#cc338b", "description": "界面相关改动"},
    {"name": "docs",      "color": "#428bca", "description": "文档"},
    {"name": "test",      "color": "#8e44ad", "description": "补充测试"},
    {"name": "gitlab-ci", "color": "#f0ad4e", "description": "CI/CD 配置相关"},
    {"name": "chore",     "color": "#95a5a6", "description": "杂务（依赖升级、重构、构建等）"},
    # 流程/状态标签
    {"name": "in-progress", "color": "#6699cc", "description": "处理中（bot 领取 issue 时自动添加）"},
    {"name": "review",      "color": "#ff9800", "description": "待人工审查确认"},
    {"name": "blocked",     "color": "#607d8b", "description": "等待补充信息/被阻塞"},
    {"name": "bot-done",    "color": "#6699cc", "description": "bot 已完成开发，待用户确认后关闭"},
    {"name": "bot-failed",  "color": "#6699cc", "description": "bot 处理失败，需人工介入"},
]


def make_ctx(insecure):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def api(base, path, method="GET", body=None, ctx=None, token=None):
    url = f"https://{base}/api/v4{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print(f"    [HTTP {e.code}] {url} {e.read().decode()[:200]}", file=sys.stderr)
        return None


def list_projects(base, namespace, ctx, token):
    # namespace 可能是用户名或群组路径：先按用户名查，命中则枚举用户项目，否则按群组枚举
    users = api(base, f"/users?username={urllib.parse.quote(namespace, safe='')}", ctx=ctx, token=token)
    if users:
        uid = users[0]["id"]
        projects = api(base, f"/users/{uid}/projects?per_page=100", ctx=ctx, token=token)
    else:
        proj_path = urllib.parse.quote(namespace, safe="")
        projects = api(base, f"/groups/{proj_path}/projects?per_page=100", ctx=ctx, token=token)
    if not projects:
        print(f"未找到命名空间 {namespace} 下的项目", file=sys.stderr)
        sys.exit(1)
    return projects


def sync_project(base, project, ctx, token):
    path = urllib.parse.quote(project["path_with_namespace"], safe="")
    existing = api(base, f"/projects/{path}/labels?per_page=100", ctx=ctx, token=token) or []
    existing_by_name = {l["name"]: l for l in existing}
    created = updated = unchanged = 0
    for spec in LABELS:
        cur = existing_by_name.get(spec["name"])
        if cur is None:
            r = api(base, f"/projects/{path}/labels", method="POST", body=spec, ctx=ctx, token=token)
            print(f"  + 创建 {spec['name']} {spec['color']} ({'OK' if r else '失败'})")
            created += 1 if r else 0
        elif cur.get("color") != spec["color"] or (cur.get("description") or "") != spec["description"]:
            label_id = urllib.parse.quote(cur["name"], safe="")
            r = api(
                base, f"/projects/{path}/labels/{label_id}",
                method="PUT",
                body={"color": spec["color"], "description": spec["description"]},
                ctx=ctx, token=token,
            )
            print(f"  ~ 更新 {spec['name']} ({'OK' if r else '失败'})")
            updated += 1 if r else 0
        else:
            unchanged += 1
    print(f"  [{project['path_with_namespace']}] 创建 {created} / 更新 {updated} / 不变 {unchanged}")


def main():
    parser = argparse.ArgumentParser(description="同步标记库到 GitLab 项目")
    parser.add_argument("--host", required=True, help="GitLab 主机 host:port")
    parser.add_argument("--namespace", required=True, help="用户名或群组路径")
    parser.add_argument("--insecure", action="store_true", default=True,
                        help="跳过 TLS 校验（自签名证书，默认开启）")
    args = parser.parse_args()

    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        print("缺少环境变量 GITLAB_TOKEN", file=sys.stderr)
        sys.exit(1)
    ctx = make_ctx(args.insecure)

    projects = list_projects(args.host, args.namespace, ctx, token)
    print(f"共 {len(projects)} 个项目:")
    for p in projects:
        print(f"- {p['path_with_namespace']}")
    for p in projects:
        sync_project(args.host, p, ctx, token)
    print("完成。")


if __name__ == "__main__":
    main()
