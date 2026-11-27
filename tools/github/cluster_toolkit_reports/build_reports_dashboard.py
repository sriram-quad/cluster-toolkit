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

"""Build the unified Cluster Toolkit Reports Dashboard HTML.

Combines four live/historical views into a single self-contained interactive
HTML report (`reports_dashboard.html`) using only the Python 3 standard library:
  1. Overview — cross-report KPIs and quick launch cards.
  2. Stale External PRs — multi-day snapshot history, newest (< 14d) external
     PRs, reviewer/author action queues, and a 4-tab Deep PR Inspector with
     CI checks, commits, changed files, nested inline code-review comments,
     conversation comments, and Gemini Code Assist suggestions.
  3. Code Scanning — full historical CodeQL alert reconstruction (open, fixed,
     and dismissed timestamps) for any calendar report date.
  4. Security Vulnerabilities — dependency-graph SBOM scanned against OSV.dev
     with deduplicated CVE/GHSA/GO advisories and repo security advisories.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import stale_pr_collector as collector  # noqa: E402

DEFAULT_REPO = "GoogleCloudPlatform/cluster-toolkit"
DEFAULT_TZ = "Asia/Kolkata"
TEMPLATE_PATH = SCRIPT_DIR / "reports_dashboard_template.html"
PLACEHOLDER = "__REPORTS_DATA_JSON__"


def sanitize_snapshots(snapshots: list[dict]) -> list[dict]:
    """Normalize any legacy manual reporter labels before rendering."""
    cleaned = copy.deepcopy(snapshots)
    for sn in cleaned:
        if "Manual Report" in (sn.get("source") or ""):
            sn["source"] = "Team Baseline Report"
    return cleaned


TOKEN_FILE = Path.home() / ".config" / "stale-pr-report" / "github_token"


def load_gh_token() -> str:
    """Resolve a GitHub token from env, token file, or non-interactive git credentials."""
    for env_key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = (os.environ.get(env_key) or "").strip()
        if val:
            return val
    if TOKEN_FILE.exists():
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except OSError:
            pass
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def http_json_paginated(
    url: str, headers: dict[str, str], max_pages: int = 10
) -> list[dict]:
    """Fetch paginated GitHub REST list endpoints following Link: rel='next'."""
    out: list[dict] = []
    next_url: str | None = url
    pages = 0
    while next_url and pages < max_pages:
        pages += 1
        req = urllib.request.Request(next_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
            if isinstance(batch, list):
                out.extend(batch)
            link = resp.headers.get("Link") or ""
            next_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
    return out


def fetch_dependabot_alerts(
    repo: str, gh_headers: dict[str, str]
) -> tuple[list[dict], list[dict]] | None:
    """Tier 1: Fetch authoritative Dependabot alerts directly from GitHub API.

    Returns (open_rows, all_rows) when authorized (requires `security_events`
    classic scope, fine-grained `Dependabot alerts: Read`, or GitHub Actions
    `security-events: read`), or None if HTTP 403/404.
    """
    eco_map = {
        "pip": "PyPI",
        "pypi": "PyPI",
        "go": "Go",
        "golang": "Go",
        "npm": "npm",
        "actions": "GitHub Actions",
        "githubactions": "GitHub Actions",
    }
    sev_ord = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

    try:
        raw_open = http_json_paginated(
            f"https://api.github.com/repos/{repo}/dependabot/alerts?state=open&per_page=100",
            gh_headers,
        )
    except Exception:  # pylint: disable=broad-except
        return None

    raw_closed: list[dict] = []
    for st_name in ("fixed", "dismissed", "auto_dismissed"):
        try:
            raw_closed.extend(
                http_json_paginated(
                    f"https://api.github.com/repos/{repo}/dependabot/alerts?state={st_name}&per_page=100",
                    gh_headers,
                    max_pages=4,
                )
            )
        except Exception:  # pylint: disable=broad-except
            pass

    def to_row(alert: dict) -> dict:
        dep = alert.get("dependency") or {}
        pkg = dep.get("package") or {}
        adv = alert.get("security_advisory") or {}
        vuln = alert.get("security_vulnerability") or {}
        raw_sev = (vuln.get("severity") or adv.get("severity") or "medium").lower()
        sev = "Medium" if raw_sev == "moderate" else raw_sev.title()
        if sev not in sev_ord:
            sev = "Medium"
        cve = adv.get("cve_id") or ""
        ghsa = adv.get("ghsa_id") or ""
        canonical = cve or ghsa or f"Dependabot-#{alert.get('number')}"
        aliases = sorted({x for x in (cve, ghsa) if x})
        eco_raw = (pkg.get("ecosystem") or "").lower()
        fixed = (vuln.get("first_patched_version") or {}).get("identifier") or "Check advisory"
        created_iso = (alert.get("created_at") or "")[:10]
        closed_iso = (alert.get("fixed_at") or alert.get("dismissed_at") or "")[:10]
        return {
            "id": canonical,
            "dependabot_number": alert.get("number"),
            "primary_osv_id": ghsa or canonical,
            "cve": cve,
            "ghsa": ghsa,
            "aliases": aliases,
            "ecosystem": eco_map.get(eco_raw, pkg.get("ecosystem") or "Other"),
            "package": pkg.get("name") or "",
            "manifest_path": dep.get("manifest_path") or "",
            "relationship": (dep.get("relationship") or "").title(),
            "installed_version": vuln.get("vulnerable_version_range") or "vulnerable",
            "fixed_version": fixed,
            "severity": sev,
            "cvss": (adv.get("cvss") or {}).get("score"),
            "summary": (adv.get("summary") or f"Vulnerability {canonical}").strip(),
            "details": (adv.get("description") or "").strip()[:500],
            "published_on": (adv.get("published_at") or created_iso)[:10],
            "created_iso": created_iso,
            "closed_iso": closed_iso,
            "state": (alert.get("state") or "open").lower(),
            "url": alert.get("html_url")
            or (f"https://github.com/advisories/{ghsa}" if ghsa else ""),
            "source": "GitHub Dependabot API",
        }

    open_rows = sorted(
        [to_row(a) for a in raw_open],
        key=lambda x: (
            sev_ord.get(x["severity"], 9),
            x["ecosystem"],
            x["package"],
            -(x.get("dependabot_number") or 0),
        ),
    )
    all_rows = sorted(
        open_rows + [to_row(a) for a in raw_closed],
        key=lambda x: (
            x["state"] != "open",
            sev_ord.get(x["severity"], 9),
            -(x.get("dependabot_number") or 0),
        ),
    )
    return open_rows, all_rows


def http_json(url: str, headers: dict[str, str], body: dict | None = None):
    """Perform a read-only GET or JSON POST request and decode the JSON body."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_bot(user_obj: dict | None) -> bool:
    """Return True if a GitHub user object represents a bot account."""
    if not user_obj:
        return True
    login = (user_obj.get("login") or "").lower()
    utype = (user_obj.get("type") or "").lower()
    return utype == "bot" or login.endswith("[bot]") or "gemini-code-assist" in login


