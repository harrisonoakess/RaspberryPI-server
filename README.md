# Pi → Railway SD Card Ingestion (Phase 4)

A Raspberry Pi copies new CSV files off removable SD cards and uploads them to a
FastAPI server on Railway whenever it has WiFi. Cards are swapped one at a time,
and each one is detected, mounted read-only, and ingested with no operator
command. Files never get lost during connectivity gaps, and re-inserting the
same card never duplicates a stored file. Built on the Phase 1 connection, which
is still here as the `/ping` heartbeat. Phase 4 adds a private, read-only
dashboard at `/dashboard`, served by the same FastAPI process, for checking that
the Pi is still reporting in and browsing what has been stored. See
`prd/phase-1-connection.md`, `prd/phase-2-data-sync.md`,
`prd/phase-3-multi-card-ingestion.md`, and `prd/phase-4-frontend-dashboard.md`.

The Pi services (`/pi`) deploy independently and are coupled to the server only
by the HTTP contract below. The server (`/server`) and the dashboard frontend
(`/frontend`) are built together into one image and run as one Railway service
on one origin.

## How it works

```text
[any qualifying SD card]
        |  insert
        v
[sdcard-mounter]  root; polls block devices, mounts the one eligible card read-only
        |
        v
[sdcard-watcher]  derives card_uuid from what is mounted, copies new-only
        |
        v
[queue/pending/<card_uuid>/ + SQLite ledger keyed by (card_uuid, filename)]
        |
        v
[uploader]  every 30s, when online — HTTPS POST /upload
        |
        v
[Railway: FastAPI] --(device_id, card_uuid, filename)--> stores only if new
        |
        v
[Railway Volume: uploads/<device_id>/<card_uuid>/<filename> + SQLite]
```

A card is **never cleared**, so every insertion presents the full history of
files that logger has ever written. Two things keep that cheap and safe:

- **On the Pi**, a SQLite ledger keyed by `(card_uuid, filename)`. A name
  already recorded `pending` or `uploaded` *for that card* is not copied again.
- **On the server**, a `UNIQUE (device_id, card_uuid, filename)` constraint.
  Delivery is at-least-once, so a retry after a lost response is expected; the
  constraint makes those attempts one stored blob and one row.

Dedup is by **name, not content hash**. That is sound only because the logger
always writes a new, distinct name and never appends to or reuses one. The name
is scoped to the card because two different cards may each carry a
`logger-0001.csv` — those are two distinct files, and both are delivered.

**Card identity** is the filesystem UUID exposed by `/dev/disk/by-uuid`. It is
read live from whatever is mounted, never pinned in configuration. It is a
logical filesystem identity, not a hardware one: reformatting a card makes it a
new logical card, and two cards that share a UUID (clones) are unsupported.

A queued file is deleted from the Pi only after the server returns `200` with an
acknowledgement that exactly matches the `device_id`, `card_uuid`, `filename`,
and byte size that were sent. Anything else — timeout, redirect, non-`200`,
malformed or mismatched JSON — leaves the file pending for the next poll.

## Layout

```text
Dockerfile                    multi-stage build: Node builds the dashboard, Python runs it
.dockerignore                 keeps local state and node_modules out of the build context
server/
  main.py                     FastAPI app: POST /ping, POST /upload, GET /health
  dashboard.py                private read-only dashboard: session auth + read APIs
  railway.json                Railway deploy config
  .env.example                local backend secrets template (real .env is gitignored)
frontend/
  src/                        React + TypeScript dashboard served under /dashboard
  package.json                npm scripts: dev, build, typecheck, test
  .nvmrc                      the exact Node version the Docker build also uses
pi/
  sdcard_mounter.py           root: polls for a qualifying card, mounts it read-only
  sdcard_watcher.py           derives the card_uuid, copies new CSVs into the queue
  uploader.py                 heartbeat + queue drain (evolves Phase 1's daemon)
  state.py                    shared SQLite ledger
  setup.sh                    idempotent installer
  config.env.example          every setting and its default
  systemd/
    sdcard-mounter.service
    sdcard-watcher.service
    uploader.service
tests/                        test suite derived from the PRD success criteria
prd/                          phase PRDs
```

## HTTP contract

