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

"""Automated Stale External PRs Fetcher, Excel Generator & Dashboard Builder.

OVERVIEW
--------
Replaces manual weekday PR triage for GoogleCloudPlatform/cluster-toolkit by:
  1. [FETCH] Querying live open pull requests, reviews, commits, conversation
     comments, and merge/conflict state from the GitHub REST API (api.github.com).
  2. [ACTION ITEM] Classifying each stale external PR into an actionable status
     bucket (Merge Conflicts, Changes Requested, Awaiting Re-Review, Approved
     Needs 2nd Review, Awaiting Review) and assigning a clear Next Action Owner
     (specific Reviewer(s) or PR Author), plus computing Day-over-Day diffs and
     tracking Merged/Closed PRs.
  3. [REPORT GEN] Generating a formatted multi-sheet Excel workbook (.xlsx), a
     self-contained interactive HTML dashboard (.html), a GitHub Actions Step
     Summary table, and a ready-to-post Google Chat alert (.txt) using 100%
     Python 3 standard library (zero third-party pip dependencies).
  4. [AUTO SCHEDULE] Designed to execute unattended every Monday-Friday at
     5:00 PM Asia/Kolkata (11:30 UTC) via `.github/workflows/stale-external-pr-report.yml`
     or a local background scheduler, with ETag HTTP caching, automatic retries,
     and optional GitHub rate-limit wait/resume.

PERMISSIONS SUMMARY
-------------------
  - GitHub REST API (public repo): READ-ONLY. Works unauthenticated (60 req/hr)
    or with `GITHUB_TOKEN` / `GH_TOKEN` (`pull-requests: read`, `issues: read`
    for 5,000 req/hr; automatically supplied in GitHub Actions).
  - Never writes to, comments on, labels, or closes any GitHub PR or issue.
  - Local filesystem: Write access to `--output-dir` and `--cache-dir` only.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as xml_escape
import zipfile
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = "GoogleCloudPlatform/cluster-toolkit"
DEFAULT_LABEL = "external"
DEFAULT_MIN_AGE_DAYS = 7
DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_OUTPUT_DIR = Path("stale_pr_reports")
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "stale-pr-report" / "http_cache"
DEFAULT_REVIEWER_MAP = SCRIPT_DIR / "reviewer_map.json"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "stale-pr-report" / "github_token"
MAX_DAILY_TABS = 30

# Bot accounts excluded when computing human review states and last-action dates.
BOT_LOGINS = {
    "gemini-code-assist[bot]",
    "github-actions[bot]",
    "google-cla[bot]",
    "dependabot[bot]",
    "renovate[bot]",
    "codecov[bot]",
    "sonarcloud[bot]",
    "ctk-pr-bot",
}


def sanitize_xml_text(value: object) -> str:
    """Strip disallowed XML 1.0 control characters and escape XML entities."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))
    return xml_escape(cleaned, {'"': "&quot;", "'": "&apos;"})


def sanitize_sheet_name(name: str) -> str:
    """Ensure a valid Excel worksheet name (<=31 chars, no []:*?/\\)."""
    cleaned = re.sub(r"[\[\]:*?/\\]", "-", name).strip("'")
    return cleaned[:31] or "Sheet"


def col_letter(col_idx_1based: int) -> str:
    """Convert 1-based column index to Excel column letters (1 -> A, 27 -> AA)."""
    result = []
    n = col_idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def parse_gh_ts(ts: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 GitHub timestamp string into a UTC-aware datetime."""
    if not ts:
        return None
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fmt_date(d: dt.datetime | None, tz: ZoneInfo) -> str:
    """Format a datetime into human-readable report format (e.g., Nov 20, 2026)."""
    if not d:
        return "Unknown"
    return d.astimezone(tz).strftime("%b %d, %Y")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes via temp file + rename to prevent partial files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text to disk."""
    atomic_write_bytes(path, text.encode("utf-8"))


# ===========================================================================
# SECTION 1 — PERMISSIONS, AUTHENTICATION & GITHUB REST API CLIENT
# ===========================================================================


def load_token() -> str | None:
    """Load an optional read-only GitHub token.

    [PERMISSION]
      - In GitHub Actions, `GITHUB_TOKEN` is injected automatically with
        read-only `pull-requests: read` and `issues: read` permissions (5,000 req/hr).
      - In local runs, no token is strictly required for public repositories
        (60 req/hr). Optionally set `GITHUB_TOKEN` / `GH_TOKEN` or store a
        fine-grained read-only token in `~/.config/stale-pr-report/github_token`.
    """
    for env_key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = (os.environ.get(env_key) or "").strip()
        if val:
            return val
    if DEFAULT_TOKEN_FILE.exists():
        try:
            val = DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    return None


def load_reviewer_map(path: Path = DEFAULT_REVIEWER_MAP) -> dict[str, str]:
    """Load GitHub handle -> display name mappings from reviewer_map.json."""
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k).lower(): str(v).strip() for k, v in raw.items() if v}
        except (OSError, json.JSONDecodeError):
            pass
    return {}


