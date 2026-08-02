# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

BPM（進捗監視盤）is a Docker-based dashboard that reads parent/child issues from the Backlog project management API and visualizes planned vs. actual progress in real time.

## Running the project

```bash
# First-time setup
cp .env.example .env
# Edit .env with real BACKLOG_SPACE, BACKLOG_API_KEY, BACKLOG_PROJECT_KEY

# Start
docker compose up -d

# Logs
docker compose logs -f backend
docker compose logs -f frontend

# Stop
docker compose down
```

The dashboard is served at `http://localhost:3031`.

### Applying changes

`frontend/html` and `frontend/nginx.conf` are bind-mounted into the frontend container, so a frontend-only change needs no rebuild:

```bash
docker compose restart frontend
```

Backend changes still require `docker compose up -d --build`. Avoid `--build` when nothing under `backend/` changed: on this host the build steps themselves are cached, but exporting the image into the containerd store takes 2–3 minutes (layer export plus the provenance attestation manifest that BuildKit generates by default). `BUILDX_NO_DEFAULT_ATTESTATIONS=1` cuts roughly 50 s off a rebuild.

There is no test suite and no linter configured.

## Environment variables

All variables are loaded from `.env` (never committed). The `.env.example` shows the required keys.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `BACKLOG_SPACE` | Yes | — | e.g. `yourspace.backlog.com` |
| `BACKLOG_API_KEY` | Yes | — | From Backlog personal settings |
| `BACKLOG_PROJECT_KEY` | Yes | — | e.g. `ASPIT` |
| `CACHE_TTL` | No | `60` | Seconds between background snapshot refreshes of `/api/issues` |
| `PROGRESS_FIELD_NAME` | No | `進捗率` | Backlog custom field name for child issue progress |
| `STATUS_FIELD_NAME` | No | `ステータス` | Backlog custom field name for parent issue status |

## Architecture

### Request flow

```
Browser → nginx :3031 → /api/* → Flask :5000 → Backlog REST API
                       → /      → static index.html
```

### Backend (`backend/app.py`)

Single-file Flask app with three layers:

1. **Backlog API helpers** (`backlog_get`, `fetch_issues_all`): Raw HTTP calls. `fetch_issues_all` handles Backlog's 100-item pagination by looping with `offset`.

2. **Caching**: Two mechanisms.
   - **`@cached` decorator** (`_cache` dict): project ID, custom fields and field options are cached for 1 hour. Resets on container restart.
   - **Issues snapshot layer** (`refresh_snapshot`, `_snapshot_refresher`): the full Backlog fetch takes ~20s, so it is never done on the request path. A background daemon thread refreshes the snapshot every `CACHE_TTL` seconds; `/api/issues` always returns the in-memory snapshot instantly. The snapshot is also persisted to `issues_snapshot.json` on the `bpm-data` volume and reloaded at startup, so a restart serves the last snapshot immediately instead of blocking ~20s. The backend runs gunicorn with `--workers 1 --threads 4` so the snapshot and refresher live in a single process (multiple workers would split the cache and multiply the fetch).
   - **Local snapshot updates** (`_raw`, `apply_issue_update`, `reflect_issue_change`): mutations must not pay the ~20s fetch, so `_raw` keeps the *inputs* to `build_response` (parent/child raw JSON + custom field ids) in memory. A mutation replaces just the changed issue in `_raw` — using the issue JSON that Backlog's own PATCH response returns — and re-runs `build_response`, which recomputes parent totals and `health` through the same code path as a full fetch (no duplicated aggregation logic). Measured ~65 ms at real project scale (~3 200 issues), vs ~20 s before. `reflect_issue_change` is the shared post-processing for all three mutation endpoints; when `_raw` is empty (before the first fetch after a restart) it falls back to `refresh_snapshot_async()`, which does the full fetch on a background thread instead of blocking the response.
   - **Refresh-in-flight protection** (`_recent_patches`, `_merge_recent_patches`): a full fetch that started *before* a mutation would otherwise republish the pre-mutation value and make the just-entered hours appear to revert for up to `CACHE_TTL`. Mutations record their updated issue JSON in `_recent_patches` (keyed by issue key, TTL 5 min); `refresh_snapshot` re-applies any entry newer than the fetch's start time before publishing.
   - `_raw` is memory-only — only the built snapshot is persisted to disk.