| Endpoint | Auth | Success | Failures |
| --- | --- | --- | --- |
| `GET /health` | none | `200 {"status":"ok"}` | — |
| `POST /ping` | `Authorization: Bearer <API_KEY>` | `200 {"status":"acknowledged","device_id":…,"received_at":…}` | `401` bad/missing token, `422` invalid body, `503` cannot persist |
| `POST /upload` | `Authorization: Bearer <API_KEY>` | `200 {"status":"stored"\|"already_stored","device_id":…,"card_uuid":…,"filename":…,"size":…,"received_at":…}` | `401` bad/missing token, `413` over 20 MiB, `400`/`422` missing field or unsafe filename/device_id/card_uuid, `500`/`503` cannot persist |
| `POST /dashboard/api/session` | password body | `204` + `dashboard_session` cookie | `401` wrong password, `422` bad body, `429` throttled |
| `GET /dashboard/api/session` | session cookie | `200 {"authenticated":true,"expires_at":…}` | `401` missing/expired/tampered |
| `DELETE /dashboard/api/session` | none | `204`, always | — |
| `GET /dashboard/api/status` | session cookie | `200 {"status":"online"\|"offline"\|"never_seen",…}` | `401`, `503` cannot read |
| `GET /dashboard/api/uploads` | session cookie | `200 {"items":[…],"next_before_id":…}` | `401`, `422` bad `limit`/`before_id`, `503` cannot read |
| `GET /dashboard/api/uploads/{id}/preview` | session cookie | `200 {"upload_id":…,"filename":…,"card_uuid":…,"records":[[…]],"truncated":…}` | `401`, `404` unknown id, `409` file missing/out of root, `422` bad id or unreadable CSV, `503` cannot read |

