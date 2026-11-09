"""密钥：每把只存一份（fnox 全局配置，age 加密），项目按需声明，运行时 fnox exec 注入。

Keel 不保存、不显示密钥值。分工：
- 全局 `~/.config/fnox/config.toml`：共享密钥 `SHARED_<NAME>`，`env = false`，默认不注入任何项目。
- 项目 `keel.toml`：`secrets = ["NAME", ...]` 声明需要哪些。
- 项目 `fnox.toml`：keel init 生成 `NAME = { default = "${SHARED_NAME}", if_missing = "error" }`，可提交。
设计与局限见报告 2026-11-03-fnox-shared-secrets。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from keel import _secret_permissions

PREFIX = "SHARED_"
FNOX_FILE = "fnox.toml"
PROJECT_HEADER = "# 由 keel init 按 keel.toml 的 secrets 生成"
MANAGED_BEGIN = "# --- BEGIN KEEL SECRETS ---"
MANAGED_END = "# --- END KEEL SECRETS ---"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INSTALL_HINT = ("安装 fnox 和 age（含 age-keygen），并将命令加入 PATH"
                if os.name == "nt" else "brew install fnox age")
ENV_IGNORE = [".env", ".env.*", "!.env.example", "!.env.sample", "!.env.template"]
SECRET_PATTERNS = [
    ("OpenAI 类密钥", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub 令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Hugging Face 令牌", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("AWS 访问密钥", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("age 私钥", re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{50,}")),
]


class SecretError(SystemExit):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("FNOX_CONFIG_DIR", Path.home() / ".config" / "fnox"))


def global_config() -> Path:
    return config_dir() / "config.toml"


def key_file() -> Path:
    return config_dir() / "age.txt"


def fnox(*args: str, cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    if not shutil.which("fnox"):
        raise SecretError(f"需要 fnox 与 age：{INSTALL_HINT}")
    return subprocess.run(["fnox", *args], cwd=cwd, input=stdin, capture_output=True,
                          text=True, encoding="utf-8")


def declared(config: dict) -> dict[str, str]:
    """项目变量名 → 共享密钥名。`"OPENAI_API_KEY=DASHSCOPE_API_KEY"` 表示项目里叫前者，取共享的后者。"""
    def misplaced(table: dict, prefix: tuple[str, ...] = ()) -> list[str]:
        paths = []
        for key, value in table.items():
            if key == "secrets" and prefix:
                paths.append(".".join((*prefix, key)))
            elif isinstance(value, dict):
                paths.extend(misplaced(value, (*prefix, key)))
        return paths

    wrong = misplaced(config)
    if wrong:
        raise SecretError(f"keel.toml 的 {', '.join(wrong)} 位于子表；secrets 必须是顶层声明。"
                          "请将该声明移到第一个 [表名] 之前；若已有顶层 secrets，先合并并确认同名映射，再删除子表声明")
    items = config.get("secrets", [])
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise SecretError("keel.toml 的顶层 secrets 必须是字符串列表，如 secrets = [\"API_KEY\"]")
    out: dict[str, str] = {}
    for item in items:
        name, _, source = str(item).partition("=")
        name, source = name.strip(), (source.strip() or name.strip())
        if not (NAME_RE.match(name) and NAME_RE.match(source)):
            raise SecretError(f"keel.toml 的 secrets 含非法项：{item!r}（格式 NAME 或 NAME=SHARED_SOURCE）")
        out[name] = source
    return out


def shared_entries() -> dict[str, dict]:
    path = global_config()
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8")).get("secrets", {})
    return {k[len(PREFIX):]: v for k, v in data.items() if k.startswith(PREFIX)}


def describe(name: str, description: str) -> None:
    """给共享密钥写说明（谁的、用在哪），fnox list 与 keel secret list --all 会显示。"""
    path = global_config()
    text = path.read_text(encoding="utf-8")
    key = re.escape(PREFIX + name)
    line = re.search(rf"^{key}\s*=\s*\{{[^\n]*\}}$", text, re.M)
    if not line:
        raise SecretError(f"没有共享密钥 {name}")
    entry = re.sub(r',?\s*description\s*=\s*"(?:[^"\\]|\\.)*"', "", line.group(0))
    entry = entry[:-1].rstrip().rstrip(",") + f", description = {json.dumps(description, ensure_ascii=False)} }}"
    path.write_text(text[:line.start()] + entry + text[line.end():], encoding="utf-8")


def registry_path() -> Path:
    return Path(os.environ.get("KEEL_REGISTRY", Path.home() / ".config" / "keel" / "projects.txt"))


def register(root: Path) -> None:
    """记住本机哪些项目声明了密钥，供 keel secret list --all 查看，避免全盘扫描。"""
    path = registry_path()
    known = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entry = str(root.resolve())
    if entry not in known:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([*known, entry]) + "\n", encoding="utf-8")


def usage_index() -> dict[str, list[str]]:
    """已登记项目的 fnox.toml 中引用了哪些 ${SHARED_X}；已删除的项目自动跳过。"""
    path = registry_path()
    out: dict[str, list[str]] = {}
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        fnox_file = Path(line) / FNOX_FILE
        if not fnox_file.exists():
            continue
        for name in set(re.findall(r"\$\{" + PREFIX + r"([A-Za-z0-9_]+)\}",
                                   fnox_file.read_text(encoding="utf-8", errors="ignore"))):
            out.setdefault(name, []).append(line.replace(str(Path.home()), "~"))
    return out


def inventory() -> list[str]:
    entries = shared_entries()
    if not entries:
        return [f"还没有共享密钥（{global_config()}）"]
    used = usage_index()
    rows = [f"共享密钥 {len(entries)} 把（{global_config()}，值已加密，此处不显示）", ""]
    width = max(len(n) for n in entries)
    for name in sorted(entries):
        desc = entries[name].get("description", "")
        where = used.get(name, [])
        rows.append(f"{name:{width}}  {desc}")
        rows.append(f"{'':{width}}  {'用于：' + '、'.join(sorted(where)) if where else '尚无项目引用'}")
    return rows


def shared_names() -> set[str]:
    path = global_config()
    if not path.exists():
        return set()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {k[len(PREFIX):] for k in data.get("secrets", {}) if k.startswith(PREFIX)}


# ---------- 全局存储 ----------

def setup() -> list[str]:
    """首次在本机建立 age 密钥与 fnox 全局配置；已存在则不动。"""
    path, key = global_config(), key_file()
    if path.exists() and "age" in tomllib.loads(path.read_text(encoding="utf-8")).get("providers", {}):
        return []
    if not shutil.which("age-keygen"):
        raise SecretError(f"需要 age-keygen：{INSTALL_HINT}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _secret_permissions.protect(path.parent, directory=True)
    done = []
    if not key.exists():
        subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
        done.append(f"生成 age 私钥 {key}（务必备份：丢失后所有密钥无法解开）")
    _secret_permissions.protect(key)
    public = subprocess.run(["age-keygen", "-y", str(key)], capture_output=True,
                            text=True, encoding="utf-8", check=True).stdout.strip()
    header = ("# fnox 全局配置（由 keel 建立）。共享密钥以 SHARED_ 前缀存放并设 env = false，\n"
              "# 只有在项目 fnox.toml 中声明的才会注入。\n")
    body = (f'default_provider = "age"\n\n[providers.age]\ntype = "age"\n'
            f'recipients = [{json.dumps(public, ensure_ascii=False)}]\n'
            f'key_file = {json.dumps(str(key), ensure_ascii=False)}\n')
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if "[providers.age]" in existing:
            return done
        path.write_text(header + body + "\n" + existing, encoding="utf-8")
    else:
        path.write_text(header + body, encoding="utf-8")
    return done + [f"写入 fnox 全局配置 {path}"]


def hide_from_env(name: str) -> None:
    """确保 SHARED_<name> 带 env = false。fnox set 新建条目时不会加，更新时会保留。"""
    path = global_config()
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^({re.escape(PREFIX + name)}\s*=\s*\{{)(?![^\n]*\benv\s*=)", re.M)
    path.write_text(pattern.sub(r"\1 env = false,", text), encoding="utf-8")


def set_secret(name: str, value: str) -> str:
    if not NAME_RE.match(name):
        raise SecretError(f"非法变量名：{name}")
    if not value:
        raise SecretError("值为空：从标准输入传入，例如 pbpaste | keel secret set NAME")
    setup()
    out = fnox("set", "-g", PREFIX + name, stdin=value)
    if out.returncode != 0:
        raise SecretError(f"fnox set 失败：{out.stderr.strip()[-300:]}")
    hide_from_env(name)
    return f"已保存 {PREFIX}{name}（全局，加密，默认不注入）"


def get_shared(name: str) -> str | None:
    # 在全局配置目录里运行，避免当前项目的 fnox.toml 影响结果
    out = fnox("get", PREFIX + name, cwd=config_dir()) if global_config().exists() else None
    if out is None or out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def parse_dotenv(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(errors="ignore").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if value:
            values[m.group(1)] = value
    return values


def import_dotenv(path: Path, only: list[str] | None = None) -> list[str]:
    """把 .env 中的值收进全局存储；不修改原文件。同名但值不同的不覆盖。"""
    values = parse_dotenv(path)
    if only:
        values = {k: v for k, v in values.items() if k in only}
    out = []
    existing = shared_names()
    for name, value in sorted(values.items()):
        if name in existing:
            same = get_shared(name) == value
            out.append(f"{'相同' if same else '冲突'} {name}：全局已有{'' if same else '不同的值，未覆盖（确认后用 keel secret set 更新）'}")
            continue
        set_secret(name, value)
        out.append(f"收入 {name}")
    return out or ["没有可导入的变量"]


# ---------- 项目 ----------

def project_entry(name: str, source: str) -> str:
    return f'{name} = {{ default = "${{{PREFIX}{source}}}", if_missing = "error" }}'


def project_parts(text: str, names: dict[str, str]):
    """识别生成的单行引用，保留手写项与其他 TOML 表；兼容旧版无标记配置。"""
    existing = tomllib.loads(text).get("secrets", {})
    table = re.search(r"^\[\s*secrets\s*\][ \t]*(?:#[^\n]*)?(?:\n|$)", text, re.M)
    if not table:
        return text.rstrip("\n") + "\n\n[secrets]\n", "", [], set(), existing
    rest = text[table.end():]
    next_table = re.search(r"^\s*\[", rest, re.M)
    end = table.end() + next_table.start() if next_table else len(text)
    kept, managed = [], set()
    inside = False
    legacy = text.startswith(PROJECT_HEADER) and MANAGED_BEGIN not in text
    for line in text[table.end():end].splitlines(keepends=True):
        if line.strip() == MANAGED_BEGIN:
            inside = True
            continue
        if line.strip() == MANAGED_END:
            inside = False
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{[^\n]*\}", line)
        name = match.group(1) if match else None
        entry = existing.get(name)
        canonical = (isinstance(entry, dict) and set(entry) == {"default", "if_missing"}
                     and entry.get("if_missing") == "error"
                     and re.fullmatch(r"\$\{SHARED_[A-Za-z_][A-Za-z0-9_]*\}", str(entry.get("default"))))
        if canonical and (inside or legacy or name in names):
            managed.add(name)
        else:
            kept.append(line)
    return text[:table.end()], text[end:], kept, managed, existing


def write_project(root: Path, config: dict) -> list[str]:
    names = declared(config)
    path = root / FNOX_FILE
    if not names and not path.exists():
        return []
    text = path.read_text(encoding="utf-8") if path.exists() else (
        PROJECT_HEADER + "；值来自 fnox 全局配置，本文件不含密文，可提交。\n"
        "# 运行：fnox exec -- <命令>\n\n[secrets]\n")
    prefix, suffix, kept, managed, existing = project_parts(text, names)
    for name, source in names.items():
        if name in existing and name not in managed and not reference_matches(existing[name], source):
            raise SecretError(f"{FNOX_FILE} 的 {name} 有自定义配置，与 keel.toml 不一致；未覆盖，请先手动处理")
    if not managed and all(n in existing for n in names):
        return []
    lines = [project_entry(n, s) for n, s in names.items() if n not in existing or n in managed]
    # 清空声明后仍保留标记，避免下次误把标记外的手写引用当成旧版生成项。
    block = "\n".join([MANAGED_BEGIN, *lines, MANAGED_END])
    body = "\n".join(p for p in (block, "".join(kept).strip("\n")) if p)
    updated = prefix + body + "\n" + suffix
    if updated == text:
        return []
    path.write_text(updated, encoding="utf-8")
    return ["fnox.toml 已同步密钥引用" + ("（已移除不再声明的引用）" if managed - names.keys() else "")]


def reference_matches(entry, source: str) -> bool:
    return (isinstance(entry, dict) and entry.get("default") == f"${{{PREFIX}{source}}}"
            and entry.get("if_missing") == "error")


def ensure_gitignore(root: Path) -> list[str]:
    path = root / ".gitignore"
    # Existing Git ignore files may have legacy-encoded comments. Preserve
    # their bytes and append only ASCII, without imposing TOML's UTF-8 format.
    original = path.read_bytes() if path.exists() else b""
    add = [p.encode("ascii") for p in ENV_IGNORE if p.encode("ascii") not in original.splitlines()]
    if not add:
        return []
    prefix = original + (b"\n" if original and not original.endswith(b"\n") else b"")
    if prefix:
        prefix += b"\n"
    path.write_bytes(prefix + b"# Plaintext secrets (managed by keel secret)\n" + b"\n".join(add) + b"\n")
    return [".gitignore 忽略 .env"]


def init(root: Path, config: dict) -> list[str]:
    names = declared(config)
    done = ensure_gitignore(root)
    done += write_project(root, config)
    if not names:
        return done
    if not shutil.which("fnox"):
        return done + [f"声明了 secrets 但未安装 fnox：{INSTALL_HINT}，然后重新运行 keel init"]
    done += setup()
    register(root)
    have = shared_names()
    lacking = sorted({s for s in names.values() if s not in have})
    if lacking:
        done.append(f"本机还没有：{', '.join(lacking)}；用 keel secret set <名称> 或 keel secret import <.env> 补上")
    return done


# ---------- 检查 ----------

def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         capture_output=True, text=True, encoding="utf-8")
    return [f for f in out.stdout.split("\0") if f]


def checks(root: Path, config: dict) -> list[tuple[str, str, str, str]]:
    out = []
    files = tracked_files(root)
    for rel in files:
        name = Path(rel).name
        if re.fullmatch(r"\.env(\.[\w-]+)?", name) and not re.search(r"\.(example|sample|template)$", name):
            out.append((rel, "secret-file", "明文 .env 在 git 里：git rm --cached，并用 keel secret import 收进 fnox", "error"))
    for rel in files:
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            m = pattern.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                out.append((f"{rel}:{line}", "secret-leak", f"疑似{label}进入 git；移除并轮换该密钥", "error"))
                break
    try:
        names = declared(config)
    except SecretError as e:
        return out + [("keel.toml", "secret-config", str(e), "error")]
    path = root / FNOX_FILE
    try:
        _, _, _, managed, existing = project_parts(path.read_text(encoding="utf-8") if path.exists() else "", names)
    except (tomllib.TOMLDecodeError, UnicodeError):
        return out + [(FNOX_FILE, "secret-config", "TOML 格式错误，请修复配置", "error")]
    wrong = [n for n, s in names.items() if not reference_matches(existing.get(n), s)]
    if wrong:
        out.append((FNOX_FILE, "secret-config", f"缺少引用或来源不一致：{', '.join(wrong)}，运行 keel init", "error"))
    if managed - names.keys():
        out.append((FNOX_FILE, "secret-config", "有不再声明的受管理引用，运行 keel init 移除", "error"))
    return out


def doctor(root: Path, config: dict) -> list[tuple[bool, str]]:
    names = declared(config)
    if not names:
        return [(True, "未声明 secrets")]
    lines = [(bool(shutil.which("fnox")), "fnox 已安装" if shutil.which("fnox") else f"fnox 未安装：{INSTALL_HINT}")]
    if not shutil.which("fnox"):
        return lines
    key = key_file()
    if key.exists():
        private = _secret_permissions.private_key(key)
        expected = "Windows ACL 仅允许当前用户和 SYSTEM，且禁用继承" if os.name == "nt" else "权限应为 600"
        lines.append((private, f"age 私钥 {key}" + ("" if private else f" 权限不符合要求：{expected}")))
    else:
        lines.append((False, f"缺少 age 私钥 {key}：新机器从备份复制，或运行 keel init 新建"))
    have = shared_names()
    lacking = sorted({s for s in names.values() if s not in have})
    lines.append((not lacking, f"共享密钥齐全（{len(names)} 个）" if not lacking
                  else f"本机缺少：{', '.join(lacking)}；keel secret set <名称>"))
    if not lacking and key.exists():
        out = fnox("check", cwd=root)
        lines.append((out.returncode == 0, "fnox check 通过" if out.returncode == 0
                      else f"fnox check 失败：{(out.stderr or out.stdout).strip()[-200:]}"))
    return lines


def status(root: Path, config: dict) -> list[str]:
    names = declared(config)
    have = shared_names()
    rows = [f"{'✓' if s in have else '✗'} {n}" + (f" ← {s}" if s != n else "") for n, s in names.items()] \
        or ["本项目未声明 secrets"]
    others = sorted(have - set(names.values()))
    if others:
        rows.append(f"本机另有共享密钥（本项目不注入）：{', '.join(others)}")
    return rows
