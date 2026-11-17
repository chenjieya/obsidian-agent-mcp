"""只为当前仓库启用版本化 hook，不覆盖用户已有的其他 hook 配置。"""

import stat
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", check=False)
    current = result.stdout.strip()
    if result.returncode not in {0, 1} or current not in {"", ".githooks"}:
        raise SystemExit("已有其他 hooksPath 或无法读取配置，未覆盖。请先检查现有 hook。")
    for name in ("pre-commit", "pre-push"):
        hook = root / ".githooks" / name
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if current == ".githooks":
        print("当前仓库已启用 pre-commit 和 pre-push；未暂存、提交或推送。")
        return
    enabled = subprocess.run(["git", "config", "--local", "core.hooksPath", ".githooks"], cwd=root,
                             capture_output=True, check=False)
    if enabled.returncode:
        raise SystemExit("无法写入当前仓库的 .git/config；请在有写入权限的本机终端重新运行此脚本。")
    print("当前仓库已启用 pre-commit 和 pre-push；未暂存、提交或推送。")


if __name__ == "__main__":
    main()