3. **Data transformation** (`_fetch_raw`, `build_response`, `format_issue`, `calc_health`): `_fetch_raw` only fetches and groups (returning `(parents, children_map, field_ids)`); `build_response` is the pure build step, kept separate so local updates can re-run it without any HTTP.
   - **Parent filtering**: issues where `parentIssueId is None`, issue type name is `"00.案件"`, and status is not `"完了"`.
   - **Health logic**: `red` if past due or actual > planned × 1.2; `yellow` if due within 3 days or actual > planned; otherwise `green`. Calculated from the *children's* hours, not the parent's own hours.
   - **Progress extraction** (`extract_custom_value`): the custom field value may be a plain number, a string with a leading number, a `dict` with a `name` key, or a list — all cases are normalised to an integer percentage.
   - `format_issue` includes both `assignee` (name) and `assigneeId` (Backlog user id, `None` if unassigned) — the id is what the frontend's assignee `<select>` matches against, since names can collide.

- **`GET /api/project-users`** (`get_project_users`, cached 1h): `[{"id", "name"}, ...]` from `/projects/:key/users`, used to populate the assignee dropdown in the child detail modal.
- **`PATCH /api/issue/<key>`** also accepts `assigneeId` (int, or `""` to clear to unassigned). Confirmed by live testing against a real issue that Backlog accepts an empty string for `assigneeId` to unassign — this is undocumented in the official API reference. Only child issues get an assignee editor in the UI; parents don't.

### Frontend (`frontend/html/index.html`)

Single HTML file, no build step, no framework. All JS is inline.

