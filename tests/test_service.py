from __future__ import annotations

import base64
import dataclasses
import unittest

import httpx

from obsidian_mcp.client import ObsidianClient
from obsidian_mcp.config import Settings
from obsidian_mcp.errors import ConflictError, ObsidianError
from obsidian_mcp.service import NoteService

from tests.fake_api import FakeVault


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.vault = FakeVault()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.vault.handle))
        self.settings = Settings(api_key="test-secret")
        self.client = ObsidianClient(self.settings, self.http)
        self.service = NoteService(self.client)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_health_checks_authentication(self) -> None:
        self.assertTrue((await self.client.health())["authenticated"])

    async def test_health_rejects_unauthenticated_200_response(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"authenticated": False}))) as http:
            with self.assertRaisesRegex(ObsidianError, "认证"):
                await ObsidianClient(self.settings, http).health()

    async def test_read_uses_content_revision_not_representation_etag(self) -> None:
        note = await self.client.read_note("项目/需求.md")
        self.assertEqual(note["revision"], self.vault.revision("项目/需求.md"))
        self.assertNotEqual(note["revision"], "representation-etag")

    async def test_missing_json_revision_falls_back_to_matching_raw_content_etag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = self.vault.handle(request)
            if request.headers.get("accept") == "application/vnd.olrapi.note+json":
                data = response.json()
                data.pop("version")
                return httpx.Response(200, json=data, headers={"ETag": '"representation-etag"'})
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            note = await ObsidianClient(self.settings, http).read_note("项目/需求.md")
        self.assertEqual(note["revision"], f'"{self.vault.revision("项目/需求.md")}"')

    async def test_create_never_overwrites_existing_note(self) -> None:
        original = self.vault.files["项目/需求.md"]
        with self.assertRaises(ConflictError):
            await self.service.create_note("项目/需求.md", "覆盖")
        self.assertEqual(self.vault.files["项目/需求.md"], original)

    async def test_create_race_is_rejected_by_backend(self) -> None:
        self.vault.before_put = ("新笔记.md", "用户抢先创建".encode())
        with self.assertRaises(ConflictError):
            await self.service.create_note("新笔记.md", "agent 创建")
        self.assertEqual(self.vault.files["新笔记.md"].decode(), "用户抢先创建")

    async def test_create_metadata_is_unicode_and_valid_json_yaml(self) -> None:
        await self.service.create_note("新目录/计划.md", "# 计划", ["项目/测试"], "active")
        note = await self.client.read_note("新目录/计划.md")
        self.assertEqual(note["frontmatter"], {"tags": ["项目/测试"], "status": "active"})

    async def test_precise_patch_and_conflict_before_write(self) -> None:
        note = await self.client.read_note("项目/需求.md")
        revision = str(note["revision"])
        await self.service.patch_note("项目/需求.md", revision, "登录", "注销")
        self.assertIn("注销", self.vault.files["项目/需求.md"].decode())
        with self.assertRaises(ConflictError):
            await self.service.patch_note("项目/需求.md", revision, "注销", "覆盖")

    async def test_race_between_patch_read_and_write_is_rejected_by_backend(self) -> None:
        revision = self.vault.revision("项目/需求.md")
        self.vault.before_put = ("项目/需求.md", "用户的新修改".encode())
        with self.assertRaises(ConflictError):
            await self.service.patch_note("项目/需求.md", revision, "登录", "注销")
        self.assertEqual(self.vault.files["项目/需求.md"].decode(), "用户的新修改")

    async def test_patch_rejects_ambiguous_match(self) -> None:
        self.vault.files["重复.md"] = b"same same"
        with self.assertRaises(ObsidianError):
            await self.service.patch_note("重复.md", self.vault.revision("重复.md"), "same", "new")
        self.assertEqual(self.vault.files["重复.md"], b"same same")

    async def test_old_plugins_are_readable_but_writes_fail_closed(self) -> None:
        self.vault.conditional = False
        await self.client.read_note("项目/需求.md")
        with self.assertRaisesRegex(ObsidianError, "升级"):
            await self.service.create_note("new.md", "test")
        self.assertNotIn("new.md", self.vault.files)
        self.assertFalse(any(request.method == "PUT" for request in self.vault.requests))

    async def test_status_patch_preserves_other_properties(self) -> None:
        await self.service.create_note("状态.md", "# 计划", ["项目"], "none")
        revision = self.vault.revision("状态.md")
        await self.service.update_property("状态.md", revision, "status", "active")
        metadata = (await self.client.read_note("状态.md"))["frontmatter"]
        self.assertEqual(metadata, {"tags": ["项目"], "status": "active"})

    async def test_invalid_status_or_tag_does_not_write(self) -> None:
        for key, value in [("status", []), ("status", "oops"), ("tags", ["#tag"])]:
            with self.subTest(key=key, value=value), self.assertRaises(ObsidianError):
                await self.service.update_property("项目/需求.md", "abc", key, value)

    async def test_pagination_search_and_hidden_paths(self) -> None:
        self.vault.files[".obsidian/secret.md"] = b"secret"
        result = await self.service.list_notes(limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["next_offset"], 1)
        search = await self.service.search_notes("登录")
        self.assertEqual(search["returned"], 1)
        params = self.vault.requests[-1].url.params
        self.assertEqual(params["contextLength"], "150")
        self.assertEqual(params["maxMatchesPerFile"], "3")

    async def test_scan_limit_requires_narrower_directory(self) -> None:
        service = NoteService(ObsidianClient(dataclasses.replace(self.settings, max_scan_files=1), self.http))
        with self.assertRaisesRegex(ObsidianError, "扫描"):
            await service.list_notes()

    async def test_builtin_template_provenance_and_instructions(self) -> None:
        result = await self.service.create_from_template("计划.md", "builtin:implementation-plan", "登录", "新增登录")
        self.assertTrue(result["template_linked"])
        note = await self.service.read_note("计划.md", include_template=True)
        self.assertIn("source_template", note)
        self.assertIn("新增登录", str(note["body"]))
        self.assertIn("审阅", str(result["instructions"]))

    async def test_custom_template_preserves_frontmatter_and_links_source(self) -> None:
        self.vault.files["Templates/自定义.md"] = '---\nowner: "张三"\n---\n\n# {{title}}\n{{requirements}}'.encode()
        await self.service.create_from_template("自定义计划.md", "Templates/自定义.md", "计划", "需求")
        note = await self.service.read_note("自定义计划.md", include_template=True)
        self.assertEqual(note["frontmatter"], {"source_template": "Templates/自定义.md", "owner": "张三"})
        self.assertIn("source_template", note)

    async def test_missing_custom_template_directory_still_lists_builtins(self) -> None:
        templates = (await self.service.list_templates())["templates"]
        self.assertEqual(len(templates), 3)

    async def test_move_no_overwrite_and_unicode_destination(self) -> None:
        revision = self.vault.revision("项目/需求.md")
        with self.assertRaises(ObsidianError):
            await self.service.move_note("项目/需求.md", "项目/关联.md", revision)
        await self.service.move_note("项目/需求.md", "归档/完成.md", revision)
        self.assertIn("归档/完成.md", self.vault.files)
        self.assertNotIn("项目/需求.md", self.vault.files)

    async def test_attachment_round_trip_and_duplicate_prevention(self) -> None:
        data = base64.b64encode(b"fake-image").decode()
        await self.service.create_attachment("附件/截图.png", data)
        self.assertEqual((await self.service.read_attachment("附件/截图.png"))["data_base64"], data)
        with self.assertRaises(ConflictError):
            await self.service.create_attachment("附件/截图.png", data)
        with self.assertRaises(ObsidianError):
            await self.service.create_attachment("附件/code.js", data)

    async def test_read_only_rejects_direct_writes_too(self) -> None:
        client = ObsidianClient(dataclasses.replace(self.settings, read_only=True), self.http)
        with self.assertRaisesRegex(ObsidianError, "只读"):
            await client.write_note("new.md", "body", None)

    async def test_backlinks_come_from_obsidian_cache(self) -> None:
        result = await self.service.read_backlinks("项目/需求.md")
        self.assertEqual(result["backlinks"], ["项目/关联.md"])

    async def test_network_errors_do_not_leak_secret_or_retry(self) -> None:
        calls = 0

        def timeout(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("test-secret", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            client = ObsidianClient(self.settings, http)
            with self.assertRaises(ObsidianError) as caught:
                await client.health()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(calls, 1)

    async def test_response_size_is_bounded(self) -> None:
        client = ObsidianClient(dataclasses.replace(self.settings, max_note_bytes=10), self.http)
        with self.assertRaisesRegex(ObsidianError, "大小"):
            await client.read_note("项目/需求.md")
