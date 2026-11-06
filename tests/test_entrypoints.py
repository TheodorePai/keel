import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel import cli, entrypoints
from keel.snapshots import check_git
from tests.test_check import Base, run


class TestEntrypointProtection(Base):
    def test_file_only_init_installs_git_policy_without_beads_and_is_idempotent(self):
        cli.init_project(self.root)
        agents = self.root / "AGENTS.md"
        original = agents.read_text()
        self.assertFalse((self.root / ".agents/skills/beads/SKILL.md").exists())
        self.assertIn(entrypoints.GIT_AUTHORITY, original)
        cli.init_project(self.root)
        self.assertEqual(agents.read_text(), original)

    def test_init_replaces_old_git_defaults_in_place_and_keeps_project_rules(self):
        agents = self.root / "AGENTS.md"
        for old in entrypoints.LEGACY_GIT_AUTHORITIES:
            with self.subTest(old=old):
                agents.write_text(f"# AGENTS.md\n\n- {old}\n- 项目只允许推送到指定分支。\n")
                cli.init_project(self.root)
                updated = agents.read_text()
                self.assertIn("- " + entrypoints.GIT_AUTHORITY, updated)
                self.assertNotIn(old, updated)
                self.assertIn("- 项目只允许推送到指定分支。\n", updated)
                cli.init_project(self.root)
                self.assertEqual(agents.read_text(), updated)

    def test_git_policy_preserves_custom_restrictions_examples_and_comments(self):
        old = entrypoints.LEGACY_GIT_AUTHORITIES[1]
        original = (f"# AGENTS.md\n\n本项目{old}\n\n"
                    f"```md\n{old}\n```\n\n~~~md\n{old}\n~~~\n\n"
                    f"<!--\n{old}\n-->\n")
        updated = entrypoints.with_git_authority(original)
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated.count(entrypoints.GIT_AUTHORITY), 1)
        self.assertEqual(entrypoints.with_git_authority(updated), updated)

    def test_git_policy_removes_duplicate_old_defaults_when_current_policy_exists(self):
        original = ("# AGENTS.md\n\n" + entrypoints.LEGACY_GIT_AUTHORITIES[0] + "\n\n"
                    + entrypoints.GIT_AUTHORITY + "\n\n" + entrypoints.LEGACY_GIT_AUTHORITIES[1] + "\n")
        updated = entrypoints.with_git_authority(original)
        self.assertEqual(updated.count(entrypoints.GIT_AUTHORITY), 1)
        for old in entrypoints.LEGACY_GIT_AUTHORITIES:
            self.assertNotIn(old, updated)
        self.assertEqual(entrypoints.with_git_authority(updated), updated)

    def test_entrypoint_links_ignore_examples_and_check_actual_local_targets(self):
        agents = self.root / "AGENTS.md"
        agents.write_text('# AGENTS.md\n\n[Reference](docs/missing.md)\n'
                          '[Website](https://example.com/guide)\n[Section](#rules)\n'
                          '```md\n[Example](docs/example.md)\n```\n'
                          '`[Inline example](docs/inline.md)`\n'
                          '<!-- [Commented out](docs/comment.md) -->\n')
        found = [f for f in cli.check(cli.load_project(self.root)) if f.rule == "agent-entrypoint-link"]
        self.assertEqual(len(found), 1)
        self.assertIn("docs/missing.md", found[0].message)
        (self.root / "docs").mkdir()
        target = self.root / "docs/local guide.md"
        target.write_text("# Local guide\n")
        agents.write_text('[Reference](<docs/local%20guide.md#rules>)\n')
        self.assertEqual(cli.check_agent_entrypoints(self.root, {}), [])

    def test_work_mode_check_uses_staged_entrypoint_not_worktree(self):
        (self.root / "keel.toml").unlink()
        cli.init_project(self.root)
        self.commit()
        agents = self.root / "AGENTS.md"
        original = agents.read_text()
        agents.write_text("\n".join(line for line in original.splitlines()
                                    if entrypoints.WORK_MODE_TARGET not in line) + "\n")
        run(self.root, "git", "add", "AGENTS.md")
        agents.write_text(original)
        self.assertIn("work-mode", {f.rule for f in check_git(self.root, staged=True)})
        self.assertNotIn("work-mode", {f.rule for f in cli.check(cli.load_project(self.root))})

    def test_new_work_mode_requirement_does_not_retroactively_invalidate_history(self):
        (self.root / "keel.toml").unlink()
        cli.init_project(self.root)
        agents = self.root / "AGENTS.md"
        agents.write_text("\n".join(line for line in agents.read_text().splitlines()
                                    if entrypoints.WORK_MODE_TARGET not in line) + "\n")
        self.commit()
        self.assertIn("work-mode", {f.rule for f in cli.check(cli.load_project(self.root))})
        self.assertNotIn("work-mode", {f.rule for f in check_git(self.root, ref="HEAD")})

    def test_custom_claude_stops_before_beads_or_git_changes(self):
        original = "@AGENTS.md\n\nCustom project rules\n"
        (self.root / "CLAUDE.md").write_text(original)
        with patch.object(cli, "setup_beads") as beads, patch.object(cli, "commit_managed") as commit:
            with self.assertRaisesRegex(SystemExit, "迁入 AGENTS.md"):
                cli.init_project(self.root, commit=True)
            beads.assert_not_called()
            commit.assert_not_called()
        self.assertEqual((self.root / "CLAUDE.md").read_text(), original)

    def test_unknown_or_modified_generation_blocks_are_preserved(self):
        for text in (
            "<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:1105d646 -->\n"
            "Never push this project\n<!-- END BEADS INTEGRATION -->\n",
            "<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->\nIncomplete\n",
        ):
            with self.subTest(text=text):
                (self.root / "AGENTS.md").write_text(text)
                with self.assertRaisesRegex(SystemExit, "原文未改动"):
                    cli.init_project(self.root)
                self.assertEqual((self.root / "AGENTS.md").read_text(), text)

    def test_setup_failure_restores_both_entrypoints_and_existing_skill(self):
        names = ["AGENTS.md", "CLAUDE.md", ".agents/skills/beads/SKILL.md"]
        skill = self.root / names[-1]
        skill.parent.mkdir(parents=True)
        skill.write_text("Custom skill\n")
        before = {name: (self.root / name).read_bytes() for name in names}

        def fail_setup(root, *args):
            for name in names:
                (root / name).write_text("Generated content\n")
            return subprocess.CompletedProcess(args, 1, "", "setup unavailable")

        with patch.object(cli, "sh", side_effect=fail_setup):
            with self.assertRaisesRegex(SystemExit, "setup unavailable"):
                cli.setup_beads_integrations(self.root)
        self.assertEqual({name: (self.root / name).read_bytes() for name in names}, before)