- **Polling**: `fetchData()` is called on load and every `POLL_MS` (60 000 ms). Forced refresh (`?t=<timestamp>`) bypasses the browser cache; the backend always returns its warm snapshot.
- **Filter state**: one global `filter` object `{ health, status, assignee, customStatus, childSort, hideDone }`. Every filter change calls `renderTable()` which re-renders from the in-memory `allData` array without re-fetching.
- **Custom status filter**: matches the numeric prefix of `customStatus` — 0–19 → `見積`, 20–29 → `設計`, 30–39 → `正式`, 40–49 → `開発`, 59–60 → `テスト`, 69 → `本番`.
- **Backlog built-in status IDs**: `1`=未対応, `2`=処理中, `3`=処理済み, `4`=完了 — `3` and `4` are both "done" states (`DONE_STATUS_IDS`/`isDoneStatus`); only `4` is Backlog's actual "完了".
- **`isParentDone`**: true if the parent's own status is done, or (failing that) every child's status is done. `filter.hideDone` (chip "✅ 処理済み・完了を隠す", shared with `gantt.html`) hides parents matching `isParentDone` and filters out individual done children from expanded rows — mutually exclusive with `filter.status === 'done'` (picking one clears the other, since together they'd always yield zero rows).
- **Progress bars**: parents show two bars (plan % based on date range, actual % based on hours); children show one bar (custom progress field if present, else hours ratio).
- **Child detail modal**: the modal itself (HTML injection, open/close/save, embedded `'dw'` worklog panel) now lives in `frontend/html/child-modal.js` / `child-modal.css`, shared with `gantt.html` — see that section below. `index.html` only keeps the page-local pieces: `findChild(childId)` (looks up `{ child, parent }` in `allData`) and the independent `'w'` worklog modal (`openWorklogModal`/`closeWorklogModal`/`worklogChildId`), wired together via `ChildModal.init({ findChild, onDataChanged, getStandaloneWorklogId })` at startup.
- **Worklog UI prefix convention**: `'w'` for the standalone worklog modal (table's actual-hours cell, `index.html` only), `'dw'` for the panel embedded in the shared child detail modal. Both are implemented once in `child-modal.js`; `worklogTargetId(prefix)` resolves the target child issue (`modalChildId` for `'dw'`, `opts.getStandaloneWorklogId()` for `'w'`).

### Shared child detail modal (`frontend/html/child-modal.js` / `child-modal.css`)

Used by both `index.html` and `gantt.html` so the same editable modal (assignee/progress/dates/check-status + the `'dw'` worklog panel) doesn't have to be duplicated. Not loaded by `worklog.html`.

- **Contract**: each page calls `ChildModal.init({ findChild, onDataChanged, getStandaloneWorklogId? })` once at startup. `findChild(id)` must return `{ child, parent }` (or `null`) by looking up the page's own data — `index.html`'s `allData`, or `gantt.html`'s `findItem()` result adapted to this shape. `onDataChanged()` is awaited after a successful save/worklog change so the caller can refresh its own view (`fetchData(true)` on both pages). `getStandaloneWorklogId` is only passed by `index.html`, for the `'w'` prefix.
- **DOM injection**: `init()` injects the modal's HTML into `document.body` (`document.getElementById('childModal')` guard prevents double-injection) rather than each page hard-coding it — this is why the HTML block was removed from `index.html`.
- **Global aliases**: `openChildModal`, `closeChildModal`, `saveChildEdits`, `mountWorklogPanel`, `loadWorklogHistory`, `addWorklog`, `deleteWorklog`, `setWorklogMsg`, `worklogTargetId` are attached to `window` so the injected modal's inline `onclick` attributes (and the dynamically generated worklog history rows' delete buttons) keep working without needing to reference a namespaced object. `ChildModal.open(id)` is the same function as `window.openChildModal`.
- After adding/deleting a worklog entry from the `'dw'` panel, `refreshChildModalHours()` patches only the 予定/実績/差分/残工数 spans in the open detail modal (by id: `mPlannedH`/`mActualH`/`mDiffH`/`mRemainingH`) rather than re-rendering the whole modal — a full re-render would wipe out an in-progress progress/date edit the user hasn't saved yet.

### Gantt (`frontend/html/gantt.html`)

Gantt view of the same `/api/issues` payload — no backend involvement, no extra endpoint. Reached from the 「📅 ガント」 header button on `index.html`. Editing (assignee/progress/dates/actual hours) is available via the shared child detail modal (click a child's title); the bars themselves are not draggable.

- **View mode** (`viewMode`, `'project'` | `'assignee'`, key `bpm2-gantt-mode`, chips `📋 案件別` / `👤 担当者別`): `renderGantt()` dispatches to the original parent-hierarchy renderer or to `renderAssigneeGantt()`. Switching modes does not touch the shared `filter` object.
- **Assignee view** (`visibleAssigneeGroups()`): flattens every parent's children, filters them (health/status/hideDone/hideUndated on the child itself; the custom-status chip is inherited from the child's *parent*, since children don't carry that field), then groups by `assigneeId` (`null`/missing → `'unassigned'`). Parent rows are not shown at all. Groups are sorted by name (五十音順), with the unassigned group always last; children within a group are sorted by start date. An assignee with zero matching children never produces an empty group — there's no "0-issue assignee" placeholder row. Each grouped child object is a shallow copy of the original with a `.parent` reference attached (for the small `[PARENT-KEY]` link next to the title and for the tooltip), so it never mutates `allData`. Group open/closed state lives in `collapsedGroups` (a `Set` of `'a:' + assigneeId`), separate from `expanded` (the project-mode parent `Set`) because assignee groups default to *open* while project-mode parents default to *closed*.
- **Assignee multi-select filter** (`selectedAssignees`, key `bpm2-gantt-assignees`, assignee-view only): array of `assigneeId` numbers plus the literal string `'unassigned'`; empty array means "everyone". Candidates come from the union of `/api/project-users` (fetched once via `loadProjectUsersForFilter()`) and whatever `assigneeId`s actually appear in `allData`, so a user who left the project but still owns issues isn't dropped from the list. This is intentionally independent of the single-select `filter.assignee` dropdown used by project mode and `index.html` — it does not touch `bpm2-filter`, so switching modes or navigating to `index.html` never clobbers the other page's single-select choice.
- **Layout**: one horizontally scrolling container. Each row is a flex pair of a `position: sticky; left: 0` left pane (`LEFT_W` = 340 px) and a track. The time-axis header is `position: sticky; top: 0`. Grid columns, weekend shading and the today line live in a single absolutely positioned `.gantt-bg` layer behind all rows, not per row.
- **Time axis** (`buildAxis`): granularity 日 / 週 / 月 switches column width and unit; the range spans all displayed items (padded, snapped to unit boundaries) and always includes today. Accepts a plain array of `{ range, children }` — the assignee view maps its groups to `{ range: null, children: group.children }` since groups have no period of their own. `xOf(date)` maps a date to px — month granularity pro-rates within the month, so column width alone is not enough.
- **Bar period** (`resolveRange`): a child needs both `start` and `due`. A parent falls back to min(child starts) → max(child dues) when its own dates are incomplete, rendered as a thinner "rolled" summary bar. This matters — in the live project only ~5 of 108 parents carry their own dates. (The assignee view never rolls up a period this way since it has no parent-level bar.)
- **`hideUndated`** (default on, `bpm2-gantt-hide-undated`): hides rows whose period cannot be resolved. Without it the chart is mostly empty rows. `countUndated()` (used for the chip's `(N)` suffix) counts children only in assignee mode, vs. parents+children in project mode.
- **Bar colour** is health; parents use the backend's `health`, children are judged client-side by `childHealth` (same thresholds as the backend's `calc_health`; `isDoneStatus` children are always green regardless of date). Fill = progress, thin vertical line = date-based expected progress.
- **Shared state**: the `filter` object and `bpm2-filter` localStorage key are shared with `index.html`, including the `childSort` key this page does not use — it is kept in the object so a round-trip through this page does not drop index.html's setting. Granularity, the undated toggle, the view mode, and the assignee multi-select each use their own separate localStorage keys.

### nginx (`frontend/nginx.conf`)

Reverse-proxies `/api/` to `http://backend:5000`. Everything else is served from the static root with an SPA fallback. `/health` returns a plain 200 for infra checks.
