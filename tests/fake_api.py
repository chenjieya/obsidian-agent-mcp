"""模拟公开 REST 契约，不替代真实插件版本兼容性验证。"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import unquote

import httpx


class FakeVault:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"项目/需求.md": "# 需求\n实现登录\n".encode(),
            "项目/关联.md": "[[需求]]".encode()}
        self.requests: list[httpx.Request] = []
        self.conditional = True
        self.opened: str | None = None
        self.before_put: tuple[str, bytes] | None = None

    def revision(self, path: str) -> str:
        return hashlib.sha256(self.files[path]).hexdigest()

    def metadata(self, path: str) -> dict[str, object]:
        body = self.files[path].decode()
        frontmatter: dict[str, object] = {}
        if body.startswith("---\n"):
            for line in body.split("---\n", 2)[1].splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    try:
                        frontmatter[key.strip()] = json.loads(value)
                    except ValueError:
                        frontmatter[key.strip()] = value.strip()
        return {"path": path, "content": body, "version": self.revision(path),
                "frontmatter": frontmatter, "tags": frontmatter.get("tags", []),
                "stat": {"ctime": 1, "mtime": 2, "size": len(self.files[path])},
                "links": [], "backlinks": ["项目/关联.md"] if path == "项目/需求.md" else [],
                "unresolvedLinks": []}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer test-secret":
            return httpx.Response(401)
        url = request.url.path
        if url == "/":
            return httpx.Response(200, json={"authenticated": True, "versions": {"plugin": "test"}})
        if url == "/openapi.json":
            parameters = [{"in": "header", "name": name} for name in
                          ("If-Match", "If-None-Match")] if self.conditional else []
            return httpx.Response(200, json={"paths": {"/vault/{filename}": {
                "put": {"parameters": parameters}}}})
        if url == "/tags/":
            return httpx.Response(200, json={"tags": [{"name": "project", "count": 1}]})
        if url == "/search/simple/":
            query = request.url.params["query"]
            results = [{"filename": path, "score": 1, "matches": [{"context": data.decode(),
                        "match": {"start": 0, "end": 1}}]}
                       for path, data in self.files.items() if path.endswith(".md") and query in data.decode()]
            return httpx.Response(200, json=results, headers={"Total-Results": str(len(results))})
        if url.startswith("/open/"):
            self.opened = url.removeprefix("/open/")
            return httpx.Response(204)
        if url == "/active/":
            return httpx.Response(200, json=self.metadata("项目/需求.md"))
        if not url.startswith("/vault/"):
            return httpx.Response(404)
        path = url.removeprefix("/vault/")
        if url.endswith("/"):
            prefix = path
            entries = {name.removeprefix(prefix).split("/", 1)[0]
                       + ("/" if "/" in name.removeprefix(prefix) else "")
                       for name in self.files if name.startswith(prefix)}
            return httpx.Response(200, json={"files": sorted(entries)}) if entries or not prefix else httpx.Response(404)
        if request.method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            if request.headers.get("accept") == "application/vnd.olrapi.note+json":
                return httpx.Response(200, json=self.metadata(path), headers={"ETag": '"representation-etag"'})
            return httpx.Response(200, content=self.files[path], headers={"ETag": f'"{self.revision(path)}"'})
        if self.before_put and request.method == "PUT":
            changed_path, data = self.before_put
            self.files[changed_path] = data
            self.before_put = None
        if request.headers.get("if-none-match") == "*" and path in self.files:
            return httpx.Response(412)
        expected = request.headers.get("if-match")
        if expected and (path not in self.files or expected != f'"{self.revision(path)}"'):
            return httpx.Response(412)
        if request.method == "PUT":
            self.files[path] = request.content
        elif request.method == "MOVE":
            destination = unquote(request.headers["destination"])
            if destination in self.files:
                return httpx.Response(409)
            self.files[destination] = self.files.pop(path)
            path = destination
        elif request.method == "PATCH":
            instruction = json.loads(request.content)
            metadata = self.metadata(path)
            properties = metadata["frontmatter"]
            assert isinstance(properties, dict)
            properties[instruction["target"]] = instruction["value"]
            body = self.files[path].decode()
            if body.startswith("---\n"):
                body = body.split("---\n", 2)[2].lstrip("\n")
            header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in properties.items())
            self.files[path] = f"---\n{header}\n---\n\n{body}".encode()
        else:
            return httpx.Response(405)
        return httpx.Response(204, headers={"ETag": f'"{self.revision(path)}"'})
