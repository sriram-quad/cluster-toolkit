# Stale External PRs Automated Report

Automates the weekday (Monday–Friday, 5:00 PM Asia/Kolkata / 11:30 UTC) **Stale External PRs**
report for `GoogleCloudPlatform/cluster-toolkit` with zero third-party Python
dependencies.

## What It Fetches (GitHub REST API)

1. `GET /repos/{owner}/{repo}/pulls?state=open&per_page=100` — lists all open PRs and
   filters non-draft, non-bot PRs labeled `external` (including both external
   community/vendor and `*-quad` Quadrant PRs) that are at least 7 days old.
2. `GET /repos/{owner}/{repo}/pulls/{number}` — retrieves mergeable state, merge
   conflicts, target branch, commit count, and diff statistics (`+add/-del`).
3. `GET /repos/{owner}/{repo}/pulls/{number}/reviews` — inspects reviewer approvals,
   requested changes, inline comments, and Gemini Code Assist suggestions.
4. `GET /repos/{owner}/{repo}/pulls/{number}/commits` — identifies the latest commit
   timestamp and detects whether the author pushed updates after a review.
5. `GET /repos/{owner}/{repo}/issues/{number}/comments` — reads human conversation
   timestamps and rebase/pre-commit requests.
6. `GET /repos/{owner}/{repo}/pulls/{prev_number}` — checks PRs from the previous
   snapshot that left the open list to classify them as `Merged` or `Closed`.

## Outputs Generated

- `stale_pr_master_tracker.xlsx` — Multi-sheet Excel workbook (`Latest Report`,
  `Summary & Reviewer Queue`, `Day-over-Day Changes`, `All Days Combined`, plus
  rolling daily dated tabs).
- `daily/stale_pr_report_YYYY-MM-DD.xlsx` — Standalone Excel report for the day.
- `stale_pr_dashboard.html` — Self-contained interactive HTML dashboard with KPI
  cards, charts, filters, reviewer workload, and day-over-day diff.
- `daily/stale_pr_chat_alert_YYYY-MM-DD.txt` — Ready-to-post team chat summary.
- `history/YYYY-MM-DD.json` — Structured daily snapshot for historical tracking.

## Permissions Required

| Component | Permission / Secret | Required? | Purpose |
| --- | --- | --- | --- |
| GitHub Actions `GITHUB_TOKEN` | `contents: read`, `pull-requests: read`, `issues: read` | Automatic | Read-only PR, review, commit, and comment access (5,000 req/hr) |
| Local CLI run (public repo) | None (`GITHUB_TOKEN` optional) | Optional | Works unauthenticated (60 req/hr) or with a read-only token (5,000 req/hr) |
| Google Chat Alert | `GCHAT_WEBHOOK_URL` repo secret | Optional | Auto-posts the 5:00 PM report into a Google Chat space |
| Shared Sheet Button | `STALE_PR_SHEET_URL` repo variable | Optional | Adds an "Open Shared Excel Sheet" button to the HTML dashboard |

## Local Usage

```bash
python3 tools/github/stale_pr_report/generate_stale_pr_report.py \
  --repo GoogleCloudPlatform/cluster-toolkit \
  --min-age-days 7 \
  --output-dir ./stale_pr_reports
```
