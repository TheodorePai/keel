import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from keel import assets
from keel.cli import check, init_project, load_project, new_note, parse_frontmatter


def run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run(self.root, "git", "init", "-q")
        run(self.root, "git", "config", "user.email", "t@t")
        run(self.root, "git", "config", "user.name", "t")
        (self.root / "keel.toml").write_text('ignore = ["sources/*"]\n')
        (self.root / "AGENTS.md").write_text("# AGENTS.md\n")
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n")
        (self.root / ".gitignore").write_text("work/.local/\n")
        personal = self.root.parent / f"{self.root.name}-personal.toml"
        personal.write_text('hf_namespace = "me"\ngh_owner = "me"\n')
        self._personal, assets.PERSONAL_CONFIG = assets.PERSONAL_CONFIG, personal
        self.addCleanup(personal.unlink, missing_ok=True)

    def tearDown(self):
        assets.PERSONAL_CONFIG = self._personal
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / "docs" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def commit(self, msg="c"):
        run(self.root, "git", "add", "-A")
        run(self.root, "git", "commit", "-qm", msg, "--no-verify")

    def rules(self):
        return [(f.rule, f.level) for f in check(load_project(self.root))]


DECISION = "---\ntype: decision\ndate: 2026-11-03\nstatus: {status}\nbeads: []\n---\n\n# 标题\n\n## 决定\n\n## 理由\n\n{body}\n"
PROTO = "---\ntype: protocol\ndate: 2026-11-03\nstatus: {s}\n---\n\n## 输入\n\n## 指标\n"
REPORT = "---\ntype: report\ndate: 2026-11-03\nstatus: draft\nprotocol: ../protocols/2026-11-03-p.md\n---\n\n## 结论\n"
IDEA = "---\ntype: idea\ndate: 2026-11-03\nstatus: open\n---\n"


class TestAgentEntrypoints(Base):
    def test_only_single_line_import_is_accepted(self):
        path = self.root / "CLAUDE.md"
        for text in ("@AGENTS.md", "@AGENTS.md\n", "@AGENTS.md\r\n"):
            with self.subTest(text=text):
                path.write_bytes(text.encode())
                self.assertEqual(self.rules(), [])
        for text in ("", "# Claude\n@AGENTS.md\n", "@AGENTS.md\n另一条规则\n",
                     "@AGENTS.md\n\n", " @AGENTS.md\n", "@other.md\n"):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertIn(("agent-entrypoint", "error"), self.rules())

    def test_missing_entrypoints_and_non_file_import_target_are_errors(self):
        for name in ("CLAUDE.md", "AGENTS.md"):
            path = self.root / name
            original = path.read_text()
            path.unlink()
            found = check(load_project(self.root))
            self.assertEqual([(f.path, f.rule) for f in found], [(name, "agent-entrypoint")])
            path.mkdir()
            self.assertIn(("agent-entrypoint", "error"), self.rules())
            path.rmdir()
            path.write_text(original)

    def test_symlink_or_non_text_claude_entrypoint_is_not_overwritten(self):
        path = self.root / "CLAUDE.md"
        target = self.root / ".pointer"
        target.write_text("@AGENTS.md\n")
        path.unlink()
        path.symlink_to(target)
        self.assertIn(("agent-entrypoint", "error"), self.rules())
        with self.assertRaisesRegex(SystemExit, "已有内容未改动"):
            init_project(self.root)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), "@AGENTS.md\n")
        path.unlink()
        path.write_bytes(b"\xff")
        self.assertIn(("agent-entrypoint", "error"), self.rules())


class TestFrontmatter(unittest.TestCase):
    def test_lists_and_scalars(self):
        meta, body, err = parse_frontmatter("---\na: 1\nb: [x, 'y']\nc:\n  - p\n  - q\nd:\n---\nhi\n")
        self.assertIsNone(err)
        self.assertEqual(meta, {"a": "1", "b": ["x", "y"], "c": ["p", "q"], "d": None})
        self.assertEqual(body, "hi\n")

    def test_missing(self):
        self.assertEqual(parse_frontmatter("# x\n")[2], "缺少 frontmatter")


