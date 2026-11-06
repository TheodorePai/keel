"""Copy owned applets to moi, stopping on local edits. Never install packages."""
from pathlib import Path
import hashlib
import json

FILES = {
    "home.tsx": "views/home.tsx",
    "home.server.ts": "views/home.server.ts",
    "bridge.ts": "lib/bridge.ts",
    "overview.tsx": "widgets/overview.tsx",
    "overview.server.ts": "widgets/overview.server.ts",
    "package.json": "package.json",
}


def install(root: Path) -> list[str]:
    source = Path(__file__).resolve().parent
    target = root / ".moi"
    if not target.is_dir() or target.is_symlink():
        raise ValueError("先使用 moi init 接入工作区；.moi 不能是符号链接")
    manifest = target / "keel-applets.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    contents = {dst: (source / src).read_bytes() for src, dst in FILES.items()}
    for dst, content in contents.items():
        path = target / dst
        if any(p.is_symlink() for p in [path, *path.parents] if p == target or target in p.parents):
            raise ValueError(f"拒绝通过符号链接安装：{dst}")
        if path.exists() and path.read_bytes() != content:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            # The first run replaces only moi's scaffold dependency declaration.
            scaffold = dst == "package.json" and dst not in previous and json.loads(path.read_text()).get("name") == "widgets"
            if not scaffold and previous.get(dst) != digest:
                raise ValueError(f"存在本地改动，未覆盖：{dst}")
    for dst, content in contents.items():
        path = target / dst
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest.write_text(json.dumps({dst: hashlib.sha256(data).hexdigest() for dst, data in contents.items()}, indent=2) + "\n")
    return list(contents)


if __name__ == "__main__":
    for file in install(Path.cwd()):
        print(f"installed {file}")