class GitHubClient:
    """Read-only GitHub REST API v3 client with ETag cache, retry & rate-limit wait."""

    def __init__(
        self,
        token: str | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        reviewer_map: dict[str, str] | None = None,
        wait_on_ratelimit: bool = False,
        max_wait_seconds: int = 3700,
    ):
        self.token = token if token is not None else load_token()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.reviewer_map = reviewer_map or {}
        self.wait_on_ratelimit = wait_on_ratelimit
        self.max_wait_seconds = max_wait_seconds
        self.rate_remaining: int | None = None
        self.api_calls = 0
        self.cache_hits = 0
        self.cache_fallbacks = 0
        self._user_name_cache: dict[str, str] = {}

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.meta.json", self.cache_dir / f"{key}.body.json"

    def has_cached(self, url: str) -> bool:
        _, body = self._cache_paths(url)
        return body.exists()

    def get_json(
        self,
        url: str,
        use_cache: bool = True,
        retries: int = 3,
        force_refresh: bool = False,
    ):
        """[FETCH] Perform a conditional GET request against api.github.com.

        Sends `If-None-Match` when an ETag is cached locally (HTTP 304 saves
        bandwidth and authenticated quota), retries transient 5xx/network errors
        with exponential backoff, and sleeps until `X-RateLimit-Reset` when
        `wait_on_ratelimit=True` for unattended scheduled runs.
        """
        meta_path, body_path = self._cache_paths(url)
        base_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "cluster-toolkit-stale-pr-report/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            base_headers["Authorization"] = f"Bearer {self.token}"

        attempt = 0
        while True:
            attempt += 1
            headers = dict(base_headers)
            if (
                use_cache
                and not force_refresh
                and meta_path.exists()
                and body_path.exists()
            ):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta.get("etag"):
                        headers["If-None-Match"] = meta["etag"]
                except (OSError, json.JSONDecodeError):
                    pass

            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.api_calls += 1
                    rem = resp.headers.get("X-RateLimit-Remaining")
                    if rem is not None and rem.isdigit():
                        self.rate_remaining = int(rem)
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    etag = resp.headers.get("ETag")
                    if use_cache:
                        meta_path.write_text(
                            json.dumps(
                                {"etag": etag, "url": url, "cached_at": int(time.time())}
                            ),
                            encoding="utf-8",
                        )
                        body_path.write_text(raw, encoding="utf-8")
                    return data
            except urllib.error.HTTPError as exc:
                rem = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
                reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
                if rem is not None and rem.isdigit():
                    self.rate_remaining = int(rem)

                if exc.code == 304 and body_path.exists():
                    self.cache_hits += 1
                    return json.loads(body_path.read_text(encoding="utf-8"))

                if exc.code in {403, 429} and self.rate_remaining == 0:
                    if self.wait_on_ratelimit and reset and reset.isdigit():
                        sleep_for = max(5, int(reset) - int(time.time()) + 5)
                        if sleep_for <= self.max_wait_seconds:
                            print(
                                f"[info] Rate limit reached; sleeping {sleep_for}s until reset...",
                                file=sys.stderr,
                            )
                            time.sleep(sleep_for)
                            continue
                    if body_path.exists():
                        self.cache_fallbacks += 1
                        return json.loads(body_path.read_text(encoding="utf-8"))

                if exc.code in {500, 502, 503, 504} and attempt <= retries:
                    time.sleep(2**attempt)
                    continue

                body_msg = ""
                try:
                    body_msg = exc.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    pass
                raise RuntimeError(
                    f"GitHub API HTTP {exc.code} for {url}: {body_msg}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt <= retries:
                    time.sleep(2**attempt)
                    continue
                if body_path.exists():
                    self.cache_fallbacks += 1
                    return json.loads(body_path.read_text(encoding="utf-8"))
                raise RuntimeError(f"Network error fetching {url}: {exc}") from exc

    def list_open_pulls(self, repo: str) -> list[dict]:
        """[FETCH] Paginate all open PRs from GET /repos/{repo}/pulls?state=open."""
        pulls: list[dict] = []
        page = 1
        while True:
            url = (
                f"https://api.github.com/repos/{repo}/pulls"
                f"?state=open&per_page=100&page={page}"
            )
            batch = self.get_json(url)
            if not batch:
                break
            pulls.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return pulls

    def resolve_display_name(self, login: str, allow_lookup: bool = True) -> str:
        """[FETCH] Resolve reviewer handle via reviewer_map.json or GET /users/{login}."""
        mapped = self.reviewer_map.get(login.lower())
        if mapped:
            return f"@{mapped}"
        if login in self._user_name_cache:
            return self._user_name_cache[login]
        url = f"https://api.github.com/users/{urllib.parse.quote(login)}"
        if self.has_cached(url) or (
            allow_lookup and (self.rate_remaining is None or self.rate_remaining > 10)
        ):
            try:
                profile = self.get_json(url, use_cache=True)
                name = (profile.get("name") or "").strip()
                display = f"@{name}" if name else f"@{login}"
                self._user_name_cache[login] = display
                return display
            except RuntimeError:
                pass
        return f"@{login}"


# ===========================================================================
# SECTION 2 — PER-PR DATA FETCHING & ACTION ITEM / STATUS CLASSIFICATION
# ===========================================================================


def is_bot(user_obj: dict | None) -> bool:
    if not user_obj:
        return True
    login = (user_obj.get("login") or "").lower()
    utype = (user_obj.get("type") or "").lower()
    return utype == "bot" or login.endswith("[bot]") or login in BOT_LOGINS


def join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def describe_merge_ci_state(mergeable: bool | None, mergeable_state: str) -> str:
    st = (mergeable_state or "unknown").lower()
    if mergeable is False or st == "dirty":
        return "Merge Conflicts"
    if st == "behind":
        return "Needs Rebase (Behind)"
    if st == "unstable":
        return "CI Failing / Pending"
    if st == "blocked":
        return "Review / Checks Required"
    if st == "clean":
        return "Clean (Merge Ready)"
    return "Checking / Unknown"


def filter_stale_candidates(
    all_pulls: list[dict],
    now_local: dt.datetime,
    tz: ZoneInfo,
    label: str = DEFAULT_LABEL,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    include_drafts: bool = False,
    exclude_quad: bool = False,
) -> list[dict]:
    """Filter open PRs to non-draft external and Quadrant (*-quad) PRs aged >= min_age_days."""
    candidates: list[dict] = []
    for p in sorted(all_pulls, key=lambda x: x["number"]):
        if p.get("draft") and not include_drafts:
            continue
        author = (p.get("user") or {}).get("login") or ""
        if is_bot(p.get("user")):
            continue
        if exclude_quad and author.lower().endswith("-quad"):
            continue

        labels = {lbl["name"].lower() for lbl in p.get("labels", [])}
        if label:
            if label.lower() not in labels:
                continue
        elif p.get("author_association") in {"OWNER", "MEMBER"}:
            continue

        created_dt = parse_gh_ts(p.get("created_at"))
        if created_dt:
            age_days = (now_local.date() - created_dt.astimezone(tz).date()).days
            if age_days < min_age_days:
                continue
        candidates.append(p)
    return candidates


def analyze_pr(
    client: GitHubClient,
    repo: str,
    pr_summary: dict,
    now_local: dt.datetime,
    tz: ZoneInfo,
    allow_extra_calls: bool = True,
) -> dict:
    """[FETCH + ACTION ITEM] Fetch full activity for one PR and determine action owner.

    Endpoints queried (all READ-ONLY):
      1. GET /repos/{repo}/pulls/{num}            -> mergeable, conflicts, diff stats
      2. GET /repos/{repo}/pulls/{num}/reviews    -> APPROVED / CHANGES_REQUESTED / comments
      3. GET /repos/{repo}/pulls/{num}/commits    -> latest commit date & post-review push
      4. GET /repos/{repo}/issues/{num}/comments  -> human discussion & rebase requests
    """
    num = pr_summary["number"]
    author = pr_summary["user"]["login"]
    title = pr_summary["title"].strip()
    html_url = pr_summary["html_url"]
    created_dt = parse_gh_ts(pr_summary["created_at"])
    updated_dt = parse_gh_ts(pr_summary["updated_at"])

    # 1. [FETCH] PR detail (mergeable, mergeable_state, base branch, diff stats)
    detail_url = f"https://api.github.com/repos/{repo}/pulls/{num}"
    detail = client.get_json(detail_url)
    mergeable = detail.get("mergeable")
    if mergeable is None and allow_extra_calls:
        # GitHub computes mergeability asynchronously after base-branch pushes;
        # retry once bypassing ETag so cached `mergeable: null` resolves cleanly.
        time.sleep(1.5)
        try:
            refreshed = client.get_json(
                detail_url, use_cache=True, force_refresh=True
            )
            if isinstance(refreshed, dict):
                detail = refreshed
                mergeable = detail.get("mergeable")
        except RuntimeError:
            pass
    mergeable_state = (detail.get("mergeable_state") or "unknown").lower()
    has_conflicts = mergeable is False or mergeable_state == "dirty"
    is_behind = mergeable_state == "behind"
    merge_ci_state = describe_merge_ci_state(mergeable, mergeable_state)
    base_branch = (detail.get("base") or {}).get("ref") or "develop"
    labels_list = [
        lbl["name"] for lbl in (detail.get("labels") or pr_summary.get("labels") or [])
    ]
    commit_count = detail.get("commits") or 0
    changed_files = detail.get("changed_files") or 0
    additions = detail.get("additions") or 0
    deletions = detail.get("deletions") or 0
    diff_summary = f"{commit_count}c | {changed_files}f (+{additions}/-{deletions})"

    # 2. [FETCH] PR reviews and commits
    reviews = client.get_json(
        f"https://api.github.com/repos/{repo}/pulls/{num}/reviews?per_page=100"
    )
    commits = client.get_json(
        f"https://api.github.com/repos/{repo}/pulls/{num}/commits?per_page=100"
    )

    # 3. [FETCH] PR conversation comments (only when comments > 0)
    issue_comments: list[dict] = []
    ic_url = f"https://api.github.com/repos/{repo}/issues/{num}/comments?per_page=100"
    if (detail.get("comments") or 0) > 0 and (
        client.has_cached(ic_url) or allow_extra_calls
    ):
        try:
            issue_comments = client.get_json(ic_url)
        except RuntimeError:
            issue_comments = []

    # Determine latest commit timestamp
    last_commit_dt: dt.datetime | None = None
    for c in commits:
        c_info = c.get("commit") or {}
        c_ts = parse_gh_ts(
            (c_info.get("committer") or {}).get("date")
            or (c_info.get("author") or {}).get("date")
        )
        if c_ts and (last_commit_dt is None or c_ts > last_commit_dt):
            last_commit_dt = c_ts

    # Aggregate latest review state per human reviewer + flag Gemini suggestions
    has_gemini_suggestions = False
    latest_decisive_by_reviewer: dict[str, tuple[str, dt.datetime]] = {}
    commented_reviewers: dict[str, dt.datetime] = {}
    last_human_review_dt: dt.datetime | None = None
    last_reviewer_login: str | None = None
    last_author_review_reply_dt: dt.datetime | None = None

    for rev in reviews:
        user = rev.get("user") or {}
        login = user.get("login") or ""
        state = (rev.get("state") or "").upper()
        sub_dt = parse_gh_ts(rev.get("submitted_at"))
        if "gemini-code-assist" in login.lower():
            has_gemini_suggestions = True
            continue
        if is_bot(user) or not sub_dt:
            continue
        if login == author:
            if last_author_review_reply_dt is None or sub_dt > last_author_review_reply_dt:
                last_author_review_reply_dt = sub_dt
            continue

        if last_human_review_dt is None or sub_dt > last_human_review_dt:
            last_human_review_dt = sub_dt
            last_reviewer_login = login

        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            prev = latest_decisive_by_reviewer.get(login)
            if prev is None or sub_dt >= prev[1]:
                latest_decisive_by_reviewer[login] = (state, sub_dt)
        elif state == "COMMENTED":
            prev_c = commented_reviewers.get(login)
            if prev_c is None or sub_dt >= prev_c:
                commented_reviewers[login] = sub_dt

    # Inspect non-bot conversation comments for last-action date & rebase requests
    last_human_comment_dt: dt.datetime | None = None
    last_author_issue_comment_dt: dt.datetime | None = None
    rebase_requested = False
    for ic in issue_comments:
        user = ic.get("user") or {}
        login = user.get("login") or ""
        c_dt = parse_gh_ts(ic.get("created_at"))
        body_lower = (ic.get("body") or "").strip().lower()
        if "gemini-code-assist" in login.lower():
            has_gemini_suggestions = True
            continue
        if is_bot(user) or not c_dt:
            continue
        if last_human_comment_dt is None or c_dt > last_human_comment_dt:
            last_human_comment_dt = c_dt
        if login == author and (
            last_author_issue_comment_dt is None or c_dt > last_author_issue_comment_dt
        ):
            last_author_issue_comment_dt = c_dt
        if login != author and ("rebase" in body_lower or "pre-commit" in body_lower):
            if not last_commit_dt or c_dt > last_commit_dt:
                rebase_requested = True

    requested_logins = [
        r["login"]
        for r in (
            detail.get("requested_reviewers")
            or pr_summary.get("requested_reviewers")
            or []
        )
        if not is_bot(r)
    ]
    requested_set = set(requested_logins)

    approved_logins = sorted(
        login
        for login, (state, _) in latest_decisive_by_reviewer.items()
        if state == "APPROVED" and login not in requested_set
    )
    changes_req_logins = sorted(
        login
        for login, (state, _) in latest_decisive_by_reviewer.items()
        if state == "CHANGES_REQUESTED" and login not in requested_set
    )
    suggested_only_logins = sorted(
        login
        for login, c_dt in commented_reviewers.items()
        if login not in latest_decisive_by_reviewer
        and login not in requested_set
        and (not last_commit_dt or c_dt > last_commit_dt)
    )

    author_pushed_after_review = bool(
        last_commit_dt
        and last_human_review_dt
        and last_commit_dt > last_human_review_dt
    )
    author_pushed_followup = bool(
        last_commit_dt
        and created_dt
        and (last_commit_dt - created_dt) > dt.timedelta(hours=12)
        and commit_count > 1
    )

    candidate_dts = [
        d
        for d in (
            created_dt,
            last_commit_dt,
            last_human_review_dt,
            last_author_review_reply_dt,
            last_human_comment_dt,
        )
        if d is not None
    ]
    last_action_dt = max(candidate_dts) if candidate_dts else updated_dt

    def fmt_user(login: str) -> str:
        return client.resolve_display_name(login, allow_lookup=allow_extra_calls)

    approved_names = [fmt_user(u) for u in approved_logins]
    changes_req_names = [fmt_user(u) for u in changes_req_logins]
    suggested_names = [fmt_user(u) for u in suggested_only_logins]
    pending_names = [fmt_user(u) for u in requested_logins]
    feedback_names = list(dict.fromkeys(changes_req_names + suggested_names))

    # [ACTION ITEM] Classify PR into Action Category, Next Action Owner & Status text
    status_parts: list[str] = []
    category = "Awaiting Review"
    waiting_on = (
        f"Reviewer: {join_names(pending_names)}" if pending_names else "Maintainers"
    )

    if changes_req_names and not author_pushed_after_review:
        category = "Merge Conflicts" if has_conflicts else "Changes Requested"
        waiting_on = f"Author (@{author})"
        has_verb = "has" if len(changes_req_names) == 1 else "have"
        be_verb = "is" if len(changes_req_names) == 1 else "are"
        if approved_names:
            app_word = (
                "one approval"
                if len(approved_names) == 1
                else f"{len(approved_names)} approvals"
            )
            status_parts.append(
                f"The PR has received {app_word} from {join_names(approved_names)}, "
                f"while {join_names(changes_req_names)} {has_verb} requested changes "
                f"and {be_verb} awaiting a response from the author."
            )
        else:
            rev_noun = "Reviewer" if len(changes_req_names) == 1 else "Reviewers"
            status_parts.append(
                f"{rev_noun} {join_names(changes_req_names)} {has_verb} requested changes "
                f"and {be_verb} awaiting a response from the author."
            )
        if pending_names:
            status_parts.append(f"Also pending review from {join_names(pending_names)}.")
    elif rebase_requested:
        category = "Merge Conflicts" if has_conflicts else "Changes Requested"
        waiting_on = f"Author (@{author})"
        status_parts.append(
            f"Awaiting the author to rebase onto the latest {base_branch} branch "
            f"and re-run pre-commit checks."
        )
        if pending_names:
            status_parts.append(f"Pending review from {join_names(pending_names)}.")
    elif author_pushed_after_review:
        category = "Merge Conflicts" if has_conflicts else "Awaiting Re-Review"
        re_review_targets = list(dict.fromkeys(pending_names + changes_req_names))
        if re_review_targets:
            waiting_on = (
                f"Author (@{author}) & {join_names(re_review_targets)}"
                if has_conflicts
                else f"Reviewer: {join_names(re_review_targets)}"
            )
            status_parts.append(
                f"The author has committed updated changes; awaiting "
                f"{join_names(re_review_targets)} to review the changes."
            )
        else:
            last_rev = (
                fmt_user(last_reviewer_login) if last_reviewer_login else "reviewers"
            )
            waiting_on = f"Reviewer: {last_rev}"
            status_parts.append(
                f"The author recently pushed updated commits addressing feedback from "
                f"{last_rev}; awaiting re-review."
            )
        if approved_names:
            app_word = (
                "one approval"
                if len(approved_names) == 1
                else f"{len(approved_names)} approvals"
            )
            status_parts.append(
                f"Already has {app_word} from {join_names(approved_names)}."
            )
    elif approved_names:
        app_word = (
            "one approval"
            if len(approved_names) == 1
            else f"{len(approved_names)} approvals"
        )
        if suggested_names:
            category = "Merge Conflicts" if has_conflicts else "Changes Requested"
            waiting_on = f"Author (@{author})"
            has_verb = "has" if len(suggested_names) == 1 else "have"
            be_verb = "is" if len(suggested_names) == 1 else "are"
            status_parts.append(
                f"The PR has received {app_word} from {join_names(approved_names)}, "
                f"while {join_names(suggested_names)} {has_verb} left review comments "
                f"and {be_verb} awaiting a response from the author."
            )
            if pending_names:
                status_parts.append(
                    f"Also pending review from {join_names(pending_names)}."
                )
        elif pending_names:
            category = (
                "Merge Conflicts" if has_conflicts else "Approved (Needs 2nd Review)"
            )
            waiting_on = f"Reviewer: {join_names(pending_names)}"
            status_parts.append(
                f"The PR has received {app_word} from {join_names(approved_names)} "
                f"and is awaiting a second review and approval from "
                f"{join_names(pending_names)}."
            )
        else:
            category = (
                "Merge Conflicts"
                if has_conflicts
                else "Approved (Ready / Pending Merge)"
            )
            waiting_on = "Maintainers (Merge / 2nd Review)"
            status_parts.append(
                f"The PR has received {app_word} from {join_names(approved_names)} "
                f"and is awaiting final review/merge."
            )
    elif suggested_names:
        category = "Merge Conflicts" if has_conflicts else "Changes Requested"
        waiting_on = f"Author (@{author})"
        rev_noun = "Reviewer" if len(suggested_names) == 1 else "Reviewers"
        has_verb = "has" if len(suggested_names) == 1 else "have"
        be_verb = "is" if len(suggested_names) == 1 else "are"
        status_parts.append(
            f"{rev_noun} {join_names(suggested_names)} {has_verb} left review comments "
            f"and {be_verb} awaiting a response from the author."
        )
        if pending_names:
            status_parts.append(f"Also pending review from {join_names(pending_names)}.")
    elif author_pushed_followup and pending_names and not has_conflicts:
        category = "Awaiting Re-Review"
        waiting_on = f"Reviewer: {join_names(pending_names)}"
        status_parts.append(
            f"The author has committed updated changes; awaiting "
            f"{join_names(pending_names)} to review the changes."
        )
    else:
        category = "Merge Conflicts" if has_conflicts else "Awaiting Review"
        if has_gemini_suggestions and has_conflicts:
            waiting_on = f"Author (@{author})" + (
                f" & {join_names(pending_names)}" if pending_names else ""
            )
            rev_tail = f" from {join_names(pending_names)}" if pending_names else ""
            status_parts.append(
                f"There are Gemini suggestions and merge conflicts to verify; "
                f"currently awaiting review{rev_tail} and an update from the author."
            )
        elif has_gemini_suggestions:
            rev_tail = f" from {join_names(pending_names)}" if pending_names else ""
            status_parts.append(
                f"There are Gemini suggestions on the changes; "
                f"currently awaiting review{rev_tail}."
            )
        elif pending_names:
            status_parts.append(f"Awaiting review from {join_names(pending_names)}.")
        else:
            waiting_on = "Maintainers (Assign Reviewer)"
            status_parts.append("Open and awaiting reviewer assignment.")

    author_replies = [
        d
        for d in (last_author_review_reply_dt, last_author_issue_comment_dt)
        if d is not None
    ]
    last_author_reply_dt = max(author_replies) if author_replies else None
    author_replied_after_review = bool(
        last_author_reply_dt
        and last_human_review_dt
        and last_author_reply_dt > last_human_review_dt
    )

    joined_lower = " ".join(status_parts).lower()
    if (
        author_replied_after_review
        and not author_pushed_after_review
        and category in {"Changes Requested", "Merge Conflicts"}
    ):
        status_parts.append(
            f"The author replied to comments on {fmt_date(last_author_reply_dt, tz)}."
        )
    if has_conflicts and "conflict" not in joined_lower:
        status_parts.append(
            "Additionally, there are merge conflicts that need to be resolved."
        )
    elif is_behind and "rebase" not in joined_lower:
        status_parts.append(f"Branch is behind {base_branch} and needs a rebase.")

    today_date = now_local.date()
    created_local_date = created_dt.astimezone(tz).date() if created_dt else today_date
    last_action_local_date = (
        last_action_dt.astimezone(tz).date() if last_action_dt else created_local_date
    )
    days_open = max(0, (today_date - created_local_date).days)
    days_inactive = max(0, (today_date - last_action_local_date).days)
    contributor_type = (
        "Quadrant (-quad)" if author.lower().endswith("-quad") else "External"
    )
    effective_pending = (
        list(dict.fromkeys(pending_names + changes_req_names))
        if author_pushed_after_review
        else pending_names
    )

    return {
        "number": num,
        "pr_id": f"#{num}",
        "url": html_url,
        "daily_change": "New",
        "title": title,
        "author": author,
        "contributor_type": contributor_type,
        "base_branch": base_branch,
        "labels": ", ".join(labels_list) or "-",
        "created_on": fmt_date(created_dt, tz),
        "created_iso": created_local_date.isoformat(),
        "last_action_on": fmt_date(last_action_dt, tz),
        "last_action_iso": last_action_local_date.isoformat(),
        "days_open": days_open,
        "days_inactive": days_inactive,
        "diff_summary": diff_summary,
        "merge_ci_state": merge_ci_state,
        "category": category,
        "waiting_on": waiting_on,
        "approvals": ", ".join(approved_names) or "-",
        "changes_requested": ", ".join(feedback_names) or "-",
        "pending_reviewers": ", ".join(effective_pending) or "-",
        "pending_list": effective_pending,
        "changes_req_list": feedback_names,
        "approved_list": approved_names,
        "conflicts": "Yes" if has_conflicts else "No",
        "status": " ".join(status_parts),
    }


# ===========================================================================
# SECTION 3 — DAY-OVER-DAY DIFF & MERGED/CLOSED PR RESOLUTION TRACKING
# ===========================================================================


def check_resolved_prs(
    client: GitHubClient,
    repo: str,
    prev_snapshot: dict | None,
    open_numbers: set[int],
    tz: ZoneInfo,
) -> list[dict]:
    """[FETCH] Check previously tracked PRs no longer open (Merged / Closed / Draft)."""
    if not prev_snapshot:
        return []
    resolved: list[dict] = []
    for prev_row in prev_snapshot.get("rows", []):
        num = prev_row.get("number")
        if not num or num in open_numbers:
            continue
        state_label = "Closed / Merged"
        closed_on = "-"
        resolution_note = "No longer in open PR list."
        try:
            if (
                client.rate_remaining is None
                or client.rate_remaining > 0
                or client.wait_on_ratelimit
            ):
                detail = client.get_json(
                    f"https://api.github.com/repos/{repo}/pulls/{num}"
                )
                if detail.get("merged"):
                    state_label = "Merged"
                    merged_dt = parse_gh_ts(detail.get("merged_at"))
                    closed_on = fmt_date(merged_dt, tz)
                    merger = (detail.get("merged_by") or {}).get("login")
                    by_txt = (
                        f" by {client.resolve_display_name(merger, allow_lookup=False)}"
                        if merger
                        else ""
                    )
                    resolution_note = f"PR was merged{by_txt} on {closed_on}."
                elif detail.get("state") == "closed":
                    state_label = "Closed"
                    closed_dt = parse_gh_ts(detail.get("closed_at"))
                    closed_on = fmt_date(closed_dt, tz)
                    resolution_note = f"PR was closed without merging on {closed_on}."
                elif detail.get("draft"):
                    state_label = "Converted to Draft"
                    resolution_note = "PR was converted to Draft."
        except RuntimeError:
            pass
        resolved.append(
            {
                "number": num,
                "pr_id": f"#{num}",
                "url": prev_row.get("url", f"https://github.com/{repo}/pull/{num}"),
                "title": prev_row.get("title", ""),
                "author": prev_row.get("author", ""),
                "change_type": state_label,
                "prev_last_action": prev_row.get("last_action_on", "-"),
                "curr_last_action": closed_on,
                "waiting_on": "Done",
                "prev_status": prev_row.get("status", ""),
                "curr_status": resolution_note,
            }
        )
    return resolved


def apply_day_over_day_diff(rows: list[dict], prev_snapshot: dict | None) -> None:
    """Tag each PR row as Baseline, New, Status Updated, or Unchanged."""
    if not prev_snapshot:
        for r in rows:
            r["daily_change"] = "Baseline"
        return
    prev_by_num = {
        r["number"]: r for r in prev_snapshot.get("rows", []) if "number" in r
    }
    for r in rows:
        prev = prev_by_num.get(r["number"])
        if not prev:
            r["daily_change"] = "New"
            continue
        changed = any(
            [
                r.get("last_action_on") != prev.get("last_action_on"),
                r.get("category") != prev.get("category"),
                r.get("approvals") != prev.get("approvals"),
                r.get("changes_requested") != prev.get("changes_requested"),
                r.get("pending_reviewers") != prev.get("pending_reviewers"),
                r.get("conflicts") != prev.get("conflicts"),
            ]
        )
        r["daily_change"] = "Status Updated" if changed else "Unchanged"


def load_history_snapshots(history_dir: Path) -> list[dict]:
    """Load all YYYY-MM-DD.json snapshots in chronological order (oldest first)."""
    if not history_dir.exists():
        return []
    snaps: list[dict] = []
    for p in sorted(history_dir.glob("????-??-??.json")):
        try:
            snaps.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return snaps


# ===========================================================================
# SECTION 4 — ZERO-DEPENDENCY MULTI-SHEET EXCEL (.XLSX) REPORT GENERATOR
# ===========================================================================

STYLE_DEFAULT = 0
STYLE_TITLE = 1
STYLE_SUBTITLE = 2
STYLE_HEADER = 3
STYLE_TEXT = 4
STYLE_TEXT_ALT = 5
STYLE_TEXT_WRAP = 6
STYLE_TEXT_WRAP_ALT = 7
STYLE_CENTER = 8
STYLE_CENTER_ALT = 9
STYLE_INT = 10
STYLE_INT_ALT = 11
STYLE_LINK = 12
STYLE_LINK_ALT = 13
STYLE_BADGE_RED = 14
STYLE_BADGE_AMBER = 15
STYLE_BADGE_BLUE = 16
STYLE_BADGE_YELLOW = 17
STYLE_BADGE_GREEN = 18
STYLE_BADGE_PURPLE = 19
STYLE_BADGE_GRAY = 20
STYLE_KPI_LABEL = 21
STYLE_KPI_VALUE = 22

CATEGORY_STYLE_MAP = {
    "Merge Conflicts": STYLE_BADGE_RED,
    "Changes Requested": STYLE_BADGE_AMBER,
    "Awaiting Re-Review": STYLE_BADGE_BLUE,
    "Awaiting Review": STYLE_BADGE_YELLOW,
    "Approved (Needs 2nd Review)": STYLE_BADGE_GREEN,
    "Approved (Ready / Pending Merge)": STYLE_BADGE_GREEN,
}

CHANGE_STYLE_MAP = {
    "New": STYLE_BADGE_PURPLE,
    "Status Updated": STYLE_BADGE_BLUE,
    "Merged": STYLE_BADGE_GREEN,
    "Closed": STYLE_BADGE_GRAY,
    "Converted to Draft": STYLE_BADGE_GRAY,
    "Unchanged": STYLE_BADGE_GRAY,
    "Baseline": STYLE_BADGE_GRAY,
}

MERGE_CI_STYLE_MAP = {
    "Merge Conflicts": STYLE_BADGE_RED,
    "Needs Rebase (Behind)": STYLE_BADGE_AMBER,
    "Needs Rebase": STYLE_BADGE_AMBER,
    "CI Failing / Pending": STYLE_BADGE_YELLOW,
    "Changes Requested": STYLE_BADGE_AMBER,
    "Review / Checks Required": STYLE_BADGE_BLUE,
    "Review Required": STYLE_BADGE_BLUE,
    "Clean (Merge Ready)": STYLE_BADGE_GREEN,
}


def build_styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="13">
    <font><sz val="10"/><color rgb="FF1E293B"/><name val="Calibri"/></font>
    <font><b/><sz val="14"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
    <font><i/><sz val="10"/><color rgb="FF334155"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
    <font><b/><u/><sz val="10"/><color rgb="FF1D4ED8"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FF991B1B"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FF9A3412"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FF1E40AF"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FF854D0E"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FF166534"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FF6B21A8"/><name val="Calibri"/></font>
    <font><sz val="10"/><color rgb="FF475569"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><color rgb="FF0F172A"/><name val="Calibri"/></font>
  </fonts>
  <fills count="13">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF0F2942"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1E3A5F"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFEE2E2"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFEDD5"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDBEAFE"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFEF9C3"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDCFCE7"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF3E8FF"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFCBD5E1"/></left>
      <right style="thin"><color rgb="FFCBD5E1"/></right>
      <top style="thin"><color rgb="FFCBD5E1"/></top>
      <bottom style="thin"><color rgb="FFCBD5E1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="23">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" indent="1"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" indent="1"/></xf>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="1" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="1" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="4" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="4" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="5" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="6" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="7" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="8" fillId="9" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="9" fillId="10" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="10" fillId="11" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="11" fillId="12" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="0" fontId="12" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>
"""


def xml_cell(ref: str, value: object, style: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}" t="n"><v>{value}</v></c>'
    text = sanitize_xml_text(value if value is not None else "")
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def make_rels_xml(links: list[tuple[str, str]]) -> str | None:
    if not links:
        return None
    entries = "".join(
        f'<Relationship Id="{rid}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{sanitize_xml_text(url)}" TargetMode="External"/>'
        for rid, url in links
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{entries}</Relationships>"
    )


def build_snapshot_sheet(snap: dict, selected: bool = False) -> tuple[str, str | None]:
    """[REPORT GEN] Build 19-column styled PR worksheet with frozen header & AutoFilter."""
    rows = snap.get("rows", [])
    repo = snap.get("repo", DEFAULT_REPO)
    date_str = snap.get("date", "")
    day_name = snap.get("day_name", "")
    gen_at = snap.get("generated_at", date_str)
    source = snap.get("source", "Automated")

    headers = [
        ("PR ID", 10),
        ("Daily Change", 16),
        ("Title", 48),
        ("Author", 18),
        ("Contributor Type", 16),
        ("Base Branch", 13),
        ("Created On", 15),
        ("Last Action Taken On", 22),
        ("Days Open", 11),
        ("Days Inactive", 13),
        ("Commits / Diff", 22),
        ("Merge / CI State", 22),
        ("Action Category", 25),
        ("Next Action Owner", 28),
        ("Approved By", 24),
        ("Changes / Comments By", 26),
        ("Pending Reviewers", 26),
        ("Conflicts", 11),
        ("Detailed Status", 68),
    ]
    num_cols = len(headers)
    last_col = col_letter(num_cols)
    cols_xml = "".join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, (_, w) in enumerate(headers, start=1)
    )

    title_txt = f"Stale External PRs Report — {repo} ({date_str} {day_name})"
    subtitle_txt = (
        f"Generated: {gen_at}   |   Source: {source}   |   Total Stale PRs: {len(rows)}"
    )
    sheet_rows: list[str] = [
        '<row r="1" ht="30" customHeight="1">'
        + xml_cell("A1", title_txt, STYLE_TITLE)
        + "".join(
            xml_cell(f"{col_letter(c)}1", "", STYLE_TITLE)
            for c in range(2, num_cols + 1)
        )
        + "</row>",
        '<row r="2" ht="22" customHeight="1">'
        + xml_cell("A2", subtitle_txt, STYLE_SUBTITLE)
        + "".join(
            xml_cell(f"{col_letter(c)}2", "", STYLE_SUBTITLE)
            for c in range(2, num_cols + 1)
        )
        + "</row>",
        '<row r="3" ht="28" customHeight="1">'
        + "".join(
            xml_cell(f"{col_letter(c)}3", h, STYLE_HEADER)
            for c, (h, _) in enumerate(headers, start=1)
        )
        + "</row>",
    ]

    hyperlinks: list[str] = []
    rel_links: list[tuple[str, str]] = []
    for idx, item in enumerate(rows):
        r = idx + 4
        alt = idx % 2 == 1
        s_txt = STYLE_TEXT_ALT if alt else STYLE_TEXT
        s_wrap = STYLE_TEXT_WRAP_ALT if alt else STYLE_TEXT_WRAP
        s_ctr = STYLE_CENTER_ALT if alt else STYLE_CENTER
        s_int = STYLE_INT_ALT if alt else STYLE_INT
        s_lnk = STYLE_LINK_ALT if alt else STYLE_LINK

        chg = item.get("daily_change", "Unchanged")
        mci = item.get("merge_ci_state", "-")
        cat = item.get("category", "Awaiting Review")
        conf = item.get("conflicts", "No")

        url = item.get("url", "")
        if url:
            rid = f"rId{idx + 1}"
            hyperlinks.append(
                f'<hyperlink ref="A{r}" r:id="{rid}" tooltip="{sanitize_xml_text(url)}"/>'
            )
            rel_links.append((rid, url))

        status_txt = item.get("status", "")
        title_str = item.get("title", "")
        est_lines = max(1, (len(status_txt) // 62) + 1, (len(title_str) // 44) + 1)
        row_ht = max(26, est_lines * 15 + 8)

        vals = [
            (item.get("pr_id", ""), s_lnk),
            (chg, CHANGE_STYLE_MAP.get(chg, s_ctr)),
            (title_str, s_wrap),
            (item.get("author", ""), s_txt),
            (item.get("contributor_type", "External"), s_ctr),
            (item.get("base_branch", "develop"), s_ctr),
            (item.get("created_on", ""), s_ctr),
            (item.get("last_action_on", ""), s_ctr),
            (item.get("days_open", 0), s_int),
            (item.get("days_inactive", 0), s_int),
            (item.get("diff_summary", "-"), s_ctr),
            (mci, MERGE_CI_STYLE_MAP.get(mci, s_ctr)),
            (cat, CATEGORY_STYLE_MAP.get(cat, s_ctr)),
            (item.get("waiting_on", "-"), s_wrap),
            (item.get("approvals", "-"), s_wrap),
            (item.get("changes_requested", "-"), s_wrap),
            (item.get("pending_reviewers", "-"), s_wrap),
            (conf, STYLE_BADGE_RED if conf == "Yes" else s_ctr),
            (status_txt, s_wrap),
        ]
        sheet_rows.append(
            f'<row r="{r}" ht="{row_ht}" customHeight="1">'
            + "".join(
                xml_cell(f"{col_letter(c)}{r}", v, st)
                for c, (v, st) in enumerate(vals, start=1)
            )
            + "</row>"
        )

    last_row = max(3, len(rows) + 3)
    tab_sel = ' tabSelected="1"' if selected else ""
    hl_xml = f"<hyperlinks>{''.join(hyperlinks)}</hyperlinks>" if hyperlinks else ""
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews>
    <sheetView{tab_sel} workbookViewId="0">
      <pane ySplit="3" topLeftCell="A4" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft" activeCell="A4" sqref="A4"/>
    </sheetView>
  </sheetViews>
  <cols>{cols_xml}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="A3:{last_col}{last_row}"/>
  <mergeCells count="2">
    <mergeCell ref="A1:{last_col}1"/>
    <mergeCell ref="A2:{last_col}2"/>
  </mergeCells>
  {hl_xml}
</worksheet>
"""
    return sheet_xml, make_rels_xml(rel_links)


def build_diff_sheet(
    latest_snap: dict, prev_snap: dict | None
) -> tuple[str, str | None]:
    """[REPORT GEN] Build Day-over-Day Changes worksheet (Previous vs. Current)."""
    headers = [
        ("PR ID", 10),
        ("Change Type", 18),
        ("Title", 44),
        ("Author", 18),
        ("Prev Last Action", 18),
        ("Curr Last Action", 18),
        ("Next Action Owner", 28),
        ("Previous Status", 58),
        ("Current Status", 62),
    ]
    num_cols = len(headers)
    last_col = col_letter(num_cols)
    cols_xml = "".join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, (_, w) in enumerate(headers, start=1)
    )

    prev_date = prev_snap.get("date", "None") if prev_snap else "None (Baseline)"
    curr_date = latest_snap.get("date", "")
    prev_by_num = (
        {r["number"]: r for r in prev_snap.get("rows", []) if "number" in r}
        if prev_snap
        else {}
    )

    diff_entries: list[dict] = list(latest_snap.get("resolved_since_prev", []))
    order_rank = {"New": 0, "Status Updated": 1, "Unchanged": 2, "Baseline": 3}
    for row in sorted(
        latest_snap.get("rows", []),
        key=lambda x: (
            order_rank.get(x.get("daily_change", ""), 9),
            x.get("number", 0),
        ),
    ):
        prev = prev_by_num.get(row["number"])
        diff_entries.append(
            {
                "pr_id": row["pr_id"],
                "url": row.get("url", ""),
                "change_type": row.get("daily_change", "Unchanged"),
                "title": row.get("title", ""),
                "author": row.get("author", ""),
                "prev_last_action": prev.get("last_action_on", "-") if prev else "(New)",
                "curr_last_action": row.get("last_action_on", "-"),
                "waiting_on": row.get("waiting_on", "-"),
                "prev_status": (
                    prev.get("status", "-") if prev else "(Not in previous report)"
                ),
                "curr_status": row.get("status", ""),
            }
        )

    sheet_rows: list[str] = [
        '<row r="1" ht="30" customHeight="1">'
        + xml_cell(
            "A1", f"Day-over-Day PR Changes ({prev_date} -> {curr_date})", STYLE_TITLE
        )
        + "".join(
            xml_cell(f"{col_letter(c)}1", "", STYLE_TITLE)
            for c in range(2, num_cols + 1)
        )
        + "</row>",
        '<row r="2" ht="22" customHeight="1">'
        + xml_cell(
            "A2",
            f"Compares each PR against prior report ({prev_date}) to surface merged, new, and updated PRs.",
            STYLE_SUBTITLE,
        )
        + "".join(
            xml_cell(f"{col_letter(c)}2", "", STYLE_SUBTITLE)
            for c in range(2, num_cols + 1)
        )
        + "</row>",
        '<row r="3" ht="28" customHeight="1">'
        + "".join(
            xml_cell(f"{col_letter(c)}3", h, STYLE_HEADER)
            for c, (h, _) in enumerate(headers, start=1)
        )
        + "</row>",
    ]

    hyperlinks: list[str] = []
    rel_links: list[tuple[str, str]] = []
    for idx, d in enumerate(diff_entries):
        r = idx + 4
        alt = idx % 2 == 1
        s_txt = STYLE_TEXT_ALT if alt else STYLE_TEXT
        s_wrap = STYLE_TEXT_WRAP_ALT if alt else STYLE_TEXT_WRAP
        s_ctr = STYLE_CENTER_ALT if alt else STYLE_CENTER
        s_lnk = STYLE_LINK_ALT if alt else STYLE_LINK
        url = d.get("url", "")
        if url:
            rid = f"rId{idx + 1}"
            hyperlinks.append(
                f'<hyperlink ref="A{r}" r:id="{rid}" tooltip="{sanitize_xml_text(url)}"/>'
            )
            rel_links.append((rid, url))

        est_lines = max(
            1, (len(d["prev_status"]) // 54) + 1, (len(d["curr_status"]) // 56) + 1
        )
        row_ht = max(26, est_lines * 15 + 8)
        vals = [
            (d["pr_id"], s_lnk),
            (d["change_type"], CHANGE_STYLE_MAP.get(d["change_type"], s_ctr)),
            (d["title"], s_wrap),
            (d["author"], s_txt),
            (d["prev_last_action"], s_ctr),
            (d["curr_last_action"], s_ctr),
            (d["waiting_on"], s_wrap),
            (d["prev_status"], s_wrap),
            (d["curr_status"], s_wrap),
        ]
        sheet_rows.append(
            f'<row r="{r}" ht="{row_ht}" customHeight="1">'
            + "".join(
                xml_cell(f"{col_letter(c)}{r}", v, st)
                for c, (v, st) in enumerate(vals, start=1)
            )
            + "</row>"
        )

    last_row = max(3, len(diff_entries) + 3)
    hl_xml = f"<hyperlinks>{''.join(hyperlinks)}</hyperlinks>" if hyperlinks else ""
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="3" topLeftCell="A4" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  <cols>{cols_xml}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="A3:{last_col}{last_row}"/>
  <mergeCells count="2">
    <mergeCell ref="A1:{last_col}1"/>
    <mergeCell ref="A2:{last_col}2"/>
  </mergeCells>
  {hl_xml}
</worksheet>
"""
    return sheet_xml, make_rels_xml(rel_links)


def build_all_combined_sheet(snapshots: list[dict]) -> tuple[str, str | None]:
    """[REPORT GEN] Build unified historical log across all tracked days."""
    headers = [
        ("Report Date", 14),
        ("Day", 8),
        ("Source / Reporter", 32),
        ("PR ID", 10),
        ("Daily Change", 16),
        ("Title", 46),
        ("Author", 18),
        ("Contributor Type", 16),
        ("Created On", 15),
        ("Last Action Taken On", 22),
        ("Action Category", 25),
        ("Next Action Owner", 28),
        ("Conflicts", 11),
        ("Status", 66),
    ]
    num_cols = len(headers)
    last_col = col_letter(num_cols)
    cols_xml = "".join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, (_, w) in enumerate(headers, start=1)
    )

    combined = [
        (snap, row) for snap in reversed(snapshots) for row in snap.get("rows", [])
    ]
    sheet_rows: list[str] = [
        '<row r="1" ht="30" customHeight="1">'
        + xml_cell("A1", "All Daily Stale PR Reports — Master Historical Log", STYLE_TITLE)
        + "".join(
            xml_cell(f"{col_letter(c)}1", "", STYLE_TITLE)
            for c in range(2, num_cols + 1)
        )
        + "</row>",
        '<row r="2" ht="22" customHeight="1">'
        + xml_cell(
            "A2",
            f"Total Days Tracked: {len(snapshots)}   |   Total PR Entries: {len(combined)}",
            STYLE_SUBTITLE,
        )
        + "".join(
            xml_cell(f"{col_letter(c)}2", "", STYLE_SUBTITLE)
            for c in range(2, num_cols + 1)
        )
        + "</row>",
        '<row r="3" ht="28" customHeight="1">'
        + "".join(
            xml_cell(f"{col_letter(c)}3", h, STYLE_HEADER)
            for c, (h, _) in enumerate(headers, start=1)
        )
        + "</row>",
    ]

    hyperlinks: list[str] = []
    rel_links: list[tuple[str, str]] = []
    for idx, (snap, item) in enumerate(combined):
        r = idx + 4
        alt = idx % 2 == 1
        s_txt = STYLE_TEXT_ALT if alt else STYLE_TEXT
        s_wrap = STYLE_TEXT_WRAP_ALT if alt else STYLE_TEXT_WRAP
        s_ctr = STYLE_CENTER_ALT if alt else STYLE_CENTER
        s_lnk = STYLE_LINK_ALT if alt else STYLE_LINK
        chg = item.get("daily_change", "Unchanged")
        cat = item.get("category", "Awaiting Review")
        conf = item.get("conflicts", "No")

        url = item.get("url", "")
        if url:
            rid = f"rId{idx + 1}"
            hyperlinks.append(
                f'<hyperlink ref="D{r}" r:id="{rid}" tooltip="{sanitize_xml_text(url)}"/>'
            )
            rel_links.append((rid, url))

        status_txt = item.get("status", "")
        row_ht = max(25, ((len(status_txt) // 60) + 1) * 15 + 8)
        vals = [
            (snap.get("date", ""), s_ctr),
            (snap.get("day_name", ""), s_ctr),
            (snap.get("source", ""), s_wrap),
            (item.get("pr_id", ""), s_lnk),
            (chg, CHANGE_STYLE_MAP.get(chg, s_ctr)),
            (item.get("title", ""), s_wrap),
            (item.get("author", ""), s_txt),
            (item.get("contributor_type", "External"), s_ctr),
            (item.get("created_on", ""), s_ctr),
            (item.get("last_action_on", ""), s_ctr),
            (cat, CATEGORY_STYLE_MAP.get(cat, s_ctr)),
            (item.get("waiting_on", "-"), s_wrap),
            (conf, STYLE_BADGE_RED if conf == "Yes" else s_ctr),
            (status_txt, s_wrap),
        ]
        sheet_rows.append(
            f'<row r="{r}" ht="{row_ht}" customHeight="1">'
            + "".join(
                xml_cell(f"{col_letter(c)}{r}", v, st)
                for c, (v, st) in enumerate(vals, start=1)
            )
            + "</row>"
        )

    last_row = max(3, len(combined) + 3)
    hl_xml = f"<hyperlinks>{''.join(hyperlinks)}</hyperlinks>" if hyperlinks else ""
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="3" topLeftCell="A4" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  <cols>{cols_xml}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="A3:{last_col}{last_row}"/>
  <mergeCells count="2">
    <mergeCell ref="A1:{last_col}1"/>
    <mergeCell ref="A2:{last_col}2"/>
  </mergeCells>
  {hl_xml}
</worksheet>
"""
    return sheet_xml, make_rels_xml(rel_links)


def build_summary_sheet(latest_snap: dict, all_snaps: list[dict]) -> str:
    """[REPORT GEN] Build KPI Summary, Reviewer Queue, and Daily Trend worksheet."""
    rows = latest_snap.get("rows", [])
    resolved = latest_snap.get("resolved_since_prev", [])
    repo = latest_snap.get("repo", DEFAULT_REPO)
    gen_str = latest_snap.get("generated_at", "")
    total = len(rows)
    cat_counts = Counter(r.get("category", "") for r in rows)
    chg_counts = Counter(r.get("daily_change", "") for r in rows)
    conflicts_count = sum(1 for r in rows if r.get("conflicts") == "Yes")
    ext_count = sum(1 for r in rows if r.get("contributor_type") == "External")
    partner_count = total - ext_count
    avg_open = round(sum(r.get("days_open", 0) for r in rows) / total, 1) if total else 0
    max_open = max((r.get("days_open", 0) for r in rows), default=0)
    avg_inact = (
        round(sum(r.get("days_inactive", 0) for r in rows) / total, 1) if total else 0
    )

    kpis = [
        ("Total Open PRs in Repository", latest_snap.get("total_open_prs") or "-"),
        ("Total Stale External PRs (Active)", total),
        ("  - External Contributor PRs", ext_count),
        ("  - Quadrant (-quad) PRs", partner_count),
        ("New Stale PRs Since Previous Report", chg_counts["New"]),
        ("Status Updated Since Previous Report", chg_counts["Status Updated"]),
        ("Merged / Closed Since Previous Report", len(resolved)),
        (
            "Awaiting Review / Re-Review",
            cat_counts["Awaiting Review"] + cat_counts["Awaiting Re-Review"],
        ),
        (
            "Approved (Needs 2nd Review / Merge)",
            cat_counts["Approved (Needs 2nd Review)"]
            + cat_counts["Approved (Ready / Pending Merge)"],
        ),
        ("Changes Requested (Awaiting Author)", cat_counts["Changes Requested"]),
        ("PRs with Merge Conflicts", conflicts_count),
        ("Average Days Open", avg_open),
        ("Oldest PR Age (Days)", max_open),
        ("Average Days Since Last Action", avg_inact),
    ]

    reviewer_stats: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"pending": [], "changes": [], "approved": []}
    )
    for item in rows:
        for rev in item.get("pending_list", []):
            reviewer_stats[rev]["pending"].append(item["pr_id"])
        for rev in item.get("changes_req_list", []):
            reviewer_stats[rev]["changes"].append(item["pr_id"])
        for rev in item.get("approved_list", []):
            reviewer_stats[rev]["approved"].append(item["pr_id"])

    sorted_reviewers = sorted(
        reviewer_stats.items(),
        key=lambda kv: (
            -len(kv[1]["pending"]),
            -len(kv[1]["changes"]),
            kv[0].lower(),
        ),
    )

    sheet_rows: list[str] = []
    r = 1
    sheet_rows.append(
        f'<row r="{r}" ht="30" customHeight="1">'
        + xml_cell(
            f"A{r}",
            f"Executive Summary, Reviewer Queue & Daily Trend — {repo}",
            STYLE_TITLE,
        )
        + "".join(xml_cell(f"{col_letter(c)}{r}", "", STYLE_TITLE) for c in range(2, 9))
        + "</row>"
    )
    r += 1
    sheet_rows.append(
        f'<row r="{r}" ht="22" customHeight="1">'
        + xml_cell(
            f"A{r}",
            f"Latest Update: {gen_str}   |   Schedule: Mon-Fri 5:00 PM (UTC+05:30)   |   Days Tracked: {len(all_snaps)}",
            STYLE_SUBTITLE,
        )
        + "".join(
            xml_cell(f"{col_letter(c)}{r}", "", STYLE_SUBTITLE) for c in range(2, 9)
        )
        + "</row>"
    )

    r += 2
    sheet_rows.append(
        f'<row r="{r}" ht="26" customHeight="1">'
        + xml_cell(f"A{r}", "Current Report KPI", STYLE_HEADER)
        + xml_cell(f"B{r}", "Value", STYLE_HEADER)
        + "</row>"
    )
    for label, val in kpis:
        r += 1
        sheet_rows.append(
            f'<row r="{r}" ht="22" customHeight="1">'
            + xml_cell(f"A{r}", label, STYLE_KPI_LABEL)
            + xml_cell(f"B{r}", val, STYLE_KPI_VALUE)
            + "</row>"
        )

    r += 2
    rev_headers = [
        "Reviewer",
        "Pending Reviews",
        "Pending PR IDs (Action Required)",
        "Changes / Comments PRs",
        "Approved PRs",
    ]
    sheet_rows.append(
        f'<row r="{r}" ht="26" customHeight="1">'
        + "".join(
            xml_cell(f"{col_letter(c)}{r}", h, STYLE_HEADER)
            for c, h in enumerate(rev_headers, start=1)
        )
        + "</row>"
    )
    for idx, (rev, st) in enumerate(sorted_reviewers):
        r += 1
        alt = idx % 2 == 1
        s_txt = STYLE_TEXT_ALT if alt else STYLE_TEXT
        s_wrap = STYLE_TEXT_WRAP_ALT if alt else STYLE_TEXT_WRAP
        s_cnt = (
            STYLE_BADGE_YELLOW
            if st["pending"]
            else (STYLE_INT_ALT if alt else STYLE_INT)
        )
        sheet_rows.append(
            f'<row r="{r}" ht="24" customHeight="1">'
            + xml_cell(f"A{r}", rev, s_txt)
            + xml_cell(f"B{r}", len(st["pending"]), s_cnt)
            + xml_cell(f"C{r}", ", ".join(st["pending"]) or "-", s_wrap)
            + xml_cell(f"D{r}", ", ".join(st["changes"]) or "-", s_wrap)
            + xml_cell(f"E{r}", ", ".join(st["approved"]) or "-", s_wrap)
            + "</row>"
        )

    r += 2
    trend_headers = [
        "Report Date",
        "Total Stale PRs",
        "External PRs",
        "Partner (-quad)",
        "New PRs",
        "Updated PRs",
        "Resolved / Merged",
        "Conflicts",
    ]
    sheet_rows.append(
        f'<row r="{r}" ht="26" customHeight="1">'
        + "".join(
            xml_cell(f"{col_letter(c)}{r}", h, STYLE_HEADER)
            for c, h in enumerate(trend_headers, start=1)
        )
        + "</row>"
    )
    for idx, sn in enumerate(reversed(all_snaps)):
        r += 1
        srows = sn.get("rows", [])
        alt = idx % 2 == 1
        s_ctr = STYLE_CENTER_ALT if alt else STYLE_CENTER
        s_int = STYLE_INT_ALT if alt else STYLE_INT
        ext_n = sum(1 for x in srows if x.get("contributor_type") == "External")
        quad_n = len(srows) - ext_n
        new_n = sum(1 for x in srows if x.get("daily_change") == "New")
        upd_n = sum(1 for x in srows if x.get("daily_change") == "Status Updated")
        res_n = len(sn.get("resolved_since_prev", []))
        conf_n = sum(1 for x in srows if x.get("conflicts") == "Yes")
        tvals = [
            (f"{sn.get('date', '')} ({sn.get('day_name', '')})", s_ctr),
            (len(srows), s_int),
            (ext_n, s_int),
            (quad_n, s_int),
            (new_n, s_int),
            (upd_n, s_int),
            (res_n, s_int),
            (conf_n, STYLE_BADGE_RED if conf_n else s_int),
        ]
        sheet_rows.append(
            f'<row r="{r}" ht="23" customHeight="1">'
            + "".join(
                xml_cell(f"{col_letter(c)}{r}", v, st)
                for c, (v, st) in enumerate(tvals, start=1)
            )
            + "</row>"
        )

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <cols>
    <col min="1" max="1" width="38" customWidth="1"/>
    <col min="2" max="2" width="18" customWidth="1"/>
    <col min="3" max="3" width="36" customWidth="1"/>
    <col min="4" max="4" width="28" customWidth="1"/>
    <col min="5" max="5" width="28" customWidth="1"/>
    <col min="6" max="6" width="16" customWidth="1"/>
    <col min="7" max="7" width="18" customWidth="1"/>
    <col min="8" max="8" width="14" customWidth="1"/>
  </cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <mergeCells count="2">
    <mergeCell ref="A1:H1"/>
    <mergeCell ref="A2:H2"/>
  </mergeCells>
