"""Install digest-verified native fnox/age test tools outside the checkout."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

root = Path(os.environ["RUNNER_TEMP"]) / "keel-windows-secrets"
root.mkdir(parents=True, exist_ok=True)
tools = [
    ("fnox", "jdx/fnox", 698005197,
     "1cdeb238348e1ceded8522296950ad4b5dba04221b462d64bf80662fae341960", ["fnox.exe"]),
    ("age", "FiloSottile/age", 535541937,
     "f48d8f8f9ebe903ab5027ed067652f2cc1db94bc206976430133b905dcd8e8c7", ["age.exe", "age-keygen.exe"]),
]
for name, repository, asset, expected, executables in tools:
    archive = root / f"{name}.zip"
    command = ["curl.exe", "--proto", "=https", "--tlsv1.2", "--fail", "--location",
                    "--silent", "--show-error", "--retry", "5", "--retry-delay", "5",
                    "--connect-timeout", "20", "--max-time", "180",
                    "-H", "Accept: application/octet-stream", "-H", "Cache-Control: no-cache",
                    "--output", str(archive)]
    api = f"https://api.github.com/repos/{repository}/releases/assets/{asset}"
    # Anonymous API requests can be rate-limited on shared hosted runners.
    # Both endpoints must produce the same pinned digest before any execution.
    release = ("https://github.com/jdx/fnox/releases/download/v1.46.0/fnox-x86_64-pc-windows-msvc.zip"
               if name == "fnox" else
               "https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-windows-amd64.zip")
    if subprocess.run([*command, api]).returncode:
        subprocess.run([*command, release], check=True)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise SystemExit(f"{name} Windows archive SHA-256 mismatch")
    with zipfile.ZipFile(archive) as package:
        for executable in executables:
            matches = [entry for entry in package.infolist() if Path(entry.filename).name == executable]
            if len(matches) != 1:
                raise SystemExit(f"Expected exactly one {executable} in the verified archive")
            with package.open(matches[0]) as source, (root / executable).open("wb") as target:
                shutil.copyfileobj(source, target)
with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as output:
    output.write(str(root) + "\n")
subprocess.run([str(root / "fnox.exe"), "--version"], check=True)
subprocess.run([str(root / "age-keygen.exe"), "--version"], check=True)
