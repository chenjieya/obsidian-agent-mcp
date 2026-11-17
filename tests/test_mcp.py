from __future__ import annotations

import dataclasses
import os
import sys
import unittest
from pathlib import Path

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from obsidian_mcp.client import ObsidianClient
from obsidian_mcp.config import Settings
from obsidian_mcp.server import build_server
from obsidian_mcp.service import NoteService

from tests.fake_api import FakeVault


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.vault = FakeVault()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.vault.handle))
        self.settings = Settings(api_key="test-secret")
        self.service = NoteService(ObsidianClient(self.settings, self.http))

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_discovery_and_real_tool_call(self) -> None:
        async with Client(build_server(self.settings, self.service)) as client:
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            self.assertTrue({"read_note", "patch_note", "create_from_template", "health_check",
                             "read_backlinks", "move_note", "read_attachment"}.issubset(names))
            result = await client.call_tool("read_note", {"path": "项目/需求.md"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["path"], "项目/需求.md")
            self.assertIn("revision", result.structured_content)
            prompts = (await client.list_prompts()).prompts
            self.assertEqual({prompt.name for prompt in prompts}, {"fill_out_template", "implement_reviewed_plan"})
            prompt = await client.get_prompt("fill_out_template", {"path": "项目/需求.md"})
            self.assertIn("审阅", str(prompt.messages))
            resources = (await client.list_resources()).resources
            self.assertIn("obsidian-agent://workflow", {str(resource.uri) for resource in resources})

    async def test_tool_conflict_is_an_mcp_error_and_preserves_note(self) -> None:
        async with Client(build_server(self.settings, self.service)) as client:
            result = await client.call_tool("create_note", {"path": "项目/需求.md", "body": "覆盖"})
            self.assertTrue(result.is_error)
            self.assertIn("重新读取", str(result.content))
            self.assertIn("登录", self.vault.files["项目/需求.md"].decode())

    async def test_image_attachment_returns_native_mcp_image_block(self) -> None:
        self.vault.files["附件/截图.png"] = b"test-image"
        async with Client(build_server(self.settings, self.service)) as client:
            result = await client.call_tool("read_attachment", {"path": "附件/截图.png"})
            self.assertFalse(result.is_error)
            self.assertEqual([content.type for content in result.content], ["text", "image"])
            metadata_only = await client.call_tool("read_attachment", {
                "path": "附件/截图.png", "include_data": False})
            self.assertEqual([content.type for content in metadata_only.content], ["text"])

    async def test_read_only_omits_content_write_tools(self) -> None:
        settings = dataclasses.replace(self.settings, read_only=True)
        service = NoteService(ObsidianClient(settings, self.http))
        async with Client(build_server(settings, service)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertNotIn("create_note", names)
            self.assertNotIn("patch_note", names)
            self.assertNotIn("set_note_status", names)
            self.assertNotIn("open_note", names)
            self.assertIn("read_note", names)

    async def test_stdio_subprocess_discovers_without_accessing_real_vault(self) -> None:
        root = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "obsidian_mcp"],
            env={**os.environ, "PYTHONPATH": str(root / "src"),
                 "OBSIDIAN_API_KEY": "test-secret", "OBSIDIAN_READ_ONLY": "true"},
        )
        async with Client(parameters, mode="legacy", read_timeout_seconds=10) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertIn("read_note", names)
            self.assertNotIn("update_note", names)


if __name__ == "__main__":
    unittest.main()