@unittest.skipUnless(shutil.which("bd"), "real Beads CLI is required for init integration")
class TestRealBeadsEntrypoints(Base):
    def assert_integrations(self, root):
        self.assertEqual((root / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        agents = (root / "AGENTS.md").read_text()
        self.assertNotIn("BEGIN BEADS", agents)
        self.assertEqual(agents.count(".agents/skills/keel/SKILL.md"), 1)
        self.assertEqual(agents.count(".agents/skills/beads/SKILL.md"), 1)
        self.assertEqual(agents.count(entrypoints.GIT_AUTHORITY), 1)
        self.assertIn(entrypoints.WORK_MODE_TARGET, cli.markdown_link_targets(agents))
        self.assertTrue((root / entrypoints.WORK_MODE_TARGET).is_file())
        self.assertTrue((root / ".agents/skills/beads/SKILL.md").is_file())
        self.assertEqual((root / ".agents/skills/beads/SKILL.md").read_bytes(),
                         (root / ".claude/skills/beads/SKILL.md").read_bytes())
        claude = json.loads((root / ".claude/settings.json").read_text())
        self.assertIn("bd prime --hook-json", json.dumps(claude))
        codex = json.loads((root / ".codex/hooks.json").read_text())
        self.assertIn("bd codex-hook SessionStart", json.dumps(codex))
        self.assertIn("bd", (root / ".cursor/hooks.json").read_text())
        hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=root, text=True, capture_output=True).stdout.strip()
        if not hooks:
            hooks = subprocess.check_output(["git", "rev-parse", "--git-path", "hooks"], cwd=root, text=True).strip()
        hook = (root / hooks / "pre-commit").read_text()
        self.assertIn(cli.HOOK_BEGIN, hook)
        self.assertIn("bd", hook)
        self.assertEqual([f for f in cli.check(cli.load_project(root)) if f.level == "error"], [])

    def test_normal_init_repeat_and_clone_restore_with_real_beads(self):
        for name in ("keel.toml", "AGENTS.md", "CLAUDE.md"):
            (self.root / name).unlink()
        cli.init_project(self.root, prefix="entryprobe", commit=True)
        self.assert_integrations(self.root)
        # Native editor configuration must be committed for another machine.
        for name in (".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json"):
            run(self.root, "git", "ls-files", "--error-unmatch", name)
        task = subprocess.check_output(
            ["bd", "create", "--title", "Restored task", "--type", "task", "--silent"],
            cwd=self.root, text=True).strip()
        before = {name: (self.root / name).read_bytes() for name in ("AGENTS.md", "CLAUDE.md", ".codex/hooks.json")}
        cli.init_project(self.root, commit=True)
        self.assertEqual({name: (self.root / name).read_bytes() for name in before}, before)
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.root, text=True), "")
        with tempfile.TemporaryDirectory(prefix="keel-init-clone-") as temp:
            origin, clone = Path(temp) / "origin.git", Path(temp) / "clone"
            run(self.root, "git", "init", "--bare", "-q", str(origin))
            run(self.root, "git", "remote", "add", "origin", str(origin))
            cli.publish_github(self.root)
            run(self.root, "git", "clone", "-q", str(origin), str(clone))
            cli.init_project(clone, commit=True)
            self.assert_integrations(clone)
            restored = subprocess.check_output(["bd", "show", task, "--json"], cwd=clone, text=True)
            self.assertIn("Restored task", restored)

    def test_real_legacy_defaults_migrate_and_keep_user_rules_and_hooks(self):
        # Exercise the unmodified upstream default generation, not --skip-agents.
        (self.root / "keel.toml").write_text('skills = ["keel"]\nignore = []\n')
        for name in ("AGENTS.md", "CLAUDE.md"):
            (self.root / name).unlink()
        run(self.root, "bd", "init", "--prefix", "legacyprobe")
        self.assertIn("BEGIN BEADS", (self.root / "AGENTS.md").read_text())
        custom = "\n## Project policy\nNever push without explicit user approval.\n"
        agents = self.root / "AGENTS.md"
        agents.write_text(agents.read_text() + custom)
        settings = self.root / ".claude/settings.json"
        config = json.loads(settings.read_text())
        config["permissions"] = {"deny": ["Bash(git push:*)"]}
        settings.write_text(json.dumps(config))
        cli.init_project(self.root, commit=True)
        self.assert_integrations(self.root)
        self.assertIn(custom.strip(), agents.read_text())
        self.assertNotIn("Non-Interactive Shell Commands", agents.read_text())
        self.assertEqual(json.loads(settings.read_text())["permissions"], config["permissions"])

    def test_custom_content_inside_real_claude_template_is_not_discarded(self):
        (self.root / "CLAUDE.md").unlink()
        run(self.root, "bd", "setup", "claude")
        path = self.root / "CLAUDE.md"
        path.write_text(path.read_text() + "\nCustom release policy\n")
        original = path.read_bytes()
        with self.assertRaisesRegex(SystemExit, "迁入 AGENTS.md"):
            cli.init_project(self.root, commit=True)
        self.assertEqual(path.read_bytes(), original)
