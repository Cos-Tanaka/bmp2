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

### Frontend (`frontend/html/index.html`)

Single HTML file, no build step, no framework. All JS is inline.

- **Polling**: `fetchData()` is called on load and every `POLL_MS` (60 000 ms). Forced refresh (`?t=<timestamp>`) bypasses the browser cache; the backend always returns its warm snapshot.
- **Filter state**: one global `filter` object `{ health, status, assignee, customStatus, childSort }`. Every filter change calls `renderTable()` which re-renders from the in-memory `allData` array without re-fetching.
- **Custom status filter**: matches the numeric prefix of `customStatus` — 0–19 → `見積`, 20–29 → `設計`, 30–39 → `正式`, 40–49 → `開発`, 59–60 → `テスト`, 69 → `本番`.
- **`isParentDone`**: returns true when every child has `statusId === 3` (Backlog's built-in "完了" status ID).
- **Progress bars**: parents show two bars (plan % based on date range, actual % based on hours); children show one bar (custom progress field if present, else hours ratio).

### nginx (`frontend/nginx.conf`)

Reverse-proxies `/api/` to `http://backend:5000`. Everything else is served from the static root with an SPA fallback. `/health` returns a plain 200 for infra checks.
