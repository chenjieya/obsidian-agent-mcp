"""python -m obsidian_mcp [--env-file ...] [--check]。"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .client import connect
from .config import Settings, load_env_file
from .errors import ObsidianError
from .server import build_server


async def check(settings: Settings) -> None:
    async with connect(settings) as client:
        result = await client.health()
        if not settings.read_only:
            try:
                await client.require_writes()
                result["conditional_writes"] = True
            except ObsidianError as exc:
                result["conditional_writes"] = False
                result["write_warning"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Obsidian 笔记协作 MCP Server（stdio）")
    parser.add_argument("--env-file", type=Path, help="显式指定配置文件；环境变量优先")
    parser.add_argument("--check", action="store_true", help="只检查连接和写入能力，不启动 MCP、不修改笔记")
    args = parser.parse_args()
    # SDK 使用 stderr；不允许第三方 HTTP 日志意外输出到 stdio 或打印凭证。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.env_file:
            load_env_file(args.env_file)
        settings = Settings.from_env()
        if args.check:
            asyncio.run(check(settings))
        else:
            build_server(settings).run(transport="stdio")
    except (ObsidianError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