def is_gemini(user_obj: dict | None) -> bool:
    """Return True if a GitHub user object is Gemini Code Assist."""
    return bool(user_obj and "gemini-code-assist" in (user_obj.get("login") or "").lower())


def clean_text(body: str, max_len: int = 600, strip_checklist: bool = False) -> str:
    """Strip HTML comments and excess blank lines and bound length cleanly."""
    if not body:
        return ""
    text = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    if strip_checklist:
        text = re.split(r"(?i)#+\s*submission checklist", text)[0]
    out_lines: list[str] = []
    blank = False
    for ln in text.splitlines():
        if not ln.strip():
            if not blank and out_lines:
                out_lines.append("")
            blank = True
        else:
            out_lines.append(ln.strip())
            blank = False
    collapsed = "\n".join(out_lines).strip()
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def parse_cvss_score(vuln: dict) -> tuple[str, float | None]:
    """Normalize OSV severity strings and optional numeric CVSS scores."""
    db_spec = vuln.get("database_specific") or {}
    sev_str = (db_spec.get("severity") or "").upper()
    if sev_str == "MODERATE":
        sev_str = "MEDIUM"
    score: float | None = None
    for item in vuln.get("severity") or []:
        try:
            score = float(str(item.get("score") or ""))
        except ValueError:
            pass
    if not sev_str and score is not None:
        if score >= 9.0:
            sev_str = "CRITICAL"
        elif score >= 7.0:
            sev_str = "HIGH"
        elif score >= 4.0:
            sev_str = "MEDIUM"
        else:
            sev_str = "LOW"
    if sev_str not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
        sev_str = "MEDIUM"
    return sev_str.title(), score


