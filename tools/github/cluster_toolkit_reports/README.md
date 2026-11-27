# Cluster Toolkit Unified Reports Dashboard

Automates the weekday (Monday–Friday, 5:00 PM Asia/Kolkata / 11:30 UTC)
**Cluster Toolkit Reports** suite for `GoogleCloudPlatform/cluster-toolkit`
using only the Python 3 standard library (zero third-party dependencies).

## Included Reports

1. **Overview** — Cross-report KPIs and single-click navigation across PR triage,
   Code Scanning, and Security Vulnerabilities.
2. **Stale External PRs & Newest (< 14d) PRs** — Multi-day historical snapshots,
   day-over-day diff, segregated Reviewer and Author action queues, multi-sheet
   Excel tracker (`.xlsx`), and a 4-tab **Deep PR Inspector** featuring CI check
   runs, recent commits, changed files, nested inline code-review comments,
   conversation comments, and Gemini Code Assist suggestions.
3. **Code Scanning Alerts (CodeQL)** — Full historical alert reconstruction
   (open, fixed, and dismissed timestamps) with a visual **Report Date**
   calendar showing the exact alerts open on any chosen day.
4. **Security Vulnerabilities (SBOM + OSV.dev)** — Dependency-graph SBOM queried
   against OSV.dev, deduplicated across CVE/GHSA/GO aliases with package upgrade
   targets, published repository advisories, and historical date filtering.

## What It Fetches (Read-Only APIs)

- `GET /repos/{owner}/{repo}/pulls?state=open` — Open external PRs (stale and `< 14d`).
- `GET /repos/{owner}/{repo}/pulls/{number}` — Merge/conflict state, branch, diff stats.
- `GET /repos/{owner}/{repo}/pulls/{number}/reviews` — Human reviews and states.
- `GET /repos/{owner}/{repo}/pulls/{number}/comments` — Inline code-review threads and Gemini suggestions.
- `GET /repos/{owner}/{repo}/issues/{number}/comments` — PR conversation comments.
- `GET /repos/{owner}/{repo}/pulls/{number}/commits` and `/files` — Commits and top changed files.
- `GET /repos/{owner}/{repo}/commits/{sha}/check-runs` — Latest CI check-run statuses.
- `GET /repos/{owner}/{repo}/code-scanning/alerts` and `/analyses` — Full CodeQL alert lifecycle.
- `GET /repos/{owner}/{repo}/dependency-graph/sbom` and `/security-advisories` — SBOM packages and repo advisories.
- `POST https://api.osv.dev/v1/querybatch` and `GET https://api.osv.dev/v1/vulns/{id}` — OSV vulnerability metadata.

## Outputs Generated

- `reports_dashboard.html` — Self-contained interactive HTML dashboard (Executive Light and Dark themes).
- `stale_pr_master_tracker.xlsx` — Multi-sheet Excel workbook (`Latest Report`, `Summary & Reviewer Queue`, `Day-over-Day Changes`, `All Days Combined`, plus rolling daily tabs).
- `daily/stale_pr_report_YYYY-MM-DD.xlsx` — Standalone daily Excel report.
- `daily/stale_pr_chat_alert_YYYY-MM-DD.txt` — Ready-to-post team chat summary.
- `history/YYYY-MM-DD.json` and `reports_cache/*.json` — Daily snapshots and security/PR caches.

## Permissions Required

| Component | Permission / Secret | Required? | Purpose |
| --- | --- | --- | --- |
| GitHub Actions `GITHUB_TOKEN` | `contents: read`, `pull-requests: read`, `issues: read`, `checks: read`, `security-events: read` | Automatic | Strictly read-only access to PRs, comments, checks, SBOM, and CodeQL history |
| Local CLI run | `GITHUB_TOKEN` (optional) | Optional | Raises API quota to 5,000 req/hr and enables Code Scanning reads |
| Google Chat Alert | `GCHAT_WEBHOOK_URL` repo secret | Optional | Auto-posts the 5:00 PM summary into a Google Chat space |
| Shared Sheet Link | `STALE_PR_SHEET_URL` repo variable | Optional | Adds an `Export Sheet` link button to the dashboard header |

## Local Usage

```bash
# 1. Collect Stale External PR snapshot & multi-sheet Excel tracker
python3 tools/github/cluster_toolkit_reports/stale_pr_collector.py \
  --repo GoogleCloudPlatform/cluster-toolkit \
  --min-age-days 7 \
  --output-dir ./cluster_toolkit_reports

# 2. Build the unified interactive HTML dashboard
python3 tools/github/cluster_toolkit_reports/build_reports_dashboard.py \
  --repo GoogleCloudPlatform/cluster-toolkit \
  --output-dir ./cluster_toolkit_reports

# 3. (Optional) Publish standalone dashboard to GitHub Pages
python3 tools/github/cluster_toolkit_reports/publish_gh_pages.py \
  --output-dir ./cluster_toolkit_reports \
  --remote https://github.com/<owner>/cluster-toolkit-reports.git
```
