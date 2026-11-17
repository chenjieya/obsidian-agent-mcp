"""验证远端保护脚本的只读边界和请求内容，不请求真实 GitHub。"""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from scripts import configure_github_security as settings


class GitHubSecurityTests(unittest.TestCase):
    def invoke(self, arguments, responder):
        output = io.StringIO()
        with patch.object(sys, "argv", ["configure_github_security.py", *arguments]), \
                patch.object(settings, "credential", return_value="synthetic-test-token"), \
                patch.object(settings, "api", side_effect=responder), contextlib.redirect_stdout(output):
            settings.main()
        self.assertNotIn("synthetic-test-token", output.getvalue())
        return output.getvalue()

    def test_read_only_mode_never_writes(self):
        calls = []

        def responder(token, path, **kwargs):
            calls.append(kwargs.get("method", "GET"))
            if path.endswith("/protection"):
                return None
            if path.endswith("/branches/main"):
                return {"name": "main"}
            return {"permissions": {"admin": True}}

        self.invoke([], responder)
        self.assertEqual(calls, ["GET", "GET", "GET"])

    def test_existing_branch_protection_is_not_overwritten(self):
        def responder(token, path, **kwargs):
            self.assertEqual(kwargs.get("method", "GET"), "GET")
            if path.endswith("/protection"):
                return {"required_status_checks": {"contexts": ["Existing check"]}}
            return {"permissions": {"admin": True}}

        with self.assertRaisesRegex(RuntimeError, "不覆盖"):
            self.invoke(["--apply"], responder)

    def test_apply_requires_admin_before_any_mutation(self):
        def responder(token, path, **kwargs):
            self.assertEqual(kwargs.get("method", "GET"), "GET")
            return {"permissions": {"admin": False}}

        with self.assertRaisesRegex(RuntimeError, "管理权限"):
            self.invoke(["--apply"], responder)

    def test_new_protection_requires_checks_from_github_actions(self):
        writes = []

        def responder(token, path, **kwargs):
            method = kwargs.get("method", "GET")
            if method != "GET":
                writes.append((method, kwargs["body"]))
                return {}
            if path == "/apps/github-actions":
                return {"id": 12345}
            if path.endswith("/protection"):
                return None if not writes else {"required_status_checks": writes[-1][1]["required_status_checks"]}
            return {"permissions": {"admin": True}}

        self.invoke(["--apply"], responder)
        self.assertEqual([method for method, body in writes], ["PATCH", "PUT"])
        body = writes[1][1]
        self.assertTrue(body["enforce_admins"])
        self.assertIsNotNone(body["required_pull_request_reviews"])
        self.assertFalse(body["allow_force_pushes"])
        self.assertEqual({check["context"] for check in body["required_status_checks"]["checks"]},
                         set(settings.REQUIRED_CHECKS))
        self.assertTrue(all(check["app_id"] == 12345 for check in body["required_status_checks"]["checks"]))
