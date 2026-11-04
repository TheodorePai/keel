import json
from unittest.mock import patch

from keel import cli
from tests.test_check import Base


class TestObsidianDefaults(Base):
    def settings_path(self):
        return self.root / ".obsidian/plugins/beads-pane/data.json"

    def test_new_defaults_use_current_project_and_discovered_bd(self):
        for binary in ("/usr/local/bin/bd", "/usr/bin/bd", None):
            with self.subTest(binary=binary):
                self.settings_path().unlink(missing_ok=True)
                with patch.object(cli.shutil, "which", return_value=binary):
                    cli.install_obsidian_templates(self.root)
                settings = json.loads(self.settings_path().read_text())
                self.assertEqual(settings["projects"], [{"name": self.root.name,
                                                       "path": str(self.root.resolve())}])
                self.assertEqual(settings["activeProject"], self.root.name)
                self.assertEqual(settings["bdPath"], binary or "bd")

    def test_existing_project_and_plugin_settings_are_preserved(self):
        target = self.settings_path()
        target.parent.mkdir(parents=True)
        original = json.dumps({"projects": [{"name": "shared", "path": "/custom/project"}],
                               "activeProject": "shared", "bdPath": "/custom/bin/bd",
                               "hiddenAssignees": ["bot"], "customSetting": True})
        target.write_text(original)
        cli.install_obsidian_templates(self.root)
        cli.install_obsidian_templates(self.root)
        self.assertEqual(target.read_text(), original)
