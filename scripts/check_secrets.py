"""检查准备提交的实际文件快照，所有输出只包含路径和规则，不输出密钥。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

GITLEAKS_VERSION = "8.30.1"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


class CheckError(Exception):
    """只包含可公开的定位信息。"""


def git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=False)
    if result.returncode:
        raise CheckError("Git 读取失败，请确认在仓库中运行并检查暂存区状态。")
    return result.stdout


def protected_path(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1] if parts else ""
    return (
        (name == ".env" or name.startswith(".env.")) and name != ".env.example"
        or name.endswith((".key", ".pem", ".p12", ".pfx"))
        or name in {"id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
        or bool(set(parts) & {".obsidian", ".aws", ".ssh", ".claude", ".tools", ".tmp", ".venv"})
    )


def safe_relative(path: str) -> Path:
    parts = PurePosixPath(path).parts
    if (not parts or path.startswith(("/", "\\")) or "\\" in path
            or any(part in {"", ".", ".."} or ":" in part for part in parts)):
        raise CheckError("发现无法安全扫描的仓库路径。")
    return Path(*parts)


def candidates(root: Path, *, worktree: bool) -> list[tuple[str, str | None]]:
    """默认读取整个 Git index，而不是容易受未暂存修改影响的工作区。"""
    if worktree:
        paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        return [(os.fsdecode(path), None) for path in sorted(set(paths.split(b"\0"))) if path]
    entries = git(root, "ls-files", "--stage", "-z")
    result: list[tuple[str, str | None]] = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        header, path = entry.split(b"\t", 1)
        mode, oid, stage = header.split()
        if stage != b"0":
            raise CheckError("暂存区存在未解决的合并冲突，不能完成密钥校验。")
        if mode not in {b"100644", b"100755"}:
            raise CheckError(f"不支持扫描符号链接或子模块：{os.fsdecode(path)!r}")
        result.append((os.fsdecode(path), oid.decode("ascii")))
    return result


def history_candidates(root: Path, revision_range: str) -> list[tuple[str, str | None]]:
    """扫描范围内每个提交的完整树，能发现先提交密钥、再删除的历史。"""
    revision = r"(?:HEAD|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"
    if not re.fullmatch(revision + r"(?:\.\." + revision + r")?", revision_range):
        raise CheckError("历史范围必须是 HEAD、完整提交 SHA 或两个完整 SHA 的范围。")
    commits = git(root, "rev-list", "--reverse", revision_range, "--").splitlines()
    if len(commits) > 5000:
        raise CheckError("历史提交超过 5000 个，扫描没有完成，推送已阻止。")
    result: list[tuple[str, str | None]] = []
    seen: set[tuple[bytes, bytes]] = set()
    for commit in commits:
        for entry in git(root, "ls-tree", "-r", "-z", commit.decode("ascii")).split(b"\0"):
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, kind, oid = header.split()
            path = os.fsdecode(name)
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise CheckError(f"历史包含不支持扫描的符号链接或子模块：{path!r}")
            if (name, oid) not in seen:
                seen.add((name, oid))
                result.append((commit.decode("ascii") + "/" + path, oid.decode("ascii")))
                if len(result) > 50000:
                    raise CheckError("历史文件版本超过 50000 个，扫描没有完成，推送已阻止。")
    return result


def known_local_secrets(root: Path) -> set[bytes]:
    """读取项目本地 .env 的敏感字段，用于拦截复制到其他文件的当前密钥。"""
    values: set[bytes] = set()
    for path in root.glob(".env*"):
        if path.name == ".env.example" or not path.is_file():
            continue
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            raise CheckError("本地环境配置不可安全读取，无法检查当前密钥是否被复制。")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if not separator or not re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)", key.upper()):
                continue
            value = value.strip().strip("\"'")
            if len(value) >= 8 and not re.search(r"(?i)(replace-with|your[-_]|placeholder|example|\$\{|{{)", value):
                values.add(value.encode("utf-8"))
    return values


def find_gitleaks(root: Path) -> str:
    local = root / ".tools" / "gitleaks" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
    binary = os.environ.get("GITLEAKS_BIN") or (str(local) if local.is_file() else shutil.which("gitleaks"))
    if not binary:
        raise CheckError("缺少 Gitleaks，请先运行 python scripts/install_gitleaks.py；校验不会跳过。")
    try:
        result = subprocess.run([binary, "version"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError("无法执行 Gitleaks，提交已阻止。") from exc
    if result.returncode or result.stdout.decode("utf-8", errors="replace").strip().lstrip("v") != GITLEAKS_VERSION:
        raise CheckError(f"Gitleaks 版本必须为 {GITLEAKS_VERSION}，请运行安装脚本。")
    return binary


def scan(root: Path, *, worktree: bool = False, history: str | None = None) -> int:
    entries = history_candidates(root, history) if history is not None else candidates(root, worktree=worktree)
    forbidden = [path for path, _ in entries if protected_path(path)]
    if forbidden:
        for path in forbidden:
            print(f"禁止提交敏感文件：{path!r}", file=sys.stderr)
        return 1
    binary = find_gitleaks(root)
    values = known_local_secrets(root)
    temporary_root = root / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    total = 0
    with tempfile.TemporaryDirectory(prefix="secret-check-", dir=temporary_root) as temporary:
        directory = Path(temporary)
        snapshot = directory / "snapshot"
        snapshot.mkdir()
        for name, oid in entries:
            relative = safe_relative(name)
            if worktree:
                source = root / relative
                if not source.exists():
                    continue  # 工作区删除的文件不会在工作区扫描中读取。
                if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
                    raise CheckError(f"不扫描仓库外部文件或符号链接：{name!r}")
                if source.stat().st_size > MAX_FILE_BYTES:
                    raise CheckError(f"文件过大，不能跳过密钥校验：{name!r}")
                content = source.read_bytes()
            else:
                size = int(git(root, "cat-file", "-s", str(oid)).strip())
                if size > MAX_FILE_BYTES:
                    raise CheckError(f"文件过大，不能跳过密钥校验：{name!r}")
                content = git(root, "cat-file", "blob", str(oid))
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise CheckError("待检查文件总大小超出 100 MiB；校验没有完成，提交已阻止。")
            if any(value in content for value in values):
                print(f"检测到本地环境配置中的密钥被复制：{name!r}（内容已隐藏）", file=sys.stderr)
                return 1
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        report = directory / "findings.json"
        ignore_file = directory / "empty.gitleaksignore"
        ignore_file.write_text("", encoding="utf-8")
        # 扫描完整 index 或历史树快照；历史不依赖 git log -p，因此不会遗漏合并提交。
        command = [binary, "dir", str(snapshot), "--config", str(root / ".gitleaks.toml"),
                   "--redact=100", "--no-banner", "--log-level", "error",
                   "--ignore-gitleaks-allow", "--gitleaks-ignore-path", str(ignore_file),
                   "--max-decode-depth", "2", "--max-archive-depth", "2",
                   "--report-format", "json", "--report-path", str(report), "--exit-code", "1"]
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"}}
        try:
            result = subprocess.run(command, cwd=root, env=environment,
                                    capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckError("Gitleaks 执行失败或超时，提交已阻止。") from exc
        if result.returncode == 1:
            try:
                findings = json.loads(report.read_text(encoding="utf-8"))
                for finding in findings:
                    name = str(finding.get("File", "unknown"))
                    try:
                        name = str(Path(name).relative_to(snapshot))
                    except ValueError:
                        pass
                    print(f"检测到疑似密钥：{name!r}:{finding.get('StartLine', '?')} "
                          f"规则={finding.get('RuleID', 'unknown')!r}（内容已隐藏）", file=sys.stderr)
            except (OSError, ValueError, TypeError, AttributeError):
                print("Gitleaks 检测到疑似密钥，提交已阻止（原始输出已隐藏）。", file=sys.stderr)
            return 1
        if result.returncode:
            raise CheckError("Gitleaks 未能完成扫描，提交已阻止（原始输出已隐藏）。")
    source = "历史文件版本" if history is not None else "工作区候选文件" if worktree else "暂存快照文件"
    print(f"密钥校验通过：检查了 {len(entries)} 个{source}。")
    return 0


def check_push(root: Path, updates: str) -> int:
    """处理 Git pre-push 标准输入，删除远端引用不上传内容，其他引用逐一检查。"""
    count = 0
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise CheckError("pre-push 引用信息无效，推送已阻止。")
        _, local_oid, _, remote_oid = fields
        if not all(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid)
                   for oid in (local_oid, remote_oid)):
            raise CheckError("pre-push 提交 SHA 无效，推送已阻止。")
        if not local_oid.strip("0"):
            continue
        # 转换附注标签为目标 commit；不能解析的引用不能静默放行。
        local_commit = git(root, "rev-parse", "--verify", local_oid + "^{commit}").decode().strip()
        if remote_oid.strip("0"):
            remote_commit = git(root, "rev-parse", "--verify", remote_oid + "^{commit}").decode().strip()
            revision_range = remote_commit + ".." + local_commit
        else:
            revision_range = local_commit
        if scan(root, history=revision_range):
            return 1
        count += 1
    if not count:
        print("没有待上传的提交内容。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="提交前密钥校验，不自动暂存或提交")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="检查实际 Git index 快照（默认）")
    mode.add_argument("--worktree", action="store_true", help="检查受跟踪及未忽略的工作区候选文件")
    mode.add_argument("--history", nargs="?", const="HEAD", help="检查指定提交范围内所有历史文件版本")
    mode.add_argument("--pre-push", action="store_true", help="读取 Git pre-push 引用并扫描待上传历史")
    args = parser.parse_args()
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").strip())
        if args.pre_push:
            return check_push(root, sys.stdin.read())
        return scan(root, worktree=args.worktree, history=args.history)
    except (CheckError, OSError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, CheckError) else "无法读取扫描输入，提交已阻止。"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
