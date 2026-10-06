"""Focused unit tests for safe PR suggestion rendering and report freshness."""
import os
import tempfile
import unittest
from unittest.mock import patch

import report
import scan
import ai_review


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with open(os.path.join(self.temp.name, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("query = input()\nnext_line = 1\n")

    def item(self, line=1, path="app.py", changed=True, expected="query = input()",
             replacement="query = safe_input()"):
        source_lines = ["query = input()", "next_line = 1"]
        context_start = max(1, line - 1)
        context_end = min(len(source_lines), line + 1)
        return {
            "source": "linter", "is_real_issue": True,
            "finding": {"file": path, "line": line,
                        "location": {
                            "path": path, "line": line, "changed": changed,
                            "current_source": expected,
                            "source_context": {"start_line": context_start,
                                               "lines": source_lines[context_start - 1:context_end]},
                            "changed_lines_nearby": [line] if changed else [],
                        }},
            "ai": {"suggested_fix": replacement,
                   "fix": {"file": path, "start_line": line, "end_line": line,
                           "replacement": replacement, "explanation": "minimal repair"}},
        }

    def test_valid_changed_line_makes_native_suggestion(self):
        self.assertEqual(report._safe_suggestion(self.item(), self.temp.name)[:2],
                         ("app.py", 1))

    def test_missing_exact_mapping_falls_back(self):
        self.assertIsNone(report._safe_suggestion(self.item(changed=False), self.temp.name))

    def test_unmappable_fix_is_shown_as_manual_textual_fallback(self):
        item = self.item(changed=False)
        item["severity"] = "high"
        item["finding"].update({"rule_id": "E0001", "tool": "pylint", "message": "parse error"})
        item["ai"].update({"reason": "syntax issue", "confidence": "high"})
        rendered = report.render_linter_item(item)
        self.assertIn("FIXABLE — MANUAL APPLICATION REQUIRED", rendered)
        self.assertIn("Suggested fix could not be safely attached to the current diff.", rendered)
        self.assertIn("query = safe_input()", rendered)

    def test_unchanged_line_rejected(self):
        self.assertIsNone(report._safe_suggestion(self.item(line=2, changed=False, expected="next_line = 1"),
                                                  self.temp.name))

    def test_nonexistent_file_rejected(self):
        self.assertIsNone(report._safe_suggestion(self.item(path="missing.py"), self.temp.name))

    def test_multiline_replacement_keeps_suggestion_fence(self):
        result = report._safe_suggestion(
            self.item(replacement='query = (\n    "SELECT ?",\n    (user_id,),\n)'), self.temp.name)
        self.assertIn("```suggestion\nquery = (\n    \"SELECT ?\",\n    (user_id,),\n)\n```", result[3])

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
             patch.object(report, "_safe_suggestion", return_value=("app.py", 1, 1, "```suggestion\nnew\n```")), \
             patch.object(report, "gh", side_effect=[
                 {"head": {"sha": "abc123"}}, {"head": {"sha": "abc123"}}, {}
             ]) as gh:
            self.assertEqual(report.post_suggestions(data), 1)
        request = gh.call_args.args[3]
        self.assertEqual(request["event"], "COMMENT")
        self.assertIn("suggestion", request["comments"][0]["body"])
        self.assertNotIn("commit", request["comments"][0])

    def test_native_review_comment_uses_both_endpoints_for_multiline_range(self):
        data = {"head": "abc123", "results": [self.item()]}
        native = ("app.py", 39, 40, "```suggestion\nreplacement\n```")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "o/r"}), \
             patch.object(report, "pr_number", return_value=3), \
             patch.object(report, "_safe_suggestion", return_value=native), \
             patch.object(report, "gh", side_effect=[
                 {"head": {"sha": "abc123"}}, {"head": {"sha": "abc123"}}, {}
             ]) as gh:
            self.assertEqual(report.post_suggestions(data), 1)
        comment = gh.call_args.args[3]["comments"][0]
        self.assertEqual(comment["start_line"], 39)
        self.assertEqual(comment["start_side"], "RIGHT")
        self.assertEqual(comment["line"], 40)
        self.assertEqual(comment["side"], "RIGHT")

    def test_stale_review_head_prevents_native_suggestion_post(self):
        data = {"head": "old123", "results": [self.item()]}
        with patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "o/r"}), \
             patch.object(report, "pr_number", return_value=3), \
             patch.object(report, "_safe_suggestion", return_value=("app.py", 1, 1, "suggestion")), \
             patch.object(report, "gh", return_value={"head": {"sha": "new456"}}) as gh:
            with self.assertRaises(report.StaleReview):
                report.post_suggestions(data)
        self.assertEqual(gh.call_count, 1)
        self.assertTrue(gh.call_args.args[1].endswith("/pulls/3"))

    def test_existing_legacy_summary_marker_is_updated(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "o/r"}), \
             patch.object(report, "pr_number", return_value=3), \
             patch.object(report, "gh", return_value=[{"id": 7, "body": "<!-- inno-review --> old"}]) as gh:
            self.assertTrue(report.post_comment("new report"))
        self.assertEqual(gh.call_args.args[0], "PATCH")

    def test_pylint_e0001_can_suggest_adjacent_previous_changed_line(self):
        with open(os.path.join(self.temp.name, "app.py"), "w", encoding="utf-8") as fh:
            source_lines = [f"# line {number}" for number in range(1, 42)]
            source_lines[38] = "    y = 20"
            source_lines[39] = "    return x + y"
            fh.write("\n".join(source_lines) + "\n")
        finding = {"tool": "pylint", "rule_id": "E0001", "file": "app.py", "line": 40}
        self.assertTrue(scan.finding_on_changed_context(finding, {"app.py": {39}}))
        item = {
            "source": "linter", "is_real_issue": True,
            "finding": {
                **finding,
                "location": {
                    "path": "app.py", "line": 40, "changed": False,
                    "current_source": source_lines[39],
                    "source_context": {"start_line": 39,
                                       "lines": [source_lines[38], source_lines[39], source_lines[40]]},
                    "changed_lines_nearby": [39],
                },
            },
            "ai": {
                "suggested_fix": "    y = 20",
                "fix": {"file": "app.py", "start_line": 39, "end_line": 39,
                        "replacement": "    y = 20", "explanation": "Correct the preceding indentation."},
            },
        }
        suggestion = report._safe_suggestion(item, self.temp.name)
        self.assertEqual(suggestion[:3], ("app.py", 39, 39))
        self.assertIn("Correct the preceding indentation.", suggestion[3])

    def test_range_rejects_unrelated_file_lines_and_unbounded_span(self):
        item = self.item()
        item["ai"]["fix"].update({"start_line": 1, "end_line": 5})
        self.assertIsNone(report._safe_suggestion(item, self.temp.name))

    def test_ai_schema_preserves_explicit_range_file_and_explanation(self):
        result = ai_review._validate({
            "is_real_issue": True, "severity": "high", "confidence": "high",
            "reason": "syntax error", "impact": "module cannot load",
            "suggested_fix": "", "fix": {
                "file": "app.py", "start_line": 39, "end_line": 40,
                "replacement": "    y = 20\n    return x + y",
                "explanation": "Repair the previous statement indentation.",
            },
        })
        self.assertEqual(result["fix"]["file"], "app.py")
        self.assertEqual(result["fix"]["start_line"], 39)
        self.assertEqual(result["fix"]["end_line"], 40)
        self.assertEqual(result["suggested_fix"], result["fix"]["replacement"])


if __name__ == "__main__":
    unittest.main()
