"""将 agent 输入限制为 Vault 内的普通相对路径。"""

import re
from urllib.parse import quote

from .errors import ObsidianError


def vault_path(value: str, *, folder: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ObsidianError("路径必须为无首尾空格的字符串。")
    if folder and not value:
        return ""
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        raise ObsidianError("请使用 Vault 相对路径和 / 分隔符。")
    if folder:
        value = value.rstrip("/")
    parts = value.split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part.startswith(".")
                or part.rstrip(". ") != part
                or re.search(r'[\x00-\x1f\x7f:<>"|?*]', part)
                or re.search(r"%(?:2e|2f|5c|00)", part, re.IGNORECASE)
                or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.IGNORECASE)):
            raise ObsidianError("路径包含隐藏目录、保留名称或不安全片段。")
    return value


def note_path(value: str) -> str:
    value = vault_path(value)
    if not value.lower().endswith(".md"):
        raise ObsidianError("笔记路径必须以 .md 结尾。")
    return value


def endpoint(value: str, *, folder: bool = False) -> str:
    value = vault_path(value, folder=folder)
    return "/vault/" + quote(value, safe="/") + ("/" if folder and value else "")