class TestRules(Base):
    def test_clean_note_passes(self):
        self.write("decisions/0001-a.md", DECISION.format(status="proposed", body="x"))
        self.assertEqual(self.rules(), [])

    def test_unclassified_and_ignored(self):
        self.write("loose.md", "# x\n")
        self.write("sources/raw.md", "# x\n")
        self.assertEqual(self.rules(), [("placement", "error")])

    def test_type_dir_mismatch(self):
        self.write("reports/a.md", DECISION.format(status="proposed", body="x"))
        self.assertIn(("type", "error"), self.rules())

    def test_status_date_numbering_required(self):
        self.write("decisions/a.md", "---\ntype: decision\nstatus: maybe\n---\n")
        self.write("incidents/b.md", "---\ntype: incident\ndate: 2026-11-03\nstatus: open\n---\n")
        self.write("ideas/x.md", IDEA)
        self.write("decisions/0001-ok.md", DECISION.format(status="proposed", body="").replace("## 理由", "## 其他"))
        self.write("ideas/2026-11-03-y.md", IDEA.replace("---\n", "area: [ui]\n---\n", 2).replace("area: [ui]\n", "", 1))
        (self.root / "keel.toml").write_text('areas = ["harmony"]\n')
        rules = {r for r, _ in self.rules()}
        self.assertTrue({"status", "date", "numbered", "required", "filename", "section", "area"} <= rules)

    def test_append_only_after_final(self):
        path = self.write("decisions/0001-a.md", DECISION.format(status="accepted", body="原文"))
        self.commit()
        path.write_text(DECISION.format(status="accepted", body="原文\n\n追加"))
        self.assertEqual(self.rules(), [])
        path.write_text(DECISION.format(status="superseded", body="改写"))
        self.assertIn(("append-only", "error"), self.rules())
        path.write_text(DECISION.format(status="proposed", body="原文"))
        self.assertIn(("append-only", "error"), self.rules())

    def test_final_allows_link_fix_and_follows_rename(self):
        path = self.write("decisions/0001-a.md", DECISION.format(status="accepted", body="见 [x](old.md)"))
        self.write("decisions/old.md", "x")
        self.commit()
        run(self.root, "git", "mv", "docs/decisions/0001-a.md", "docs/decisions/0001-b.md")
        new = self.root / "docs/decisions/0001-b.md"
        new.write_text(DECISION.format(status="accepted", body="见 [x](new.md)"))
        rules = self.rules()
        self.assertNotIn(("append-only", "error"), rules)
        new.write_text(DECISION.format(status="accepted", body="改了"))
        self.assertIn(("append-only", "error"), self.rules())

    def test_proposed_can_be_rewritten(self):
        path = self.write("decisions/0001-a.md", DECISION.format(status="proposed", body="原文"))
        self.commit()
        path.write_text(DECISION.format(status="accepted", body="改写"))
        self.assertEqual(self.rules(), [])

    def test_report_requires_frozen_protocol_committed_first(self):
        p = self.write("protocols/2026-11-03-p.md", PROTO.format(s="draft"))
        self.write("reports/2026-11-03-r.md", REPORT)
        self.assertIn(("protocol", "error"), self.rules())  # 未冻结
        p.write_text(PROTO.format(s="frozen"))
        self.assertIn(("protocol", "error"), self.rules())  # 冻结未提交
        (self.root / "docs/reports/2026-11-03-r.md").unlink()
        self.commit("freeze")
        self.write("reports/2026-11-03-r.md", REPORT)
        self.assertEqual(self.rules(), [])

    def test_report_committed_before_freeze_fails(self):
        p = self.write("protocols/2026-11-03-p.md", PROTO.format(s="draft"))
        self.write("reports/2026-11-03-r.md", REPORT)
        self.commit("report first")
        p.write_text(PROTO.format(s="frozen"))
        self.commit("freeze later")
        self.assertIn(("protocol", "error"), self.rules())

    def test_protocol_and_report_same_commit_rejected(self):
        self.write("protocols/2026-11-03-p.md", PROTO.format(s="frozen"))
        self.write("reports/2026-11-03-r.md", REPORT)
        self.commit("same commit")
        self.assertIn(("protocol", "error"), self.rules())

    def test_protocol_ancestor_with_same_timestamp_allowed(self):
        with patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2026-11-03T00:00:00Z",
                                     "GIT_COMMITTER_DATE": "2026-11-03T00:00:00Z"}):
            self.write("protocols/2026-11-03-p.md", PROTO.format(s="frozen"))
            self.commit("freeze")
            self.write("reports/2026-11-03-r.md", REPORT)
            self.commit("report")
        self.assertEqual(self.rules(), [])

    def test_protocol_order_survives_renames(self):
        self.write("protocols/2026-11-03-p.md", PROTO.format(s="frozen"))
        self.commit("freeze")
        report = self.write("reports/2026-11-03-r.md", REPORT)
        self.commit("report")
        run(self.root, "git", "mv", "docs/protocols/2026-11-03-p.md", "docs/protocols/2026-11-03-new.md")
        report.write_text(REPORT.replace("2026-11-03-p.md", "2026-11-03-new.md"))
        run(self.root, "git", "mv", str(report), str(report.with_name("2026-11-03-new.md")))
        self.assertEqual(self.rules(), [])
        self.commit("rename both")
        self.assertEqual(self.rules(), [])

    def test_freeze_on_another_branch_does_not_preregister_report(self):
        self.commit("base")
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        run(self.root, "git", "checkout", "-qb", "freeze")
        self.write("protocols/2026-11-03-p.md", PROTO.format(s="frozen"))
        self.commit("freeze")
        run(self.root, "git", "checkout", "-qb", "report", base)
        self.write("reports/2026-11-03-r.md", REPORT)
        self.commit("report")
        run(self.root, "git", "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "-qm", "merge", "freeze")
        self.assertIn(("protocol", "error"), self.rules())

    def test_links(self):
        self.write("ideas/2026-11-03-a.md", IDEA + "[[2026-11-03-b]] [ok](2026-11-03-b.md) [bad](nope.md) "
                                                  "[[missing]] `[[code]]` [w](https://x)\n")
        self.write("ideas/2026-11-03-b.md", IDEA)
        self.assertEqual(self.rules(), [("link", "error"), ("link", "error")])

    def test_explicit_runtime_links_are_rejected_even_when_basename_exists_elsewhere(self):
        self.write("ideas/2026-11-03-a.md", IDEA + "[[work/.local/README]]\n"
                   "[missing](../../work/%2Elocal/missing.md)\n[alias](../../.cached-alias)\n")
        (self.root / "README.md").write_text("# actual project\n")
        local = self.root / "work/.local/README.md"
        local.parent.mkdir(parents=True)
        local.write_text("cache\n")
        (self.root / ".cached-alias").symlink_to(local)
        self.assertEqual(self.rules(), [("local-link", "error")] * 3)

    def test_ignored_tool_files_cannot_satisfy_wikilinks(self):
        ignore = self.root / ".gitignore"
        ignore.write_text(ignore.read_text() + "node_modules/\n")
        cached = self.root / "work/node_modules/pkg/cached-only.md"
        cached.parent.mkdir(parents=True)
        cached.write_text("cache\n")
        self.write("ideas/2026-11-03-a.md", IDEA + "[[cached-only]]\n")
        self.assertEqual(self.rules(), [("link", "error")])

    def test_incident_repeat_warns(self):
        for i in range(3):
            self.write(f"incidents/2026-11-03-i{i}.md", "---\ntype: incident\ndate: 2026-11-03\n"
                       "status: resolved\ncause: stale-render-key\n---\n\n## 现象\n\n## 原因\n\n## 修复\n")
        self.assertEqual(self.rules(), [("repeat", "warn")])


class TestNewAndInit(Base):
    def test_new_numbers_decisions_and_passes_check(self):
        project = load_project(self.root)
        a = new_note(project, "decision", "第一个决定", None, [])
        b = new_note(project, "decision", "Second one", None, [])
        self.assertEqual((a.name[:5], b.name), ("0001-", "0002-second-one.md"))
        for kind in project.types:
            if kind not in ("report", "incident"):
                new_note(project, kind, f"{kind} 样例", f"{kind}-sample", ["k-1"], ["ui"])
        proto = next((self.root / "docs/protocols").glob("*.md"))
        report = new_note(project, "report", "报告", "r", [], protocol=proto.stem)
        incident = new_note(project, "incident", "事故", "i", [], cause="stale-key")
        self.assertIn(f"protocol: ../protocols/{proto.name}", report.read_text())
        self.assertIn("cause: stale-key", incident.read_text())
        self.assertTrue(incident.name.startswith(time.strftime("%Y-%m-%d-")))
        bad = [f for f in check(project) if f.rule not in ("beads", "protocol")]
        self.assertEqual(bad, [])

    def test_init_is_idempotent(self):
        (self.root / "keel.toml").unlink()
        init_project(self.root)
        init_project(self.root)
        hook = (self.root / ".git/hooks/pre-commit").read_text()
        self.assertEqual(hook.count("BEGIN KEEL"), 1)
        self.assertTrue((self.root / "_keel/templates/decision.md").exists())
        self.assertIn('file.inFolder("docs")', (self.root / "_keel/notes.base").read_text())
        self.assertEqual((self.root / ".gitignore").read_text().count("_keel/"), 1)
        self.assertTrue(os.access(self.root / ".git/hooks/pre-commit", os.X_OK))


if __name__ == "__main__":
    unittest.main()


class TestAssetsAndVault(Base):
    def test_assets_rules(self):
        self.write("ideas/2026-11-03-a.md", IDEA + "![](assets/Pasted%20image%201.png) ![x](<assets/b c.png>)\n")
        for name in ("Pasted image 1.png", "b c.png", "orphan.png"):
            self.write(f"ideas/assets/{name}", "x")
        self.write("ideas/loose.png", "x")
        big = self.root / "docs/ideas/assets/big.bin"
        big.write_bytes(b"0" * (6 * 1024 * 1024))
        found = sorted((f.path.split("/")[-1], f.level) for f in check(load_project(self.root)))
        self.assertEqual(found, [("big.bin", "warn"), ("loose.png", "warn"), ("orphan.png", "warn")])
        self.commit()
        rules = {(f.path.split("/")[-1], f.rule) for f in check(load_project(self.root))}
        self.assertIn(("big.bin", "large-file"), rules)

    def test_init_merges_obsidian_config(self):
        obs = self.root / ".obsidian"
        obs.mkdir()
        (obs / "app.json").write_text('{"vimMode": true, "attachmentFolderPath": "x"}')
        (obs / "community-plugins.json").write_text('["dataview"]')
        (obs / "appearance.json").write_text('{"cssTheme": "Minimal"}')
        (obs / "core-plugins.json").write_text('{"graph": false, "bases": false}')
        (self.root / "keel.toml").write_text('vault_exclude = ["work/"]\n')
        init_project(self.root)
        import json
        app = json.loads((obs / "app.json").read_text())
        self.assertEqual((app["vimMode"], app["attachmentFolderPath"], app["userIgnoreFilters"]),
                         (True, "./assets", ["work/"]))
        self.assertEqual(app["defaultViewMode"], "preview")  # 默认键：缺失时补上
        appearance = json.loads((obs / "appearance.json").read_text())
        self.assertEqual(appearance["cssTheme"], "Minimal")  # 默认键：项目已设的保留
        self.assertTrue(appearance["nativeMenus"])
        core = json.loads((obs / "core-plugins.json").read_text())
        self.assertEqual((core["graph"], core["bases"]), (False, True))  # 外观类保留，强制键覆盖
        plugins = json.loads((obs / "community-plugins.json").read_text())
        self.assertEqual(plugins[0], "dataview")
        self.assertIn("beads-pane", plugins)
        self.assertTrue((obs / "themes/Baseline/theme.css").exists())


class TestDoltRemoteUrl(unittest.TestCase):
    def test_forms(self):
        from keel.cli import dolt_remote_url
        self.assertEqual(dolt_remote_url("https://github.com/o/r.git"), "git+https://github.com/o/r.git")
        self.assertEqual(dolt_remote_url("git@github.com:o/r.git"), "git+ssh://git@github.com/o/r.git")
        self.assertEqual(dolt_remote_url("git+https://x/y.git"), "git+https://x/y.git")
        self.assertTrue(dolt_remote_url("/tmp/r.git").startswith("git+file:///"))