def extract_fixed_version(vuln: dict, pkg_name: str) -> str:
    """Extract up to three fixed package versions from OSV affected ranges."""
    fixed: list[str] = []
    for aff in vuln.get("affected") or []:
        pname = ((aff.get("package") or {}).get("name") or "").lower()
        if pname and pkg_name.lower() not in pname and pname not in pkg_name.lower():
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if ev.get("fixed") and ev["fixed"] not in fixed:
                    fixed.append(ev["fixed"])
    return ", ".join(fixed[:3]) if fixed else "Check advisory"


def fetch_security_snapshot(
    repo: str,
    gh_headers: dict[str, str],
    tz: ZoneInfo,
) -> dict:
    """Fetch Code Scanning history, repo advisories, and SBOM/OSV CVEs."""
    now_local = dt.datetime.now(tz)
    now_date = now_local.date()

    cs_main = http_json(
        f"https://api.github.com/repos/{repo}/code-scanning/alerts?state=open&ref=refs/heads/main&per_page=100",
        gh_headers,
    )
    cs_dev = http_json(
        f"https://api.github.com/repos/{repo}/code-scanning/alerts?state=open&ref=refs/heads/develop&per_page=100",
        gh_headers,
    )
    cs_fixed = http_json(
        f"https://api.github.com/repos/{repo}/code-scanning/alerts?state=fixed&per_page=100",
        gh_headers,
    )
    cs_dismissed = http_json(
        f"https://api.github.com/repos/{repo}/code-scanning/alerts?state=dismissed&per_page=100",
        gh_headers,
    )
    analyses = http_json(
        f"https://api.github.com/repos/{repo}/code-scanning/analyses?per_page=10",
        gh_headers,
    )

    main_nums = {a["number"] for a in cs_main}
    dev_nums = {a["number"] for a in cs_dev}
    by_num: dict[int, dict] = {}
    for bucket in (cs_fixed, cs_dismissed, cs_dev, cs_main):
        for alert in bucket:
            by_num[alert["number"]] = alert

    sev_map = {
        "critical": "Critical",
        "high": "High",
        "error": "High",
        "medium": "Medium",
        "warning": "Medium",
        "low": "Low",
        "note": "Low",
    }

    def parse_iso(raw: str | None) -> tuple[str, str]:
        if not raw:
            return "", ""
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(tz)
        return parsed.strftime("%Y-%m-%d"), parsed.strftime("%b %d, %Y")

    all_cs: list[dict] = []
    for num in sorted(by_num.keys(), reverse=True):
        alert = by_num[num]
        rule = alert.get("rule") or {}
        tool = alert.get("tool") or {}
        inst = alert.get("most_recent_instance") or {}
        loc = inst.get("location") or {}
        raw_sev = (
            rule.get("security_severity_level") or rule.get("severity") or "medium"
        ).lower()
        c_iso, c_disp = parse_iso(alert.get("created_at"))
        f_iso, f_disp = parse_iso(alert.get("fixed_at"))
        d_iso, d_disp = parse_iso(alert.get("dismissed_at"))
        closed_iso = f_iso or d_iso or ""
        days_open = 0
        if c_iso:
            end_d = dt.date.fromisoformat(closed_iso) if closed_iso else now_date
            days_open = max(0, (end_d - dt.date.fromisoformat(c_iso)).days)
        path = loc.get("path") or ""
        sl, el = loc.get("start_line"), loc.get("end_line")
        line_str = f"L{sl}-{el}" if (sl and el and el != sl) else (f"L{sl}" if sl else "")
        if path.startswith("community/front-end/ofe"):
            area = "OFE Web UI"
        elif path.startswith("tools/"):
            area = "Tools / Tests"
        elif path.startswith("modules/"):
            area = "Modules"
        elif path.startswith(("pkg/", "cmd/")):
            area = "Core CLI"
        else:
            area = path.split("/")[0] if "/" in path else "Repo"
        branches = [b for b, s in (("main", main_nums), ("develop", dev_nums)) if num in s]
        if not branches:
            branches = [(inst.get("ref") or "").replace("refs/heads/", "") or "main"]
        all_cs.append(
            {
                "number": num,
                "alert_id": f"#{num}",
                "url": alert.get("html_url") or "",
                "severity": sev_map.get(raw_sev, "Medium"),
                "tool": tool.get("name") or "CodeQL",
                "rule_id": rule.get("id") or "",
                "rule_name": rule.get("description") or rule.get("name") or "",
                "rule_help": (rule.get("full_description") or "").strip(),
                "cwes": [
                    t.replace("external/cwe/cwe-", "CWE-")
                    for t in (rule.get("tags") or [])
                    if t.startswith("external/cwe/cwe-")
                ],
                "file": path,
                "line": line_str,
                "area": area,
                "branches": ", ".join(branches),
                "message": (
                    (inst.get("message") or {}).get("text")
                    or rule.get("description")
                    or ""
                ).strip(),
                "created_iso": c_iso,
                "created_on": c_disp,
                "closed_iso": closed_iso,
                "closed_on": f_disp or d_disp or "",
                "dismissed_reason": alert.get("dismissed_reason") or "",
                "days_open": days_open,
                "state": (alert.get("state") or "open").lower(),
            }
        )
    open_cs = [r for r in all_cs if r["state"] == "open"]

    advisories_raw = http_json(
        f"https://api.github.com/repos/{repo}/security-advisories?per_page=100",
        gh_headers,
    )
    repo_advisories = []
    for adv in advisories_raw:
        sev = (adv.get("severity") or "medium").title()
        repo_advisories.append(
            {
                "id": adv.get("cve_id") or adv.get("ghsa_id") or "Advisory",
                "ghsa_id": adv.get("ghsa_id") or "",
                "cve_id": adv.get("cve_id") or "",
                "severity": "Medium" if sev == "Moderate" else sev,
                "state": adv.get("state") or "published",
                "summary": adv.get("summary") or "",
                "url": adv.get("html_url") or "",
                "published_on": (adv.get("published_at") or adv.get("created_at") or "")[:10],
            }
        )

    sbom = http_json(
        f"https://api.github.com/repos/{repo}/dependency-graph/sbom",
        gh_headers,
    ).get("sbom", {})
    eco_map = {
        "golang": "Go",
        "pypi": "PyPI",
        "githubactions": "GitHub Actions",
        "npm": "npm",
    }
    # Preserve per-manifest SPDXID entries so multi-manifest alerts match GitHub Dependabot 1-to-1.
    pkg_entries: list[dict] = []
    unique_purls_set: set[str] = set()
    for pkg in sbom.get("packages") or []:
        ver = pkg.get("versionInfo")
        if not ver:
            continue
        for ref in pkg.get("externalRefs") or []:
            if ref.get("referenceType") == "purl":
                purl = ref.get("referenceLocator") or ""
                if purl.startswith("pkg:"):
                    unique_purls_set.add(purl)
                    eco_raw = purl.split(":")[1].split("/")[0]
                    pkg_entries.append(
                        {
                            "spdx_id": pkg.get("SPDXID") or purl,
                            "purl": purl,
                            "name": pkg.get("name") or "",
                            "version": ver,
                            "ecosystem": eco_map.get(eco_raw, eco_raw),
                        }
                    )
                break

    sev_ord = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    vuln_all_rows: list[dict] = []
    vuln_source = "GitHub Dependabot API"

    dep_result = fetch_dependabot_alerts(repo, gh_headers)
    if dep_result is not None:
        vuln_rows, vuln_all_rows = dep_result
    else:
        vuln_source = "GitHub SBOM + GHSA Advisory DB"
        osv_headers = {"Content-Type": "application/json", "User-Agent": "ctk-sec/1.0"}
        unique_purls = sorted(unique_purls_set)
        purl_to_vids: dict[str, list[str]] = {}
        unique_vids: set[str] = set()
        for i in range(0, len(unique_purls), 200):
            chunk = unique_purls[i : i + 200]
            res = http_json(
                "https://api.osv.dev/v1/querybatch",
                osv_headers,
                body={"queries": [{"package": {"purl": u}} for u in chunk]},
            )
            for purl, item in zip(chunk, res.get("results") or []):
                vids = [vuln["id"] for vuln in (item.get("vulns") or [])]
                if vids:
                    purl_to_vids[purl] = vids
                    unique_vids.update(vids)

        vuln_details: dict[str, dict] = {}

        def fetch_osv(vid: str) -> tuple[str, dict | None]:
            try:
                url = f"https://api.osv.dev/v1/vulns/{urllib.parse.quote(vid)}"
                return vid, http_json(url, {"User-Agent": "ctk-sec/1.0"})
            except Exception:  # pylint: disable=broad-except
                return vid, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for vid, det in pool.map(fetch_osv, sorted(unique_vids)):
                if det:
                    vuln_details[vid] = det

        # Match GitHub Dependabot: require a GitHub Advisory (GHSA-*) and deduplicate per (GHSA, SPDXID).
        dedup_map: dict[tuple[str, str], dict] = {}
        sev_rank = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}
        for entry in pkg_entries:
            for vid in purl_to_vids.get(entry["purl"], []):
                det = vuln_details.get(vid) or {}
                aliases = [vid] + list(det.get("aliases") or [])
                ghsas = sorted({a for a in aliases if a.startswith("GHSA-")})
                if not ghsas:
                    continue
                cves = sorted({a for a in aliases if a.startswith("CVE-")})
                canonical = cves[0] if cves else ghsas[0]
                key = (ghsas[0], entry["spdx_id"])
                sev, score = parse_cvss_score(det)
                fixed_ver = extract_fixed_version(det, entry["name"])
                summary = (det.get("summary") or "").strip()
                if not summary:
                    lines = (det.get("details") or "").strip().splitlines()
                    summary = (
                        lines[0][:160]
                        if lines
                        else f"Vulnerability {canonical} in {entry['name']}"
                    )
                adv_url = f"https://github.com/advisories/{ghsas[0]}"

                existing = dedup_map.get(key)
                if not existing:
                    dedup_map[key] = {
                        "id": canonical,
                        "primary_osv_id": vid,
                        "cve": cves[0] if cves else "",
                        "ghsa": ghsas[0],
                        "aliases": sorted(set(aliases)),
                        "ecosystem": entry["ecosystem"],
                        "package": entry["name"],
                        "installed_version": entry["version"],
                        "fixed_version": fixed_ver,
                        "severity": sev,
                        "cvss": score,
                        "summary": summary,
                        "details": (det.get("details") or "").strip()[:500],
                        "published_on": (det.get("published") or "")[:10],
                        "url": adv_url,
                        "source": vuln_source,
                    }
                else:
                    existing["aliases"] = sorted(set(existing["aliases"] + aliases))
                    if sev_rank.get(sev, 0) > sev_rank.get(existing["severity"], 0):
                        existing["severity"] = sev
                    if existing["fixed_version"] == "Check advisory" and fixed_ver != "Check advisory":
                        existing["fixed_version"] = fixed_ver
                    if not existing["cve"] and cves:
                        existing["cve"] = cves[0]
                        existing["id"] = cves[0]

        vuln_rows = sorted(
            dedup_map.values(),
            key=lambda x: (
                sev_ord.get(x["severity"], 9),
                x["ecosystem"],
                x["package"],
                x["id"],
            ),
        )

    latest_at = analyses[0].get("created_at", "") if analyses else ""
    return {
        "repo": repo,
        "generated_at": now_local.strftime("%b %d, %Y %I:%M %p %Z"),
        "generated_date": now_local.strftime("%Y-%m-%d"),
        "code_scanning": {
            "open_count": len(open_cs),
            "main_open_count": len(cs_main),
            "develop_open_count": len(cs_dev),
            "dismissed_count": sum(1 for r in all_cs if r["state"] == "dismissed"),
            "fixed_count": sum(1 for r in all_cs if r["state"] == "fixed"),
            "latest_analysis_at": latest_at[:16].replace("T", " ") + " UTC" if latest_at else "",
            "source": "GitHub Code Scanning API (CodeQL)",
            "alerts": open_cs,
            "all_alerts": all_cs,
        },
        "vulnerabilities": {
            "open_count": len(vuln_rows),
            "sbom_packages_scanned": len(unique_purls_set),
            "source": vuln_source,
            "repo_advisories": repo_advisories,
            "alerts": vuln_rows,
            "all_alerts": vuln_all_rows,
        },
    }


