#!/usr/bin/env python3
"""Unit tests for usage-report helpers."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("GITHUB_ORG", "fullsend-playground")
os.environ.setdefault("METRICS_REPO", "join-playground")
os.environ.setdefault("REPORT_PERIOD_HOURS", "1")

SCRIPT = Path(__file__).with_name("usage-report.py")
spec = importlib.util.spec_from_file_location("usage_report", SCRIPT)
usage_report = importlib.util.module_from_spec(spec)
sys.modules["usage_report"] = usage_report
spec.loader.exec_module(usage_report)


class ShouldCountRunTests(unittest.TestCase):
    def test_excludes_usage_report_by_path(self):
        run = {
            "name": "Fullsend Usage Report",
            "path": ".github/workflows/usage-report.yml",
        }
        self.assertFalse(usage_report.should_count_run(run))

    def test_counts_fullsend_agent_runs(self):
        run = {
            "name": "fullsend",
            "path": ".github/workflows/fullsend.yaml",
        }
        self.assertTrue(usage_report.should_count_run(run))

    def test_counts_invite_workflow(self):
        run = {
            "name": "Auto Invite to Org",
            "path": ".github/workflows/invite.yml",
        }
        self.assertTrue(usage_report.should_count_run(run))


class ActivityHelpersTests(unittest.TestCase):
    def test_has_user_activity_when_commands_present(self):
        totals = {
            "commands": 1,
            "issues": 0,
            "prs": 0,
            "runs": 0,
            "unique_users": 1,
        }
        self.assertTrue(usage_report.has_user_activity(totals))

    def test_no_user_activity_when_all_zero(self):
        totals = {
            "commands": 0,
            "issues": 0,
            "prs": 0,
            "runs": 0,
            "unique_users": 0,
        }
        self.assertFalse(usage_report.has_user_activity(totals))

    def test_idle_streak_counts_prior_quiet_snapshots(self):
        snapshots = [
            {"cmds": 0, "iss": 0, "prs": 0, "users": 0},
            {"cmds": 0, "iss": 0, "prs": 0, "users": 0},
            {"cmds": 0, "iss": 0, "prs": 0, "users": 0},
            {"cmds": 1, "iss": 0, "prs": 0, "users": 1},
        ]
        self.assertEqual(usage_report.idle_streak_periods(snapshots), 3)


if __name__ == "__main__":
    unittest.main()
