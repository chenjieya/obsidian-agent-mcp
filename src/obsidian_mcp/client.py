"""异步 REST 客户端：认证、限流边界、条件写入和凭证脱敏集中处理。"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import httpx

from .config import Settings
from .errors import ConflictError, NotFoundError, ObsidianError
from .paths import endpoint, note_path

NOTE_JSON = "application/vnd.olrapi.note+json"


def object_json(response: httpx.Response) -> dict[str, object]:
    try:
        value: object = response.json()
    except ValueError as exc:
        raise ObsidianError("插件没有返回有效 JSON，请检查地址及插件版本。") from exc
    if not isinstance(value, dict):
        raise ObsidianError("插件返回的数据结构不是 JSON 对象。")
    return cast(dict[str, object], value)


def revision_header(revision: str) -> str:
    """只接受一次读取取得的单一强版本，不允许通配符绕过并发保护。"""
    value = revision.strip('"')
    if (not value or any(c in value for c in ',"*') or value.startswith("W/")
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise ObsidianError("revision 必须是 read_note 返回的单一版本号。")
    return '"' + value + '"'


class ObsidianClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self._conditional_writes: bool | None = None

    async def request(
        self, method: str, path: str, *, headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None, content: bytes | None = None,
        json_body: object = None, max_bytes: int | None = None,
    ) -> httpx.Response:
        """流式接收并施加响应大小上限；不自动重试写请求。"""
        limit = max_bytes or self.settings.max_note_bytes
        request_headers = {"Authorization": f"Bearer {self.settings.api_key}", **(headers or {})}
        try:
            async with self.http.stream(
                method, self.settings.api_url.rstrip("/") + path,
                headers=request_headers, params=params, content=content,
                **({"json": json_body} if json_body is not None else {}),
            ) as response:
                if response.status_code == 412:
                    raise ConflictError("笔记已变化或目标已存在；未执行覆盖，请重新读取后决定。")
                if response.status_code in {401, 403}:
                    raise ObsidianError("认证失败或路径访问被拒绝，请检查 API Key 及插件权限。")
                if response.status_code == 404:
                    raise NotFoundError("笔记、目录或接口不存在，请检查路径及插件版本。")
                if response.status_code == 429:
                    raise ObsidianError("插件请求受限，请稍后重试并检查认证配置。")
                if response.status_code >= 300:
                    raise ObsidianError(f"插件请求失败（HTTP {response.status_code}），未自动重试。")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise ObsidianError("插件响应超过配置的大小限制，请缩小查询范围。")
                return httpx.Response(
                    response.status_code, headers=response.headers,
                    content=bytes(chunks), request=response.request,
                )
        except httpx.TimeoutException as exc:
            raise ObsidianError("连接超时；写请求结果可能未知，请读取确认后再决定是否重试。") from exc
        except httpx.HTTPError as exc:
            raise ObsidianError(
                "无法连接 Obsidian，请确认应用和 Local REST API 已启用，并检查地址及证书。"
            ) from exc

    async def require_writes(self) -> None:
        if self.settings.read_only:
            raise ObsidianError("服务处于只读模式。")
        if self._conditional_writes is None:
            try:
                response = await self.request("GET", "/openapi.json", max_bytes=4 * 1024 * 1024)
            except NotFoundError as exc:
                raise ObsidianError("无法确认插件条件写入能力，请升级 Local REST API 后重启 MCP。") from exc
            spec = object_json(response)
            # 检查声明的请求头，不用描述文字推测是否支持条件写入。
            names: set[str] = set()

            def walk(value: object) -> None:
                if isinstance(value, dict):
                    if value.get("in") == "header" and isinstance(value.get("name"), str):
                        names.add(value["name"].lower())
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(spec)
            self._conditional_writes = {"if-match", "if-none-match"}.issubset(names)
        if not self._conditional_writes:
            raise ObsidianError(
                "插件未声明条件写入能力；读取可用，写入已禁用。请升级 Local REST API 后重启 MCP。"
            )

    async def health(self) -> dict[str, object]:
        data = object_json(await self.request("GET", "/"))
        if data.get("authenticated") is not True:
            raise ObsidianError("API Key 未通过插件认证。")
        versions = data.get("versions", {})
        return {"connected": True, "authenticated": True, "versions": versions,
                "read_only": self.settings.read_only, "transport": "stdio"}

    async def read_note(self, path: str | None = None) -> dict[str, object]:
        url = endpoint(note_path(path)) if path is not None else "/active/"
        data = object_json(await self.request("GET", url, headers={"Accept": NOTE_JSON}))
        actual_path, body = data.get("path"), data.get("content")
        if not isinstance(actual_path, str) or not isinstance(body, str):
            raise ObsidianError("笔记响应缺少 path/content，请检查插件版本。")
        note_path(actual_path)
        revision = data.get("version")
        # JSON 的 ETag 可能取决于其他笔记，因此只信任 version；缺失时读取纯文本 ETag。
        if not isinstance(revision, str) or not revision:
            raw = await self.request("GET", endpoint(actual_path), headers={"Accept": "text/markdown"})
            if raw.text != body:
                raise ConflictError("读取期间笔记发生变化，请重试。")
            revision = raw.headers.get("etag")
        return {
            "path": actual_path, "body": body, "revision": revision,
            "frontmatter": data.get("frontmatter", {}), "tags": data.get("tags", []),
            "stat": data.get("stat", {}), "links": data.get("links", []),
            "backlinks": data.get("backlinks"),
            "unresolved_links": data.get("unresolvedLinks", []),
        }

    async def write_note(self, path: str, body: str, revision: str | None) -> dict[str, object]:
        path = note_path(path)
        payload = body.encode("utf-8")
        if len(payload) > self.settings.max_note_bytes:
            raise ObsidianError("笔记超过 OBSIDIAN_MAX_NOTE_BYTES。")
        await self.require_writes()
        headers = {"Content-Type": "text/markdown; charset=utf-8"}
        headers.update({"If-Match": revision_header(revision)} if revision is not None
                       else {"If-None-Match": "*"})
        response = await self.request("PUT", endpoint(path), headers=headers, content=payload)
        return {"path": path, "revision": response.headers.get("etag"),
                "created": revision is None}


@asynccontextmanager
async def connect(settings: Settings) -> AsyncIterator[ObsidianClient]:
    verify: ssl.SSLContext | bool = settings.verify_ssl
    if settings.ca_cert:
        verify = ssl.create_default_context(cafile=settings.ca_cert)
    async with httpx.AsyncClient(
        verify=verify, timeout=settings.timeout, trust_env=False, follow_redirects=False,
    ) as http:
        yield ObsidianClient(settings, http)