</worksheet>
"""


def write_workbook(
    output_path: Path, sheets: list[tuple[str, str, str | None]]
) -> None:
    """[REPORT GEN] Package worksheets, styles, and hyperlink rels into an .xlsx file."""
    buf = io.BytesIO()
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    wb_sheets = []
    wb_rels = []

    for idx, (title, _, _) in enumerate(sheets, start=1):
        safe_title = sanitize_xml_text(sanitize_sheet_name(title))
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        wb_sheets.append(
            f'<sheet name="{safe_title}" sheetId="{idx}" r:id="rId{idx}"/>'
        )
        wb_rels.append(
            f'<Relationship Id="rId{idx}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )

    styles_rid = f"rId{len(sheets) + 1}"
    wb_rels.append(
        f'<Relationship Id="{styles_rid}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        f'Target="styles.xml"/>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{''.join(overrides)}</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(wb_sheets)}</sheets></workbook>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(wb_rels)}</Relationships>"
    )

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/styles.xml", build_styles_xml())
        for idx, (_, s_xml, s_rels) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", s_xml)
            if s_rels:
                zf.writestr(f"xl/worksheets/_rels/sheet{idx}.xml.rels", s_rels)

    atomic_write_bytes(output_path, buf.getvalue())


# ===========================================================================
# SECTION 5 — CHAT ALERT, GITHUB ACTIONS STEP SUMMARY & ENTRYPOINT
# ===========================================================================


def build_chat_message(snap: dict) -> str:
    """[REPORT GEN] Build team Google Chat alert matching the daily 5 PM format."""
    rows = snap.get("rows", [])
    resolved = snap.get("resolved_since_prev", [])
    lines = [
        "Hi Team,",
        f"The Stale external PRs report ({snap.get('date', '')} — {len(rows)} open):",
        "",
    ]
    if resolved:
        merged_str = ", ".join(f"{x['pr_id']} ({x['change_type']})" for x in resolved)
        lines.extend([f"Resolved since previous report : {merged_str}", ""])

    if not rows:
        lines.append("No stale external PRs today!")
        return "\n".join(lines) + "\n"

    for item in rows:
        lines.extend(
            [
                f"PR ID : [{item['pr_id']}]({item['url']})",
                f"Title : {item['title']}",
                f"Author : {item['author']}",
                f"Created on : {item['created_on']}",
                f"Last action taken on : {item['last_action_on']}",
                f"Status : {item['status']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_markdown_summary(snap: dict) -> str:
    """[REPORT GEN] Render a GitHub Actions Step Summary Markdown dashboard."""
    rows = snap.get("rows", [])
    resolved = snap.get("resolved_since_prev", [])
    lines = [
        f"## Stale External PRs Report — {snap.get('date', '')} ({len(rows)} open)",
        "",
        f"Generated: **{snap.get('generated_at', '')}** | "
        f"Repo: `{snap.get('repo', DEFAULT_REPO)}` | "
        f"Resolved since prior run: **{len(resolved)}**",
        "",
        "| PR | Change | Author | Created | Last Action | Category | Next Action Owner | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        safe_status = r["status"].replace("|", "\\|")
        safe_owner = r["waiting_on"].replace("|", "\\|")
        lines.append(
            f"| [{r['pr_id']}]({r['url']}) | {r['daily_change']} | `@{r['author']}` | "
            f"{r['created_on']} | {r['last_action_on']} | **{r['category']}** | "
            f"{safe_owner} | {safe_status} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automated Daily Stale External PRs Fetcher, Excel & Dashboard Generator."
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/repo")
    parser.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help="PR label identifying external PRs (default: external)",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=DEFAULT_MIN_AGE_DAYS,
        help="Minimum PR age in days to flag as stale (default: 7)",
    )
    parser.add_argument(
        "--min-inactive-days",
        type=int,
        default=0,
        help="Minimum days since last human action (default: 0)",
    )
    parser.add_argument("--include-drafts", action="store_true")
    parser.add_argument(
        "--exclude-quad",
        action="store_true",
        help="Optionally exclude *-quad Quadrant PRs (included by default).",
    )
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--sheet-url",
        default=os.environ.get("SHEET_SHARE_URL", ""),
        help="Optional shared Google Sheet URL embedded in the HTML dashboard.",
    )
    parser.add_argument(
        "--github-summary",
        type=Path,
        default=None,
        help="Optional path to $GITHUB_STEP_SUMMARY for GitHub Actions output.",
    )
    parser.add_argument(
        "--wait-on-ratelimit",
        action="store_true",
        help="Sleep and auto-resume if GitHub API rate limit is reached.",
    )
    parser.add_argument("--json-summary", action="store_true")
    args = parser.parse_args()

    tz = ZoneInfo(args.timezone)
    now_local = dt.datetime.now(tz)
    date_slug = now_local.strftime("%Y-%m-%d")
    day_name = now_local.strftime("%a")
    generated_str = now_local.strftime("%b %d, %Y %I:%M %p %Z")

    history_dir = args.output_dir / "history"
    daily_dir = args.output_dir / "daily"
    history_dir.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)

    existing_snaps = load_history_snapshots(history_dir)
    prior_snaps = [s for s in existing_snaps if s.get("date", "") < date_slug]
    prev_snap = prior_snaps[-1] if prior_snaps else None

    client = GitHubClient(
        reviewer_map=load_reviewer_map(),
        wait_on_ratelimit=args.wait_on_ratelimit,
    )

    # Step 1: Fetch all open PRs and filter stale external candidates
    all_pulls = client.list_open_pulls(args.repo)
    open_numbers = {p["number"] for p in all_pulls}
    candidates = filter_stale_candidates(
        all_pulls,
        now_local,
        tz,
        label=args.label,
        min_age_days=args.min_age_days,
        include_drafts=args.include_drafts,
        exclude_quad=args.exclude_quad,
    )

    # Step 2: Fetch per-PR details, reviews, commits & comments
    rows: list[dict] = []
    total_cand = len(candidates)
    for idx, p in enumerate(candidates):
        prs_remaining = total_cand - idx - 1
        allow_extra = (
            client.wait_on_ratelimit
            or client.rate_remaining is None
            or client.rate_remaining > (prs_remaining * 3 + 3)
        )
        analyzed = analyze_pr(
            client, args.repo, p, now_local, tz, allow_extra_calls=allow_extra
        )
        if analyzed["days_inactive"] < args.min_inactive_days:
            continue
        rows.append(analyzed)

    # Step 3: Compute day-over-day changes and check merged/closed PRs
    apply_day_over_day_diff(rows, prev_snap)
    resolved = check_resolved_prs(client, args.repo, prev_snap, open_numbers, tz)

    today_snap = {
        "date": date_slug,
        "day_name": day_name,
        "generated_at": generated_str,
        "generated_epoch": int(time.time()),
        "source": "Automated GitHub Fetch (5 PM Bot)",
        "repo": args.repo,
        "total_open_prs": len(all_pulls),
        "resolved_since_prev": resolved,
        "rows": rows,
    }
    atomic_write_text(
        history_dir / f"{date_slug}.json", json.dumps(today_snap, indent=2)
    )
    all_snaps = load_history_snapshots(history_dir)

    # Step 4: Generate Master Multi-Day Excel + Standalone Daily Excel
    master_xlsx = args.output_dir / "stale_pr_master_tracker.xlsx"
    latest_s_xml, latest_s_rels = build_snapshot_sheet(today_snap, selected=True)
    summary_s_xml = build_summary_sheet(today_snap, all_snaps)
    diff_s_xml, diff_s_rels = build_diff_sheet(today_snap, prev_snap)
    combined_s_xml, combined_s_rels = build_all_combined_sheet(all_snaps)

    master_sheets: list[tuple[str, str, str | None]] = [
        ("Latest Report", latest_s_xml, latest_s_rels),
        ("Summary & Reviewer Queue", summary_s_xml, None),
        ("Day-over-Day Changes", diff_s_xml, diff_s_rels),
        ("All Days Combined", combined_s_xml, combined_s_rels),
    ]
    for sn in list(reversed(all_snaps))[:MAX_DAILY_TABS]:
        tab_title = f"{sn.get('date', '')} {sn.get('day_name', '')}".strip()
        s_xml, s_rels = build_snapshot_sheet(sn, selected=False)
        master_sheets.append((tab_title, s_xml, s_rels))
    write_workbook(master_xlsx, master_sheets)

    daily_xlsx = daily_dir / f"stale_pr_report_{date_slug}.xlsx"
    latest_alias_xlsx = args.output_dir / "stale_pr_report_latest.xlsx"
    daily_sheets: list[tuple[str, str, str | None]] = [
        ("Stale External PRs", latest_s_xml, latest_s_rels),
        ("Summary & Reviewer Queue", summary_s_xml, None),
        ("Day-over-Day Changes", diff_s_xml, diff_s_rels),
    ]
    write_workbook(daily_xlsx, daily_sheets)
    write_workbook(latest_alias_xlsx, daily_sheets)

    # Step 5: Generate Chat Alert, Interactive HTML Dashboard & Optional GH Summary
    chat_path = daily_dir / f"stale_pr_chat_alert_{date_slug}.txt"
    latest_chat_path = args.output_dir / "stale_pr_chat_alert_latest.txt"
    chat_text = build_chat_message(today_snap)
    atomic_write_text(chat_path, chat_text)
    atomic_write_text(latest_chat_path, chat_text)

    dashboard_path = args.output_dir / "stale_pr_dashboard.html"
    sys.path.insert(0, str(SCRIPT_DIR))
    from dashboard_builder import build_dashboard_html  # pylint: disable=import-outside-toplevel

    atomic_write_text(
        dashboard_path,
        build_dashboard_html(all_snaps, sheet_url=args.sheet_url),
    )

    if args.github_summary:
        with args.github_summary.open("a", encoding="utf-8") as sf:
            sf.write(build_markdown_summary(today_snap))

    chg_counts = Counter(r["daily_change"] for r in rows)
    summary = {
        "date": date_slug,
        "generated_at": generated_str,
        "repo": args.repo,
        "total_open_prs": len(all_pulls),
        "stale_external_prs": len(rows),
        "days_tracked": len(all_snaps),
        "new_count": chg_counts["New"],
        "updated_count": chg_counts["Status Updated"],
        "unchanged_count": chg_counts["Unchanged"],
        "resolved_count": len(resolved),
        "conflicts_count": sum(1 for r in rows if r["conflicts"] == "Yes"),
        "master_excel": str(master_xlsx),
        "daily_excel": str(daily_xlsx),
        "dashboard_html": str(dashboard_path),
        "chat_alert_file": str(chat_path),
        "category_breakdown": dict(Counter(r["category"] for r in rows)),
    }

    if args.json_summary:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Updated Stale External PRs Tracker ({generated_str})")
        print(f"  Master Excel : {master_xlsx} ({len(master_sheets)} sheets)")
        print(f"  Daily Excel  : {daily_xlsx}")
        print(f"  Dashboard    : {dashboard_path}")
        print(f"  Chat Alert   : {chat_path}")
        print(
            f"  PR Summary   : {len(rows)} stale ({chg_counts['New']} new, "
            f"{chg_counts['Status Updated']} updated, "
            f"{chg_counts['Unchanged']} unchanged, {len(resolved)} resolved)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