def fetch_newest_and_deep_prs(
    repo: str,
    latest_snap_rows: list[dict],
    gh_headers: dict[str, str],
    tz: ZoneInfo,
) -> tuple[dict, dict]:
    """Fetch newest (< 14d) external PRs plus deep inspection payloads per PR."""
    rev_map = collector.load_reviewer_map(SCRIPT_DIR / "reviewer_map.json")
    client = collector.GitHubClient(reviewer_map=rev_map)
    now_local = dt.datetime.now(tz)

    pulls = client.list_open_pulls(repo)
    newest_rows: list[dict] = []
    for pr_item in sorted(pulls, key=lambda x: x["number"], reverse=True):
        if pr_item.get("draft") or collector.is_bot(pr_item.get("user")):
            continue
        labels = {lbl["name"].lower() for lbl in (pr_item.get("labels") or [])}
        if "external" not in labels:
            continue
        cdt = collector.parse_gh_ts(pr_item.get("created_at"))
        if not cdt or (now_local.date() - cdt.astimezone(tz).date()).days >= 14:
            continue
        row = collector.analyze_pr(client, repo, pr_item, now_local, tz, allow_extra_calls=True)
        row["daily_change"] = "New" if row["days_open"] <= 2 else "Newest (<14d)"
        newest_rows.append(row)
    newest_rows.sort(key=lambda r: (r["days_open"], -r["number"]))

    pr_nums = sorted(
        {r["number"] for r in latest_snap_rows} | {r["number"] for r in newest_rows}
    )

    def fmt_name(login: str) -> str:
        return rev_map.get((login or "").lower(), login or "")

    def fetch_one(num: int) -> tuple[str, dict]:
        pr = http_json(f"https://api.github.com/repos/{repo}/pulls/{num}", gh_headers)
        head = pr.get("head") or {}
        head_sha = head.get("sha") or ""
        commits_raw = http_json(
            f"https://api.github.com/repos/{repo}/pulls/{num}/commits?per_page=100",
            gh_headers,
        )
        reviews_raw = http_json(
            f"https://api.github.com/repos/{repo}/pulls/{num}/reviews?per_page=100",
            gh_headers,
        )
        inline_raw = http_json(
            f"https://api.github.com/repos/{repo}/pulls/{num}/comments?per_page=100",
            gh_headers,
        )
        issue_raw = http_json(
            f"https://api.github.com/repos/{repo}/issues/{num}/comments?per_page=100",
            gh_headers,
        )
        files_raw = http_json(
            f"https://api.github.com/repos/{repo}/pulls/{num}/files?per_page=30",
            gh_headers,
        )

        checks: dict = {"passed": 0, "failed": 0, "pending": 0, "neutral": 0, "runs": []}
        if head_sha:
            try:
                cr = http_json(
                    f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs?per_page=50",
                    gh_headers,
                )
                for run in cr.get("check_runs") or []:
                    st_s = (run.get("status") or "").lower()
                    conc = (run.get("conclusion") or "").lower()
                    if st_s != "completed":
                        bkt = "pending"
                    elif conc == "success":
                        bkt = "passed"
                    elif conc in {"failure", "timed_out", "action_required", "cancelled"}:
                        bkt = "failed"
                    else:
                        bkt = "neutral"
                    checks[bkt] += 1
                    checks["runs"].append(
                        {
                            "name": run.get("name") or "Check",
                            "bucket": bkt,
                            "conclusion": conc or st_s,
                            "url": run.get("html_url") or "",
                        }
                    )
            except Exception:  # pylint: disable=broad-except
                pass
        b_ord = {"failed": 0, "pending": 1, "neutral": 2, "passed": 3}
        checks["runs"].sort(key=lambda x: (b_ord.get(x["bucket"], 9), x["name"]))
        checks["runs"] = checks["runs"][:16]

        commits = [
            {
                "sha": (c.get("sha") or "")[:7],
                "message": ((c.get("commit") or {}).get("message") or "").splitlines()[0][:110],
                "author": fmt_name(
                    (c.get("author") or {}).get("login")
                    or ((c.get("commit") or {}).get("author") or {}).get("name")
                    or ""
                ),
                "date": (
                    (((c.get("commit") or {}).get("committer") or {}).get("date"))
                    or (((c.get("commit") or {}).get("author") or {}).get("date"))
                    or ""
                )[:10],
                "url": c.get("html_url") or "",
            }
            for c in commits_raw[-6:][::-1]
        ]

        reviews = [
            {
                "id": rv.get("id"),
                "reviewer": fmt_name((rv.get("user") or {}).get("login") or ""),
                "login": (rv.get("user") or {}).get("login") or "",
                "state": (rv.get("state") or "COMMENTED").upper(),
                "date": (rv.get("submitted_at") or "")[:10],
                "body": clean_text(rv.get("body") or "", 600),
                "url": rv.get("html_url") or "",
            }
            for rv in reviews_raw
            if not is_bot(rv.get("user"))
        ][-15:][::-1]

        review_comments: list[dict] = []
        gemini_comments: list[dict] = []
        for ic in inline_raw:
            user = ic.get("user")
            body = clean_text(ic.get("body") or "", 600)
            if not body:
                continue
            item = {
                "id": ic.get("id"),
                "review_id": ic.get("pull_request_review_id"),
                "in_reply_to_id": ic.get("in_reply_to_id"),
                "author": (
                    "Gemini Code Assist"
                    if is_gemini(user)
                    else fmt_name((user or {}).get("login") or "")
                ),
                "login": (user or {}).get("login") or "",
                "date": (ic.get("created_at") or "")[:10],
                "path": ic.get("path") or "",
                "line": ic.get("line") or ic.get("original_line") or None,
                "body": body,
                "url": ic.get("html_url") or "",
            }
            if is_gemini(user):
                gemini_comments.append(item)
            elif not is_bot(user):
                review_comments.append(item)

        comments = [
            {
                "author": fmt_name((cm.get("user") or {}).get("login") or ""),
                "login": (cm.get("user") or {}).get("login") or "",
                "date": (cm.get("created_at") or "")[:10],
                "body": clean_text(cm.get("body") or "", 600),
                "url": cm.get("html_url") or "",
            }
            for cm in issue_raw
            if not is_bot(cm.get("user")) and clean_text(cm.get("body") or "", 600)
        ][-20:][::-1]

        files = sorted(
            [
                {
                    "filename": f.get("filename") or "",
                    "status": f.get("status") or "modified",
                    "additions": f.get("additions") or 0,
                    "deletions": f.get("deletions") or 0,
                }
                for f in files_raw
            ],
            key=lambda x: -(x["additions"] + x["deletions"]),
        )[:15]

        return str(num), {
            "head_branch": head.get("label") or head.get("ref") or "",
            "base_branch": (pr.get("base") or {}).get("ref") or "develop",
            "body_summary": clean_text(pr.get("body") or "", 650, strip_checklist=True),
            "assignees": [
                fmt_name(a.get("login") or "")
                for a in (pr.get("assignees") or [])
                if a.get("login")
            ],
            "labels": [
                lbl.get("name") or ""
                for lbl in (pr.get("labels") or [])
                if lbl.get("name")
            ],
            "total_commits": pr.get("commits") or len(commits_raw),
            "total_files": pr.get("changed_files") or len(files_raw),
            "additions": pr.get("additions") or 0,
            "deletions": pr.get("deletions") or 0,
            "review_comments_count": pr.get("review_comments") or 0,
            "issue_comments_count": pr.get("comments") or 0,
            "checks": checks,
            "commits": commits,
            "reviews": reviews,
            "review_comments": review_comments[-30:][::-1],
            "gemini_comments": gemini_comments[-10:][::-1],
            "comments": comments,
            "files": files,
        }

    deep: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for pr_key, details in pool.map(fetch_one, pr_nums):
            deep[pr_key] = details

    newest_payload = {
        "generated_at": now_local.strftime("%b %d, %Y %I:%M %p %Z"),
        "date": now_local.strftime("%Y-%m-%d"),
        "rows": newest_rows,
    }
    return newest_payload, deep


