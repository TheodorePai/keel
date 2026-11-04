"""大文件资产：清单 assets.toml 登记逻辑资产，内容放 HF / GitHub Releases / 原始来源 / 本机。

一条清单对应一个逻辑资产（一个模型、一个数据集版本、一个交付件），不论包含多少文件。
Keel 不保存令牌：认证交给 hf、gh 自己管理，这里只调用它们。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "assets.toml"
PERSONAL_CONFIG = Path(os.environ.get("KEEL_CONFIG", Path.home() / ".config" / "keel" / "config.toml"))
KINDS = {
    "model": "上传到 HF 模型仓库",
    "dataset": "上传到 HF 数据集仓库",
    "deliverable": "上传到 GitHub Releases（单个文件）",
    "public": "不上传，从 --source 原始地址下载",
    "generated": "不上传，用 --command 重新生成",
    "local": "不上传，只在本机，靠备份恢复",
}
HF_FOLDER_LIMIT = 10_000   # HF：单个目录文件数上限
HF_REPO_FILES = 100_000    # HF：单仓库建议上限
PACK_HINT = 10_000         # 超过即建议合并成 Parquet / WebDataset 分片


class AssetError(SystemExit):
    pass


# ---------- 配置 ----------

def dumps_toml(tables: dict[str, dict], prefix: str = "") -> str:
    """只写本模块用到的 TOML：顶层标量与一层表，值为 str/int/bool。"""
    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return json.dumps(str(v), ensure_ascii=False)
    lines = [f"{k} = {value(v)}" for k, v in tables.items() if not isinstance(v, dict)]
    for name, table in tables.items():
        if isinstance(table, dict):
            lines += ["", f"[{prefix}{name}]"] + [f"{k} = {value(v)}" for k, v in table.items() if v is not None]
    return "\n".join(lines).lstrip("\n") + "\n"


def run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co")


def hf_token() -> str | None:
    """读取 hf CLI 已保存的登录（与 huggingface_hub 相同的位置），Keel 自己不保存。"""
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    path = Path(os.environ.get("HF_TOKEN_PATH", home / "token"))
    return path.read_text().strip() or None if path.exists() else None


def hf_api(path: str):
    """调用 HF REST API，返回 (JSON, 下一页 URL)；404 返回 (None, None)。"""
    url = path if path.startswith("http") else f"{HF_ENDPOINT}/api/{path}"
    token = hf_token()
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
            return json.loads(r.read()), nxt.group(1) if nxt else None
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            return None, None
        raise AssetError(f"HF API {url} 返回 {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise AssetError(f"无法连接 HF（{getattr(e, 'reason', e)}）；离线时笔记与检查照常可用")


def hf_cli(*args: str) -> subprocess.CompletedProcess:
    if not shutil.which("hf"):
        raise AssetError("需要 hf 命令：uv tool install huggingface_hub，然后 hf auth login")
    out = run("hf", *args, check=False)
    if out.returncode != 0:
        raise AssetError(f"hf {' '.join(args[:2])} 失败：{(out.stderr or out.stdout).strip()[-500:]}")
    return out


def hf_whoami() -> str | None:
    try:
        data, _ = hf_api("whoami-v2") if hf_token() else (None, None)
    except AssetError:
        return None
    return data.get("name") if data else None


def gh_login() -> str | None:
    if not shutil.which("gh"):
        return None
    out = run("gh", "api", "user", "-q", ".login", check=False)
    return out.stdout.strip() or None


def load_personal() -> dict:
    return tomllib.loads(PERSONAL_CONFIG.read_text()) if PERSONAL_CONFIG.exists() else {}


def ensure_personal() -> tuple[dict, list[str]]:
    """个人配置只记账号名与本机路径，不记令牌。缺失时从当前 hf / gh 登录生成。"""
    if PERSONAL_CONFIG.exists():
        return load_personal(), []
    data = {"hf_namespace": hf_whoami() or "", "gh_owner": gh_login() or "",
            "local_backup": "Time Machine"}
    PERSONAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    PERSONAL_CONFIG.write_text("# Keel 个人配置：账号名与本机设置，不放令牌（令牌由 hf / gh 自己管理）\n"
                               + dumps_toml(data))
    return data, [f"写入个人配置 {PERSONAL_CONFIG}（hf={data['hf_namespace'] or '未登录'}，"
                  f"gh={data['gh_owner'] or '未登录'}）"]


def project_slug(root: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"


def default_storage(root: Path, personal: dict) -> dict:
    slug, hf, gh = project_slug(root), personal.get("hf_namespace"), personal.get("gh_owner")
    storage = {}
    if hf:
        storage["model"] = f"hf://models/{hf}/{slug}-models"
        storage["dataset"] = f"hf://datasets/{hf}/{slug}-datasets"
    if gh:
        storage["deliverable"] = f"gh://{gh}/{slug}"
    return storage


def ensure_storage(root: Path, config_path: Path) -> list[str]:
    personal, done = ensure_personal()
    config = tomllib.loads(config_path.read_text())
    if "storage" in config:
        return done
    storage = default_storage(root, personal)
    text = config_path.read_text().rstrip("\n")
    text += ("\n\n# 大文件存放位置（keel asset add 按 kind 选择）。账号名来自 ~/.config/keel/config.toml\n"
             + dumps_toml({"storage": storage}))
    config_path.write_text(text)
    return done + [f"写入 [storage]：{', '.join(f'{k}={v}' for k, v in storage.items()) or '无（hf/gh 均未登录）'}"]


# ---------- 清单 ----------

@dataclass
class Store:
    root: Path
    config: dict

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def load(self) -> dict[str, dict]:
        if not self.manifest_path.exists():
            return {}
        return tomllib.loads(self.manifest_path.read_text()).get("assets", {})

    def save(self, assets: dict[str, dict]) -> None:
        header = ("# Keel 资产清单：一条 = 一个逻辑资产（模型 / 数据集版本 / 交付件），由 keel asset add 维护。\n"
                  "# 内容不进 git；新机器运行 keel fetch 取回并校验。\n\n")
        self.manifest_path.write_text(header + dumps_toml(dict(sorted(assets.items())), "assets."))

    def storage(self, kind: str) -> str:
        target = self.config.get("storage", {}).get(kind)
        if not target:
            raise AssetError(f"keel.toml 的 [storage] 没有配置 {kind}；运行 keel doctor 查看")
        return target


def parse_hf(uri: str) -> tuple[str, str, str]:
    """hf://models/ns/repo[/sub/path] → (repo_type, repo_id, sub)。"""
    m = re.match(r"^hf://(models|datasets)/([^/]+/[^/@]+)(?:/(.*))?$", uri)
    if not m:
        raise AssetError(f"无法解析 HF 地址：{uri}")
    return m.group(1)[:-1], m.group(2), m.group(3) or ""


