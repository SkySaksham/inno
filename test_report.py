"""Focused unit tests for safe PR suggestion rendering and report freshness."""
import os
import tempfile
import unittest
from unittest.mock import patch

import report


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with open(os.path.join(self.temp.name, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("query = input()\nnext_line = 1\n")

    def item(self, line=1, path="app.py", changed=True, expected="query = input()",
             replacement="query = safe_input()"):
        return {
            "source": "linter", "is_real_issue": True,
            "finding": {"file": path, "line": line,
                        "location": {"path": path, "line": line, "changed": changed,
                                     "current_source": expected}},
            "ai": {"suggested_fix": replacement},
        }

    def test_valid_changed_line_makes_native_suggestion(self):
        self.assertEqual(report._safe_suggestion(self.item(), self.temp.name)[:2],
                         ("app.py", 1))

    def test_missing_exact_mapping_falls_back(self):
        self.assertIsNone(report._safe_suggestion(self.item(changed=False), self.temp.name))

    def test_unchanged_line_rejected(self):
        self.assertIsNone(report._safe_suggestion(self.item(line=2, changed=False, expected="next_line = 1"),
                                                  self.temp.name))

    def test_nonexistent_file_rejected(self):
        self.assertIsNone(report._safe_suggestion(self.item(path="missing.py"), self.temp.name))

    def test_multiline_replacement_keeps_suggestion_fence(self):
        result = report._safe_suggestion(
            self.item(replacement='query = (\n    "SELECT ?",\n    (user_id,),\n)'), self.temp.name)
        self.assertIn("```suggestion\nquery = (\n    \"SELECT ?\",\n    (user_id,),\n)\n```", result[2])

    def test_multiple_findings_keep_distinct_locations_and_do_not_edit_source(self):
        second = self.item(line=2, expected="next_line = 1", replacement="next_line = 2")
        first = self.item()
        self.assertEqual(report._safe_suggestion(first, self.temp.name)[:2], ("app.py", 1))
        self.assertEqual(report._safe_suggestion(second, self.temp.name)[:2], ("app.py", 2))
        with open(os.path.join(self.temp.name, "app.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "query = input()\nnext_line = 1\n")

    def test_stale_snapshot_rejected_after_source_changes(self):
        with open(os.path.join(self.temp.name, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("query = parameterized()\nnext_line = 1\n")
        self.assertIsNone(report._safe_suggestion(self.item(), self.temp.name))

    def test_gate_semantics_remain_severity_and_confidence_based(self):
        high_linter = {"severity": "high", "source": "linter", "is_real_issue": True}
        low_semantic = {"severity": "high", "source": "semantic", "confidence": "low"}
        self.assertTrue(report.is_blocking(high_linter, report.RANK["high"]))
        self.assertFalse(report.is_blocking(low_semantic, report.RANK["high"]))

    def test_report_uses_only_current_run_findings_and_sha(self):
        data = {"head": "1234567890abcdef", "results": []}
        markdown = report.build_markdown(data, "high")
        self.assertIn("1234567890ab", markdown)
        self.assertIn("Gate passed", markdown)
        self.assertNotIn("old finding", markdown)

    def test_posting_suggestions_only_posts_review_comments(self):
        data = {"head": "abc123", "results": [self.item()]}
        with patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "o/r"}), \
             patch.object(report, "pr_number", return_value=3), \
             patch.object(report, "_safe_suggestion", return_value=("app.py", 1, "```suggestion\nnew\n```")), \
             patch.object(report, "gh", return_value={}) as gh:
            self.assertEqual(report.post_suggestions(data), 1)
        request = gh.call_args.args[3]
        self.assertEqual(request["event"], "COMMENT")
        self.assertIn("suggestion", request["comments"][0]["body"])
        self.assertNotIn("commit", request["comments"][0])


if __name__ == "__main__":
    unittest.main()
