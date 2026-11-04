import os
import tomllib

from keel import __version__, skills
from keel.cli import check, init_project, load_project
from tests.test_check import Base


class TestSkills(Base):
    def setUp(self):
        super().setUp()
        (self.root / "keel.toml").unlink()
        init_project(self.root)

    def rules(self):
        return {(f.path, f.rule) for f in check(load_project(self.root))}

    def test_init_copies_keel_skill_and_mirror(self):
        local = self.root / ".agents/skills/keel"
        self.assertTrue((local / "SKILL.md").exists())
        self.assertFalse(local.is_symlink())
        self.assertEqual(skills.tree_digest(local), skills.tree_digest(self.root / ".claude/skills/keel"))
        lock = tomllib.loads((self.root / "keel.lock").read_text())
        self.assertEqual((lock["keel_version"], list(lock["skills"])), (__version__, ["keel"]))
        self.assertEqual({r for _, r in self.rules()} & {"skill-sync", "skill-mirror", "skill-format"}, set())

    def test_local_edit_is_flagged_and_not_overwritten(self):
        skill = self.root / ".agents/skills/keel/SKILL.md"
        skill.write_text(skill.read_text() + "\n本地改动\n")
        self.assertIn((".agents/skills/keel", "skill-modified"), self.rules())
        out = skills.sync(self.root, load_project(self.root).config, __version__)
        self.assertTrue(any("跳过 keel" in line for line in out))
        self.assertIn("本地改动", skill.read_text())
        skills.sync(self.root, load_project(self.root).config, __version__, force=True)
        self.assertNotIn("本地改动", skill.read_text())

    def test_project_skill_format_and_mirror(self):
        own = self.root / ".agents/skills/device-deploy"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("---\nname: device-deploy\ndescription: 部署到真机\n---\n见 [脚本](run.sh)\n")
        self.assertIn((".agents/skills/device-deploy/SKILL.md", "skill-link"), self.rules())
        (own / "run.sh").write_text("echo")
        skills.sync(self.root, load_project(self.root).config, __version__)
        self.assertTrue((self.root / ".claude/skills/device-deploy/run.sh").exists())
        (own / "run.sh").write_text("echo 2")
        self.assertIn((".claude/skills/device-deploy", "skill-mirror"), self.rules())
        bad = self.root / ".agents/skills/Bad_Name"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: other\n---\n")
        self.assertIn((".agents/skills/Bad_Name/SKILL.md", "skill-format"), self.rules())

    def test_symlink_rejected_and_unmanaged_mirror_kept(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (target / "SKILL.md").write_text("---\nname: linked\ndescription: x\n---\n")
        os.symlink(target, self.root / ".agents/skills/linked")
        self.assertIn((".agents/skills/linked", "skill-symlink"), self.rules())
        os.unlink(self.root / ".agents/skills/linked")
        own = self.root / ".agents/skills/mine"
        own.mkdir()
        (own / "SKILL.md").write_text("---\nname: mine\ndescription: x\n---\n")
        handmade = self.root / ".claude/skills/mine"
        handmade.mkdir(parents=True)
        (handmade / "SKILL.md").write_text("手写的，不要覆盖")
        out = skills.sync(self.root, load_project(self.root).config, __version__)
        self.assertTrue(any("未镜像 mine" in line for line in out))
        self.assertEqual((handmade / "SKILL.md").read_text(), "手写的，不要覆盖")

    def test_removed_from_config_is_deleted(self):
        cfg = self.root / "keel.toml"
        cfg.write_text(cfg.read_text().replace('skills = ["keel"]', "skills = []"))
        skills.sync(self.root, load_project(self.root).config, __version__)
        self.assertFalse((self.root / ".agents/skills/keel").exists())
        self.assertFalse((self.root / ".claude/skills/keel").exists())

    def test_unknown_skill_refused(self):
        with self.assertRaises(SystemExit):
            skills.sync(self.root, {"skills": ["nope"]}, __version__)


class TestAgentsPointer(Base):
    def test_pointer_added_once_after_title(self):
        (self.root / "AGENTS.md").write_text("# AGENTS.md\n\n已有内容\n")
        init_project(self.root)
        init_project(self.root)
        text = (self.root / "AGENTS.md").read_text()
        self.assertEqual(text.count(".agents/skills/keel/SKILL.md"), 1)
        self.assertTrue(text.startswith("# AGENTS.md\n\n本项目使用 Keel"))
        self.assertIn("已有内容", text)