The dashboard endpoints never accept the ingest `API_KEY`, and the ingest
endpoints never accept a dashboard session. See [Dashboard](#dashboard).

### `POST /ping`

Body: `{"device_id": "raspberrypi-uploader", "sent_at": "2026-07-28T18:30:00Z"}`

- `device_id`: 1–63 characters, letters/numbers/internal hyphens, no leading or
  trailing hyphen. Derived from the Pi's hostname at runtime.
- `sent_at`: RFC 3339 **UTC** timestamp (`Z` or `+00:00`). Diagnostic only —
  the server's `received_at` is authoritative.
- Duplicates are accepted and stored as separate rows. The heartbeat has no
  idempotency; `/upload` does.
- The Pi sends this every **5 minutes** (`PING_INTERVAL_SECONDS`), not on every
  30-second poll. Upload work never waits for it.

### `POST /upload`

`multipart/form-data` with exactly four required fields:

| Field | Kind | Rules |
| --- | --- | --- |
| `device_id` | text | same validation as `/ping` |
| `card_uuid` | text | 1–64 characters of letters, digits, and hyphens (`^[A-Za-z0-9-]{1,64}$`, full-string, **case-sensitive**) |
| `filename` | text | **authoritative**; 1–255 characters and ≤255 UTF-8 bytes, a single basename, no `/` `\` NUL or control characters, not `.` or `..`, ends in `.csv` (case-insensitive) |
| `file` | file part | 0 – 20,971,520 bytes |

`card_uuid` is validated as an **opaque safe token**, not a strict UUID shape:
`vfat`/`exfat` cards expose short `blkid` ids such as `A1B2-C3D4`, while
`ext2`/`ext3`/`ext4` expose full RFC 4122 UUIDs. The Pi sends the exact token
from `/dev/disk/by-uuid`, so comparison is case-sensitive.

The file part's own `filename=` attribute is informational; the Pi sends a fixed
`upload.csv` there so no real name is ever interpolated into a header.

- **New identity** → `200` with `"status":"stored"`, after both the blob and the
  row are committed.
- **Existing identity** → `200` with `"status":"already_stored"` and the
  **existing** row's metadata. The stored blob is never overwritten; under the
  logger assumptions, later bytes for the same identity are not authoritative.
- Identity is `(device_id, card_uuid, filename)`. Changing **either** the device
  or the card stores a distinct row and blob.
- Blobs land at `<volume>/uploads/<device_id>/<card_uuid>/<filename>`. The server
  validates every value before deriving a path and never joins an unchecked
  client value to the volume path.
- Redirects and every status other than `200` are upload failures on the Pi. The
  client validates TLS, does not follow redirects, and never logs the API key.

Example success:

```json
{
  "status": "stored",
  "device_id": "raspberrypi-uploader",
  "card_uuid": "A1B2-C3D4",
  "filename": "logger-0001.csv",
  "size": 1234,
  "received_at": "2026-07-30T18:30:01Z"
}
```

Ingest itself has no retrieval endpoints. Reading stored data is the
[dashboard](#dashboard)'s job, behind its own separate credential.

## Tests

Canonical command, from the repo root:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

The tests cover the `/ping` and `/upload` contracts, auth and validation
boundaries (including the `card_uuid` token rules), the 0-byte and 20 MiB
limits, the dedup matrix across devices and cards, restart durability, the
Phase 2 reset guards on both sides, mounter candidate filtering and
reconciliation, card discovery and mount-change safety in the watcher, per-card
queueing and reinsertion dedup, the nested queue the uploader drains, the
acknowledgement rules that gate deleting a local copy, WiFi detection, the poll
and heartbeat cadences, and the log rate limiter. They also cover the dashboard:
startup configuration guards, login throttling, session signing and expiry, the
read-API contracts, the status time boundary, cursor pagination, safe path
validation, the CSV preview limits, and every failure response.

The dashboard frontend has its own non-interactive commands, from `frontend/`:

```bash
npm ci          # honours package-lock.json, same as the Docker build
npm run typecheck
npm test        # Vitest + React Testing Library
npm run build   # production Vite build
```

Hardware and platform behaviour (real cards, real mounts, real WiFi association,
systemd, Railway volumes) is **not** covered by either suite and is verified
manually below, as is the [browser smoke test](#browser-smoke-test).

## Local server

Copy the secrets template once, then edit `server/.env`. It is gitignored and
never reaches the image or the frontend bundle:

```bash
cp server/.env.example server/.env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # session secret
```

Run the backend from the repo root, loading that file into the environment:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
set -a && source server/.env && set +a
.venv/bin/python -m uvicorn main:app --app-dir server --host 127.0.0.1 --port 8000 --reload
```

The server refuses to start unless `API_KEY`, `DASHBOARD_PASSWORD`, and
`DASHBOARD_SESSION_SECRET` are all set, the session secret is at least 32 UTF-8
bytes, and the dashboard password differs from `API_KEY`. `DATABASE_PATH` and
`UPLOADS_PATH` override the volume-derived defaults
(`<volume>/pings.db`, `<volume>/uploads`).

For the dashboard UI, run Vite in a second terminal and open
<http://127.0.0.1:5173/dashboard/>:

```bash
cd frontend && npm ci && npm run dev
```

Vite proxies `/dashboard/api` to FastAPI on port 8000, so the login flow uses
the real cookie. Because local FastAPI is plain HTTP, the session cookie omits
`Secure` locally; on Railway it always carries it. To exercise the compiled
build against FastAPI instead, `npm run build` and start the backend with
`DASHBOARD_DIST_PATH=$PWD/frontend/dist`, then open
<http://127.0.0.1:8000/dashboard>.

### Verify the upload contract

```bash
U=http://127.0.0.1:8000
K='Authorization: Bearer local-dev-key'
A=1234-ABCD      # stands in for card A's filesystem UUID
B=5678-EF01      # card B

: > empty.csv
head -c 20971520 /dev/zero | tr '\0' 'x' > big.csv
head -c 20971521 /dev/zero | tr '\0' 'x' > over.csv
printf 'sensor,value\n1,2\n' > logger-0001.csv

# 200 stored — 0 bytes, then 20 MiB
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F card_uuid=$A -F filename=empty.csv -F file=@empty.csv
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F card_uuid=$A -F filename=big.csv -F file=@big.csv

# 200 stored, then 200 already_stored for the same identity
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F card_uuid=$A -F filename=logger-0001.csv -F file=@logger-0001.csv
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F card_uuid=$A -F filename=logger-0001.csv -F file=@logger-0001.csv

# 200 stored — same filename, different card, so a distinct row and blob
printf 'sensor,value\n9,9\n' > card-b-logger-0001.csv
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F card_uuid=$B -F filename=logger-0001.csv \
  -F file=@card-b-logger-0001.csv

# 401, 422, 422, 422, 422, 413 — none of these create a blob or a row
code() { curl -s -o /dev/null -w '%{http_code}\n' "$@"; }
code -X POST $U/upload -F device_id=pi -F card_uuid=$A -F filename=x.csv -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" -F device_id=pi -F card_uuid=$A -F filename=../escape.csv -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" -F device_id=pi -F card_uuid=$A -F filename=notes.txt -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" -F device_id=pi -F filename=x.csv -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" -F device_id=pi -F card_uuid=../escape -F filename=x.csv -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F card_uuid=$A -F filename=over.csv -F file=@over.csv
```

Inspect the Volume and the database directly:

```bash
find server/data/uploads -type f -exec wc -c {} \;
sqlite3 server/data/pings.db \
  'SELECT id, device_id, card_uuid, filename, stored_path, size, received_at FROM uploads;'
```

Expect one blob and one row per `(device_id, card_uuid, filename)`, the two
`logger-0001.csv` files stored side by side under different card directories,
sizes matching the source files, and nothing at all from the rejected requests.

## Railway deployment

One service, one replica — Railway volumes do not support multiple replicas.

1. **Service** → *Settings* → set **Root Directory** to `/` (the repository
   root) and **Config File Path** to `/server/railway.json`. Phase 4 moved the
   root directory off `/server` so the Docker build context contains both
   `frontend/` and `server/`; the deploy configuration stayed in `server/`.
   `railway.json` selects the `DOCKERFILE` builder and supplies the start
   command, `numReplicas: 1`, and a `/health` healthcheck.
2. **Volume** → attach one, mount path `/data`. Railway injects
   `RAILWAY_VOLUME_MOUNT_PATH`, and the server puts `pings.db` and `uploads/`
   inside it. Size the volume for the CSVs you expect to keep: nothing is
   deleted automatically.
3. **Variables** → three secrets, all required:

   | Variable | Purpose | Generate with |
   | --- | --- | --- |
   | `API_KEY` | Pi ingest bearer token; same value in the Pi's `config.env` | `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` |
   | `DASHBOARD_PASSWORD` | typed into the dashboard login form | your choice, but **not** equal to `API_KEY` |
   | `DASHBOARD_SESSION_SECRET` | signs the session cookie; ≥ 32 UTF-8 bytes | `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` |

   Nobody ever types `DASHBOARD_SESSION_SECRET`, so generate it randomly rather
   than choosing a long phrase — a memorable phrase is not equivalent entropy.
   Changing it invalidates every existing session. The deploy fails fast and
   logs `REFUSING TO START` if any of the three is missing, if the secret is too
   short, or if `DASHBOARD_PASSWORD` equals `API_KEY`.
   `PHASE3_RESET_UPLOADS` is set only for the one-time Phase 2 → Phase 3
   rollout below, then removed.
4. **Networking** → generate a public domain. That HTTPS URL is `SERVER_URL`,
   and `SERVER_URL/dashboard` is the dashboard.

The database and uploads directory are created at application startup, not at
build or pre-deploy time, so they land on the mounted volume.

The image is built by the repository-root `Dockerfile`: a Node stage installs
from `frontend/package-lock.json` with `npm ci`, runs the frontend type check,
tests, and production build, then a Python stage installs the pinned server
requirements and copies in only `main.py`, `dashboard.py`, and the compiled
`dist/`. Both base images are pinned to an exact patch tag and an immutable
digest. No `node_modules`, frontend source, test file, or Pi file reaches the
runtime image.

### Railway receipt check

```bash
railway link
DOMAIN=https://<your-service>.up.railway.app

printf 'sensor,value\n1,2\n' > logger-0001.csv
curl -s -X POST "$DOMAIN/upload" -H "Authorization: Bearer $API_KEY" \
  -F device_id=raspberrypi-uploader -F card_uuid=1234-ABCD \
  -F filename=logger-0001.csv -F file=@logger-0001.csv

railway ssh 'ls -lR $RAILWAY_VOLUME_MOUNT_PATH/uploads/raspberrypi-uploader/'
railway ssh 'sqlite3 $RAILWAY_VOLUME_MOUNT_PATH/pings.db \
  "SELECT device_id, card_uuid, filename, size, received_at FROM uploads;"'
```

Confirm the blob bytes, filename, device ID, byte size, and row. If `sqlite3` is
absent from the runtime image, read the rows with Python instead:

```bash
railway ssh "python3 -c \"import os,sqlite3;print(sqlite3.connect(os.environ['RAILWAY_VOLUME_MOUNT_PATH']+'/pings.db').execute('SELECT * FROM uploads').fetchall())\""
```

### Ping durability check (Phase 1 criterion 3)

```bash
ping() { curl -s -X POST "$DOMAIN/ping" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"device_id":"raspberrypi-uploader","sent_at":"2026-07-28T18:30:00Z"}'; echo; }
rows() { railway ssh 'sqlite3 $RAILWAY_VOLUME_MOUNT_PATH/pings.db \
  "SELECT id, device_id, sent_at, received_at FROM pings;"'; }
```

Run in order: `ping` → restart the service → `rows` → `ping` → redeploy →
`rows` → `ping`. Rows must survive both the restart and the redeploy.

A deployment with an attached volume has a brief downtime window during
redeploy. A ping or upload that fails then succeeds on the next poll.

## Dashboard

`SERVER_URL/dashboard` is a private, single-administrator page for answering two
questions: is the Pi still checking in, and what has been stored? It is served
by the same FastAPI process on the same origin as ingest.

**It is read-only.** There is no upload, edit, delete, retention, or full-file
download path — not hidden, not disabled, absent. Its APIs open SQLite with
`query_only` set, so the database refuses writes from the dashboard even if a
future change tried to make one.

### Signing in

Open `SERVER_URL/dashboard` and enter `DASHBOARD_PASSWORD`. That is a different
credential from the Pi's `API_KEY`, and the two are never interchangeable: the
ingest key cannot sign in, a dashboard session cannot ingest, and the server
refuses to start if the two values are equal.

- A successful login sets a signed `dashboard_session` cookie that is valid for
  12 hours. It is `HttpOnly`, `SameSite=Strict`, scoped to `/dashboard`, and
  `Secure` on Railway. The password is never stored in the URL, in browser
  storage, or in any response.
- Five failed attempts from one source address within 15 minutes lock further
  attempts out with `429` until the oldest failure ages out — including attempts
  with the correct password. A successful login clears the counter. The counter
  is in memory, so restarting the service clears it too.
- **Sign out** expires the cookie. Sessions are stateless and signed, so a
  restart or redeploy does *not* invalidate one that is still inside its 12
  hours; rotate `DASHBOARD_SESSION_SECRET` to invalidate every session at once.

### Reading the connection card

The card says **Online**, **Offline**, or **Never seen**, plus the exact
last-heartbeat time. It refreshes every 60 seconds and on **Refresh**.

Online means a heartbeat was *received* within the last 10 minutes. That is an
inference from recent check-ins, not proof of a live connection — the wording on
the card says "last heartbeat" for exactly that reason. Ten minutes allows one
missed heartbeat at the recommended five-minute cadence. The time shown is the
server-authored `received_at`, not the Pi-authored `sent_at`, so a Pi with a
wrong clock cannot make itself look recent. If the status read fails, the card
says **unavailable** rather than offline: a server fault is not the Pi's fault.

### Browsing and previewing

The uploads table lists every stored file newest first, with its card UUID, so
two `logger-0001.csv` files from two different physical cards stay
distinguishable. **Load more** pages through the rest.

**Preview** opens the first records of a file as raw CSV cells. It shows at most
100 records and about 1 MB of cell content, and says so when it truncates. Empty
files, non-UTF-8 bytes, malformed CSV, and missing blobs each get their own
message. Stored paths, environment variables, and filesystem details never reach
the browser.

### Browser smoke test

Automated tests do not exercise a real browser, so run this once against the
production build after deploying, at a desktop width and again at a phone width
(around 390 px):

1. Visit `SERVER_URL/dashboard` signed out. Only the password form is visible.
2. Enter a wrong password: a generic error, no dashboard content.
3. Sign in. The connection card appears first, with a visible last-heartbeat
   time. Confirm the Pi's next heartbeat lands within one 60-second refresh.
4. Upload a CSV from the Pi (or with the receipt-check `curl` above) and confirm
   the new row appears after **Refresh**, with the right card UUID and size.
5. Open **Preview**, check the records against the source file, close with
   `Esc`, and confirm focus returns to the button that opened it.
6. Open a different file's preview and confirm none of the previous file's rows
   are shown.
7. Tab through the page: every control is reachable and has a visible focus
   ring. The page itself never scrolls sideways — wide tables scroll inside
   their own container.
8. Sign out and confirm the dashboard is gone and the password form is back.

## Phase 2 → Phase 3 rollout (one time)

The required `card_uuid` field is a **breaking** change, and the ledger, queue
layout, and `uploads` table all change shape. Phase 2 state is **reset, not
migrated**. Both sides refuse to run on Phase 2 state unless you say so
explicitly, so a half-migrated deployment cannot happen silently.

- **Server guard**: startup inspects `PRAGMA table_info(uploads)`. A legacy
  table without `card_uuid` aborts startup unless `PHASE3_RESET_UPLOADS=1`. With
  the flag, startup deletes only the contents of the uploads root, drops and
  recreates only the `uploads` table, and **preserves `pings`**. Any filesystem
  or database failure aborts startup rather than serving mixed state. Once the
  Phase 3 schema exists the flag is a no-op.
- **Pi guard**: `setup.sh` detects a legacy `files` table or flat files directly
  under `queue/pending/` and stops unless invoked with `--reset-phase2-state`.
  With that flag it stops the three services, deletes `state.db`, `state.db-wal`,
  `state.db-shm`, and the contents of `queue/pending/` and `queue/tmp/`, and
  recreates the empty directories. It never touches `config.env`, the API key,
  or logs.

Maintenance order:

1. Remove the data card, and stop `uploader`, `sdcard-watcher`, and
   `sdcard-mounter` if they are already installed.
2. Set `PHASE3_RESET_UPLOADS=1` on Railway and deploy the Phase 3 server.
3. Confirm `/health` succeeds, the constraint is composite, upload rows and
   blobs are empty, and the ping rows survived:

   ```bash
   curl -s $DOMAIN/health
   railway ssh 'sqlite3 $RAILWAY_VOLUME_MOUNT_PATH/pings.db \
     "SELECT sql FROM sqlite_master WHERE name=\"uploads\";
      SELECT COUNT(*) FROM uploads; SELECT COUNT(*) FROM pings;"'
   railway ssh 'ls -R $RAILWAY_VOLUME_MOUNT_PATH/uploads'
   ```

4. **Remove `PHASE3_RESET_UPLOADS` from Railway.** Later restarts must retain
   new Phase 3 uploads.
5. On the Pi, deploy the Phase 3 `pi/` directory and run, with no card inserted:

   ```bash
   sudo ./setup.sh --reset-phase2-state
   ```

6. Confirm all three units are enabled, the old udev rule and mount helper are
   gone, and no card-specific setting is required:

   ```bash
   systemctl is-enabled sdcard-mounter sdcard-watcher uploader
   ls /etc/udev/rules.d/99-piuploader-sdcard.rules /usr/local/lib/piuploader/mount-card.sh
   ```

7. Start the three services, then run the hardware verification below.

The mock upload data and the Phase 2 local queue and ledger are intentionally
unrecoverable after this reset. The never-cleared physical cards remain the
source of truth.

## Environment facts (fill in from the Pi)

`pi/setup.sh` prints these and writes them to
`/var/log/piuploader/environment-report.txt`. Copy the real values here after
the first run on hardware — they are **not yet recorded**:

| Fact | Value |
| --- | --- |
| Pi model | _pending — expected `Raspberry Pi 3 Model B Plus Rev 1.3`_ |
| OS distribution / release | _pending_ |
| Architecture | _pending — must be `aarch64` (64-bit)_ |
| Python 3 version | _pending_ |
| systemd version | _pending_ |
| Detected WiFi interface | _pending — expected `wlan0`_ |
| Hostname / `device_id` | _pending_ |
| Mountable card filesystems | _pending — of `vfat exfat ext4 ext3 ext2`_ |
| Card A: device, filesystem, UUID | _pending — from `lsblk`, see verification step 2_ |
| Card B: device, filesystem, UUID | _pending — second card not yet available_ |
| pyudev version | _pending — optional, falls back to polling_ |

## Pi installation

Requirements: Raspberry Pi 3 Model B+, Raspberry Pi OS 64-bit, systemd, and a
USB card reader for the logger's SD card (the Pi's own slot holds the boot card).

