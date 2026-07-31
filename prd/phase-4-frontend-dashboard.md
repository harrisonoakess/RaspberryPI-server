# PRD: Phase 4 — Private Data Dashboard

## 1. Problem / Background

Phase 2 established durable, at-least-once delivery of CSV files from one
Raspberry Pi to a FastAPI service on Railway. Upload metadata is stored in
SQLite and CSV blobs are stored on the attached Railway Volume. The server also
stores authenticated heartbeat pings from the Pi.

The repository currently has a local HTML viewer in
`tools/railway_viewer.py`. It binds to loopback and shells out to the Railway
CLI for each read. That was useful for receipt verification, but it is not the
hosted, maintainable frontend needed for routine use. Phase 4 replaces it with
a private React dashboard served by the existing FastAPI service.

The dashboard is read-only. It lets a trusted administrator see whether the Pi
has checked in recently, browse stored uploads, and preview CSV records without
exposing Railway credentials, volume paths, or the Pi's ingest credential to
browser code.

Phase 4 depends on `phase-3-multi-card-ingestion.md` and assumes it is already
implemented and deployed. Phase 3 changes the `uploads` table's uniqueness
constraint to `(device_id, card_uuid, filename)` and the blob storage path to
`uploads_root/device_id/card_uuid/filename`. Every schema, storage-path, and
API contract reference below reflects that post-Phase-3 shape, not Phase 2's
`(device_id, filename)` shape.

## 2. Goals

- Replace the current local HTML viewer with a private, hosted dashboard.
- Let an administrator sign in with a dashboard-specific password.
- Show a prominent Pi connection indicator based on the most recent accepted
  heartbeat, together with the exact last-seen time.
- List all upload records newest first using bounded, cursor-based pagination.
- Preview a bounded portion of any stored UTF-8 CSV.
- Keep the dashboard and its read APIs read-only.
- Deploy the React frontend and FastAPI server together as one Railway service
  and one origin.
- Preserve the Phase 1 through Phase 3 ingest contracts and the current Phase 3
  storage layout.

## 3. Non-Goals

- Editing or deleting uploads, pings, database rows, or stored files.
- Uploading files through the dashboard.
- Downloading complete CSV files.
- Charts, aggregation, domain-specific sensor analysis, or CSV schema
  assumptions.
- Public access, user registration, named user accounts, roles, password reset,
  or multiple administrators with separate identities.
- Multiple-device fleet management. Phase 4 retains the existing one-Pi scope.
- Moving the frontend to Vercel or another independently deployed service.
- Changing the Pi's polling, heartbeat, queueing, or upload behavior.
- Replacing SQLite or the Railway Volume.

## 4. Constraints and Security

- The Railway service remains a single replica with the existing attached
  Volume.
- The React application is served by FastAPI under `/dashboard`; its JSON APIs
  are under `/dashboard/api`. The same-origin design must not require CORS.
- Dashboard authentication uses two new required Railway secrets:
  `DASHBOARD_PASSWORD` and `DASHBOARD_SESSION_SECRET`.
- `DASHBOARD_PASSWORD` must be independent of the Phase 1/2 `API_KEY`. The
  ingest key must never be accepted by the dashboard, embedded in frontend
  assets, returned by an endpoint, or logged. Startup fails if
  `DASHBOARD_PASSWORD` equals the resolved `API_KEY`. The configured dashboard
  password and submitted passwords are compared exactly; login input is not
  silently trimmed or normalized.
- `DASHBOARD_SESSION_SECRET` must contain at least 32 UTF-8 bytes. The README
  documents generating it with a cryptographically secure random generator;
  a merely long, human-chosen phrase is not presented as equivalent entropy.
- Password comparison is constant-time, including for non-ASCII input (for
  example, compare fixed-length SHA-256 digests of the UTF-8 values with
  `hmac.compare_digest`). Authentication failures use a generic response that
  does not reveal configuration or secret values.
- A successful login creates a signed, stateless session valid for 12 hours.
  The session is carried only in a cookie named `dashboard_session` with
  `HttpOnly`, `SameSite=Strict`, `Path=/dashboard`, and `Max-Age=43200`. The
  cookie payload contains issued-at and expiry timestamps exactly 12 hours
  apart and is authenticated with HMAC-SHA-256 using
  `DASHBOARD_SESSION_SECRET`.