def build_reports_dashboard(
    output_dir: Path,
    repo: str = DEFAULT_REPO,
    sheet_url: str = "",
    refresh_live: bool = True,
    tz_name: str = DEFAULT_TZ,
    out_path: Path | None = None,
) -> Path:
    """Assemble and write the self-contained `reports_dashboard.html` file."""
    tz = ZoneInfo(tz_name)
    history_dir = output_dir / "history"
    cache_dir = output_dir / "reports_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    snaps = sanitize_snapshots(
        [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(history_dir.glob("????-??-??.json"))
        ]
    )
    latest_rows = snaps[-1].get("rows", []) if snaps else []

    sec_cache = cache_dir / "security_snapshot.json"
    newest_cache = cache_dir / "newest_prs.json"
    deep_cache = cache_dir / "pr_deep_details.json"

    security = (
        json.loads(sec_cache.read_text(encoding="utf-8"))
        if sec_cache.exists()
        else {}
    )
    newest_prs = (
        json.loads(newest_cache.read_text(encoding="utf-8"))
        if newest_cache.exists()
        else {"rows": []}
    )
    pr_details = (
        json.loads(deep_cache.read_text(encoding="utf-8"))
        if deep_cache.exists()
        else {}
    )

    if refresh_live:
        token = load_gh_token()
        gh_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ctk-reports-dashboard/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            gh_headers["Authorization"] = f"Bearer {token}"
        try:
            security = fetch_security_snapshot(repo, gh_headers, tz)
            sec_cache.write_text(
                json.dumps(security, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[warn] Live security fetch failed, using cache: {exc}", file=sys.stderr)
        try:
            newest_prs, pr_details = fetch_newest_and_deep_prs(
                repo, latest_rows, gh_headers, tz
            )
            newest_cache.write_text(
                json.dumps(newest_prs, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            deep_cache.write_text(
                json.dumps(pr_details, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[warn] Live PR details fetch failed, using cache: {exc}", file=sys.stderr)

    payload = {
        "sheetUrl": sheet_url or os.environ.get("SHEET_SHARE_URL", ""),
        "snapshots": snaps,
        "newestPrs": newest_prs,
        "prDetails": pr_details,
        "security": security,
    }
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        raise RuntimeError(f"Missing placeholder {PLACEHOLDER} in {TEMPLATE_PATH}")
    safe_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace(
        "</", "<\\/"
    )
    out_html = out_path or (output_dir / "reports_dashboard.html")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(tpl.replace(PLACEHOLDER, safe_json, 1), encoding="utf-8")
    return out_html


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for building the unified reports dashboard."""
    parser = argparse.ArgumentParser(
        description="Build the unified Cluster Toolkit Reports HTML dashboard."
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Target owner/repo")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("REPORT_OUTPUT_DIR", "./cluster_toolkit_reports")),
        help="Directory containing history/ and reports_cache/",
    )
    parser.add_argument(
        "--sheet-url",
        default=os.environ.get("SHEET_SHARE_URL", ""),
        help="Optional external spreadsheet URL for the Export Sheet button",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TZ,
        help="IANA timezone used for timestamps (default: Asia/Kolkata)",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Render strictly from existing history/ and reports_cache/ files",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional custom output HTML path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    out_file = build_reports_dashboard(
        output_dir=args.output_dir,
        repo=args.repo,
        sheet_url=args.sheet_url,
        refresh_live=not args.no_refresh,
        tz_name=args.timezone,
        out_path=args.out,
    )
    print(f"Built unified Cluster Toolkit Reports dashboard -> {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