The Python code needs **only the standard library**. `pyudev` is optional: with
it, an insertion is noticed immediately; without it, the watcher polls every
`CARD_SCAN_INTERVAL_SECONDS`. That costs latency, not correctness.

1. Give the Pi a stable hostname that is a valid `device_id`. Both services exit
   with a configuration error if the hostname cannot produce one:

   ```bash
   sudo hostnamectl set-hostname raspberrypi-uploader
   ```

2. Copy `pi/` to the Pi and run the installer. **No card needs to be inserted**
   — nothing card-specific is pinned at install time:

   ```bash
   sudo ./setup.sh
   ```

   It is idempotent — safe to re-run, and it never overwrites an existing
   `config.env`, queue, or ledger. It:

   - verifies the model, architecture, Python 3, systemd, hostname, and that
     `lsblk`, `findmnt`, and `mount` are present;
   - detects the WiFi interface, and reports which of
     `vfat exfat ext4 ext3 ext2` this kernel can actually mount;
   - **refuses to run on Phase 2 local state** unless `--reset-phase2-state` is
     given (see the rollout section above);
   - **removes the Phase 2 udev rule and mount helper**, which would otherwise
     race `sdcard-mounter` for the same mountpoint;
   - creates the `piuploader` system user, `/etc/piuploader` (`0750`),
     `/var/log/piuploader` (`0750`), `/var/lib/piuploader` (`0750`, queue and
     ledger), `/opt/piuploader`, and the mountpoint;
   - installs all three services, **removes Phase 1's
     `connectivity-daemon.service`**, and enables `sdcard-mounter`,
     `sdcard-watcher`, and `uploader`;
   - writes the environment report.

   Useful flags:

   | Flag | Use |
   | --- | --- |
   | `--reset-phase2-state` | delete the Phase 2 ledger and queue contents, which Phase 3 cannot read |
   | `--skip-hardware-check` | install on non-Pi hardware for development |

