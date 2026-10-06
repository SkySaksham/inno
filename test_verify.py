"""Tests for static-only follow-up verification and human-applied fix metrics."""
import unittest
from unittest.mock import patch

import verify


def static_item(item_id="F1", severity="high", suggestion="value = safe()",
                is_real=True):
    return {
        "id": item_id, "source": "linter", "severity": severity,
        "confidence": "high", "is_real_issue": is_real,
        "finding": {
            "file": "src/app.py", "line": 7,
            "tool": "bandit", "rule_id": "B608", "rule_name": "hardcoded_sql_expressions",
            "message": "Possible SQL injection",
            "location": {"path": "src/app.py", "line": 7,
                         "changed": True, "current_source": "value = input()"},
        },
        "ai": {"is_real_issue": is_real, "suggested_fix": suggestion,
               "severity": severity, "confidence": "high"},
        "semantic": None,
    }


class VerificationTests(unittest.TestCase):
    def state(self, item):
        return {"version": 1, "initial_head": "oldsha", "threshold": "high",
                "initial_results": [item], "accepted_suggestions": [],
                "eslint_configured": False}

    def test_absent_finding_and_suggested_patch_counts_as_human_applied(self):
        item = static_item()
        with patch.object(verify, "suggestion_was_added", return_value=True):
            markdown, state, passed = verify.make_verification(self.state(item), [], "newsha")
        self.assertTrue(passed)
        self.assertIn("Total fixes accepted/applied by the human: **1**", markdown)
        self.assertIn("Total bugs fixed: **1**", markdown)
        self.assertIn("READY TO MERGE", markdown)
        self.assertEqual(state["metrics"]["remaining"], 0)

    def test_absent_finding_without_matching_suggested_patch_is_not_accepted(self):
        item = static_item()
        with patch.object(verify, "suggestion_was_added", return_value=False):
            markdown, _, passed = verify.make_verification(self.state(item), [], "newsha")
        self.assertTrue(passed)
        self.assertIn("Total fixes accepted/applied by the human: **0**", markdown)

    def test_existing_static_finding_remains_blocking(self):
        item = static_item()
        markdown, _, passed = verify.make_verification(self.state(item), [item["finding"]], "newsha")
        self.assertFalse(passed)
        self.assertIn("NOT READY TO MERGE", markdown)
        self.assertIn("Total remaining bugs: **1**", markdown)

    def test_semantic_only_blocker_is_not_claimed_fixed_by_static_absence(self):
        semantic = {"id": "SF1", "source": "semantic", "severity": "high",
                    "confidence": "high", "is_real_issue": None, "finding": None,
                    "ai": None, "semantic": {"suggested_fix": "safe()"}}
        markdown, _, passed = verify.make_verification(self.state(semantic), [], "newsha")
        self.assertFalse(passed)
        self.assertIn("Total bugs fixed: **0**", markdown)
        self.assertIn("NOT READY TO MERGE", markdown)

    def test_false_positive_not_counted_as_remaining_bug(self):
        false_positive = static_item(is_real=False)
        markdown, _, passed = verify.make_verification(
            self.state(false_positive), [false_positive["finding"]], "newsha")
        self.assertTrue(passed)
        self.assertIn("Total false positives: **1**", markdown)
        self.assertIn("Total remaining bugs: **0**", markdown)
        self.assertIn("Bandit: PASS", markdown)

    def test_new_high_finding_blocks_gate(self):
        baseline = static_item()
        new_finding = dict(baseline["finding"], rule_id="B999", rule_name="new_rule",
                           severity="high")
        _, _, passed = verify.make_verification(self.state(baseline), [new_finding], "newsha")
        self.assertFalse(passed)

    def test_configured_eslint_failure_prevents_ready_status(self):
        item = static_item()
        state = self.state(item)
        state["eslint_configured"] = True
        markdown, _, passed = verify.make_verification(state, [], "newsha", eslint_exit=2)
        self.assertFalse(passed)
        self.assertIn("ESLint: FAIL", markdown)


if __name__ == "__main__":
    unittest.main()
