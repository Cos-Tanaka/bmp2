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
   - **Issues snapshot layer** (`refresh_snapshot`, `_snapshot_refresher`): the full Backlog fetch takes ~20s, so it is never done on the request path. A background daemon thread refreshes the snapshot every `CACHE_TTL` seconds; `/api/issues` always returns the in-memory snapshot instantly. The snapshot is also persisted to `issues_snapshot.json` on the `bpm-data` volume and reloaded at startup, so a restart serves the last snapshot immediately instead of blocking ~20s. Mutations (PATCH issue, add/delete worklog) call `refresh_snapshot()` synchronously so the edit is reflected at once. The backend runs gunicorn with `--workers 1 --threads 4` so the snapshot and refresher live in a single process (multiple workers would split the cache and multiply the fetch).

3. **Data transformation** (`_fetch_and_build`, `build_response`, `format_issue`, `calc_health`):
   - **Parent filtering**: issues where `parentIssueId is None`, issue type name is `"00.案件"`, and status is not `"完了"`.
   - **Health logic**: `red` if past due or actual > planned × 1.2; `yellow` if due within 3 days or actual > planned; otherwise `green`. Calculated from the *children's* hours, not the parent's own hours.
   - **Progress extraction** (`extract_custom_value`): the custom field value may be a plain number, a string with a leading number, a `dict` with a `name` key, or a list — all cases are normalised to an integer percentage.
   - `format_issue` includes both `assignee` (name) and `assigneeId` (Backlog user id, `None` if unassigned) — the id is what the frontend's assignee `<select>` matches against, since names can collide.

- **`GET /api/project-users`** (`get_project_users`, cached 1h): `[{"id", "name"}, ...]` from `/projects/:key/users`, used to populate the assignee dropdown in the child detail modal.
- **`PATCH /api/issue/<key>`** also accepts `assigneeId` (int, or `""` to clear to unassigned). Confirmed by live testing against a real issue that Backlog accepts an empty string for `assigneeId` to unassign — this is undocumented in the official API reference. Only child issues get an assignee editor in the UI; parents don't.

### Frontend (`frontend/html/index.html`)

Single HTML file, no build step, no framework. All JS is inline.

- **Polling**: `fetchData()` is called on load and every `POLL_MS` (60 000 ms). Forced refresh (`?t=<timestamp>`) bypasses the browser cache; the backend always returns its warm snapshot.
- **Filter state**: one global `filter` object `{ health, status, assignee, customStatus, childSort }`. Every filter change calls `renderTable()` which re-renders from the in-memory `allData` array without re-fetching.
- **Custom status filter**: matches the numeric prefix of `customStatus` — 0–19 → `見積`, 20–29 → `設計`, 30–39 → `正式`, 40–49 → `開発`, 59–60 → `テスト`, 69 → `本番`.
- **`isParentDone`**: returns true when every child has `statusId === 3` (Backlog's built-in "完了" status ID).
- **Progress bars**: parents show two bars (plan % based on date range, actual % based on hours); children show one bar (custom progress field if present, else hours ratio).
- **Child detail modal** (`openChildModal`): assignee is an editable `<select>` (falls back to read-only text if `/api/project-users` failed to load) alongside the existing start/due/progress/check-status editors, saved together by `saveChildEdits()`.
- **Worklog UI is shared between two mount points** via a `prefix` argument (`'w'` for the standalone worklog modal opened from the table's actual-hours cell, `'dw'` for the panel embedded in the child detail modal): `mountWorklogPanel`, `loadWorklogHistory`, `addWorklog`, `deleteWorklog`, `setWorklogMsg` all take `prefix` and operate on elements named `${prefix}Hours`, `${prefix}Name`, `${prefix}Date`, `${prefix}Msg`, `${prefix}Add`, `${prefix}History`. `worklogTargetId(prefix)` resolves which child issue is being edited (`modalChildId` for `'dw'`, `worklogChildId` for `'w'`) — the two panels can be open independently without interfering.
- After adding/deleting a worklog entry from the `'dw'` panel, `refreshChildModalHours()` patches only the 予定/実績/差分/残工数 spans in the open detail modal (by id: `mPlannedH`/`mActualH`/`mDiffH`/`mRemainingH`) rather than re-rendering the whole modal — a full re-render would wipe out an in-progress progress/date edit the user hasn't saved yet.

### Gantt (`frontend/html/gantt.html`)

Read-only Gantt view of the same `/api/issues` payload — no backend involvement, no extra endpoint. Reached from the 「📅 ガント」 header button on `index.html`.

- **Layout**: one horizontally scrolling container. Each row is a flex pair of a `position: sticky; left: 0` left pane (`LEFT_W` = 340 px) and a track. The time-axis header is `position: sticky; top: 0`. Grid columns, weekend shading and the today line live in a single absolutely positioned `.gantt-bg` layer behind all rows, not per row.
- **Time axis** (`buildAxis`): granularity 日 / 週 / 月 switches column width and unit; the range spans all displayed items (padded, snapped to unit boundaries) and always includes today. `xOf(date)` maps a date to px — month granularity pro-rates within the month, so column width alone is not enough.
- **Bar period** (`resolveRange`): a child needs both `start` and `due`. A parent falls back to min(child starts) → max(child dues) when its own dates are incomplete, rendered as a thinner "rolled" summary bar. This matters — in the live project only ~5 of 108 parents carry their own dates.
- **`hideUndated`** (default on, `bpm2-gantt-hide-undated`): hides rows whose period cannot be resolved. Without it the chart is mostly empty rows.
- **Bar colour** is health; parents use the backend's `health`, children are judged client-side by `childHealth` (same thresholds as the backend's `calc_health`). Fill = progress, thin vertical line = date-based expected progress.
- **Shared state**: the `filter` object and `bpm2-filter` localStorage key are shared with `index.html`, including the `childSort` key this page does not use — it is kept in the object so a round-trip through this page does not drop index.html's setting. Granularity and the undated toggle use their own keys.

### nginx (`frontend/nginx.conf`)

Reverse-proxies `/api/` to `http://backend:5000`. Everything else is served from the static root with an SPA fallback. `/health` returns a plain 200 for infra checks.