def parse_gh(uri: str) -> tuple[str, str | None, str | None]:
    """gh://owner/repo[@tag/file] → (repo, tag, file)。"""
    m = re.match(r"^gh://([^/]+/[^/@]+)(?:@(.+)/([^/]+))?$", uri)
    if not m:
        raise AssetError(f"无法解析 GitHub 地址：{uri}")
    return m.group(1), m.group(2), m.group(3)


# ---------- 摘要 ----------

def iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.name != ".DS_Store")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(path: Path) -> tuple[str, int, int]:
    """单文件：文件 SHA-256；目录：按相对路径排序的“路径\\0哈希”清单再取 SHA-256。"""
    files = iter_files(path)
    if path.is_file():
        return "sha256:" + sha256_file(path), 1, path.stat().st_size
    h = hashlib.sha256()
    for f in files:
        h.update(f"{f.relative_to(path).as_posix()}\0{sha256_file(f)}\n".encode())
    return "sha256:" + h.hexdigest(), len(files), sum(f.stat().st_size for f in files)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


# ---------- 后端（测试可替换） ----------

def hf_upload(repo_type: str, repo_id: str, local: Path, path_in_repo: str, message: str) -> str:
    hf_cli("repo", "create", repo_id, "--repo-type", repo_type, "--private", "--exist-ok")
    target = path_in_repo if local.is_dir() else f"{path_in_repo}/{local.name}"
    hf_cli("upload", repo_id, str(local), target, "--repo-type", repo_type, "--quiet",
           "--commit-message", message, "--exclude", ".DS_Store",
           *(["--delete", f"{path_in_repo}/**"] if local.is_dir() else []))
    data, _ = hf_api(f"{repo_type}s/{repo_id}/revision/main")
    if not data:
        raise AssetError(f"上传后读取 {repo_id} 的提交失败")
    return data["sha"]


def hf_download(repo_type: str, repo_id: str, revision: str, sub: str, dest: Path) -> Path:
    hf_cli("download", repo_id, "--repo-type", repo_type, "--revision", revision, "--quiet",
           "--local-dir", str(dest), *(["--include", f"{sub}/**"] if sub else []))
    shutil.rmtree(dest / ".cache", ignore_errors=True)  # hf 在 local-dir 下留的元数据
    return dest / sub if sub else dest