3. Fill in the real values and restart:

   ```bash
   sudo nano /etc/piuploader/config.env   # SERVER_URL and API_KEY
   sudo systemctl restart sdcard-mounter sdcard-watcher uploader
   ```

   `config.env` is `root:piuploader 0640` — readable by the services, not
   world-readable, never committed. See `pi/config.env.example` for every
   setting and its default. Phase 2's `CARD_UUID` and `CARD_FILESYSTEM` are
   ignored if still present and can be deleted.

4. Watch it:

   ```bash
   journalctl -u sdcard-mounter -u sdcard-watcher -u uploader -f
   sudo tail -f /var/log/piuploader/sdcard-mounter.log
   sudo tail -f /var/log/piuploader/sdcard-watcher.log
   sudo tail -f /var/log/piuploader/uploader.log
   ```

Both logs go to stdout (captured by the journal) and to a rotating file capped
at 1 MB with 5 backups, so they cannot grow without bound. The API key is never
logged.

### Inspecting Pi state

```bash
sudo ls -lR /var/lib/piuploader/queue/pending/          # waiting to upload, per card
sudo sqlite3 /var/lib/piuploader/state.db \
  'SELECT card_uuid, filename, status, discovered_at FROM files
   ORDER BY card_uuid, discovered_at;'
findmnt /mnt/sdcard                                     # ro,nodev,nosuid,noexec (+noload on ext3/4)
lsblk -o PATH,TYPE,RM,HOTPLUG,FSTYPE,UUID,MOUNTPOINTS   # what the mounter sees
```

