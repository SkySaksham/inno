"""Tests for static-only follow-up verification and human-applied fix metrics."""
import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import verify
import report


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


def baseline_items():
    items = []
    for index in range(6):
        severity = "high" if index < 2 else "medium"
        item = static_item(item_id=f"F{index + 1}", severity=severity)
        finding = item["finding"]
        finding["file"] = f"src/file_{index}.py"
        finding["rule_id"] = f"B{600 + index}"
        finding["rule_name"] = f"rule_{index}"
        item["severity"] = severity
        items.append(item)
    return items


def raw_finding(file, rule, severity="high"):
    rule_number = int(rule[1:])
    rule_name = (f"rule_{rule_number - 600}" if 600 <= rule_number < 700
                 else f"rule_{rule_number}")
    return {"file": file, "tool": "bandit", "rule_id": rule,
            "rule_name": rule_name, "severity": severity,
            "line": 7, "message": "current analyzer message"}


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
        self.assertIn("Resolved baseline findings: **1**", markdown)
        self.assertIn("READY TO MERGE", markdown)
        self.assertEqual(state["metrics"]["remaining_bugs"], 0)

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
        self.assertIn("Resolved baseline findings: **0**", markdown)
        self.assertIn("NOT READY TO MERGE", markdown)

    def test_false_positive_not_counted_as_remaining_bug(self):
        false_positive = static_item(is_real=False)
        markdown, _, passed = verify.make_verification(
            self.state(false_positive), [false_positive["finding"]], "newsha")
        self.assertTrue(passed)
        self.assertIn("Total false positives: **1**", markdown)
        self.assertIn("Total remaining bugs: **0**", markdown)
        self.assertIn("| Bandit | PASS |", markdown)

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
        self.assertIn("| ESLint | FAIL |", markdown)

    def test_accounting_partitions_six_baseline_into_two_unresolved_and_nine_new(self):
        baseline = baseline_items()
        state = {"initial_head": "base123", "threshold": "high",
                 "initial_results": baseline, "eslint_configured": False}
        findings = [raw_finding(f"src/file_{i}.py", f"B{600 + i}",
                                "high" if i < 2 else "medium") for i in (4, 5)]
        findings.extend(raw_finding(f"src/new_{i}.py", f"B{700 + i}") for i in range(9))
        with patch.object(verify, "suggestion_was_added", return_value=True):
            markdown, result, _ = verify.make_verification(state, findings, "head456")
        self.assertEqual(result["metrics"]["resolved"], 4)
        self.assertEqual(result["metrics"]["unresolved"], 2)
        self.assertEqual(result["metrics"]["new"], 9)
        self.assertEqual(result["metrics"]["after"], 11)
        self.assertIn("Baseline findings:       6", markdown)
        self.assertIn("High severity: **2**", markdown)
        self.assertIn("Medium severity: **4**", markdown)
        self.assertIn("Resolved:                4", markdown)
        self.assertIn("Unresolved:              2", markdown)
        self.assertIn("New findings:            9", markdown)
        self.assertIn("After verification:      11", markdown)

    def test_accounting_without_new_findings_reports_two_after_verification(self):
        baseline = baseline_items()
        state = {"initial_head": "base123", "threshold": "high",
                 "initial_results": baseline, "eslint_configured": False}
        findings = [raw_finding(f"src/file_{i}.py", f"B{600 + i}",
                                "high" if i < 2 else "medium") for i in (4, 5)]
        with patch.object(verify, "suggestion_was_added", return_value=True):
            markdown, result, _ = verify.make_verification(state, findings, "head456")
        self.assertEqual(result["metrics"]["resolved"], 4)
        self.assertEqual(result["metrics"]["unresolved"], 2)
        self.assertEqual(result["metrics"]["new"], 0)
        self.assertEqual(result["metrics"]["after"], 2)
        self.assertIn("New findings:            0", markdown)
        self.assertIn("After verification:      2", markdown)

    def test_verification_patches_existing_inno_comment_with_generated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            findings_path = os.path.join(tmp, "findings.json")
            event_path = os.path.join(tmp, "event.json")
            baseline = baseline_items()[:1]
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"initial_head": "base123", "threshold": "high",
                           "initial_results": baseline, "eslint_configured": False}, fh)
            with open(findings_path, "w", encoding="utf-8") as fh:
                json.dump({"findings": []}, fh)
            with open(event_path, "w", encoding="utf-8") as fh:
                json.dump({"pull_request": {"number": 42,
                                             "head": {"sha": "head456"}}}, fh)
            args = SimpleNamespace(state=state_path, findings=findings_path,
                                   eslint=None, eslint_exit=None)
            with patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo",
                                         "GITHUB_EVENT_PATH": event_path}, clear=False), \
                 patch.object(report, "gh", side_effect=[
                     [{"id": 99, "body": report.MARKER + "\nold report"}], {}
                 ]) as gh, self.assertRaises(SystemExit) as exit_error:
                verify.verify(args)
            self.assertEqual(exit_error.exception.code, 0)
            self.assertEqual(gh.call_count, 2)
            method, url, token, payload = gh.call_args_list[1].args
            self.assertEqual(method, "PATCH")
            self.assertTrue(url.endswith("/issues/comments/99"))
            self.assertEqual(token, "token")
            self.assertIn("post-fix verification", payload["body"])
            self.assertIn("Baseline findings:       1", payload["body"])
            state_start = payload["body"].rfind("<!-- inno-review-state:")
            encoded_start = state_start + len("<!-- inno-review-state:")
            encoded_end = payload["body"].find(" -->", encoded_start)
            saved_state = json.loads(base64.urlsafe_b64decode(
                payload["body"][encoded_start:encoded_end].encode("ascii")))
            self.assertEqual(saved_state["initial_head"], "base123")
            self.assertEqual(saved_state["metrics"]["after"], 0)

    def test_verification_fails_if_existing_comment_cannot_be_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            findings_path = os.path.join(tmp, "findings.json")
            event_path = os.path.join(tmp, "event.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"initial_head": "base123", "threshold": "high",
                           "initial_results": [], "eslint_configured": False}, fh)
            with open(findings_path, "w", encoding="utf-8") as fh:
                json.dump({"findings": []}, fh)
            with open(event_path, "w", encoding="utf-8") as fh:
                json.dump({"pull_request": {"number": 42,
                                             "head": {"sha": "head456"}}}, fh)
            args = SimpleNamespace(state=state_path, findings=findings_path,
                                   eslint=None, eslint_exit=None)
            with patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo",
                                         "GITHUB_EVENT_PATH": event_path}, clear=False), \
                 patch.object(report, "gh", return_value=[]) as gh, \
                 self.assertRaisesRegex(SystemExit, "could not update the existing"):
                verify.verify(args)
            self.assertEqual(gh.call_count, 1)


if __name__ == "__main__":
    unittest.main()
