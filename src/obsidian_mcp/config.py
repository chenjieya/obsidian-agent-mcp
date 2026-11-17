"""显式读取配置；不扫描 Obsidian 配置、不自动获取用户密钥。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ObsidianError


def load_env_file(path: Path) -> None:
    """读取简单 KEY=value 文件；已有环境变量优先，不执行插值或脚本。"""
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().isidentifier():
            raise ObsidianError(f"配置文件第 {number} 行不是有效的 KEY=value。")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ObsidianError(f"配置文件第 {number} 行引号未闭合。")
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(default)).lower().strip()
    if value not in {"true", "false"}:
        raise ObsidianError(f"{name} 只能设为 true 或 false。")
    return value == "true"


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ObsidianError(f"{name} 必须是正整数。") from exc
    if value <= 0:
        raise ObsidianError(f"{name} 必须是正整数。")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    api_url: str = "https://127.0.0.1:27124"
    ca_cert: str | None = None
    verify_ssl: bool = True
    timeout: float = 20
    template_folder: str = "Templates"
    max_note_bytes: int = 2 * 1024 * 1024
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_scan_files: int = 2000
    read_only: bool = False

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.api_url)
            url.port
        except ValueError as exc:
            raise ObsidianError("OBSIDIAN_API_URL 地址或端口无效。") from exc
        if (url.scheme not in {"http", "https"}
                or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username or url.password or url.query or url.fragment
                or url.path not in {"", "/"}):
            raise ObsidianError("OBSIDIAN_API_URL 必须是本机 HTTP(S) 根地址。")
        if not self.api_key or self.api_key == "replace-with-your-api-key":
            raise ObsidianError("请配置 OBSIDIAN_API_KEY，从 Local REST API 插件设置获取。")
        if any(ord(c) < 32 or ord(c) > 126 for c in self.api_key):
            raise ObsidianError("OBSIDIAN_API_KEY 包含无效字符。")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ObsidianError("OBSIDIAN_TIMEOUT 必须是有限正数。")
        if min(self.max_note_bytes, self.max_attachment_bytes, self.max_scan_files) <= 0:
            raise ObsidianError("文件大小与扫描上限必须为正数。")
        if self.ca_cert and not Path(self.ca_cert).is_file():
            raise ObsidianError("OBSIDIAN_CA_CERT 指向的证书文件不存在。")

    @classmethod
    def from_env(cls) -> Settings:
        try:
            timeout = float(os.environ.get("OBSIDIAN_TIMEOUT", "20"))
        except ValueError as exc:
            raise ObsidianError("OBSIDIAN_TIMEOUT 必须是数字。") from exc
        return cls(
            api_key=os.environ.get("OBSIDIAN_API_KEY", "").strip(),
            api_url=os.environ.get("OBSIDIAN_API_URL", "https://127.0.0.1:27124").rstrip("/"),
            ca_cert=os.environ.get("OBSIDIAN_CA_CERT") or None,
            verify_ssl=_boolean("OBSIDIAN_VERIFY_SSL", True),
            timeout=timeout,
            template_folder=os.environ.get("OBSIDIAN_TEMPLATE_FOLDER", "Templates"),
            max_note_bytes=_positive_int("OBSIDIAN_MAX_NOTE_BYTES", 2 * 1024 * 1024),
            max_attachment_bytes=_positive_int("OBSIDIAN_MAX_ATTACHMENT_BYTES", 10 * 1024 * 1024),
            max_scan_files=_positive_int("OBSIDIAN_MAX_SCAN_FILES", 2000),
            read_only=_boolean("OBSIDIAN_READ_ONLY", False),
        )
