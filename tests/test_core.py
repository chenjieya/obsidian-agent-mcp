from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from obsidian_mcp.client import revision_header
from obsidian_mcp.config import Settings, load_env_file
from obsidian_mcp.errors import ObsidianError
from obsidian_mcp.paths import endpoint, note_path, vault_path
from obsidian_mcp.templates import render_template


class CoreTests(unittest.TestCase):
    def test_paths_reject_traversal_configuration_and_windows_aliases(self) -> None:
        for path in ["../note.md", "/note.md", "C:/note.md", "a\\b.md", "a//b.md",
                     ".obsidian/config.md", "A/.git/token.md", "a/../b.md", "a./b.md",
                     "CON.md", "a/%2e%2e/b.md", "a/%2fb.md", "a/\x00.md"]:
            with self.subTest(path=path), self.assertRaises(ObsidianError):
                note_path(path)

    def test_unicode_and_reserved_url_characters_are_encoded(self) -> None:
        self.assertEqual(note_path("项目/100% #需求.md"), "项目/100% #需求.md")
        self.assertEqual(endpoint("项目/100% #需求.md"),
                         "/vault/%E9%A1%B9%E7%9B%AE/100%25%20%23%E9%9C%80%E6%B1%82.md")
        self.assertEqual(vault_path("", folder=True), "")
        self.assertEqual(endpoint("项目", folder=True), "/vault/%E9%A1%B9%E7%9B%AE/")

    def test_invalid_url_and_key_are_rejected_without_leaking_credentials(self) -> None:
        for url in ["https://example.com", "https://secret@localhost:27124",
                    "http://localhost/path", "http://localhost/?x=1", "ftp://localhost",
                    "http://[broken", "http://localhost:invalid"]:
            with self.subTest(url=url), self.assertRaises(ObsidianError) as error:
                Settings(api_key="test-secret", api_url=url)
            self.assertNotIn("test-secret", str(error.exception))
        with self.assertRaises(ObsidianError):
            Settings(api_key="")
        self.assertNotIn("test-secret", repr(Settings(api_key="test-secret")))

    def test_revision_rejects_wildcards_lists_weak_tags_and_controls(self) -> None:
        for revision in ["*", "", '"a","b"', 'W/"abc"', "a\r\nb", "a\x00b", "a b"]:
            with self.subTest(revision=revision), self.assertRaises(ObsidianError):
                revision_header(revision)
        self.assertEqual(revision_header('"abc"'), '"abc"')

    def test_environment_overrides_file_and_file_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('OBSIDIAN_API_KEY="file-key"\nCUSTOM_VALUE=$(anything)\n', encoding="utf-8")
            with patch.dict(os.environ, {"OBSIDIAN_API_KEY": "env-key"}, clear=True):
                load_env_file(path)
                self.assertEqual(Settings.from_env().api_key, "env-key")
                self.assertEqual(os.environ["CUSTOM_VALUE"], "$(anything)")

    def test_bad_environment_values_fail_clearly(self) -> None:
        for name, value in [("OBSIDIAN_VERIFY_SSL", "maybe"), ("OBSIDIAN_TIMEOUT", "nan"),
                            ("OBSIDIAN_MAX_NOTE_BYTES", "0")]:
            with patch.dict(os.environ, {"OBSIDIAN_API_KEY": "key", name: value}, clear=True):
                with self.assertRaises(ObsidianError):
                    Settings.from_env()

    def test_templates_do_not_execute_code_and_reject_missing_values(self) -> None:
        self.assertEqual(render_template("# {{title}}\n<% arbitrary_code() %>", {"title": "测试"}),
                         "# 测试\n<% arbitrary_code() %>")
        with self.assertRaises(ObsidianError):
            render_template("{{missing}}", {})