### Behaviour

**`sdcard-mounter`** (root, the only privileged service)

- Polls block-device state every `CARD_MOUNT_INTERVAL_SECONDS` (default 2) and
  reconciles it against `CARD_MOUNTPOINT`. Polling rather than udev events means
  no add/remove ordering to get wrong, and a card present at boot needs no
  special case.
- A device qualifies only if it is a partition — or a whole disk carrying a
  filesystem with no filesystem-bearing partition on it — **and** is removable
  or hotplug, **and** is not on the same physical disk as `/`, **and** has one
  of `vfat exfat ext2 ext3 ext4`, **and** has a nonempty UUID matching
  `^[A-Za-z0-9-]{1,64}$`. Everything else is declined with a logged reason.
- Exactly one qualifying device is mounted `ro,nodev,nosuid,noexec`, plus
  `noload` on `ext3`/`ext4` so mounting never replays a journal onto the card.
- Zero qualifying devices clears a stale mount. **More than one changes nothing**
  and logs an ambiguous state — one card at a time is the operating procedure.
- Never writes to a card, never formats one, never changes a UUID. A failed
  mount or unmount is logged and retried on the next tick.

**`sdcard-watcher`**

- Derives the card identity live: it confirms `CARD_MOUNTPOINT` is a real mount
  boundary, then finds which `/dev/disk/by-uuid` entry matches the mounted
  device. No UUID is configured anywhere. If nothing safe resolves, it stays in
  `card_absent`/`card_not_mounted` and writes nothing.
