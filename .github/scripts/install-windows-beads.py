"""Install the pinned native Windows Beads CLI into the CI runner's temp directory."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

root = Path(os.environ["RUNNER_TEMP"]) / "keel-windows-beads"
root.mkdir(parents=True, exist_ok=True)
archive = root / "beads.zip"
subprocess.run(["curl.exe", "--proto", "=https", "--tlsv1.2", "--fail", "--location",
                "--silent", "--show-error", "--retry", "5", "--retry-delay", "5",
                "--connect-timeout", "20", "--max-time", "180",
                "-H", "Accept: application/octet-stream", "-H", "Cache-Control: no-cache",
                "--output", str(archive),
                "https://api.github.com/repos/gastownhall/beads/releases/assets/670845961"], check=True)
with archive.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if digest != "e59cb7742278185493e30c2eb97cb318db8d7a58c28ee5e88c2a30c7630c0975":
    raise SystemExit("Beads 1.3.2 Windows archive SHA-256 mismatch")
with zipfile.ZipFile(archive) as package:
    matches = [entry for entry in package.infolist() if Path(entry.filename).name == "bd.exe"]
    if len(matches) != 1:
        raise SystemExit("Expected exactly one bd.exe in the verified archive")
    with package.open(matches[0]) as source, (root / "bd.exe").open("wb") as target:
        shutil.copyfileobj(source, target)
with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as output:
    output.write(str(root) + "\n")
subprocess.run([str(root / "bd.exe"), "--version"], check=True)