- The cookie also carries `Secure` whenever the process is running on
  Railway, detected by the presence of `RAILWAY_ENVIRONMENT_ID` or
  `RAILWAY_DEPLOYMENT_ID`. It does not depend only on whether a Volume happens
  to be mounted. `Secure` is omitted only for local HTTP development, so a real
  login flow is testable locally without local TLS tooling. See §8 for the
  corresponding local-dev note.
- Failed logins are limited per source address to five attempts in a rolling
  15-minute window. Further attempts return `429` with `Retry-After`. A
  successful login clears that source's failures. An in-memory limiter is
  sufficient for the one-replica MVP; process restarts may clear it. The
  source address for Railway traffic is the rightmost, whitespace-trimmed IP
  address in `X-Forwarded-For`: Railway appends that value, while values to its
  left may have been supplied by the client and must not be trusted. The value
  is validated and canonicalized with Python's `ipaddress` module. The direct
  connection address is the fallback when the header is absent or its
  rightmost value is invalid, as in local development. Expired limiter entries
  are pruned so addresses are not retained beyond the rolling window.
- Dashboard HTML and API responses send `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and a
  Content Security Policy that, at minimum, limits default, scripts,
  connections, images, fonts, forms, and framing to the compiled same-origin
  application; disallows objects and base-URI changes; and never permits inline
  script execution. Any narrowly required inline-style allowance for the
  Radix dialog is documented and does not relax `script-src`.
- Every dashboard API except `POST /dashboard/api/session` and the idempotent
  `DELETE /dashboard/api/session` requires a valid, unexpired session. Invalid,
  expired, or tampered sessions return `401` and clear the cookie.
- The login shell and its hashed static assets are necessarily retrievable
  before authentication so the browser can present the password form. They
  contain no credentials or protected data. Authentication protects the read
  APIs and what the application renders, not the secrecy of compiled frontend
  source code.
- Database reads use parameterized queries. API responses never contain
  `stored_path`, database paths, Railway credentials, environment variables, or
  filesystem error details.
- CSV paths come only from a database row selected by integer upload ID. The
  stored path is `uploads_root/device_id/card_uuid/filename` (Phase 3's
  layout). The implementation must additionally confirm that the resolved
  path is inside the configured uploads root before opening it.
- All timestamps returned by the API are RFC 3339 UTC timestamps. The browser
  may render them in the viewer's local timezone while retaining the exact UTC
  value in accessible text or a tooltip.

## 5. Required Implementation Order

Phase 4 implementation is deliberately sequenced. The old viewer is removed
before the replacement is added so the repository has one supported UI and
does not accumulate two independent CSV-reading implementations.

### Milestone 1 — Remove the current HTML viewer

1. Delete `tools/railway_viewer.py`.
2. Delete `tests/test_railway_viewer.py`.
3. Remove the local viewer instructions and layout entry from `README.md`.
4. Confirm there are no remaining code or documentation references to the
   viewer.
5. Run the canonical pytest suite and confirm that all remaining Phase 1
   through Phase 3 behavior still passes before starting dashboard
   implementation.

This milestone removes only the viewer and its dedicated tests/documentation.
It must not remove or alter stored data, the ingest endpoints, the Pi services,
or their tests.

### Milestone 2 — Add authenticated read APIs

Add the session, status, upload-list, and CSV-preview contracts in §6 to the
FastAPI service. Reads use the existing SQLite database and Railway Volume
directly; they do not invoke the Railway CLI.

### Milestone 3 — Add and deploy the React dashboard

Add a React, TypeScript, and Vite application under top-level `frontend/`, as a
sibling of `server/` and `pi/`. These remain three separate code areas in one
repository; Phase 4 does not create more repositories or another deployed
service. A multi-stage server build installs from a committed lockfile, runs
frontend tests and the production build, then copies only the compiled assets
into the Python runtime image. FastAPI serves those assets and an SPA fallback
only under `/dashboard`; dashboard API routes, ingest routes, and unrelated
unknown routes must never fall through to the SPA.

## 6. HTTP Contract

All response bodies below are JSON unless the successful response is explicitly
`204 No Content`.

### 6.1 Session

#### `POST /dashboard/api/session`

Request:

```json
{
  "password": "administrator-supplied password"
}
```

- Success: `204`, with the secure `dashboard_session` cookie.
- Missing or invalid body: `400` or `422`.
- Incorrect password: `401` with `{"detail":"Invalid credentials"}`.
- Rate limited: `429` with a `Retry-After` header.
- The fifth failed password comparison within the rolling window returns the
  normal `401`; the sixth and later attempts return `429`, even if a later
  request supplies the correct password, until the oldest retained failure
  leaves the window. `Retry-After` is the ceiling in whole seconds until that
  happens.
- The response must never include the supplied password or a session token in
  JSON.

#### `GET /dashboard/api/session`

- Valid session: `200`.

  ```json
  {
    "authenticated": true,
    "expires_at": "2026-08-01T03:30:00Z"
  }
  ```

- Missing, invalid, or expired session: `401`.

#### `DELETE /dashboard/api/session`

- Always returns `204` and expires the session cookie. Repeating logout is
  harmless and does not require a valid session. The clearing cookie uses
  `Max-Age=0`, `Path=/dashboard`, and the same `SameSite` and environment-based
  `Secure` attributes as the login cookie.

### 6.2 Pi status

#### `GET /dashboard/api/status`

The status is derived from the newest row in `pings`, using the server-authored
`received_at`; the Pi-authored `sent_at` is not used for connection status.

```json
{
  "status": "online",
  "device_id": "raspberrypi-uploader",
  "last_seen_at": "2026-07-31T14:00:00Z",
  "online_window_seconds": 600,
  "server_time": "2026-07-31T14:04:00Z"
}
```

Status rules:

- `online`: a ping exists and `server_time - last_seen_at` is less than or
  equal to 600 seconds.
- `offline`: a ping exists and its age is greater than 600 seconds.
- `never_seen`: no ping exists; `device_id` and `last_seen_at` are `null`.
- A future `last_seen_at` caused by clock or data inconsistency is treated as
  online, but the exact timestamp is still returned and a warning is logged.
- A database read failure returns `503`; it must not be presented as offline.
- A newest row whose `received_at` cannot be parsed as an RFC 3339 UTC timestamp
  is treated as unavailable and returns `503` with a safe detail while logging
  the internal inconsistency; it is not presented as offline.

The 10-minute window allows one missed heartbeat at the current recommended
five-minute cadence. It is an inference from recent check-ins, not proof of a
continuous network connection.

### 6.3 Upload list

#### `GET /dashboard/api/uploads`

Query parameters:

- `limit`: optional integer from 1 through 100; default `50`.
- `before_id`: optional canonical positive integer. When present, return rows
  whose IDs are lower than this value.
- Unknown, repeated, malformed, or out-of-range parameters return `422`.

Rows are ordered by `id DESC`. The implementation queries one extra row to
determine whether another page exists.

```json
{
  "items": [
    {
      "id": 42,
      "device_id": "raspberrypi-uploader",
      "card_uuid": "A1B2-C3D4",
      "filename": "logger-0042.csv",
      "size": 1234,
      "received_at": "2026-07-31T14:02:00Z"
    }
  ],
  "next_before_id": 42
}
```

Each item includes `card_uuid` (Phase 3's opaque, validated token identifying
the physical card the file came from) so an admin can distinguish uploads from
different cards. `next_before_id` is the last returned ID when more rows
exist, otherwise it is `null`. An empty database returns an empty `items`
array and `null` cursor. `stored_path` is never selected into or returned from
this response. A database read failure, or a selected row whose `received_at`
cannot be returned as RFC 3339 UTC, returns `503` with a safe detail.

### 6.4 CSV preview

#### `GET /dashboard/api/uploads/{upload_id}/preview`

- `upload_id` is a canonical positive integer.
- An unknown upload returns `404`.
- A row whose stored file is missing or outside the configured uploads root
  returns `409`.
- Files are decoded as strict UTF-8 and parsed with Python's CSV parser in
  strict mode. Invalid UTF-8 or malformed CSV returns `422` with a safe,
  user-facing reason.
- An empty CSV succeeds with an empty `records` array.
- A preview contains at most the first 100 records.
- A single parsed field may contain at most 262,144 characters, and accumulated
  preview cell content may contain at most 1,048,576 UTF-8 bytes. A field of
  exactly 262,144 characters is allowed; exceeding it returns `422`.
- `truncated` is `true` only when at least one complete record is omitted by the
  100-record or aggregate-content cap. A file containing exactly 100 records
  and no more returns all 100 with `truncated: false`; a 101-record file returns
  the first 100 with `truncated: true`.
- Aggregate size is the sum of `len(cell.encode("utf-8"))` for cells in returned
  records. A record that would make the total exceed 1,048,576 bytes is omitted
  whole and sets `truncated: true`; partial records are never returned. A record
  that brings the total to exactly the limit is included, and `truncated`
  depends on whether another record exists.
- A filesystem read failure returns `503` without exposing the stored path.

```json
{
  "upload_id": 42,
  "filename": "logger-0042.csv",
  "card_uuid": "A1B2-C3D4",
  "records": [
    ["sensor", "value"],
    ["temperature", "21.4"]
  ],
  "truncated": false
}
```

`card_uuid` identifies the physical card the previewed file came from, matching
the upload-list contract in §6.3. The API returns raw record arrays and does
not assert that the first record is a header.

## 7. Dashboard Experience

### 7.1 Login

- Unauthenticated visits to `/dashboard` show only a password form.
- The frontend checks the session on initial load. Any later `401` from a
  protected dashboard API clears protected UI state and returns to the login
  screen rather than leaving stale status, upload, or preview data visible.
- Successful login opens the dashboard without putting credentials in the URL
  or browser storage.
- Invalid credentials and rate limiting are clearly but generically reported.
- Logout clears the session and returns to the login screen.

### 7.2 Connection status

- The first dashboard element is a prominent status card.
- `online` is visually green, `offline` is red, and `never_seen` is neutral.
  Color is never the only status indicator.
- The card shows the device ID when known and the exact last-seen time.
- Status refreshes every 60 seconds and whenever the user manually refreshes.
- A status API failure shows an explicit unavailable/error state, not offline.
- The copy says “Online — last heartbeat …” or “Offline — last heartbeat …” so
  the inference is not represented as a persistent socket connection.

### 7.3 Uploads and previews

- The uploads table shows filename, card UUID, device ID, formatted byte size,
  received time, and a Preview action, newest first. The card UUID must be
  visible so same-named files from different physical cards are distinguishable.
- “Load more” follows `next_before_id`, appends without duplicating rows, and is
  hidden when the cursor is `null`.
- Empty, loading, and API failure states are distinct.
- Selecting Preview opens an on-page panel or dialog containing the filename,
  row numbers, and a horizontally scrollable grid of raw CSV cells.
- The preview reports empty files and truncated output clearly.
- Closing one preview and opening another must not display stale records from
  the previous request.
- The interface must be keyboard-operable, use semantic controls and table
  markup, expose focus states, and remain usable on phone and desktop widths.

## 8. Deployment and Configuration

- The existing Railway service remains the only deployed service. Vercel is not
  used in Phase 4.
- The repository retains three top-level code areas: `frontend/`, `server/`,
  and `pi/`. The frontend is built into the server image; it is not a separate
  repository or Railway service. The Pi remains independently deployable and is
  not copied into the server runtime image.
- Railway's service Root Directory changes from `/server` to the repository
  root so the Docker build context contains both `frontend/` and `server/`.
  Railway's Config File Path is explicitly `/server/railway.json`, and that file
  selects the `DOCKERFILE` builder. The repository-root `Dockerfile` sets its
  Python runtime working directory/start command so `server/main.py` remains the
  FastAPI entry point.
- The server build becomes a deterministic multi-stage Docker build with a
  Node frontend build stage and a Python runtime stage. Node and Python base
  images use explicit patch-level tags and immutable image digests, npm uses
  `npm ci` with a committed lockfile, and Python dependencies remain exactly
  pinned. Development dependencies and source files from the Node stage are not
  copied into the runtime image.
- A repository-root `.dockerignore` excludes Git metadata, virtual
  environments, local `.env` files, databases, uploaded data, caches, and other
  local state from the build context. The Dockerfile uses explicit `COPY`
  targets rather than copying the whole repository into the runtime stage.
- The production frontend uses relative `/dashboard/api/...` URLs.
- FastAPI serves hashed frontend assets with long-lived immutable caching, but
  serves the SPA HTML and all authenticated API responses with `no-store`.
- `/health` remains unauthenticated and continues to test process liveness.
- `/ping` and `/upload` retain their existing bearer-token authentication and
  request/response contracts.
- Application startup fails clearly when `API_KEY`, `DASHBOARD_PASSWORD`, or
  `DASHBOARD_SESSION_SECRET` is missing or empty, when the session secret is
  shorter than §4 requires, or when `DASHBOARD_PASSWORD` equals `API_KEY`.
- Local development documents separate commands for the Vite development
  server and FastAPI, using a Vite development proxy for `/dashboard/api`.
  Because local FastAPI runs over plain HTTP, the session cookie omits
  `Secure` in that environment only (see §4) so login is actually testable
  without local TLS tooling.
- Local backend placeholders live in `server/.env.example`; a real
  `server/.env` is gitignored and is loaded only by the FastAPI development
  command. Dashboard and session secrets are never Vite variables, never use a
  `VITE_` prefix, and never appear in `frontend/.env` or compiled assets.
- The README documents secret creation, local development, Railway deployment,
  login, the 10-minute status rule, and the dashboard's read-only scope.

## 9. Success Criteria / Verification

1. **Old viewer removed first:** `tools/railway_viewer.py`,
   `tests/test_railway_viewer.py`, and their README references are gone before
   dashboard code is introduced. The canonical pytest suite passes after the
   removal.
2. **Authentication boundary:** an unauthenticated client may retrieve only the
   login shell and its public compiled assets; it cannot read status, upload
   metadata, or CSV data, and the application does not render protected
   dashboard state. Correct credentials create a cookie with every required
   security attribute. Incorrect credentials, spoofed leftmost forwarded IPs,
   throttling, logout, expiration, and a tampered signature behave as specified.
   Startup rejects equal ingest and dashboard passwords, and the ingest
   `API_KEY` cannot log in.
3. **Connection signal:** with no pings, the UI shows never seen. At exactly 600
   seconds after the latest server-received ping it shows online; after 600
   seconds it shows offline. The exact last-seen time is visible, and a database
   failure shows unavailable rather than offline.
4. **Upload browsing:** multiple stored uploads appear newest first. Cursor
   pagination returns every row once across pages, validates its boundaries,
   and never exposes `stored_path` or other server internals.
5. **CSV preview:** quoted, multiline, Unicode, empty, exactly-100-record,
   101-record, exact aggregate-limit, over-aggregate-limit, exact field-limit,
   over-field-limit, malformed, non-UTF-8, missing-file, outside-root, and
   unknown-ID cases produce the specified result without modifying the file or
   database. No response contains a partial CSV record.
6. **Frontend behavior:** login, logout, all status states, refresh, pagination,
   preview switching, empty states, errors, keyboard use, and narrow-screen
   layout are covered by frontend tests and a browser smoke test.
7. **Ingest regression:** the canonical pytest suite continues to pass,
   including `/health`, `/ping`, `/upload`, Pi queueing, retry, and dedup tests.
   No Phase 1 through Phase 3 HTTP response shape changes.
8. **Railway verification:** after deployment with the existing Volume, a
   browser can sign in, see a new Pi heartbeat within the next 60-second
   dashboard refresh, see a newly uploaded CSV, preview its first records, and
   sign out. Restarting or redeploying the service preserves pings, upload rows,
   and blobs; a session may remain valid because it is stateless and signed by
   the persistent Railway secret.

## 10. Required Automated Checks

From the repository root:

```bash
.venv/bin/python -m pytest tests -q
```

The frontend package also provides non-interactive commands for:

- TypeScript type checking.
- Unit/component tests.
- A production Vite build.

Backend tests cover authentication, rate limiting, session signing and expiry,
read API contracts, time boundaries, pagination, safe path validation, CSV
limits, and failure responses. Frontend tests cover the user-visible states in
§7. The browser smoke test is a documented manual check against the production
build at desktop and phone widths; it verifies real focus behavior, horizontal
overflow, and the login-to-preview flow without requiring a second frontend
end-to-end test framework for this MVP. Hardware-specific Pi behavior and the
live Railway Volume cannot be fully verified locally; the manual Railway check
in success criterion 8 is therefore required before Phase 4 is declared
complete.

## 11. Implementation Decisions Addendum (2026-07-31)

The decisions below were made in a pre-implementation review and are reflected
in §4–§10. They remain summarized here so a fresh session can pick up mid-build
without re-deriving them from conversation history. If a future edit creates a
conflict, the detailed contract and success criteria in §4–§10 control.

1. **Build pipeline.** `server/railway.json`'s `builder` changes from
   `NIXPACKS` to `DOCKERFILE`. A new `Dockerfile` at the repo root implements
   the multi-stage build described in §8 (Node frontend build stage, Python
   runtime stage). Railway's Root Directory changes from `/server` to the
   repository root, and its Config File Path is explicitly
   `/server/railway.json`, so the build context contains both top-level source
   directories while the existing deployment configuration remains in
   `server/`. This is a deliberate, accepted change to the working Nixpacks
   pipeline used since Phase 1.
2. **Frontend directory location.** The frontend lives at top-level
   `./frontend/`, a sibling of `pi/` and `server/`. These are three code areas
   in the same repository, not three repositories. The Dockerfile's build
   context and `COPY` instructions reference `frontend/` and `server/` at the
   repo root. This is a layout choice only; the same-origin, single-service
   deployment architecture in §4/§8 is unchanged.
3. **Styling.** Plain CSS Modules for layout and visuals. Radix UI primitives
   are used only for the upload preview dialog (§7.3), because correct focus
   trapping, focus restore, Escape handling, and ARIA roles for a modal are
   easy to get subtly wrong by hand and §9 explicitly tests preview-switching
   and keyboard behavior. No Tailwind or other utility CSS framework, and no
   broader component library.
4. **Frontend tooling.** Package manager is `npm`. Node version is pinned to
   the same exact Node 22 patch release via `frontend/.nvmrc` and the
   Dockerfile's build stage, so local dev and the container build cannot
   silently drift apart. Frontend tests use Vitest + React Testing Library.
5. **Local secrets.** `server/.env.example` is committed with placeholder
   `API_KEY`, `DASHBOARD_PASSWORD`, and `DASHBOARD_SESSION_SECRET` values and
   setup notes; the real `server/.env` is gitignored. The operator chooses their
   own local `DASHBOARD_PASSWORD`; `DASHBOARD_SESSION_SECRET` is a generated
   high-entropy random value since it is never typed by a human. No dashboard
   secret is a Vite environment variable. Railway's production secrets remain
   operator-managed via the Railway dashboard/CLI and are out of scope for any
   local tooling or committed file.
6. **Delivery sequencing.** Each §5 milestone (remove old viewer; add backend
   read APIs; add frontend + Docker + deploy) is implemented in its own
   session rather than continuously in one long conversation, to keep context
   focused per milestone. This document — not prior conversation history — is
   the source of truth a new session should read first. Work happens on a
   feature branch; pushing, opening a PR, and deploying to Railway (setting
   production secrets, triggering a deploy) each require separate explicit
   go-ahead and are not assumed by this addendum.

## 12. Post-Phase 4

> Full-file downloads were taken up in `prd/phase-5-file-downloads.md`. The
> Non-Goal in §3 above records what Phase 4 decided, and is left as it was
> written; Phase 5 is the current contract for downloads.

- Named administrator accounts, external identity providers, audit logs, and
  password rotation workflows.
- Independent frontend deployment on Vercel or another static host.
- Full-file downloads, retention controls, deletion, and restore workflows.
- Charts, schema-aware parsing, aggregation, export, and sensor-specific
  analysis.
- Multiple devices, per-device filters, device-specific health cards, and
  fleet alerting.
- Push-based status updates, notifications, and alert delivery.
