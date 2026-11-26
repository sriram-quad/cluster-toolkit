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

"""[REPORT GEN] Self-contained interactive HTML dashboard builder for Stale PRs.

Generates a single `.html` file embedding all historical + live daily snapshots
as inline JSON with interactive KPI cards, SVG status donut chart, daily trend
chart, reviewer action queue, search/filter/sort PR table, Day-over-Day diff,
and one-click copyable Google Chat alert text. Requires zero server backend or
runtime API permissions.
"""

from __future__ import annotations

import json
from pathlib import Path


def build_dashboard_html(
    snapshots: list[dict], sheet_url: str = "", excel_url: str = ""
) -> str:
    """Return a single-file interactive HTML dashboard from daily snapshots."""
    payload = {"sheetUrl": sheet_url, "snapshots": snapshots}
    safe_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    def _esc_attr(val: str) -> str:
        return (
            val.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    excel_dl_html = ""
    if excel_url:
        latest_epoch = (
            (snapshots[-1].get("generated_epoch") if snapshots else None) or ""
        )
        bust_url = (
            f"{excel_url}?v={latest_epoch}"
            if latest_epoch and "?" not in excel_url
            else excel_url
        )
        excel_dl_html = f"""
          <a href="{_esc_attr(bust_url)}" download="stale_pr_master_tracker.xlsx"
            class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-[var(--app-muted)] border border-[var(--app-border)] hover:opacity-85 transition">
            <span>Download .xlsx</span>
          </a>"""

    sheet_link_html = excel_dl_html
    if sheet_url:
        sheet_link_html += f"""
          <a href="{_esc_attr(sheet_url)}" target="_blank" rel="noopener noreferrer"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-[var(--app-primary)] text-[var(--app-primary-foreground)] hover:opacity-90 transition shadow-sm">
            <span>Open Shared Excel Sheet</span>
          </a>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>Stale External PRs Dashboard — cluster-toolkit</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    :root {{
      --app-background: #f8fafc;
      --app-foreground: #0f172a;
      --app-card: #ffffff;
      --app-border: #e2e8f0;
      --app-primary: #1d4ed8;
      --app-primary-foreground: #ffffff;
      --app-muted: #f1f5f9;
      --app-muted-foreground: #475569;
    }}
    html.dark {{
      --app-background: #0b1120;
      --app-foreground: #f1f5f9;
      --app-card: #111827;
      --app-border: #1f2937;
      --app-primary: #3b82f6;
      --app-primary-foreground: #ffffff;
      --app-muted: #1e293b;
      --app-muted-foreground: #94a3b8;
    }}
  </style>
</head>
<body class="bg-[var(--app-background)] text-[var(--app-foreground)] font-sans antialiased min-h-screen">
  <div class="max-w-[1440px] mx-auto px-4 sm:px-6 py-5 space-y-5">
    <header class="bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 sm:p-5 shadow-sm">
      <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
        <div class="space-y-1">
          <div class="flex flex-wrap items-center gap-2.5">
            <h1 class="text-lg sm:text-xl font-bold tracking-tight">Stale External PRs Dashboard</h1>
            <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-500/15 text-emerald-600 border border-emerald-500/30">
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>Mon-Fri 5:00 PM (UTC+05:30) Auto-Report
            </span>
          </div>
          <p id="headerSubtitle" class="text-xs sm:text-sm text-[var(--app-muted-foreground)]"></p>
        </div>
        <div class="flex flex-wrap items-center gap-2.5">
          <div class="inline-flex items-center gap-2 bg-[var(--app-muted)] border border-[var(--app-border)] rounded-lg px-2.5 py-1">
            <span class="text-[11px] font-medium text-[var(--app-muted-foreground)]">Report Date:</span>
            <select id="dateSelect" onchange="onDateChange(this.value)"
              class="bg-transparent text-xs font-semibold text-[var(--app-foreground)] focus:outline-none cursor-pointer"></select>
          </div>
          <button type="button" onclick="document.documentElement.classList.toggle('dark')"
            class="px-3 py-1.5 rounded-lg text-xs font-medium bg-[var(--app-muted)] border border-[var(--app-border)]">Theme</button>{sheet_link_html}
        </div>
      </div>
      <div class="mt-4 pt-3 border-t border-[var(--app-border)] flex flex-wrap items-center gap-1.5">
        <button type="button" data-tab="overview" onclick="switchTab('overview')" class="nav-tab px-3.5 py-1.5 rounded-lg text-xs font-semibold">Overview &amp; PR Tracker</button>
        <button type="button" data-tab="diff" onclick="switchTab('diff')" class="nav-tab px-3.5 py-1.5 rounded-lg text-xs font-semibold">Day-over-Day Changes <span id="diffBadge" class="ml-1 px-1.5 py-0.5 rounded-full text-[10px]"></span></button>
        <button type="button" data-tab="reviewers" onclick="switchTab('reviewers')" class="nav-tab px-3.5 py-1.5 rounded-lg text-xs font-semibold">Reviewer Queue</button>
        <button type="button" data-tab="chat" onclick="switchTab('chat')" class="nav-tab px-3.5 py-1.5 rounded-lg text-xs font-semibold">Daily Chat Alert</button>
      </div>
    </header>

    <section id="kpiGrid" class="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3"></section>

    <div id="tab-overview" class="space-y-5">
      <section class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 shadow-sm flex flex-col">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-[var(--app-muted-foreground)] mb-3">Status Breakdown</h2>
          <div id="categoryChart" class="flex-1 flex items-center gap-4"></div>
        </div>
        <div class="bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 shadow-sm flex flex-col">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-[var(--app-muted-foreground)] mb-3">Daily Stale PR Trend</h2>
          <div id="trendChart" class="flex-1 flex items-end min-h-[140px]"></div>
        </div>
        <div class="bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 shadow-sm flex flex-col">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-[var(--app-muted-foreground)] mb-3">Top Pending Reviewers</h2>
          <div id="reviewerMiniChart" class="flex-1 space-y-2"></div>
        </div>
      </section>

      <section class="bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl shadow-sm overflow-hidden">
        <div class="p-4 border-b border-[var(--app-border)] flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div role="search" class="flex-1 max-w-md">
            <input id="searchInput" type="text" oninput="applyFilters()" placeholder="Search PR #, title, author, reviewer, status..."
              class="w-full px-3 py-1.5 text-xs rounded-lg bg-[var(--app-muted)] border border-[var(--app-border)] focus:outline-none">
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <select id="typeFilter" onchange="applyFilters()" class="px-2.5 py-1.5 text-xs rounded-lg bg-[var(--app-muted)] border border-[var(--app-border)]">
              <option value="all">All (External + Quadrant)</option>
              <option value="External">External Only</option>
              <option value="Quadrant (-quad)">Quadrant (-quad) Only</option>
            </select>
            <select id="sortSelect" onchange="applyFilters()" class="px-2.5 py-1.5 text-xs rounded-lg bg-[var(--app-muted)] border border-[var(--app-border)]">
              <option value="number_asc">Sort: PR #</option>
              <option value="days_open_desc">Sort: Days Open</option>
              <option value="inactive_desc">Sort: Days Inactive</option>
              <option value="conflicts_first">Sort: Conflicts First</option>
            </select>
            <button type="button" id="resetFiltersBtn" onclick="resetFilters()" class="hidden px-2.5 py-1.5 text-xs font-medium rounded-lg bg-rose-500/15 text-rose-600 border border-rose-500/30">Clear</button>
            <span id="resultCounter" class="text-xs text-[var(--app-muted-foreground)]"></span>
          </div>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-left border-collapse">
            <thead>
              <tr class="bg-[var(--app-muted)] text-[11px] font-semibold uppercase tracking-wider text-[var(--app-muted-foreground)] border-b border-[var(--app-border)]">
                <th class="py-2.5 px-3 w-24">PR</th>
                <th class="py-2.5 px-3 min-w-[240px]">Title &amp; Author</th>
                <th class="py-2.5 px-3 w-36">Timeline</th>
                <th class="py-2.5 px-3 w-44">Category</th>
                <th class="py-2.5 px-3 w-48">Next Action Owner</th>
                <th class="py-2.5 px-3 min-w-[300px]">Status Summary</th>
              </tr>
            </thead>
            <tbody id="prTableBody" class="divide-y divide-[var(--app-border)] text-xs"></tbody>
          </table>
        </div>
      </section>
    </div>

    <div id="tab-diff" class="hidden bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 shadow-sm space-y-3">
      <h2 id="diffTitle" class="text-sm font-bold"></h2>
      <div id="diffList" class="space-y-2.5"></div>
    </div>

    <div id="tab-reviewers" class="hidden">
      <div id="reviewerCardsGrid" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4"></div>
    </div>

    <div id="tab-chat" class="hidden bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 shadow-sm space-y-3">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-bold">Formatted Google Chat Alert Message</h2>
        <button type="button" onclick="copyChatText()" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-[var(--app-primary)] text-[var(--app-primary-foreground)]">
          <span id="copyChatLabel">Copy Chat Report</span>
        </button>
      </div>
      <textarea id="chatTextarea" readonly rows="20" class="w-full p-3 rounded-lg text-xs font-mono bg-[var(--app-muted)] border border-[var(--app-border)]"></textarea>
    </div>
  </div>

  <script>
    const DATA = {safe_json};
    const SNAPS = DATA.snapshots || [];
    const state = {{ snapIdx: Math.max(0, SNAPS.length - 1), tab: 'overview', category: 'all', reviewer: 'all', changeFilter: 'all' }};
    const CAT_META = {{
      'Merge Conflicts': {{ color: '#ef4444', badge: 'bg-rose-500/15 text-rose-600 border-rose-500/30' }},
      'Changes Requested': {{ color: '#f97316', badge: 'bg-amber-500/15 text-amber-600 border-amber-500/30' }},
      'Awaiting Re-Review': {{ color: '#3b82f6', badge: 'bg-blue-500/15 text-blue-600 border-blue-500/30' }},
      'Awaiting Review': {{ color: '#eab308', badge: 'bg-yellow-500/15 text-yellow-600 border-yellow-500/30' }},
      'Approved (Needs 2nd Review)': {{ color: '#10b981', badge: 'bg-emerald-500/15 text-emerald-600 border-emerald-500/30' }},
      'Approved (Ready / Pending Merge)': {{ color: '#059669', badge: 'bg-emerald-500/15 text-emerald-600 border-emerald-500/30' }}
    }};
    const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    const currentSnap = () => SNAPS[state.snapIdx] || {{ rows: [], resolved_since_prev: [] }};
    const prevSnap = () => state.snapIdx > 0 ? SNAPS[state.snapIdx - 1] : null;

    function onDateChange(v) {{ state.snapIdx = parseInt(v, 10) || 0; renderAll(); }}
    function switchTab(tab) {{
      state.tab = tab;
      ['overview','diff','reviewers','chat'].forEach(t => document.getElementById('tab-' + t).classList.toggle('hidden', t !== tab));
      document.querySelectorAll('.nav-tab').forEach(b => {{
        const a = b.getAttribute('data-tab') === tab;
        b.className = 'nav-tab px-3.5 py-1.5 rounded-lg text-xs font-semibold ' + (a ? 'bg-[var(--app-primary)] text-[var(--app-primary-foreground)]' : 'bg-[var(--app-muted)] text-[var(--app-muted-foreground)]');
      }});
    }}
    function filterByKpi(k) {{
      switchTab('overview'); state.reviewer = 'all'; state.changeFilter = 'all';
      if (k === 'all') state.category = 'all';
      else if (k === 'updated') {{ state.category = 'all'; state.changeFilter = 'updated'; }}
      else state.category = state.category === k ? 'all' : k;
      renderAll();
    }}
    function filterByReviewer(rev) {{ switchTab('overview'); state.reviewer = state.reviewer === rev ? 'all' : rev; renderAll(); }}
    function resetFilters() {{
      state.category = 'all'; state.reviewer = 'all'; state.changeFilter = 'all';
      document.getElementById('searchInput').value = '';
      document.getElementById('typeFilter').value = 'all';
      renderAll();
    }}
    function computeReviewers(rows) {{
      const m = {{}};
      const get = n => (m[n] = m[n] || {{ name: n, pending: [], changes: [], approved: [] }});
      rows.forEach(r => {{
        (r.pending_list || []).forEach(u => get(u).pending.push(r));
        (r.changes_req_list || []).forEach(u => get(u).changes.push(r));
        (r.approved_list || []).forEach(u => get(u).approved.push(r));
      }});
      return Object.values(m).sort((a,b) => (b.pending.length - a.pending.length) || a.name.localeCompare(b.name));
    }}
    function applyFilters() {{
      const rows = currentSnap().rows || [];
      const q = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      const tf = document.getElementById('typeFilter').value;
      const sb = document.getElementById('sortSelect').value;
      let list = rows.filter(r => {{
        if (tf !== 'all' && r.contributor_type !== tf) return false;
        if (state.category === 'await_rev' && r.category !== 'Awaiting Review' && r.category !== 'Awaiting Re-Review') return false;
        else if (state.category === 'approved' && !(r.category || '').startsWith('Approved')) return false;
        else if (state.category === 'Merge Conflicts' && r.conflicts !== 'Yes' && r.category !== 'Merge Conflicts') return false;
        else if (!['all','await_rev','approved','Merge Conflicts'].includes(state.category) && r.category !== state.category) return false;
        if (state.changeFilter === 'updated' && r.daily_change !== 'New' && r.daily_change !== 'Status Updated') return false;
        if (state.reviewer !== 'all' && !(r.pending_list || []).includes(state.reviewer)) return false;
        if (q && ![r.pr_id,r.title,r.author,r.waiting_on,r.status].join(' ').toLowerCase().includes(q)) return false;
        return true;
      }}).sort((a,b) => sb === 'days_open_desc' ? b.days_open - a.days_open : sb === 'inactive_desc' ? b.days_inactive - a.days_inactive : sb === 'conflicts_first' ? (b.conflicts === 'Yes') - (a.conflicts === 'Yes') : a.number - b.number);

      document.getElementById('resetFiltersBtn').classList.toggle('hidden', !(q || tf !== 'all' || state.category !== 'all' || state.reviewer !== 'all' || state.changeFilter !== 'all'));
      document.getElementById('resultCounter').textContent = `Showing ${{list.length}} of ${{rows.length}} PRs`;
      document.getElementById('prTableBody').innerHTML = list.map(r => {{
        const cb = (CAT_META[r.category] || {{ badge: 'bg-[var(--app-muted)] border-[var(--app-border)]' }}).badge;
        return `<tr class="align-top hover:bg-[var(--app-muted)]/40">
          <td class="py-3 px-3"><a href="${{esc(r.url)}}" target="_blank" rel="noopener noreferrer" class="font-bold text-blue-600 hover:underline">${{esc(r.pr_id)}} ↗</a><div class="mt-1 text-[10px] text-[var(--app-muted-foreground)]">${{esc(r.daily_change)}}</div></td>
          <td class="py-3 px-3"><div class="font-semibold">${{esc(r.title)}}</div><div class="text-[11px] text-[var(--app-muted-foreground)]">@${{esc(r.author)}} · ${{esc(r.contributor_type)}}</div></td>
          <td class="py-3 px-3 text-[11px] whitespace-nowrap"><div>Created: ${{esc(r.created_on)}}</div><div>Last: ${{esc(r.last_action_on)}}</div><div class="text-[10px] text-[var(--app-muted-foreground)]">${{r.days_open}}d open · ${{r.days_inactive}}d idle</div></td>
          <td class="py-3 px-3"><span class="inline-block px-2 py-0.5 rounded text-[11px] font-semibold border ${{cb}}">${{esc(r.category)}}</span></td>
          <td class="py-3 px-3 text-[11px] font-medium">${{esc(r.waiting_on)}}</td>
          <td class="py-3 px-3 text-[11px] leading-relaxed">${{esc(r.status)}}</td>
        </tr>`;
      }}).join('') || '<tr><td colspan="6" class="py-8 text-center text-[var(--app-muted-foreground)]">No matching PRs.</td></tr>';
    }}
    function renderAll() {{
      const sn = currentSnap(), prev = prevSnap(), rows = sn.rows || [], res = sn.resolved_since_prev || [];
      document.getElementById('headerSubtitle').textContent = `${{sn.repo || 'GoogleCloudPlatform/cluster-toolkit'}} · Updated ${{sn.generated_at || sn.date}}`;
      const updN = rows.filter(r => r.daily_change === 'New' || r.daily_change === 'Status Updated').length;
      document.getElementById('diffBadge').textContent = updN + res.length;

      const cards = [
        ['all', 'Total Stale PRs', rows.length, 'border-l-blue-500'],
        ['await_rev', 'Awaiting Reviewer', rows.filter(r => r.category === 'Awaiting Review' || r.category === 'Awaiting Re-Review').length, 'border-l-sky-500'],
        ['approved', 'Approved (1+)', rows.filter(r => (r.category || '').startsWith('Approved')).length, 'border-l-emerald-500'],
        ['Changes Requested', 'Awaiting Author', rows.filter(r => r.category === 'Changes Requested').length, 'border-l-amber-500'],
        ['Merge Conflicts', 'Merge Conflicts', rows.filter(r => r.conflicts === 'Yes').length, 'border-l-rose-500'],
        ['updated', 'Changed vs Prior', updN + res.length, 'border-l-purple-500']
      ];
      document.getElementById('kpiGrid').innerHTML = cards.map(([id,lbl,val,acc]) =>
        `<button type="button" onclick="filterByKpi('${{id}}')" class="text-left bg-[var(--app-card)] border border-[var(--app-border)] border-l-4 ${{acc}} rounded-xl p-3.5 shadow-sm"><div class="text-[11px] text-[var(--app-muted-foreground)]">${{lbl}}</div><div class="mt-1 text-2xl font-bold">${{val}}</div></button>`
      ).join('');

      const C = 2 * Math.PI * 38, counts = {{}};
      rows.forEach(r => counts[r.category] = (counts[r.category] || 0) + 1);
      let off = 0;
      const slices = Object.entries(counts).map(([k,n]) => {{
        const len = (n / Math.max(1, rows.length)) * C, col = (CAT_META[k] || {{color:'#64748b'}}).color;
        const s = `<circle cx="50" cy="50" r="38" fill="none" stroke="${{col}}" stroke-width="14" stroke-dasharray="${{len.toFixed(2)}} ${{(C-len).toFixed(2)}}" stroke-dashoffset="${{(-off).toFixed(2)}}" transform="rotate(-90 50 50)"/>`;
        off += len; return s;
      }}).join('');
      const legend = Object.entries(counts).map(([k,n]) =>
        `<button type="button" data-cat="${{esc(k)}}" onclick="filterByKpi(this.getAttribute('data-cat'))" class="w-full flex justify-between text-xs px-2 py-1 rounded hover:bg-[var(--app-muted)]"><span class="truncate">${{esc(k)}}</span><strong>${{n}}</strong></button>`
      ).join('');
      document.getElementById('categoryChart').innerHTML = `<svg viewBox="0 0 100 100" class="w-24 h-24 shrink-0">${{slices}}</svg><div class="flex-1 min-w-0">${{legend}}</div>`;

      const recent = SNAPS.slice(-10), maxT = Math.max(1, ...recent.map(s => (s.rows || []).length));
      document.getElementById('trendChart').innerHTML = `<div class="w-full flex items-end justify-around gap-2">${{recent.map(s => {{
        const idx = SNAPS.indexOf(s), cnt = (s.rows || []).length, h = Math.round((cnt / maxT) * 100);
        return `<button type="button" onclick="onDateChange('${{idx}}')" class="flex-1 flex flex-col items-center gap-1"><span class="text-[11px] font-bold">${{cnt}}</span><div class="w-8 h-20 flex items-end"><div class="w-full bg-blue-500 rounded-t" style="height:${{h}}%"></div></div><span class="text-[10px] text-[var(--app-muted-foreground)]">${{esc((s.date||'').slice(5))}}</span></button>`;
      }}).join('')}}</div>`;

      const revs = computeReviewers(rows);
      const topR = revs.filter(r => r.pending.length).slice(0, 5), maxR = Math.max(1, ...topR.map(r => r.pending.length));
      document.getElementById('reviewerMiniChart').innerHTML = topR.map(r =>
        `<button type="button" data-rev="${{esc(r.name)}}" onclick="filterByReviewer(this.getAttribute('data-rev'))" class="w-full text-left p-1 rounded hover:bg-[var(--app-muted)]"><div class="flex justify-between text-xs"><span>${{esc(r.name)}}</span><strong class="text-blue-600">${{r.pending.length}}</strong></div><div class="w-full h-1.5 bg-[var(--app-muted)] rounded mt-1"><div class="h-full bg-blue-500 rounded" style="width:${{Math.round((r.pending.length/maxR)*100)}}%"></div></div></button>`
      ).join('') || '<p class="text-xs text-[var(--app-muted-foreground)]">No pending reviews.</p>';

      applyFilters();

      document.getElementById('diffTitle').textContent = prev ? `Changes: ${{prev.date}} -> ${{sn.date}}` : `Baseline (${{sn.date}})`;
      const pm = {{}}; if (prev) (prev.rows || []).forEach(r => pm[r.number] = r);
      document.getElementById('diffList').innerHTML = prev ? rows.map(r =>
        `<div class="p-3 rounded border border-[var(--app-border)] text-xs space-y-1"><div class="font-semibold"><a href="${{esc(r.url)}}" target="_blank" rel="noopener noreferrer" class="text-blue-600">${{esc(r.pr_id)}} ↗</a> · ${{esc(r.daily_change)}} · ${{esc(r.title)}}</div><div class="text-[var(--app-muted-foreground)]">Prior: ${{esc(pm[r.number] ? pm[r.number].status : 'New')}}</div><div>Current: ${{esc(r.status)}}</div></div>`
      ).join('') : '<p class="text-xs text-[var(--app-muted-foreground)]">No prior snapshot.</p>';

      document.getElementById('reviewerCardsGrid').innerHTML = revs.map(r =>
        `<div class="bg-[var(--app-card)] border border-[var(--app-border)] rounded-xl p-4 shadow-sm space-y-2 text-xs"><div class="flex justify-between font-bold"><span>${{esc(r.name)}}</span><span>${{r.pending.length}} pending</span></div><div>Pending: ${{r.pending.map(p => `<a href="${{esc(p.url)}}" target="_blank" rel="noopener noreferrer" class="text-blue-600 underline">${{esc(p.pr_id)}}</a>`).join(', ') || '-'}}</div><div>Approved: ${{r.approved.map(p => esc(p.pr_id)).join(', ') || '-'}}</div></div>`
      ).join('');

      document.getElementById('chatTextarea').value = ['Hi Team,', `The Stale external PRs report (${{sn.date}} — ${{rows.length}} open):`, ''].concat(
        rows.flatMap(r => [`PR ID : [${{r.pr_id}}](${{r.url}})`, `Title : ${{r.title}}`, `Author : ${{r.author}}`, `Created on : ${{r.created_on}}`, `Last action taken on : ${{r.last_action_on}}`, `Status : ${{r.status}}`, ''])
      ).join('\\n').trim();
    }}
    async function copyChatText() {{
      const ta = document.getElementById('chatTextarea'), lbl = document.getElementById('copyChatLabel');
      try {{ await navigator.clipboard.writeText(ta.value); lbl.textContent = 'Copied!'; }}
      catch (_) {{ ta.select(); lbl.textContent = 'Selected (Ctrl+C)'; }}
      setTimeout(() => lbl.textContent = 'Copy Chat Report', 2000);
    }}
    document.getElementById('dateSelect').innerHTML = SNAPS.map((s,i) => `<option value="${{i}}" ${{i===state.snapIdx?'selected':''}}>${{esc(s.date)}} (${{esc(s.day_name||'')}})</option>`).reverse().join('');
    switchTab('overview');
    renderAll();
  </script>
</body>
</html>
"""


def write_dashboard_from_history(
    history_dir: Path, output_html: Path, sheet_url: str = ""
) -> Path:
    snaps = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(history_dir.glob("????-??-??.json"))
    ]
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(
        build_dashboard_html(snaps, sheet_url=sheet_url), encoding="utf-8"
    )
    return output_html
