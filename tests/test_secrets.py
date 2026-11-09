import os
import sys
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import tomllib
from pathlib import Path
from unittest.mock import patch

from keel import secrets, _secret_permissions
from keel.cli import check, init_project, load_project, main
from keel.snapshots import check_git
from tests.test_check import Base, run

HAVE_FNOX = bool(shutil.which("fnox") and shutil.which("age-keygen"))


class ConfigurationFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="keel-encoding-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "项目 😀"
        self.root.mkdir()
        self.cfg = base / "配置 😀"
        self.cfg.mkdir()
        environment = patch.dict(os.environ, {"FNOX_CONFIG_DIR": str(self.cfg)})
        environment.start()
        self.addCleanup(environment.stop)
        for name, value in (("config_dir", self.cfg), ("registry_path", base / "项目索引.txt")):
            patcher = patch.object(secrets, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def configuration_roundtrip(self):
        key = secrets.key_file()
        key.write_text("nonsecret fixture", encoding="utf-8")
        config = secrets.global_config()
        config.write_text('[custom]\nnote = "保留😀"\n', encoding="utf-8")
        with patch.object(secrets.shutil, "which", return_value="fixture-tool"), \
                patch.object(secrets.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "age1fixture\n", "")):
            secrets.setup()
        config.write_text(config.read_text(encoding="utf-8")
                          + '\n[secrets]\nSHARED_K = { value = "ciphertext", description = "原说明😀" }\n',
                          encoding="utf-8")
        self.assertEqual(secrets.shared_names(), {"K"})
        secrets.describe("K", "更新说明😀")
        secrets.hide_from_env("K")
        secrets.hide_from_env("K")
        entry = secrets.shared_entries()["K"]
        self.assertEqual(entry["description"], "更新说明😀")
        self.assertFalse(entry["env"])
        self.assertEqual(tomllib.loads(config.read_text(encoding="utf-8"))["custom"]["note"], "保留😀")

        project = self.root / secrets.FNOX_FILE
        project.write_text('[custom]\nnote = "项目😀"\n\n[secrets]\n', encoding="utf-8")
        ignore = self.root / ".gitignore"
        legacy_ignore = b"# legacy comment \x80\xff\ncustom/**\r\n"
        ignore.write_bytes(legacy_ignore)
        names = {"secrets": ["API_KEY=K"]}
        with patch.object(secrets.shutil, "which", return_value="fixture-tool"), \
                patch.object(secrets, "fnox", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(secrets, "tracked_files", return_value=[]):
            secrets.init(self.root, names)
            self.assertEqual(secrets.checks(self.root, names), [])
            self.assertTrue(all(ok for ok, _ in secrets.doctor(self.root, names)))
        self.assertEqual(tomllib.loads(project.read_text(encoding="utf-8"))["custom"]["note"], "项目😀")
        self.assertTrue(ignore.read_bytes().startswith(legacy_ignore))
        saved_ignore = ignore.read_bytes()
        self.assertEqual(secrets.ensure_gitignore(self.root), [])
        self.assertEqual(ignore.read_bytes(), saved_ignore)
        self.assertEqual(set(secrets.usage_index()), {"K"})
        self.assertIn(self.root.name, secrets.usage_index()["K"][0])
        self.assertIn("更新说明😀", "\n".join(secrets.inventory()))


class TestConfigurationEncoding(ConfigurationFixture):
    def test_configuration_roundtrip_uses_utf8_with_system_defaults(self):
        self.configuration_roundtrip()

    def test_configuration_roundtrip_with_legacy_default_encodings(self):
        # Emulate CP1252 and GBK defaults even on UTF-8 Linux/macOS runners.
        # This performs real file IO; explicit encodings retain their meaning.
        for encoding in ("cp1252", "gbk"):
            with self.subTest(encoding=encoding), patch.object(
                    io, "text_encoding", side_effect=lambda value, *args: encoding if value is None else value):
                self.configuration_roundtrip()


@unittest.skipUnless(HAVE_FNOX, "需要 fnox 与 age")
class TestFnoxEncoding(ConfigurationFixture):
    def test_real_fnox_preserves_unicode_values_and_configuration(self):
        # No CLI/template helpers: exercise the secrets configuration pipeline
        # directly under the interpreter's actual locale, including utf8_mode=0.
        self.assertIn("已保存", secrets.set_secret("K", "FAKE-中文😀"))
        secrets.describe("K", "密钥说明😀")
        self.assertEqual(secrets.get_shared("K"), "FAKE-中文😀")
        self.assertEqual(secrets.shared_names(), {"K"})
        secrets.init(self.root, {"secrets": ["API_KEY=K"]})
        self.assertTrue(all(ok for ok, _ in secrets.doctor(self.root, {"secrets": ["API_KEY=K"]})))
        self.assertIn("密钥说明😀", "\n".join(secrets.inventory()))
        self.assertFalse(secrets.shared_entries()["K"]["env"])


class TestPrivateKeyPermissions(Base):
    def setUp(self):
        super().setUp()
        self.cfg = self.root / "加密 keys 😀 # 'quoted'"
        self.cfg.mkdir()
        patcher = patch.object(secrets, "config_dir", return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_setup_writes_valid_toml_and_keeps_existing_tables_and_key(self):
        key = secrets.key_file()
        key.write_text("nonsecret fixture")
        config = secrets.global_config()
        config.write_text("[custom]\nkeep = true\n")
        with patch.object(secrets.shutil, "which", return_value="fixture-age-keygen"), \
                patch.object(secrets.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "age1fixture\n", "")):
            secrets.setup()
            first = config.read_bytes()
            self.assertEqual(secrets.setup(), [])
            self.assertEqual(config.read_bytes(), first)
        data = tomllib.loads(config.read_text())
        self.assertEqual(data["providers"]["age"]["key_file"], str(key))
        self.assertTrue(data["custom"]["keep"])
        self.assertEqual(key.read_text(), "nonsecret fixture")
        self.assertTrue(_secret_permissions.private_key(key))

    def test_doctor_rejects_broadened_key_permissions(self):
        key = secrets.key_file()
        key.write_text("nonsecret fixture")
        _secret_permissions.protect(key)
        def doctor():
            with patch.object(secrets.shutil, "which", return_value="fixture-fnox"), \
                    patch.object(secrets, "shared_names", return_value={"K"}), \
                    patch.object(secrets, "fnox", return_value=subprocess.CompletedProcess([], 0, "", "")):
                return next(ok for ok, message in secrets.doctor(self.root, {"secrets": ["K"]})
                            if message.startswith("age 私钥"))
        self.assertTrue(doctor())
        if os.name == "nt":
            # Independently broaden the real ACL with the operating system CLI.
            subprocess.run(["icacls", str(key), "/grant", "*S-1-1-0:(R)"],
                           check=True, capture_output=True)
        else:
            key.chmod(0o644)
        self.assertFalse(_secret_permissions.private_key(key))
        self.assertFalse(doctor())


if os.name == "nt":
    class TestWindowsKeyPermissions(Base):
        def test_symlinks_and_hardlinks_are_not_secured_or_accepted(self):
            outside = self.root / "outside"
            outside.write_text("keep")
            for name, create in (("link", lambda path: path.symlink_to(outside)),
                                 ("hardlink", lambda path: os.link(outside, path))):
                path = self.root / name
                create(path)
                with self.subTest(name=name), self.assertRaises(OSError):
                    _secret_permissions.protect(path)
                self.assertFalse(_secret_permissions.private_key(path))
                self.assertEqual(outside.read_text(), "keep")

        def test_access_denied_does_not_report_a_private_key(self):
            key = self.root / "age.txt"
            key.write_text("nonsecret fixture")
            with patch.object(_secret_permissions, "_get_security", return_value=5):
                self.assertFalse(_secret_permissions.private_key(key))


class TestSecretScan(Base):
    def rules(self):
        return {(f.path.split(":")[0], f.rule) for f in check(load_project(self.root))}

    def test_env_and_leaks_in_git_are_errors(self):
        (self.root / ".env").write_text("A=1\n")
        (self.root / ".env.example").write_text("A=\n")
        fake = "sk-" + "a1B2" * 8
        (self.root / "app.py").write_text(f'KEY = "{fake}"\n')
        run(self.root, "git", "add", "-f", ".env", ".env.example", "app.py")
        rules = self.rules()
        self.assertIn((".env", "secret-file"), rules)
        self.assertNotIn((".env.example", "secret-file"), rules)
        self.assertIn(("app.py", "secret-leak"), rules)

    def test_init_ignores_dotenv(self):
        init_project(self.root)
        (self.root / ".env").write_text("A=1\n")
        (self.root / ".env.example").write_text("A=\n")
        ignored = subprocess.run(["git", "-C", str(self.root), "check-ignore", ".env", ".env.example"],
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual(ignored, [".env"])

    def test_dotenv_parsing(self):
        p = self.root / "x.env"
        p.write_text('export A="q v"\nB=plain # note\nC=\n# D=1\nE=\'s\'\n')
        self.assertEqual(secrets.parse_dotenv(p), {"A": "q v", "B": "plain", "E": "s"})


class TestProjectReferences(Base):
    def config(self, source="SOURCE_A"):
        return {"secrets": [f"API_KEY={source}"]}

    def test_misplaced_legacy_declarations_are_rejected_even_with_top_level(self):
        for table in ("storage", "custom.nested"):
            for top_level in ("", 'secrets = []\n', 'secrets = ["OTHER_KEY"]\n'):
                with self.subTest(table=table, top_level=top_level):
                    text = top_level + f'[{table}]\nsecrets = ["API_KEY=SOURCE_A"]\n'
                    config = tomllib.loads(text)
                    with self.assertRaisesRegex(secrets.SecretError, table + r"\.secrets.*顶层"):
                        secrets.declared(config)
                    findings = secrets.checks(self.root, config)
                    self.assertTrue(any(r == "secret-config" and level == "error"
                                        for _, r, _, level in findings))

    def test_misplaced_empty_declaration_and_invalid_top_level_type_rejected(self):
        for config in ({"storage": {"secrets": []}}, {"secrets": "API_KEY"},
                       {"secrets": {"API_KEY": "SOURCE_A"}}, {"secrets": [1]}):
            with self.subTest(config=config), self.assertRaises(secrets.SecretError):
                secrets.declared(config)
        self.assertEqual(secrets.declared({"storage": {"model": "hf://models/me/test"}}), {})

    def test_check_and_staged_check_report_legacy_storage_secrets(self):
        cfg = self.root / "keel.toml"
        cfg.write_text('[storage]\nsecrets = ["API_KEY=SOURCE_A"]\n')
        self.assertIn("secret-config", {f.rule for f in check(load_project(self.root))})
        run(self.root, "git", "add", ".")
        # 工作区修好也不能隐藏暂存区的旧配置。
        cfg.write_text('secrets = []\n[storage]\n')
        self.assertIn("secret-config", {f.rule for f in check_git(self.root, staged=True)})

    def test_secret_commands_do_not_silently_overwrite_misplaced_configuration(self):
        cfg = self.root / "keel.toml"
        original = '[storage]\nsecrets = ["API_KEY=SOURCE_A"]\n'
        cfg.write_text(original)
        for args in (["secret", "use", "OTHER_KEY"], ["secret", "list"], ["doctor"]):
            with self.subTest(args=args), patch("keel.cli.Path.cwd", return_value=self.root), \
                    patch("keel.cli.assets.doctor") as remote, \
                    self.assertRaisesRegex(secrets.SecretError, r"storage.secrets.*顶层"):
                main(args)
            remote.assert_not_called()
            self.assertEqual(cfg.read_text(), original)
        with patch.object(secrets, "setup") as setup:
            with self.assertRaisesRegex(secrets.SecretError, r"storage.secrets.*顶层"):
                secrets.init(self.root, tomllib.loads(original))
            setup.assert_not_called()
        self.assertFalse((self.root / "fnox.toml").exists())

    def test_alias_change_is_checked_updated_and_idempotent(self):
        secrets.write_project(self.root, self.config())
        cfg = self.config("SOURCE_B")
        self.assertIn("secret-config", {r for _, r, _, _ in secrets.checks(self.root, cfg)})
        secrets.write_project(self.root, cfg)
        path = self.root / "fnox.toml"
        self.assertIn("${SHARED_SOURCE_B}", path.read_text())
        self.assertNotIn("${SHARED_SOURCE_A}", path.read_text())
        self.assertEqual(secrets.checks(self.root, cfg), [])
        text = path.read_text()
        self.assertEqual(secrets.write_project(self.root, cfg), [])
        self.assertEqual(path.read_text(), text)

    def test_removed_declarations_stop_injection_and_keep_user_entries(self):
        secrets.write_project(self.root, self.config())
        path = self.root / "fnox.toml"
        path.write_text(path.read_text() + 'CUSTOM = { value = "custom" }\n'
                        'OTHER = { default = "${SHARED_OTHER}", if_missing = "error" }\n'
                        '\n[providers.custom]\ntype = "test"\n')
        self.assertTrue(secrets.checks(self.root, {}))
        # 即使 secrets 已清空，init 也要移除旧引用，不需要创建全局密钥。
        with patch.object(secrets, "setup") as setup:
            secrets.init(self.root, {})
            setup.assert_not_called()
        data = tomllib.loads(path.read_text())
        self.assertEqual(set(data["secrets"]), {"CUSTOM", "OTHER"})
        self.assertEqual(data["providers"]["custom"]["type"], "test")
        self.assertEqual(secrets.checks(self.root, {}), [])

    def test_custom_conflict_is_preserved_and_reported(self):
        path = self.root / "fnox.toml"
        original = '[secrets]\nAPI_KEY = { provider = "custom", key = "service-key" }\n'
        path.write_text(original)
        with self.assertRaisesRegex(secrets.SecretError, "自定义配置"):
            secrets.write_project(self.root, self.config())
        self.assertEqual(path.read_text(), original)
        self.assertIn("secret-config", {r for _, r, _, _ in secrets.checks(self.root, self.config())})

    def test_legacy_generated_references_migrate_without_losing_other_tables(self):
        path = self.root / "fnox.toml"
        path.write_text(secrets.PROJECT_HEADER + '\n[secrets]\n' + secrets.project_entry("API_KEY", "SOURCE_A") +
                        '\nCUSTOM = { value = "custom" }\n\n[settings]\nflag = true\n')
        secrets.write_project(self.root, self.config("SOURCE_B"))
        data = tomllib.loads(path.read_text())
        self.assertEqual(data["secrets"]["API_KEY"]["default"], "${SHARED_SOURCE_B}")
        self.assertEqual(data["settings"], {"flag": True})
        self.assertEqual(data["secrets"]["CUSTOM"], {"value": "custom"})

    def test_cli_alias_replaces_mapping_and_inserts_top_level_declaration(self):
        cfg = self.root / "keel.toml"
        cfg.write_text('[storage]\nmodel = "hf://models/me/test"\n')
        with patch("keel.cli.Path.cwd", return_value=self.root), \
                patch.object(secrets.shutil, "which", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["secret", "use", "API_KEY=SOURCE_A"]), 0)
            self.assertEqual(main(["secret", "use", "API_KEY=SOURCE_B"]), 0)
        data = tomllib.loads(cfg.read_text())
        self.assertEqual(data["secrets"], ["API_KEY=SOURCE_B"])
        self.assertEqual(data["storage"]["model"], "hf://models/me/test")
        self.assertIn("${SHARED_SOURCE_B}", (self.root / "fnox.toml").read_text())


@unittest.skipUnless(HAVE_FNOX, "需要 fnox 与 age")
class TestSecretsWithFnox(Base):
    def setUp(self):
        super().setUp()
        self.cfg = Path(tempfile.mkdtemp(prefix="keel secrets 中文 😀 "))
        self.addCleanup(shutil.rmtree, self.cfg)
        old = os.environ.get("FNOX_CONFIG_DIR")
        os.environ["FNOX_CONFIG_DIR"] = str(self.cfg)
        os.environ["KEEL_REGISTRY"] = str(self.cfg / "projects.txt")
        self.addCleanup(os.environ.pop, "KEEL_REGISTRY", None)
        self.addCleanup(lambda: os.environ.__setitem__("FNOX_CONFIG_DIR", old) if old
                        else os.environ.pop("FNOX_CONFIG_DIR", None))

    def env_of(self, project):
        out = subprocess.run(["fnox", "exec", "--", sys.executable, "-c",
                              "import json, os; print(json.dumps(dict(os.environ)))"],
                             cwd=project, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL)
        return (json.loads(out.stdout) if out.returncode == 0 else {}), out.returncode

    def test_shared_once_injected_on_demand(self):
        secrets.set_secret("OPENAI_API_KEY", "FAKE-openai")
        secrets.set_secret("HUOSHAN_API_KEY", "FAKE-huoshan")
        self.assertTrue(_secret_permissions.private_key(secrets.key_file()))
        self.assertNotIn("FAKE", secrets.global_config().read_text())
        cfg = self.root / "keel.toml"
        cfg.write_text(cfg.read_text() + 'secrets = ["OPENAI_API_KEY", "LLM_KEY=HUOSHAN_API_KEY"]\n')
        init_project(self.root)
        env, rc = self.env_of(self.root)
        self.assertEqual((rc, env.get("OPENAI_API_KEY"), env.get("LLM_KEY")), (0, "FAKE-openai", "FAKE-huoshan"))
        self.assertNotIn("HUOSHAN_API_KEY", env)
        self.assertIn("✓ LLM_KEY ← HUOSHAN_API_KEY", secrets.status(self.root, load_project(self.root).config))
        self.assertFalse(any(k.startswith("SHARED_") for k in env))
        self.assertNotIn("FAKE", (self.root / "fnox.toml").read_text())
        self.assertTrue(all(ok for ok, _ in secrets.doctor(self.root, load_project(self.root).config)))

    def test_rotation_keeps_hidden_and_missing_fails(self):
        secrets.set_secret("K", "v1")
        secrets.set_secret("K", "v2")
        entries = [l for l in secrets.global_config().read_text().splitlines() if l.startswith("SHARED_K")]
        self.assertEqual(len(entries), 1)
        self.assertIn("env = false", entries[0])
        self.assertEqual(secrets.get_shared("K"), "v2")
        cfg = self.root / "keel.toml"
        cfg.write_text(cfg.read_text() + 'secrets = ["NOPE"]\n')
        out = secrets.init(self.root, load_project(self.root).config)
        self.assertTrue(any("本机还没有：NOPE" in line for line in out))
        _, rc = self.env_of(self.root)
        self.assertNotEqual(rc, 0)

    def test_alias_change_and_removal_change_the_actual_injected_environment(self):
        secrets.set_secret("SOURCE_A", "FAKE-A")
        secrets.set_secret("SOURCE_B", "FAKE-B")
        secrets.init(self.root, {"secrets": ["API_KEY=SOURCE_A"]})
        self.assertEqual(self.env_of(self.root)[0].get("API_KEY"), "FAKE-A")
        secrets.init(self.root, {"secrets": ["API_KEY=SOURCE_B"]})
        self.assertEqual(self.env_of(self.root)[0].get("API_KEY"), "FAKE-B")
        secrets.init(self.root, {})
        env, rc = self.env_of(self.root)
        self.assertEqual(rc, 0)
        self.assertNotIn("API_KEY", env)

    def test_import_does_not_overwrite_conflicts(self):
        secrets.set_secret("A", "old")
        env = self.root / "proj.env"
        env.write_text("A=new\nB=b\n")
        out = secrets.import_dotenv(env)
        self.assertTrue(any(line.startswith("冲突 A") for line in out))
        self.assertEqual((secrets.get_shared("A"), secrets.get_shared("B")), ("old", "b"))
        self.assertEqual(env.read_text(), "A=new\nB=b\n")


@unittest.skipUnless(HAVE_FNOX, "需要 fnox 与 age")
class TestInventory(TestSecretsWithFnox):
    def test_describe_and_usage(self):
        secrets.set_secret("HUOSHAN_API_KEY", "v")
        secrets.describe("HUOSHAN_API_KEY", "我的火山方舟，旧")
        secrets.describe("HUOSHAN_API_KEY", "我的火山方舟")
        self.assertEqual(secrets.get_shared("HUOSHAN_API_KEY"), "v")
        cfg = self.root / "keel.toml"
        cfg.write_text(cfg.read_text() + 'secrets = ["LLM=HUOSHAN_API_KEY"]\n')
        init_project(self.root)
        rows = secrets.inventory()
        text = "\n".join(rows)
        self.assertIn("我的火山方舟", text)
        self.assertNotIn("旧", text)
        self.assertIn(self.root.name, text)
        self.assertNotIn("v\n", text)


class TestSecretCli(Base):
    def test_set_note_only_updates_description_without_value(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch("keel.cli.secrets.set_secret") as store, patch("keel.cli.secrets.describe") as note, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["secret", "set", "K", "--note", "我的"]), 0)
        store.assert_not_called()
        note.assert_called_once_with("K", "我的")

    def test_set_without_value_or_note_fails(self):
        with patch.object(sys.stdin, "isatty", return_value=True), self.assertRaises(SystemExit):
            main(["secret", "set", "K"])

    def test_list_all_works_outside_a_project(self):
        with patch("keel.cli.Path.cwd", return_value=Path("/")), \
                patch("keel.cli.secrets.inventory", return_value=["K  说明"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["secret", "list", "--all"]), 0)
        self.assertIn("K", out.getvalue())
