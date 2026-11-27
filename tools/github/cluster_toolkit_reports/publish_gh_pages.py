#!/usr/bin/env python3
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

"""Publish the unified Cluster Toolkit Reports Dashboard to GitHub Pages.

Stages `reports_dashboard.html` as `index.html` alongside `.nojekyll` and
`stale_pr_master_tracker.xlsx` on a clean single-commit orphan branch
(default: `gh-pages`) and pushes it to a configured standalone repository.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from build_reports_dashboard import DEFAULT_REPO, build_reports_dashboard  # noqa: E402


def stage_pages_directory(
    stage_dir: Path,
    reports_html: Path,
    master_xlsx: Path | None = None,
) -> list[str]:
    """Copy built dashboard artifacts into a clean GitHub Pages staging folder."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged: list[str] = ["index.html", ".nojekyll"]
    shutil.copyfile(reports_html, stage_dir / "index.html")
    (stage_dir / ".nojekyll").write_text("", encoding="utf-8")
    if master_xlsx and master_xlsx.exists():
        shutil.copyfile(master_xlsx, stage_dir / "stale_pr_master_tracker.xlsx")
        staged.append("stale_pr_master_tracker.xlsx")
    return staged


def publish_reports_site(
    output_dir: Path,
    remote_url: str,
    repo: str = DEFAULT_REPO,
    branch: str = "gh-pages",
    sheet_url: str = "",
    refresh_live: bool = True,
    git_name: str = "cluster-toolkit-reports-bot",
    git_email: str = "cluster-toolkit-reports-bot@users.noreply.github.com",
) -> None:
    """Build (optional) and force-push an orphan GitHub Pages deployment branch."""
    reports_html = build_reports_dashboard(
        output_dir=output_dir,
        repo=repo,
        sheet_url=sheet_url,
        refresh_live=refresh_live,
    )
    history_dir = output_dir / "history"
    snaps = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(history_dir.glob("????-??-??.json"))
    ]
    latest_date = snaps[-1]["date"] if snaps else "latest"
    master_xlsx = output_dir / "stale_pr_master_tracker.xlsx"

    with tempfile.TemporaryDirectory(prefix="ctk_reports_pages_") as tmp_dir:
        stage = Path(tmp_dir)
        stage_pages_directory(stage, reports_html, master_xlsx)

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(stage), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

        git("init", "-b", branch)
        git("config", "user.name", git_name)
        git("config", "user.email", git_email)
        git("add", "-A")
        git("commit", "-m", f"chore(pages): update Cluster Toolkit Reports ({latest_date})")
        git("push", "--force", remote_url, f"HEAD:refs/heads/{branch}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for publishing the reports site."""
    parser = argparse.ArgumentParser(
        description="Publish Cluster Toolkit Reports Dashboard to GitHub Pages."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("REPORT_OUTPUT_DIR", "./cluster_toolkit_reports")),
        help="Directory holding history/, reports_cache/, and Excel tracker",
    )
    parser.add_argument(
        "--remote",
        default=os.environ.get("REPORTS_PAGES_REMOTE", ""),
        required=not bool(os.environ.get("REPORTS_PAGES_REMOTE", "")),
        help="Target GitHub Pages repository URL (e.g., https://github.com/<user>/cluster-toolkit-reports.git)",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Upstream owner/repo to query")
    parser.add_argument("--branch", default="gh-pages", help="Target Pages branch")
    parser.add_argument("--sheet-url", default=os.environ.get("SHEET_SHARE_URL", ""))
    parser.add_argument("--no-refresh", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    publish_reports_site(
        output_dir=args.output_dir,
        remote_url=args.remote,
        repo=args.repo,
        branch=args.branch,
        sheet_url=args.sheet_url,
        refresh_live=not args.no_refresh,
    )
    print(f"Published Cluster Toolkit Reports to {args.remote} ({args.branch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