def hf_remote_files(repo_type: str, repo_id: str, revision: str, sub: str) -> dict[str, tuple[str, str]]:
    """远端文件 → (算法, 值)：LFS 文件给 sha256，普通文件给 git blob sha1。"""
    out = {}
    url = f"{repo_type}s/{repo_id}/tree/{revision}/{sub}?recursive=true&expand=true"
    while url:
        items, url = hf_api(url)
        for item in items or []:
            if item.get("type") != "file":
                continue
            rel = item["path"][len(sub) + 1:] if sub else item["path"]
            lfs = item.get("lfs")
            out[rel] = ("sha256", lfs["oid"]) if lfs else ("blob", item["oid"])
    return out


def gh_upload(repo: str, tag: str, file: Path) -> None:
    if run("gh", "release", "view", tag, "-R", repo, check=False).returncode != 0:
        run("gh", "release", "create", tag, "-R", repo, "--title", tag, "--notes", "Keel 资产，由 keel asset add 上传")
    run("gh", "release", "upload", tag, str(file), "-R", repo, "--clobber")


def gh_download(repo: str, tag: str, filename: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    run("gh", "release", "download", tag, "-R", repo, "-p", filename, "-D", str(dest), "--clobber")
    return dest / filename


def http_download(url: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / (Path(url.split("?")[0]).name or "download")
    with urllib.request.urlopen(url) as r, target.open("wb") as f:
        shutil.copyfileobj(r, f)
    return target


# ---------- 命令 ----------

def git_tracked(root: Path, rel: str) -> list[str]:
    return run("git", "ls-files", "--", rel, cwd=root, check=False).stdout.split()


def ensure_gitignored(root: Path, rel: str) -> None:
    ignore = root / ".gitignore"
    line = "/" + rel.rstrip("/") + ("/" if (root / rel).is_dir() else "")
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if line not in lines:
        if "# Keel 资产（内容见 assets.toml）" not in lines:
            lines += ["", "# Keel 资产（内容见 assets.toml）"]
        ignore.write_text("\n".join([*lines, line]).lstrip("\n") + "\n")


def guard_file_counts(path: Path) -> list[str]:
    if path.is_file():
        return []
    files = iter_files(path)
    per_dir = Counter(f.parent for f in files)
    worst = max(per_dir.values(), default=0)
    if worst > HF_FOLDER_LIMIT or len(files) > HF_REPO_FILES:
        raise AssetError(f"{len(files)} 个文件（单目录最多 {worst} 个）超过 HF 限制"
                         f"（单目录 {HF_FOLDER_LIMIT}、单仓库建议 {HF_REPO_FILES}）。"
                         "先合并成 Parquet / WebDataset 分片再登记。")
    if len(files) > PACK_HINT:
        return [f"提醒：{len(files)} 个文件，建议合并成少量 Parquet / WebDataset 分片，上传下载更快"]
    return []


def add(store: Store, rel: str, kind: str, name: str | None = None, source: str | None = None,
        revision: str | None = None, command: str | None = None, shareable: bool = True) -> list[str]:
    if kind not in KINDS:
        raise AssetError(f"未知 kind {kind}；可选：{', '.join(KINDS)}")
    path = (store.root / rel).resolve()
    rel = path.relative_to(store.root.resolve()).as_posix()
    if not path.exists():
        raise AssetError(f"不存在：{rel}")
    name = name or re.sub(r"[^a-z0-9-]+", "-", path.stem.lower()).strip("-")
    tracked = git_tracked(store.root, rel)
    if tracked:
        raise AssetError(f"{rel} 已在 git 里（{len(tracked)} 个文件）。先 git rm -r --cached {rel}，再登记；"
                         "已进历史的副本不会因此消失，需要时另行重写历史")
    notes = guard_file_counts(path)
    if kind == "deliverable" and path.is_dir():
        raise AssetError("deliverable 只支持单个文件；目录请先打包成 zip")
    if kind == "public" and not source:
        raise AssetError("public 需要 --source（https://… 或 hf://models/<repo>）")
    if kind == "generated" and not command:
        raise AssetError("generated 需要 --command（重新生成的命令）")

    value, files, size = digest(path)
    entry = {"kind": kind, "path": rel, "path_type": "directory" if path.is_dir() else "file",
             "source": source, "revision": revision, "digest": value,
             "files": files, "bytes": size, "shareable": shareable, "command": command}
    if kind in ("model", "dataset"):
        repo_type, repo_id, _ = parse_hf(store.storage(kind))
        # HF 不保存空目录；空资产可完全由清单重建，无需上传。
        entry["revision"] = hf_upload(repo_type, repo_id, path, name, f"keel asset add {name}") if files else None
        entry["source"] = f"hf://{repo_type}s/{repo_id}/{name}"
    elif kind == "deliverable":
        repo, _, _ = parse_gh(store.storage(kind))
        tag = f"asset-{name}-{value[7:19]}"
        gh_upload(repo, tag, path)
        entry["source"], entry["revision"] = f"gh://{repo}@{tag}/{path.name}", tag
    elif kind == "local":
        entry["source"] = "local"
    ensure_gitignored(store.root, rel)
    assets = store.load()
    assets[name] = entry
    store.save(assets)
    return notes + [f"{name}: {kind}，{files} 个文件，{human(size)}，{value[:19]}…"
                    + (f"，{entry['source']}@{str(entry['revision'])[:12]}" if entry.get("revision") else "")]


def place(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def fetch_one(store: Store, name: str, entry: dict) -> tuple[str, str]:
    path = store.root / entry["path"]
    if path.exists() and digest(path)[0] == entry["digest"]:
        return "ok", "已存在且校验一致"
    kind, source = entry["kind"], entry.get("source") or ""
    if kind == "generated":
        return "todo", f"需要重新生成：{entry['command']}"
    if kind == "local":
        return ("mismatch", "本机内容与清单不一致") if path.exists() else ("missing", "本机缺失，从备份恢复")
    with tempfile.TemporaryDirectory(dir=store.root, prefix=".keel-fetch-") as tmp:
        tmp = Path(tmp)
        path_type = entry.get("path_type")
        if entry["files"] == 0 and path_type != "file":
            got = tmp / "empty"
            got.mkdir()
        elif source.startswith("hf://"):
            repo_type, repo_id, sub = parse_hf(source)
            got = hf_download(repo_type, repo_id, entry.get("revision") or "main", sub, tmp)
            if got.is_dir() and entry["files"] == 1:
                # 老清单没有 path_type：先校验目录摘要，再尝试单文件摘要。
                # 不能仅凭 files == 1 将一个数据集目录当成文件。
                if path_type == "file" or (path_type is None and digest(got)[0] != entry["digest"]):
                    candidates = iter_files(got)
                    if len(candidates) == 1:
                        got = candidates[0]
        elif source.startswith("gh://"):
            repo, tag, filename = parse_gh(source)
            got = gh_download(repo, tag, filename, tmp)
        elif source.startswith(("https://", "http://")):
            got = http_download(source, tmp)
        else:
            return "error", f"无法从 {source!r} 取回"
        actual = digest(got)[0]
        if actual != entry["digest"]:
            return "mismatch", f"下载内容摘要 {actual[:19]}… 与清单不一致，未覆盖本地"
        place(got, path)
    return "fetched", f"已取回 {entry['path']}"


def fetch(store: Store, names: list[str]) -> list[tuple[str, str, str]]:
    assets = store.load()
    unknown = [n for n in names if n not in assets]
    if unknown:
        raise AssetError(f"清单中没有：{', '.join(unknown)}")
    return [(n, *fetch_one(store, n, assets[n])) for n in (names or assets)]


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def verify(store: Store, name: str) -> list[str]:
    assets = store.load()
    if name not in assets:
        raise AssetError(f"清单中没有：{name}")
    entry = assets[name]
    path = store.root / entry["path"]
    if not path.exists():
        return [f"{name}: 本地不存在，运行 keel fetch {name}"]
    value, files, size = digest(path)
    if value == entry["digest"]:
        return [f"{name}: 一致（{files} 个文件，{human(size)}）"]
    out = [f"{name}: 不一致；清单 {entry['files']} 个文件 {human(entry['bytes'])}，本地 {files} 个 {human(size)}"]
    source = entry.get("source") or ""
    if source.startswith("hf://") and path.is_dir():
        repo_type, repo_id, sub = parse_hf(source)
        remote = hf_remote_files(repo_type, repo_id, entry["revision"], sub) if entry["files"] else {}
        local = {f.relative_to(path).as_posix(): f for f in iter_files(path)}
        for rel in sorted(remote.keys() - local.keys()):
            out.append(f"  缺少 {rel}")
        for rel in sorted(local.keys() - remote.keys()):
            out.append(f"  多出 {rel}")
        for rel in sorted(local.keys() & remote.keys()):
            algo, want = remote[rel]
            have = sha256_file(local[rel]) if algo == "sha256" else git_blob_sha1(local[rel])
            if have != want:
                out.append(f"  改动 {rel}")
    return out


def checks(root: Path, config: dict) -> list[tuple[str, str, str, str]]:
    """供 keel check 使用：(path, rule, message, level)。只看 git 状态与清单，不重算大文件摘要。"""
    store = Store(root, config)
    out = []
    try:
        assets = store.load()
    except tomllib.TOMLDecodeError as e:
        return [(MANIFEST_NAME, "asset-manifest", f"无法解析：{e}", "error")]
    for name, entry in assets.items():
        rel = entry.get("path", "")
        if entry.get("kind") not in KINDS or not entry.get("digest") or not rel:
            out.append((MANIFEST_NAME, "asset-manifest", f"{name} 缺少 kind / path / digest", "error"))
            continue
        if entry.get("path_type") not in (None, "file", "directory"):
            out.append((MANIFEST_NAME, "asset-manifest", f"{name} 的 path_type 必须是 file 或 directory", "error"))
        if git_tracked(root, rel):
            out.append((rel, "asset-tracked", f"资产 {name} 的内容在 git 里；git rm -r --cached {rel}", "error"))
        elif run("git", "check-ignore", "-q", rel, cwd=root, check=False).returncode != 0:
            out.append((rel, "asset-ignore", f"资产 {name} 的路径未被 .gitignore 忽略", "error"))
    limit = float(config.get("asset_max_mb", 5)) * 1024 * 1024
    listed = run("git", "ls-files", "-z", "--cached", cwd=root, check=False).stdout.split("\0")
    for rel in filter(None, listed):
        p = root / rel
        if p.is_file() and p.stat().st_size > limit:
            out.append((rel, "large-file", f"{human(p.stat().st_size)} 超过 {limit / 1048576:g} MB；"
                        "用 keel asset add 登记，并 git rm --cached", "error"))
    return out


def doctor(root: Path, config: dict) -> list[tuple[bool, str]]:
    lines: list[tuple[bool, str]] = []
    for tool in ("bd", "hf", "gh", "git"):
        lines.append((bool(shutil.which(tool)), f"{tool} 已安装"))
    gh = gh_login()
    hf = None
    if not hf_token():
        lines.append((False, "HF 未登录：运行 hf auth login"))
    else:
        try:
            data, _ = hf_api("whoami-v2")
            hf = data.get("name") if data else None
            lines.append((bool(hf), f"HF 已登录：{hf}" if hf else "HF 登录已失效：运行 hf auth login"))
        except AssetError as e:
            lines.append((False, str(e)))
    lines.append((bool(gh), f"GitHub 已登录：{gh}" if gh else "GitHub 未登录：运行 gh auth login"))
    lines.append((PERSONAL_CONFIG.exists(), f"个人配置 {PERSONAL_CONFIG}"))
    storage = config.get("storage", {})
    lines.append((bool(storage), "keel.toml 有 [storage]" if storage else "keel.toml 缺少 [storage]：运行 keel init"))
    if storage and hf:
        for kind in ("model", "dataset"):
            if kind in storage:
                repo_type, repo_id, _ = parse_hf(storage[kind])
                try:
                    info, _ = hf_api(f"{repo_type}s/{repo_id}")
                except AssetError as e:
                    lines.append((False, f"{kind}: {e}"))
                    continue
                if info:
                    private = info.get("private")
                    lines.append((bool(private), f"{kind}: {repo_id} 存在，{'私有' if private else '公开！'}"))
                else:
                    lines.append((True, f"{kind}: {repo_id} 尚未创建，首次 keel asset add 时自动创建（私有）"))
    if storage.get("deliverable") and gh:
        repo, _, _ = parse_gh(storage["deliverable"])
        ok = run("gh", "repo", "view", repo, check=False).returncode == 0
        lines.append((ok, f"deliverable: {repo} {'可访问' if ok else '不存在或无权限'}"))
    store = Store(root, config)
    assets = store.load()
    present = sum((root / e["path"]).exists() for e in assets.values())
    lines.append((present == len(assets), f"清单 {len(assets)} 个资产，本机已有 {present} 个"
                  + ("" if present == len(assets) else "；运行 keel fetch")))
    return lines
