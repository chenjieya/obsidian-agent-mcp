"""下载固定版本官方二进制，校验归档 SHA-256，只写入项目 .tools 目录。"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    release = json.loads((root / "scripts/gitleaks-release.json").read_text(encoding="utf-8"))
    machine = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    extension = ".zip" if system == "windows" else ".tar.gz"
    archive_name = f"gitleaks_{release['version']}_{system}_{architecture}{extension}"
    expected = release["sha256"].get(archive_name)
    if expected is None:
        raise SystemExit("此平台没有固定校验值，请从官方发布页安装并校验对应版本。")
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{release['version']}/{archive_name}"
    print(f"下载官方 Gitleaks {release['version']}（仅本项目开发工具）…")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read(50 * 1024 * 1024 + 1)
    if len(payload) > 50 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != expected:
        raise SystemExit("下载文件的 SHA-256 不匹配，未安装或执行。")
    binary_name = "gitleaks.exe" if system == "windows" else "gitleaks"
    # 只取指定文件，不解包归档内的任意路径。
    if extension == ".zip":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            binary = archive.read(binary_name)
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = archive.getmember(binary_name)
            if not member.isfile():
                raise SystemExit("归档中的可执行文件类型不正确。")
            stream = archive.extractfile(member)
            if stream is None:
                raise SystemExit("归档中没有可执行文件。")
            with stream:
                binary = stream.read()
    destination = root / ".tools" / "gitleaks" / binary_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(binary)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result = subprocess.run([str(destination), "version"], capture_output=True, check=True)
    if result.stdout.decode("utf-8").strip().lstrip("v") != release["version"]:
        raise SystemExit("安装后版本验证失败。")
    print("安装完成，归档 SHA-256 与版本均已验证；没有修改全局 PATH。")


if __name__ == "__main__":
    main()