- Scans **regular `.csv` files one level deep only**, in `CARD_SCAN_SUBDIR` if
  set or the card root otherwise. Directories, symlinks, non-CSV files, dotfiles,
  and deeper paths are ignored; symlinks are never followed off the card. Garmin
  avionics cards keep their flight logs under `data_log/`, so they need
  `CARD_SCAN_SUBDIR=data_log`.
- Ignores **dotfiles**, which keeps macOS AppleDouble sidecars (`._log_1.csv`
  beside `log_1.csv`) out of the ledger. A card that has been read on a Mac
  carries them, and they match every other rule.
- Skips any filename already `pending` or `uploaded` **for that card**. Another
  card's identical filename is a different file and is still copied.
- Copies to `queue/tmp/`, fsyncs, re-checks that the same card is still mounted,
  then **atomically renames** into `queue/pending/<card_uuid>/`. An interrupted
  copy, or one that spanned a card swap, is never published and writes no ledger
  row; the file is retried when that card is next mounted.
- Rejects an unsafe name or a file over 20 MiB, logs why, and keeps going with
  the later files.
- Scans on every tick rather than only on insertion. Ledger dedup makes the
  repeat free and removes any insertion-transition state to get wrong.

**`uploader`**

- One sequential 30-second poll loop, so requests can never overlap — a slow
  upload delays the next tick rather than running alongside it.
- While disconnected, **no HTTP request is attempted at all**, neither heartbeat
  nor upload. The daemon keeps running.
- While connected: sends `/ping` if its 5-minute interval is due, then attempts
  every queued file. Uploads do not wait for the heartbeat.
- Walks `queue/pending/<card_uuid>/<filename>` — exactly two levels, regular
  files only, symlinks never followed, in stable `(card_uuid, filename)` order.
  Anything else is logged and left untouched.
- Marks a file `uploaded` and deletes the queued copy **only** on a `200` whose
  acknowledgement matches the `device_id`, `card_uuid`, `filename`, and size
  sent. Both `stored` and `already_stored` count as success.
- One failed file does not block the ones after it; each keeps its `pending`
  status and is retried on a later tick.
- HTTPS with certificate verification, redirects refused, 10-second ping timeout
  and 120-second upload timeout.

**Both**

- **Rate limiting**: the first occurrence of a state or error category is logged
  immediately; an unchanged category repeats at most once every 5 minutes (and
  reports how many entries it suppressed); a change of category, a recovery, and
  every successful acknowledgement are logged immediately. Upload and ping
  failure categories are kept separate so one cannot mask the other.
- **Boot**: all three start before WiFi or a card is available and survive their
  absence. `Restart=on-failure` with a start-rate limit keeps a misconfiguration
  from hot-looping.

**Swapping cards**: remove one card, **wait at least five seconds**, then insert
the next. Rapid A→B replacement and two cards at once are unsupported.

### Manual hardware verification (PRD §7.3)

Record the commands and results for each.

1. **Setup without a card** — `sudo ./setup.sh` completes with no data card
   inserted, and all three services stay running in an absent-card state.

2. **First-card facts** — with card A inserted:

   ```bash
   lsblk -o PATH,TYPE,RM,HOTPLUG,FSTYPE,UUID,MOUNTPOINTS
   ```

   Confirm exactly one qualifying filesystem and a nonempty safe UUID. Record
   whether it is on a partition (`/dev/sda1`) or the whole card (`/dev/sda`) —
   both are supported. Copy the values into the environment facts table.

3. **Card present at boot** — boot the Pi with card A already inserted. Within
   the mounter plus watcher intervals it is mounted and scanned, with no
   operator command.

4. **Read-only mount** — `findmnt /mnt/sdcard` shows `ro,nodev,nosuid,noexec`,
   plus `noload`/`norecovery` if the card is `ext3` or `ext4`.

5. **Offline queueing** — with WiFi off, card A's new root-level CSVs appear
   complete under `/var/lib/piuploader/queue/pending/<card-a-uuid>/`, their
   ledger rows are `pending`, `queue/tmp/` is empty after the scan, no HTTP
   request is made, and the card's contents and timestamps are unchanged.

6. **Online transfer** — reconnect WiFi. Matching acknowledgements mark the rows
   `uploaded`, remove the queued copies, and create one server row and blob per
   `(device_id, card_uuid, filename)`. Then force each failure and confirm the
   file stays pending and is retried: point `SERVER_URL` at an unreachable host
   (timeout), at a host that redirects (redirect refused), and set a wrong
   `API_KEY` (`401`).

