# Copyright 2026 "Google LLC"
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the automated Stale External PRs report and dashboard."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard_builder  # noqa: E402
import generate_stale_pr_report as report  # noqa: E402


def _make_sample_snap(date_str: str, status: str = "Awaiting review.") -> dict:
    return {
        "date": date_str,
        "day_name": "Mon",
        "generated_at": f"{date_str} 05:00 PM UTC+05:30",
        "source": "Unit Test",
        "repo": "GoogleCloudPlatform/cluster-toolkit",
        "total_open_prs": 20,
        "resolved_since_prev": [],
        "rows": [
            {
                "number": 6493,
                "pr_id": "#6493",
                "url": "https://github.com/GoogleCloudPlatform/cluster-toolkit/pull/6493",
                "daily_change": "Status Updated",
                "title": "docs(ofe): update documentation",
                "author": "ek-nag",
                "contributor_type": "External",
                "base_branch": "develop",
                "labels": "external",
                "created_on": "Oct 15, 2026",
                "last_action_on": "Nov 20, 2026",
                "days_open": 39,
                "days_inactive": 3,
                "diff_summary": "2c | 3f (+40/-10)",
                "merge_ci_state": "Merge Conflicts",
                "category": "Merge Conflicts",
                "waiting_on": "Author (@ek-nag)",
                "approvals": "@Vivek Vikram Singh",
                "changes_requested": "@Adarsh Kullachi",
                "pending_reviewers": "-",
                "pending_list": [],
                "changes_req_list": ["@Adarsh Kullachi"],
                "approved_list": ["@Vivek Vikram Singh"],
                "conflicts": "Yes",
                "status": status,
            }
        ],
    }


def test_filter_stale_candidates_excludes_drafts_and_recent():
    tz = ZoneInfo("Asia/Kolkata")
    now = dt.datetime(2026, 11, 23, 17, 0, tzinfo=tz)
    pulls = [
        {
            "number": 100,
            "draft": False,
            "user": {"login": "ext-dev", "type": "User"},
            "labels": [{"name": "external"}],
            "created_at": "2026-11-10T00:00:00Z",
        },
        {
            "number": 101,
            "draft": True,
            "user": {"login": "ext-dev", "type": "User"},
            "labels": [{"name": "external"}],
            "created_at": "2026-11-01T00:00:00Z",
        },
        {
            "number": 102,
            "draft": False,
            "user": {"login": "ext-dev", "type": "User"},
            "labels": [{"name": "external"}],
            "created_at": "2026-11-21T00:00:00Z",
        },
        {
            "number": 103,
            "draft": False,
            "user": {"login": "internal-dev", "type": "User"},
            "labels": [{"name": "release-chore"}],
            "created_at": "2026-10-01T00:00:00Z",
        },
        {
            "number": 104,
            "draft": False,
            "user": {"login": "dev-quad", "type": "User"},
            "labels": [{"name": "external"}],
            "created_at": "2026-10-01T00:00:00Z",
        },
    ]
    matched = report.filter_stale_candidates(pulls, now, tz, min_age_days=7)
    assert [p["number"] for p in matched] == [100, 104]

    ext_only = report.filter_stale_candidates(
        pulls, now, tz, min_age_days=7, exclude_quad=True
    )
    assert [p["number"] for p in ext_only] == [100]


def test_day_over_day_diff_classification():
    prev = _make_sample_snap("2026-11-20", status="Old")
    curr = _make_sample_snap("2026-11-23", status="Old")
    report.apply_day_over_day_diff(curr["rows"], prev)
    assert curr["rows"][0]["daily_change"] == "Unchanged"

    curr["rows"][0]["last_action_on"] = "Nov 23, 2026"
    report.apply_day_over_day_diff(curr["rows"], prev)
    assert curr["rows"][0]["daily_change"] == "Status Updated"


def test_write_workbook_produces_valid_openxml(tmp_path: Path):
    prev = _make_sample_snap("2026-11-20")
    curr = _make_sample_snap("2026-11-23")
    s1, r1 = report.build_snapshot_sheet(curr, selected=True)
    s2 = report.build_summary_sheet(curr, [prev, curr])
    s3, r3 = report.build_diff_sheet(curr, prev)
    s4, r4 = report.build_all_combined_sheet([prev, curr])

    out = tmp_path / "tracker.xlsx"
    report.write_workbook(
        out,
        [
            ("Latest Report", s1, r1),
            ("Summary & Reviewer Queue", s2, None),
            ("Day-over-Day Changes", s3, r3),
            ("All Days Combined", s4, r4),
        ],
    )
    assert out.exists()
    with zipfile.ZipFile(out, "r") as zf:
        assert zf.testzip() is None
        for member in zf.namelist():
            ET.fromstring(zf.read(member))


def test_dashboard_and_chat_outputs():
    snap = _make_sample_snap("2026-11-23", status="Needs conflict fix.")
    snap["generated_epoch"] = 1795520000
    chat = report.build_chat_message(snap)
    assert "[#6493]" in chat
    assert "Needs conflict fix." in chat

    html = dashboard_builder.build_dashboard_html(
        [snap], excel_url="./stale_pr_master_tracker.xlsx"
    )
    assert "<!DOCTYPE html>" in html
    assert "no-cache, no-store, must-revalidate" in html
    assert "./stale_pr_master_tracker.xlsx?v=1795520000" in html
    assert "#6493" in html
    assert "{{" not in html


def test_analyze_pr_retries_when_mergeable_is_none(monkeypatch):
    monkeypatch.setattr(report.time, "sleep", lambda _s: None)
    detail_calls: list[bool] = []

    class StubClient:
        def get_json(
            self,
            url: str,
            use_cache: bool = True,
            retries: int = 3,
            force_refresh: bool = False,
        ):
            del use_cache, retries
            if url.endswith("/pulls/6493"):
                detail_calls.append(force_refresh)
                if not force_refresh:
                    return {
                        "mergeable": None,
                        "mergeable_state": "unknown",
                        "base": {"ref": "develop"},
                        "labels": [{"name": "external"}],
                        "commits": 1,
                        "changed_files": 1,
                        "additions": 5,
                        "deletions": 1,
                        "comments": 0,
                        "requested_reviewers": [],
                    }
                return {
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "base": {"ref": "develop"},
                    "labels": [{"name": "external"}],
                    "commits": 1,
                    "changed_files": 1,
                    "additions": 5,
                    "deletions": 1,
                    "comments": 0,
                    "requested_reviewers": [],
                }
            return []

        def has_cached(self, _url: str) -> bool:
            return False

        def resolve_display_name(self, login: str, allow_lookup: bool = True) -> str:
            del allow_lookup
            return f"@{login}"

    tz = ZoneInfo("Asia/Kolkata")
    now = dt.datetime(2026, 11, 24, 17, 0, tzinfo=tz)
    pr_summary = {
        "number": 6493,
        "title": "docs(ofe): update documentation",
        "html_url": "https://github.com/GoogleCloudPlatform/cluster-toolkit/pull/6493",
        "user": {"login": "ek-nag", "type": "User"},
        "labels": [{"name": "external"}],
        "created_at": "2026-10-15T00:00:00Z",
        "updated_at": "2026-11-20T00:00:00Z",
    }
    row = report.analyze_pr(
        StubClient(),  # type: ignore[arg-type]
        "GoogleCloudPlatform/cluster-toolkit",
        pr_summary,
        now,
        tz,
    )
    assert detail_calls == [False, True]
    assert row["conflicts"] == "Yes"
    assert row["category"] == "Merge Conflicts"
