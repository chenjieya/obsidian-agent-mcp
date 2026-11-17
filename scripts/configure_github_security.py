"""使用现有 GitHub 登录配置推送保护及 main 的必需 CI 检查，不输出凭证。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request

REPOSITORY = "chenjieya/obsidian-agent-mcp"
REQUIRED_CHECKS = ["Secret checks", "Tests (ubuntu-latest)", "Tests (windows-latest)", "Tests (macos-latest)"]


def credential() -> str:
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if value:
        return value
    result = subprocess.run(["git", "credential", "fill"],
        input=b"protocol=https\nhost=github.com\n\n", capture_output=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}, check=False)
    fields = dict(line.split("=", 1) for line in result.stdout.decode("utf-8").splitlines() if "=" in line)
    if result.returncode or not fields.get("password"):
        raise RuntimeError("没有 GitHub 登录凭证；请先在本机运行 git credential-manager github login --browser。")
    return fields["password"]


def api(token: str, path: str, *, method: str = "GET", body: object = None,
        missing_ok: bool = False) -> dict[str, object] | None:
    # 所有凭证仅发送到固定官方 API 主机，不接收任意目标 URL。
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "User-Agent": "obsidian-agent-mcp-security-setup", "Content-Type": "application/json"}
    request = urllib.request.Request("https://api.github.com" + path, headers=headers,
        method=method, data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
            if not isinstance(result, dict):
                raise RuntimeError("GitHub API 返回结构无效。")
            return result
    except urllib.error.HTTPError as error:
        if error.code == 404 and missing_ok:
            return None
        raise RuntimeError(f"GitHub API 操作失败（HTTP {error.code}）；请检查登录权限与仓库设置。") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub 推送保护和必需检查设置")
    parser.add_argument("--apply", action="store_true", help="实际启用远端设置；默认只读检查")
    args = parser.parse_args()
    token = credential()
    prefix = "/repos/" + REPOSITORY
    repository = api(token, prefix)
    permissions = repository.get("permissions", {}) if repository else {}
    if not isinstance(permissions, dict) or permissions.get("admin") is not True:
        raise RuntimeError("当前账号没有仓库管理权限，不能配置远端保护。")
    if api(token, prefix + "/branches/main", missing_ok=True) is None:
        raise RuntimeError("远端 main 尚不存在，请先完成经过本地密钥校验的首次推送。")
    protection = api(token, prefix + "/branches/main/protection", missing_ok=True)
    if args.apply:
        if protection is not None:
            raise RuntimeError("远端已有分支保护，本脚本不覆盖；请对照文档合并必需检查。")
        api(token, prefix, method="PATCH", body={"security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
        }})
        integration = api(token, "/apps/github-actions")
        if not integration or not isinstance(integration.get("id"), int):
            raise RuntimeError("无法确认 GitHub Actions 身份，未修改分支保护。")
        api(token, prefix + "/branches/main/protection", method="PUT", body={
            "required_status_checks": {"strict": True, "contexts": [], "checks": [
                {"context": name, "app_id": integration["id"]} for name in REQUIRED_CHECKS]},
            "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "restrictions": None, "allow_force_pushes": False, "allow_deletions": False,
        })
        repository = api(token, prefix)
        protection = api(token, prefix + "/branches/main/protection")
    security = repository.get("security_and_analysis", {}) if repository else {}
    checks = protection.get("required_status_checks", {}) if protection else {}
    print(json.dumps({"repository": REPOSITORY, "applied": args.apply,
        "security_and_analysis": security, "required_status_checks": checks,
        "required_checks_expected": REQUIRED_CHECKS,
        "branch_protection_present": protection is not None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, RuntimeError) else "无法完成 GitHub 设置检查，未输出原始响应。"
        raise SystemExit(message) from None
