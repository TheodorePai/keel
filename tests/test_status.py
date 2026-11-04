import unittest

from keel.status import render


class RenderTest(unittest.TestCase):
    def test_sections(self):
        issues = [
            {"id": "k-1", "title": "做 A", "status": "in_progress", "issue_type": "task", "assignee": "codex"},
            {"id": "k-2", "title": "卡住", "status": "blocked", "issue_type": "task"},
            {"id": "k-3", "title": "v1 发布", "status": "open", "issue_type": "epic"},
            {"id": "k-4", "title": "同步可用", "status": "closed", "issue_type": "epic",
             "closed_at": "2026-11-01T00:00:00Z", "close_reason": "两台机器可互相同步"},
            {"id": "k-5", "title": "旧任务", "status": "closed", "issue_type": "task"},
        ]
        notes = [("2026-11-02", "decision", "accepted", "docs/decisions/0001-a.md"),
                 ("2026-11-03", "concept", "", "docs/concepts/b.md")]
        out = "\n".join(render(issues, notes, ["v1.0  2026-11-03"]))
        self.assertIn("k-1  做 A  (codex)", out)
        self.assertIn("## 阻塞\n  k-2  卡住", out)
        self.assertIn("## 未完成的里程碑\n  k-3  v1 发布", out)
        self.assertIn("2026-11-01  k-4  同步可用\n      两台机器可互相同步", out)
        self.assertIn("未关闭任务 3 个，其中进行中 1、阻塞 1", out)
        self.assertIn("docs/decisions/0001-a.md", out)
        self.assertNotIn("concepts", out)  # 活文档不算记录
        self.assertIn("v1.0", out)

    def test_without_beads(self):
        out = "\n".join(render(None, [], []))
        self.assertIn("无法读取 bd 任务库", out)
        self.assertIn("## 最近的标签\n  无", out)


if __name__ == "__main__":
    unittest.main()
