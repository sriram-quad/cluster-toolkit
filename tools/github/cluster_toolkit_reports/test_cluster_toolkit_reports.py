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

"""Unit tests for the unified Cluster Toolkit Reports dashboard & collector."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_reports_dashboard as brd  # noqa: E402
import publish_gh_pages as pages  # noqa: E402
import stale_pr_collector as collector  # noqa: E402


def _make_sample_snap(date_str: str, status: str = "Awaiting review.") -> dict:
    return {
        "date": date_str,
        "day_name": "Thu",
        "generated_at": f"{date_str} 05:00 PM UTC+05:30",
        "source": "Unit Test",
        "repo": "GoogleCloudPlatform/cluster-toolkit",
        "total_open_prs": 22,
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
                "days_open": 42,
                "days_inactive": 6,
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


def _make_sample_security() -> dict:
    return {
        "repo": "GoogleCloudPlatform/cluster-toolkit",
        "generated_at": "Nov 27, 2026 05:00 PM UTC+05:30",
        "generated_date": "2026-11-27",
        "code_scanning": {
            "open_count": 1,
            "main_open_count": 1,
            "develop_open_count": 1,
            "dismissed_count": 0,
            "fixed_count": 1,
            "latest_analysis_at": "2026-11-27 10:00 UTC",
            "source": "GitHub Code Scanning API (CodeQL)",
            "alerts": [
                {
                    "number": 41,
                    "alert_id": "#41",
                    "url": "https://github.com/GoogleCloudPlatform/cluster-toolkit/security/code-scanning/41",
                    "severity": "Medium",
                    "tool": "CodeQL",
                    "rule_id": "py/bind-socket-all-network-interfaces",
                    "rule_name": "Binding a socket to all network interfaces",
                    "rule_help": "Bind explicitly to a specific interface.",
                    "cwes": ["CWE-668"],
                    "file": "tools/server.py",
                    "line": "L10",
                    "area": "Tools / Tests",
                    "branches": "main, develop",
                    "message": "Socket binds to 0.0.0.0",
                    "created_iso": "2026-05-02",
                    "created_on": "May 02, 2026",
                    "closed_iso": "",
                    "closed_on": "",
                    "dismissed_reason": "",
                    "days_open": 209,
                    "state": "open",
                }
            ],
            "all_alerts": [
                {
                    "number": 44,
                    "alert_id": "#44",
                    "url": "https://github.com/GoogleCloudPlatform/cluster-toolkit/security/code-scanning/44",
                    "severity": "Medium",
                    "tool": "CodeQL",
                    "rule_id": "actions/missing-workflow-permissions",
                    "rule_name": "Missing workflow permissions",
                    "rule_help": "Declare explicit permissions.",
                    "cwes": ["CWE-275"],
                    "file": ".github/workflows/ci.yml",
                    "line": "L15",
                    "area": ".github",
                    "branches": "main",
                    "message": "Permissions block missing.",
                    "created_iso": "2026-06-25",
                    "created_on": "Jun 25, 2026",
                    "closed_iso": "2026-07-22",
                    "closed_on": "Jul 22, 2026",
                    "dismissed_reason": "",
                    "days_open": 27,
                    "state": "fixed",
                },
                {
                    "number": 41,
                    "alert_id": "#41",
                    "url": "https://github.com/GoogleCloudPlatform/cluster-toolkit/security/code-scanning/41",
                    "severity": "Medium",
                    "tool": "CodeQL",
                    "rule_id": "py/bind-socket-all-network-interfaces",
                    "rule_name": "Binding a socket to all network interfaces",
                    "rule_help": "Bind explicitly to a specific interface.",
                    "cwes": ["CWE-668"],
                    "file": "tools/server.py",
                    "line": "L10",
                    "area": "Tools / Tests",
                    "branches": "main, develop",
                    "message": "Socket binds to 0.0.0.0",
                    "created_iso": "2026-05-02",
                    "created_on": "May 02, 2026",
                    "closed_iso": "",
                    "closed_on": "",
                    "dismissed_reason": "",
                    "days_open": 209,
                    "state": "open",
                },
            ],
        },
        "vulnerabilities": {
            "open_count": 1,
            "sbom_packages_scanned": 487,
            "repo_advisories": [],
            "alerts": [
                {
                    "id": "CVE-2026-11332",
                    "primary_osv_id": "GHSA-w8p5-mx5w-cpqj",
                    "cve": "CVE-2026-11332",
                    "ghsa": "GHSA-w8p5-mx5w-cpqj",
                    "aliases": ["CVE-2026-11332", "GHSA-w8p5-mx5w-cpqj"],
                    "ecosystem": "PyPI",
                    "package": "ansible-core",
                    "installed_version": "2.15.13",
                    "fixed_version": "2.19.11",
                    "severity": "High",
                    "cvss": 8.1,
                    "summary": "Argument injection in ansible-galaxy",
                    "details": "Upgrade ansible-core.",
                    "published_on": "2026-10-30",
                    "url": "https://github.com/advisories/GHSA-w8p5-mx5w-cpqj",
                    "source": "SBOM + OSV.dev",
                }
            ],
        },
    }


def test_filter_stale_candidates_and_diff():
    tz = ZoneInfo("Asia/Kolkata")
    now = dt.datetime(2026, 11, 26, 17, 0, tzinfo=tz)
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
            "number": 104,
            "draft": False,
            "user": {"login": "dev-quad", "type": "User"},
            "labels": [{"name": "external"}],
            "created_at": "2026-10-01T00:00:00Z",
        },
    ]
    matched = collector.filter_stale_candidates(pulls, now, tz, min_age_days=7)
    assert [p["number"] for p in matched] == [100, 104]
    ext_only = collector.filter_stale_candidates(
        pulls, now, tz, min_age_days=7, exclude_quad=True
    )
    assert [p["number"] for p in ext_only] == [100]

    prev = _make_sample_snap("2026-11-25", status="Old")
    curr = _make_sample_snap("2026-11-26", status="Old")
    collector.apply_day_over_day_diff(curr["rows"], prev)
    assert curr["rows"][0]["daily_change"] == "Unchanged"
    curr["rows"][0]["last_action_on"] = "Nov 26, 2026"
    collector.apply_day_over_day_diff(curr["rows"], prev)
    assert curr["rows"][0]["daily_change"] == "Status Updated"


def test_write_workbook_valid_openxml(tmp_path: Path):
    prev = _make_sample_snap("2026-11-25")
    curr = _make_sample_snap("2026-11-26")
    s1, r1 = collector.build_snapshot_sheet(curr, selected=True)
    s2 = collector.build_summary_sheet(curr, [prev, curr])
    s3, r3 = collector.build_diff_sheet(curr, prev)
    s4, r4 = collector.build_all_combined_sheet([prev, curr])

    out = tmp_path / "tracker.xlsx"
    collector.write_workbook(
        out,
        [
            ("Latest Report", s1, r1),
            ("Summary & Reviewer Queue", s2, None),
            ("Day-over-Day Changes", s3, r3),
            ("All Days Combined", s4, r4),
        ],
    )
    with zipfile.ZipFile(out, "r") as zf:
        assert zf.testzip() is None
        for member in zf.namelist():
            ET.fromstring(zf.read(member))


def test_security_and_text_helpers():
    assert brd.parse_cvss_score({"database_specific": {"severity": "MODERATE"}}) == (
        "Medium",
        None,
    )
    assert brd.parse_cvss_score({"severity": [{"score": "9.8"}]}) == ("Critical", 9.8)
    fixed = brd.extract_fixed_version(
        {
            "affected": [
                {
                    "package": {"name": "ansible-core"},
                    "ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.19.11"}]}],
                }
            ]
        },
        "ansible-core",
    )
    assert fixed == "2.19.11"
    cleaned = brd.clean_text(
        "<!-- hidden -->\nHello world\n\n### Submission Checklist\n- item",
        strip_checklist=True,
    )
    assert cleaned == "Hello world"
    sanitized = brd.sanitize_snapshots([{"source": "Manual Report (Alice)"}])
    assert sanitized[0]["source"] == "Team Baseline Report"


def test_build_reports_dashboard_and_pages_stage(tmp_path: Path):
    history_dir = tmp_path / "history"
    cache_dir = tmp_path / "reports_cache"
    history_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)

    snap = _make_sample_snap("2026-11-26")
    (history_dir / "2026-11-26.json").write_text(json.dumps(snap), encoding="utf-8")
    (cache_dir / "security_snapshot.json").write_text(
        json.dumps(_make_sample_security()), encoding="utf-8"
    )
    (cache_dir / "newest_prs.json").write_text(
        json.dumps({"generated_at": "Nov 27, 2026", "date": "2026-11-27", "rows": []}),
        encoding="utf-8",
    )
    (cache_dir / "pr_deep_details.json").write_text(
        json.dumps(
            {
                "6493": {
                    "review_comments": [
                        {
                            "id": 1,
                            "review_id": 10,
                            "author": "Adarsh Kullachi",
                            "path": "docs/ofe.md",
                            "line": 12,
                            "date": "2026-11-20",
                            "body": "Please rebase onto develop.",
                            "url": "https://github.com/GoogleCloudPlatform/cluster-toolkit/pull/6493#discussion_r1",
                        }
                    ],
                    "comments": [],
                    "gemini_comments": [],
                    "reviews": [],
                    "commits": [],
                    "files": [],
                    "checks": {"passed": 1, "failed": 0, "pending": 0, "neutral": 0, "runs": []},
                }
            }
        ),
        encoding="utf-8",
    )

    out_html = brd.build_reports_dashboard(tmp_path, refresh_live=False)
    html = out_html.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert "no-cache, no-store, must-revalidate" in html
    assert brd.PLACEHOLDER not in html
    assert "Report Date (Full Day Snapshot)" in html

    match = re.search(r"const DATA = (\{.*?\});\n", html)
    assert match is not None
    embedded = json.loads(match.group(1).replace("<\\/", "</"))
    assert len(embedded["snapshots"]) == 1
    assert embedded["security"]["code_scanning"]["open_count"] == 1
    assert len(embedded["security"]["code_scanning"]["all_alerts"]) == 2
    assert embedded["prDetails"]["6493"]["review_comments"][0]["path"] == "docs/ofe.md"

    xlsx = tmp_path / "stale_pr_master_tracker.xlsx"
    xlsx.write_bytes(b"PK")
    stage = tmp_path / "pages_stage"
    staged = pages.stage_pages_directory(stage, out_html, xlsx)
    assert staged == ["index.html", ".nojekyll", "stale_pr_master_tracker.xlsx"]
    assert (stage / "index.html").exists()
    assert (stage / ".nojekyll").exists()