7. **Unchanged reinsertion** — remove card A, wait five seconds, reinsert it
   unchanged. Zero copies and zero upload requests for known filenames.

8. **New file on a known card** — add one new CSV to card A and reinsert. Only
   the new filename is queued and uploaded.

9. **Second-card facts** — when card B is available, record the same `lsblk`
   output and confirm its UUID is nonempty and **different** from card A's. A
   duplicate UUID is unsupported and must be corrected before using both cards.

10. **Colliding filename** — the motivating case. While offline, queue
    `logger-0001.csv` from card A; remove it, wait five seconds, and queue a
    *different* `logger-0001.csv` from card B. Both must coexist locally under
    their own card directories. Reconnect WiFi and confirm two distinct server
    rows and blobs, and no false `already_stored`.

11. **Clear rejection** — an unsupported or ambiguous removable device is not
    mounted or ingested, and the reason is visible in `sdcard-mounter` logs.

## Security notes

- HTTPS with certificate verification on every network, including open and
  captive-portal WiFi. Verification is never disabled, and redirects are never
  followed, so the API key only ever reaches `SERVER_URL`.
- One shared bearer token: Railway variable on the server,
  `/etc/piuploader/config.env` (`root:piuploader 0640`) on the Pi. Compared with
  a constant-time check. Per-device auth and key rotation are post-MVP.
- **Two separate credentials.** The dashboard has its own password and its own
  cookie session; the ingest `API_KEY` cannot sign in to it, and startup fails if
  the two values are equal. Neither the ingest key nor any dashboard secret is a
  Vite variable, so neither can be compiled into a browser asset. Passwords are
  compared as fixed-length SHA-256 digests with `hmac.compare_digest`, which
  stays constant-time for non-ASCII input, and failures return one generic
  message that reveals nothing about the configuration.
- Dashboard session cookies are stateless and signed with HMAC-SHA-256 over the
  issued-at and expiry pair. A tampered signature, a payload whose lifetime is
  not exactly 12 hours, or an expired one is rejected and the cookie is cleared.
  There is no server-side session store to leak or grow.
- Login throttling keys on the **rightmost** `X-Forwarded-For` value, which is
  the one Railway appends; values to its left are client-supplied and are not
  trusted, so a spoofed header cannot reset a source's failure count.
- Dashboard responses carry `no-store`, `nosniff`, `Referrer-Policy:
  same-origin`, and a CSP that keeps scripts, connections, images, fonts, forms,
  and framing on the same origin and never permits inline script. Inline
  *styles* are allowed only because the modal dialog sets them at runtime; that
  allowance does not touch `script-src`.
- The compiled login shell and its hashed assets are readable before signing in,
  because the browser needs them to draw the password form. They contain no
  credentials and no stored data. Authentication protects the read APIs and what
  the page renders, not the secrecy of compiled frontend code.
- CSV previews resolve a path only from a row selected by integer upload ID, and
  re-check that the resolved path is inside the uploads root before opening it.
  A row pointing anywhere else is a `409`, not a file read.
- `sdcard-watcher` and `uploader` run as the non-root `piuploader` user with
  `NoNewPrivileges`, `ProtectSystem=strict`, and write access limited to
  `/var/log/piuploader` and `/var/lib/piuploader`. Neither has mount privileges.
- `sdcard-mounter` is the **only** privileged service. It runs as root because
  mounting requires it, and deliberately in the host mount namespace so its
  mount is visible to the watcher — `PrivateTmp`, `ProtectSystem`,
  `ProtectHome`, and `ReadWritePaths` would each create a private namespace and
  hide the card, so they are intentionally absent from that unit.
- Cards are mounted `ro,nodev,nosuid,noexec` (plus `noload` on `ext3`/`ext4`),
  so nothing on a card can be executed and mounting cannot write to it. Only
  removable/hotplug devices that are not on the root disk are eligible, so the
  Pi's own boot media is never a candidate.
- Any card that mounts is trusted with its contents: this is a single-operator
  internal deployment, so there is no card allowlist and no file-count or
  storage quota. Revisit if untrusted media could ever be inserted.
- Filename **and `card_uuid`** validation are whitelists of shapes, enforced
  independently on the Pi and on the server. Nothing is stripped or rewritten —
  a value either passes as-is or is rejected — and the server never joins an
  unchecked client value to the Volume path. Both path segments of a queued file
  are re-validated before the uploader sends it.
- A captive portal is not a security control. If a location uses one, someone
  must complete the login by hand; until then WiFi reads as connected while
  requests fail, which the daemons log as rate-limited failures and recover from
  on a later poll.
- The sensor data is not regulated, so there is no encryption at rest. Reassess
  if its sensitivity changes.
- No backups, retention, or disaster recovery. **Volume loss loses the uploaded
  CSVs**, though the never-cleared cards remain the source of truth — clearing
  the Pi's ledger would let a reinsertion re-upload everything.
