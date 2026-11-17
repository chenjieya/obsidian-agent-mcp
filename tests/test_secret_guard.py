"""在隔离的临时 Git 仓库中验证拦截；只使用合成密钥，不创建 commit。"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_secrets import CheckError, candidates, check_push, find_gitleaks, protected_path, scan

PROJECT = Path(__file__).resolve().parents[1]
BINARY = PROJECT / ".tools/gitleaks" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")


class SecretGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = PROJECT / ".tmp"
        temporary.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="guard-test-", dir=temporary)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_git("init", "-b", "main")
        shutil.copyfile(PROJECT / ".gitleaks.toml", self.root / ".gitleaks.toml")
        self.secret = hashlib.sha256(b"isolated fake credential for regression tests").hexdigest()

    def run_git(self, *args: str) -> bytes:
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)
        return result.stdout

    def put(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def check(self, *, worktree: bool = False, history: str | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with patch.dict(os.environ, {"GITLEAKS_BIN": str(BINARY)}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = scan(self.root, worktree=worktree, history=history)
        return status, output.getvalue()

    def commit_fixture(self, message: str) -> str:
        # 仅在临时测试仓库创建合成历史，绝不连接远端。
        self.run_git("add", ".")
        self.run_git("-c", "user.name=Secret Check Test", "-c", "user.email=test@example.invalid",
                     "commit", "-m", message)
        return self.run_git("rev-parse", "HEAD").decode().strip()

    def test_protected_filenames_and_safe_example(self) -> None:
        for name in [".env", "nested/.env.production", "PRIVATE.KEY", "certs/private.pem",
                     "server.pfx", ".aws/credentials", ".tools/gitleaks.exe", "secrets.json"]:
            with self.subTest(name=name):
                self.assertTrue(protected_path(name))
        for name in [".env.example", "nested/.env.example", "certs/public.crt", "src/server.py"]:
            self.assertFalse(protected_path(name))

    def test_force_added_env_is_blocked_without_echoing_secret(self) -> None:
        self.put(".gitignore", ".env\n")
        self.put(".env", "OBSIDIAN_API_KEY=" + self.secret)
        self.run_git("add", "-f", ".env")
        status, output = self.check()
        self.assertEqual(status, 1)
        self.assertIn(".env", output)
        self.assertNotIn(self.secret, output)

    def test_index_blob_is_independent_of_unstaged_contents(self) -> None:
        self.put("note.txt", "staged version")
        self.run_git("add", "note.txt")
        self.put("note.txt", "working version")
        name, oid = candidates(self.root, worktree=False)[0]
        self.assertEqual(name, "note.txt")
        self.assertEqual(self.run_git("cat-file", "blob", str(oid)), b"staged version")

    def test_current_local_secret_copied_without_assignment_is_blocked(self) -> None:
        self.put(".env", "OBSIDIAN_API_KEY=" + self.secret)
        self.put("note.txt", "copied token: " + self.secret)
        self.run_git("add", "note.txt")
        with patch("scripts.check_secrets.find_gitleaks", return_value="unused"):
            status, output = self.check()
        self.assertEqual(status, 1)
        self.assertIn("note.txt", output)
        self.assertNotIn(self.secret, output)

    def test_missing_scanner_fails_closed(self) -> None:
        with patch("scripts.check_secrets.shutil.which", return_value=None), \
                patch.dict(os.environ, {"GITLEAKS_BIN": ""}):
            with self.assertRaisesRegex(CheckError, "缺少 Gitleaks"):
                find_gitleaks(self.root)

    def test_wrong_scanner_version_fails_closed(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout=b"0.0.0", stderr=b"")
        with patch.dict(os.environ, {"GITLEAKS_BIN": "fake"}), \
                patch("scripts.check_secrets.subprocess.run", return_value=result):
            with self.assertRaisesRegex(CheckError, "版本"):
                find_gitleaks(self.root)

    def test_push_deletion_has_no_content_to_upload(self) -> None:
        zero = "0" * 40
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(check_push(self.root, f"(delete) {zero} refs/heads/old {'a' * 40}\n"), 0)

    def test_invalid_push_input_is_rejected(self) -> None:
        with self.assertRaises(CheckError):
            check_push(self.root, "untrusted input")

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_deleted_secret_in_history_is_still_blocked(self) -> None:
        self.put("config.txt", "OBSIDIAN_API_KEY=" + self.secret)
        self.commit_fixture("synthetic secret")
        self.put("config.txt", "OBSIDIAN_API_KEY=replace-with-your-api-key")
        self.commit_fixture("remove synthetic secret")
        self.assertEqual(self.check()[0], 0)
        status, output = self.check(history="HEAD")
        self.assertEqual(status, 1)
        self.assertNotIn(self.secret, output)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_protected_file_deleted_from_head_is_still_blocked(self) -> None:
        self.put(".env", "OBSIDIAN_API_KEY=placeholder")
        self.commit_fixture("synthetic private config")
        self.run_git("rm", ".env")
        self.commit_fixture("remove private config")
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.check(history="HEAD")[0], 1)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_push_new_branch_scans_history_not_worktree(self) -> None:
        self.put("config.txt", "OBSIDIAN_API_KEY=" + self.secret)
        commit = self.commit_fixture("synthetic outgoing secret")
        self.put("config.txt", "OBSIDIAN_API_KEY=replace-with-your-api-key")
        output = io.StringIO()
        with patch.dict(os.environ, {"GITLEAKS_BIN": str(BINARY)}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = check_push(self.root, f"refs/heads/main {commit} refs/heads/main {'0' * 40}\n")
        self.assertEqual(status, 1)
        self.assertNotIn(self.secret, output.getvalue())

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_clean_outgoing_range_passes(self) -> None:
        self.put("normal.txt", "safe first version")
        base = self.commit_fixture("safe base")
        self.put("normal.txt", "safe second version")
        tip = self.commit_fixture("safe update")
        output = io.StringIO()
        with patch.dict(os.environ, {"GITLEAKS_BIN": str(BINARY)}), contextlib.redirect_stdout(output):
            self.assertEqual(check_push(self.root,
                f"refs/heads/main {tip} refs/heads/main {base}\n"), 0)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_clean_snapshot_and_public_certificate_pass(self) -> None:
        self.put("normal.md", "## 1. safe content")
        self.put("certs/public.crt", "public certificate content")
        self.put(".env.example", "OBSIDIAN_API_KEY=replace-with-your-api-key")
        self.run_git("add", ".")
        self.assertEqual(self.check()[0], 0)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_secret_in_allowed_example_is_not_whitelisted(self) -> None:
        self.put(".env.example", "OBSIDIAN_API_KEY=" + self.secret)
        self.run_git("add", ".env.example")
        status, output = self.check()
        self.assertEqual(status, 1)
        self.assertIn("obsidian-rest-api-key", output)
        self.assertNotIn(self.secret, output)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_staged_secret_remains_blocked_after_worktree_is_cleaned(self) -> None:
        self.put("config.txt", "OBSIDIAN_API_KEY=" + self.secret)
        self.run_git("add", "config.txt")
        self.put("config.txt", "OBSIDIAN_API_KEY=replace-with-your-api-key")
        status, output = self.check()
        self.assertEqual(status, 1)
        self.assertNotIn(self.secret, output)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_unstaged_secret_does_not_change_index_scan(self) -> None:
        self.put("config.txt", "OBSIDIAN_API_KEY=replace-with-your-api-key")
        self.run_git("add", "config.txt")
        self.put("config.txt", "OBSIDIAN_API_KEY=" + self.secret)
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.check(worktree=True)[0], 1)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_inline_allow_comment_does_not_bypass_scan(self) -> None:
        self.put("config.txt", "OBSIDIAN_API_KEY=" + self.secret + " # gitleaks:allow\n")
        self.run_git("add", "config.txt")
        self.assertEqual(self.check()[0], 1)

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_scanner_failure_does_not_pass_or_echo_raw_output(self) -> None:
        original_run = subprocess.run

        def fail_scan(arguments, **kwargs):
            if arguments[:2] == [str(BINARY), "dir"]:
                return subprocess.CompletedProcess(arguments, 2,
                    stdout=self.secret.encode(), stderr=self.secret.encode())
            return original_run(arguments, **kwargs)

        output = io.StringIO()
        with patch.dict(os.environ, {"GITLEAKS_BIN": str(BINARY)}), \
                patch("scripts.check_secrets.subprocess.run", side_effect=fail_scan), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(CheckError, "未能完成扫描"):
                scan(self.root)
        self.assertNotIn(self.secret, output.getvalue())

    @unittest.skipUnless(BINARY.is_file(), "需要安装项目固定版本 Gitleaks")
    def test_native_git_hook_blocks_before_any_commit(self) -> None:
        (self.root / "scripts").mkdir()
        (self.root / ".githooks").mkdir()
        shutil.copyfile(PROJECT / "scripts/check_secrets.py", self.root / "scripts/check_secrets.py")
        hook = self.root / ".githooks/pre-commit"
        shutil.copyfile(PROJECT / ".githooks/pre-commit", hook)
        hook.chmod(0o755)
        self.run_git("config", "core.hooksPath", ".githooks")
        launchers = self.root / "python-bin"
        launchers.mkdir()
        python3 = launchers / "python3"
        python3.write_text('#!/bin/sh\nexec "' + Path(sys.executable).as_posix() + '" "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
        environment = {**os.environ, "GITLEAKS_BIN": str(BINARY),
                       "PATH": str(launchers) + os.pathsep + os.environ.get("PATH", "")}
        clean = subprocess.run(["git", "hook", "run", "pre-commit"], cwd=self.root,
                               env=environment, capture_output=True, check=False)
        self.assertEqual(clean.returncode, 0, clean.stderr.decode("utf-8", errors="replace"))
        self.put(".env", "OBSIDIAN_API_KEY=" + self.secret)
        self.run_git("add", "-f", ".env")
        result = subprocess.run(["git", "hook", "run", "pre-commit"], cwd=self.root,
            env=environment, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("禁止提交敏感文件".encode("utf-8"), result.stderr)
        self.assertNotIn(self.secret.encode(), result.stdout + result.stderr)
        self.assertEqual(self.run_git("rev-list", "--all", "--count").strip(), b"0")
