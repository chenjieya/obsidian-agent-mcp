"""笔记业务语义；通过 REST API 操作，不直接读取用户 Vault 或配置。"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import quote

from .client import ObsidianClient, object_json, revision_header
from .errors import ConflictError, NotFoundError, ObsidianError
from .paths import endpoint, note_path, vault_path
from .templates import BUILTIN_TEMPLATES, TEMPLATE_INSTRUCTIONS, render_template


def window(limit: int, offset: int) -> None:
    if not 1 <= limit <= 100 or offset < 0:
        raise ObsidianError("limit 必须在 1～100，offset 必须非负。")


def check_revision(note: dict[str, object], expected: str) -> None:
    observed = note.get("revision")
    if not isinstance(observed, str) or not observed:
        raise ObsidianError("插件未提供笔记版本，不能安全更新，请升级 Local REST API。")
    if revision_header(observed) != revision_header(expected):
        raise ConflictError("版本已变化，请重新读取后修改。")


class NoteService:
    def __init__(self, client: ObsidianClient) -> None:
        self.client = client
        self.settings = client.settings
        vault_path(self.settings.template_folder.rstrip("/"))

    async def list_directory(self, folder: str = "") -> list[str]:
        folder = vault_path(folder, folder=True)
        data = object_json(await self.client.request("GET", endpoint(folder, folder=True)))
        files = data.get("files")
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise ObsidianError("目录接口返回了无效 files 列表。")
        result: list[str] = []
        for item in files:
            path = (folder + "/" if folder else "") + item
            try:
                vault_path(path, folder=item.endswith("/"))
            except ObsidianError:
                continue
            result.append(path)
        return sorted(result)

    async def markdown_files(self, folder: str = "", recursive: bool = True) -> list[str]:
        pending = [vault_path(folder, folder=True)]
        files: list[str] = []
        visited: set[str] = set()
        entries_seen = 0
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            entries = await self.list_directory(current)
            entries_seen += len(entries)
            if entries_seen > self.settings.max_scan_files:
                raise ObsidianError("扫描超过 OBSIDIAN_MAX_SCAN_FILES，请指定更小的目录。")
            for entry in entries:
                if entry.endswith("/") and recursive:
                    pending.append(entry.rstrip("/"))
                elif entry.lower().endswith(".md"):
                    files.append(entry)
        return sorted(files)

    async def list_notes(
        self, folder: str = "", recursive: bool = True, limit: int = 50, offset: int = 0,
    ) -> dict[str, object]:
        window(limit, offset)
        paths = await self.markdown_files(folder, recursive)
        return {"notes": paths[offset:offset + limit], "total": len(paths),
                "next_offset": offset + limit if offset + limit < len(paths) else None}

    async def search_notes(self, query: str, limit: int = 20) -> dict[str, object]:
        window(limit, 0)
        if not query.strip():
            raise ObsidianError("搜索内容不能为空。")
        response = await self.client.request(
            "POST", "/search/simple/", params={"query": query, "contextLength": 150,
                                               "maxResults": limit, "maxMatchesPerFile": 3},
        )
        try:
            results: object = response.json()
        except ValueError as exc:
            raise ObsidianError("搜索接口返回无效 JSON。") from exc
        if not isinstance(results, list):
            raise ObsidianError("搜索接口返回结构无效。")
        visible: list[dict[str, object]] = []
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                continue
            try:
                note_path(item["filename"])
            except ObsidianError:
                continue
            matches = item.get("matches", [])
            visible.append({"path": item["filename"], "score": item.get("score"),
                            "matches": matches[:3] if isinstance(matches, list) else []})
        return {"results": visible[:limit], "returned": min(len(visible), limit),
                "server_total": response.headers.get("total-results"),
                "limit": limit}

    async def read_note(self, path: str, include_template: bool = False) -> dict[str, object]:
        note = await self.client.read_note(path)
        if include_template:
            metadata = note.get("frontmatter")
            source = metadata.get("source_template") if isinstance(metadata, dict) else None
            if isinstance(source, str):
                note["source_template"] = await self.template(source)
        return note

    async def create_note(
        self, path: str, body: str, tags: list[str] | None = None,
        status: str | None = None, source_template: str | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if tags is not None:
            if any(not tag or any(c.isspace() for c in tag) or "#" in tag for tag in tags):
                raise ObsidianError("tags 使用不带 # 且无空白的标签名称。")
            metadata["tags"] = tags
        if status is not None:
            if status not in {"none", "active", "onHold", "completed", "dropped"}:
                raise ObsidianError("status 必须是 none、active、onHold、completed 或 dropped。")
            metadata["status"] = status
        if source_template:
            metadata["source_template"] = source_template
        if metadata:
            if body.lstrip("\ufeff\r\n ").startswith("---"):
                raise ObsidianError("body 已有 frontmatter；请直接在正文内填写属性，或移除后传入 tags/status。")
            # JSON 字符串及数组是合法 YAML 值，避免引入 YAML 写入依赖。
            header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}"
                               for key, value in metadata.items())
            body = f"---\n{header}\n---\n\n{body}"
        return await self.client.write_note(path, body, None)

    async def patch_note(
        self, path: str, revision: str, old_string: str, new_string: str,
    ) -> dict[str, object]:
        if not old_string:
            raise ObsidianError("old_string 不能为空；追加内容请使用 append_note。")
        note = await self.client.read_note(path)
        check_revision(note, revision)
        body = cast(str, note["body"])
        if body.count(old_string) != 1:
            raise ObsidianError("old_string 必须精确匹配一处，请增加上下文。")
        return await self.client.write_note(path, body.replace(old_string, new_string, 1), revision)

    async def append_note(self, path: str, revision: str, content: str) -> dict[str, object]:
        note = await self.client.read_note(path)
        check_revision(note, revision)
        return await self.client.write_note(path, cast(str, note["body"]) + content, revision)

    async def update_property(
        self, path: str, revision: str, key: str, value: str | list[str],
    ) -> dict[str, object]:
        """使用插件解析 YAML，保留未知属性；一次只更新一个字段。"""
        note_path(path)
        if key not in {"status", "tags"}:
            raise ObsidianError("当前仅支持修改 status、tags 属性。")
        if key == "status" and (not isinstance(value, str)
                                or value not in {"none", "active", "onHold", "completed", "dropped"}):
            raise ObsidianError("status 值无效。")
        if key == "tags" and (not isinstance(value, list)
                              or any(not tag or "#" in tag or any(c.isspace() for c in tag) for tag in value)):
            raise ObsidianError("tags 必须为不带 # 的标签名称列表。")
        await self.client.require_writes()
        response = await self.client.request(
            "PATCH", endpoint(path), headers={"If-Match": revision_header(revision)},
            json_body={"targetType": "frontmatter", "target": key,
                       "operation": "replace", "value": value, "createTargetIfMissing": True},
        )
        return {"path": path, "revision": response.headers.get("etag"), "property": key}

    async def read_backlinks(self, path: str) -> dict[str, object]:
        note = await self.client.read_note(path)
        backlinks = note.get("backlinks")
        if not isinstance(backlinks, list):
            raise ObsidianError("插件版本未提供反向链接，请升级 Local REST API。")
        paths: list[str] = []
        for item in backlinks:
            if isinstance(item, str):
                try:
                    paths.append(note_path(item))
                except ObsidianError:
                    continue
        return {"path": path, "backlinks": paths,
                "note": "来自 Obsidian 当前元数据缓存；启动后索引尚未完成时可能不完整。"}

    async def list_tags(self) -> dict[str, object]:
        return object_json(await self.client.request("GET", "/tags/"))

    async def template(self, template_id: str) -> dict[str, object]:
        if template_id.startswith("builtin:"):
            key = template_id.removeprefix("builtin:")
            if key not in BUILTIN_TEMPLATES:
                raise ObsidianError("内置模板不存在。")
            return {"id": template_id, "body": BUILTIN_TEMPLATES[key],
                    "instructions": TEMPLATE_INSTRUCTIONS}
        path = note_path(template_id)
        prefix = self.settings.template_folder.rstrip("/") + "/"
        if not path.startswith(prefix):
            raise ObsidianError("自定义模板必须位于 OBSIDIAN_TEMPLATE_FOLDER。")
        note = await self.client.read_note(path)
        return {"id": path, "body": note["body"], "frontmatter": note["frontmatter"],
                "instructions": TEMPLATE_INSTRUCTIONS}

    async def list_templates(self, keyword: str = "") -> dict[str, object]:
        items = [{"id": "builtin:" + key, "title": key, "source": "builtin"}
                 for key in BUILTIN_TEMPLATES]
        # 不存在的自定义模板目录按空目录处理；认证和连接错误必须显式返回。
        try:
            paths = await self.markdown_files(self.settings.template_folder)
        except NotFoundError:
            paths = []
        items = [{"id": path, "title": PurePosixPath(path).stem, "source": "vault"}
                 for path in paths] + items
        return {"templates": [item for item in items if keyword.casefold() in item["title"].casefold()]}

    async def create_from_template(
        self, path: str, template_id: str, title: str, requirements: str = "",
        variables: dict[str, str] | None = None,
    ) -> dict[str, object]:
        template = await self.template(template_id)
        body = render_template(cast(str, template["body"]),
                               {**(variables or {}), "title": title, "requirements": requirements,
                                "source_template": template_id})
        if body.startswith(("---\n", "---\r\n", "\ufeff---")):
            metadata = template.get("frontmatter", {})
            if isinstance(metadata, dict) and "source_template" in metadata:
                # 模板可自行用 source_template: "{{source_template}}" 声明来源。
                if metadata["source_template"] != "{{source_template}}":
                    raise ObsidianError("模板的 source_template 请使用 {{source_template}} 占位符。")
            else:
                body = re.sub(r"\A(\ufeff?---\r?\n)",
                              lambda match: match[1] + "source_template: "
                              + json.dumps(template_id, ensure_ascii=False) + "\n", body, count=1)
            result = await self.create_note(path, body)
        else:
            result = await self.create_note(path, body, status="none", source_template=template_id)
        result["template_linked"] = True
        result["instructions"] = TEMPLATE_INSTRUCTIONS
        return result

    async def move_note(self, path: str, destination: str, revision: str) -> dict[str, object]:
        path, destination = note_path(path), note_path(destination)
        if path == destination:
            raise ObsidianError("源路径和目标路径相同。")
        await self.client.require_writes()
        response = await self.client.request(
            "MOVE", endpoint(path), headers={"If-Match": revision_header(revision),
                "Destination": quote(destination, safe="/"), "Allow-Overwrite": "false"},
        )
        return {"path": destination, "previous_path": path,
                "revision": response.headers.get("etag")}

    async def create_attachment(self, path: str, data_base64: str) -> dict[str, object]:
        path = vault_path(path)
        if PurePosixPath(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}:
            raise ObsidianError("附件仅支持 PNG、JPEG、GIF、WebP、PDF。")
        if len(data_base64) > ((self.settings.max_attachment_bytes + 2) // 3) * 4:
            raise ObsidianError("附件超过大小限制。")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ObsidianError("附件必须是有效的纯 base64 字符串。") from exc
        if not data or len(data) > self.settings.max_attachment_bytes:
            raise ObsidianError("附件为空或超过大小限制。")
        await self.client.require_writes()
        await self.client.request("PUT", endpoint(path), content=data,
            headers={"If-None-Match": "*", "Content-Type": mimetypes.guess_type(path)[0] or "application/octet-stream"})
        return {"path": path, "size": len(data), "markdown": f"![]({quote(path, safe='/')})"}

    async def read_attachment(self, path: str) -> dict[str, object]:
        path = vault_path(path)
        if PurePosixPath(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}:
            raise ObsidianError("附件仅支持 PNG、JPEG、GIF、WebP、PDF。")
        response = await self.client.request("GET", endpoint(path), headers={"Accept": "*/*"},
            max_bytes=self.settings.max_attachment_bytes)
        return {"path": path, "size": len(response.content),
                "content_type": response.headers.get("content-type"),
                "data_base64": base64.b64encode(response.content).decode("ascii")}
