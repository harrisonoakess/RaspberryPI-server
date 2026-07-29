# Pi → Railway SD Card Ingestion (Phase 2)

A Raspberry Pi copies new CSV files off a removable SD card and uploads them to
a FastAPI server on Railway whenever it has WiFi. Files never get lost during
connectivity gaps, and re-inserting the same card never duplicates a stored
file. Built on the Phase 1 connection, which is still here as the `/ping`
heartbeat. See `prd/phase-1-connection.md` and `prd/phase-2-data-sync.md`.

The server (`/server`) and the Pi services (`/pi`) deploy independently and are
coupled only by the HTTP contract below.

## How it works

```text
[SD card] --insert--> [sdcard-watcher] --copies new-only--> [queue/pending/ + SQLite ledger]
                                                                     |
                                                              [uploader]  every 30s, when online
                                                                     |  HTTPS POST /upload
                                                                     v
                                                       [Railway: FastAPI] --(device_id, filename)-->
                                                                     |    stores only if new
                                                                     v
                                                        [Railway Volume + SQLite]
```

The card is **never cleared**, so every insertion presents the full history of
files the logger has ever written. Two things keep that cheap and safe:

- **On the Pi**, a SQLite ledger keyed by filename. A name already recorded
  `pending` or `uploaded` is not copied again.
- **On the server**, a `UNIQUE (device_id, filename)` constraint. Delivery is
  at-least-once, so a retry after a lost response is expected; the constraint
  makes those attempts one stored blob and one row.

Dedup is by **filename, not content hash**. That is sound only because the
logger always writes a new, distinct name and never appends to or reuses one.
If that ever changes, revisit §8 of the Phase 2 PRD.

A queued file is deleted from the Pi only after the server returns `200` with an
acknowledgement that exactly matches the `device_id`, `filename`, and byte size
that were sent. Anything else — timeout, redirect, non-`200`, malformed or
mismatched JSON — leaves the file pending for the next poll.

## Layout

```text
server/
  main.py                     FastAPI app: POST /ping, POST /upload, GET /health
  railway.json                Railway deploy config
pi/
  sdcard_watcher.py           watches for the card, copies new CSVs into the queue
  uploader.py                 heartbeat + queue drain (evolves Phase 1's daemon)
  state.py                    shared SQLite ledger
  setup.sh                    idempotent installer
  config.env.example          every setting and its default
  systemd/
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
| `POST /upload` | `Authorization: Bearer <API_KEY>` | `200 {"status":"stored"\|"already_stored","device_id":…,"filename":…,"size":…,"received_at":…}` | `401` bad/missing token, `413` over 10 MiB, `400`/`422` missing field or unsafe filename/device_id, `500`/`503` cannot persist |

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

`multipart/form-data` with exactly three required fields:

| Field | Kind | Rules |
| --- | --- | --- |
| `device_id` | text | same validation as `/ping` |
| `filename` | text | **authoritative**; 1–255 characters and ≤255 UTF-8 bytes, a single basename, no `/` `\` NUL or control characters, not `.` or `..`, ends in `.csv` (case-insensitive) |
| `file` | file part | 0 – 10,485,760 bytes |

The file part's own `filename=` attribute is informational; the Pi sends a fixed
`upload.csv` there so no real name is ever interpolated into a header.

- **New identity** → `200` with `"status":"stored"`, after both the blob and the
  row are committed.
- **Existing identity** → `200` with `"status":"already_stored"` and the
  **existing** row's metadata. The stored blob is never overwritten; under the
  logger assumptions, later bytes for the same identity are not authoritative.
- Blobs land at `<volume>/uploads/<device_id>/<filename>`. The server validates
  every value before deriving a path and never joins an unchecked client value
  to the volume path.
- Redirects and every status other than `200` are upload failures on the Pi. The
  client validates TLS, does not follow redirects, and never logs the API key.

Retrieval, listing, and preview endpoints are deliberately out of scope. Receipt
is verified by direct Volume and SQLite inspection.

## Tests

Canonical command, from the repo root:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

342 tests cover the `/ping` and `/upload` contracts, auth and validation
boundaries, the 0-byte and 10 MiB limits, duplicate handling, restart
durability, card scanning scope, atomic queueing, reinsertion dedup, the
acknowledgement rules that gate deleting a local copy, WiFi detection, the poll
and heartbeat cadences, and the log rate limiter.

Hardware and platform behaviour (a real card and udev mount, real WiFi
association, systemd, Railway volumes) is **not** covered by the suite and is
verified manually below.

## Local server

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
API_KEY=local-dev-key RAILWAY_VOLUME_MOUNT_PATH=./data \
  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

The server refuses to start without `API_KEY`. `DATABASE_PATH` and
`UPLOADS_PATH` override the volume-derived defaults
(`<volume>/pings.db`, `<volume>/uploads`).

### Verify the upload contract (PRD criterion 1)

```bash
U=http://127.0.0.1:8000
K='Authorization: Bearer local-dev-key'

: > empty.csv
head -c 10485760 /dev/zero | tr '\0' 'x' > big.csv
head -c 10485761 /dev/zero | tr '\0' 'x' > over.csv
printf 'sensor,value\n1,2\n' > logger-0001.csv

# 200 stored — 0 bytes, then 10 MiB
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F filename=empty.csv -F file=@empty.csv
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F filename=big.csv -F file=@big.csv

# 200 stored, then 200 already_stored for the same identity
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F filename=logger-0001.csv -F file=@logger-0001.csv
curl -s -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F filename=logger-0001.csv -F file=@logger-0001.csv

# 401, 422, 422, 413 — none of these create a blob or a row
code() { curl -s -o /dev/null -w '%{http_code}\n' "$@"; }
code -X POST $U/upload -F device_id=pi -F filename=x.csv -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" -F device_id=pi -F filename=../escape.csv -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" -F device_id=pi -F filename=notes.txt -F file=@logger-0001.csv
code -X POST $U/upload -H "$K" \
  -F device_id=raspberrypi-uploader -F filename=over.csv -F file=@over.csv
```

Inspect the Volume and the database directly:

```bash
find server/data/uploads -type f -exec wc -c {} \;
sqlite3 server/data/pings.db \
  'SELECT id, device_id, filename, stored_path, size, received_at FROM uploads;'
```

Expect one blob and one row per `(device_id, filename)`, sizes matching the
source files, and nothing at all from the rejected requests.

## Railway deployment

One service, one replica — Railway volumes do not support multiple replicas.

1. **Service** → *Settings* → set **Root Directory** to `/server`.
   `railway.json` supplies the start command, `numReplicas: 1`, and a `/health`
   healthcheck.
2. **Volume** → attach one, mount path `/data`. Railway injects
   `RAILWAY_VOLUME_MOUNT_PATH`, and the server puts `pings.db` and `uploads/`
   inside it. Size the volume for the CSVs you expect to keep: nothing is
   deleted automatically.
3. **Variables** → `API_KEY=<long random string>`. Generate with
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
   Use the same value in the Pi's `config.env`.
4. **Networking** → generate a public domain. That HTTPS URL is `SERVER_URL`.

The database and uploads directory are created at application startup, not at
build or pre-deploy time, so they land on the mounted volume.

### Railway receipt check (PRD criterion 2)

```bash
railway link
DOMAIN=https://<your-service>.up.railway.app

printf 'sensor,value\n1,2\n' > logger-0001.csv
curl -s -X POST "$DOMAIN/upload" -H "Authorization: Bearer $API_KEY" \
  -F device_id=raspberrypi-uploader -F filename=logger-0001.csv -F file=@logger-0001.csv

railway ssh 'ls -l $RAILWAY_VOLUME_MOUNT_PATH/uploads/raspberrypi-uploader/'
railway ssh 'sqlite3 $RAILWAY_VOLUME_MOUNT_PATH/pings.db \
  "SELECT device_id, filename, size, received_at FROM uploads;"'
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
| Card filesystem type | _pending — the card's format is not yet known_ |
| Card filesystem UUID | _pending_ |
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

2. **Insert the logger's SD card**, copy `pi/` to the Pi, and run the installer:

   ```bash
   sudo ./setup.sh
   ```

   It is idempotent — safe to re-run, and it never overwrites an existing
   `config.env`, queue, or ledger. It:

   - verifies the model, architecture, Python 3, systemd, and hostname;
   - detects the WiFi interface;
   - **detects the card** with `lsblk`, records its UUID and filesystem type in
     `config.env`, and **fails clearly** if the filesystem is not one of
     `vfat exfat ext4 ext3 ext2` or the kernel cannot mount it;
   - installs a udev rule that mounts **only that UUID**, read-only with
     `nodev,nosuid,noexec`, at `CARD_MOUNTPOINT` via `systemd-mount`;
   - creates the `piuploader` system user, `/etc/piuploader` (`0750`),
     `/var/log/piuploader` (`0750`), `/var/lib/piuploader` (`0750`, queue and
     ledger), `/opt/piuploader`, and the mountpoint;
   - installs both services, **removes Phase 1's `connectivity-daemon.service`**,
     and enables `uploader` and `sdcard-watcher`;
   - writes the environment report.

   Useful flags:

   | Flag | Use |
   | --- | --- |
   | `--card-device /dev/sda1` | name the card instead of auto-detecting (required if more than one removable partition is attached) |
   | `--redetect-card` | replace a `CARD_UUID` already in `config.env` |
   | `--skip-card-detection` | re-run with no card inserted, leaving the card config alone |
   | `--skip-hardware-check` | install on non-Pi hardware for development |

3. Fill in the real values and restart:

   ```bash
   sudo nano /etc/piuploader/config.env   # SERVER_URL and API_KEY
   sudo systemctl restart uploader sdcard-watcher
   ```

   `config.env` is `root:piuploader 0640` — readable by the services, not
   world-readable, never committed. See `pi/config.env.example` for every
   setting and its default.

4. Watch it:

   ```bash
   journalctl -u uploader -u sdcard-watcher -f
   sudo tail -f /var/log/piuploader/uploader.log
   sudo tail -f /var/log/piuploader/sdcard-watcher.log
   ```

Both logs go to stdout (captured by the journal) and to a rotating file capped
at 1 MB with 5 backups, so they cannot grow without bound. The API key is never
logged.

### Inspecting Pi state

```bash
sudo ls -l /var/lib/piuploader/queue/pending/          # waiting to upload
sudo sqlite3 /var/lib/piuploader/state.db \
  'SELECT filename, status, discovered_at FROM files ORDER BY discovered_at;'
findmnt /mnt/sdcard                                     # must show ro,nodev,nosuid,noexec
```

### Behaviour

**`sdcard-watcher`**

- Acts only on the configured `CARD_UUID`, and only through
  `CARD_MOUNTPOINT`. It confirms the mountpoint really holds that device before
  scanning, so another removable drive mounted there is never read.
- Scans **root-level regular `.csv` files only**. Directories, symlinks,
  non-CSV files, and nested paths are ignored; symlinks are never followed off
  the card.
- Skips any filename already `pending` or `uploaded` in the ledger.
- Copies to `queue/tmp/`, fsyncs, then **atomically renames** into
  `queue/pending/`. An interrupted or failed copy is never published, and the
  card is opened read-only and never modified or deleted from.
- Rejects an unsafe name or a file over 10 MiB, logs why, and keeps going with
  the later files.
- Scans once per insertion, not once per tick, so a card left in place is not
  re-read.

**`uploader`**

- One sequential 30-second poll loop, so requests can never overlap — a slow
  upload delays the next tick rather than running alongside it.
- While disconnected, **no HTTP request is attempted at all**, neither heartbeat
  nor upload. The daemon keeps running.
- While connected: sends `/ping` if its 5-minute interval is due, then attempts
  every queued file. Uploads do not wait for the heartbeat.
- Marks a file `uploaded` and deletes the queued copy **only** on a `200` whose
  acknowledgement matches the `device_id`, `filename`, and size sent. Both
  `stored` and `already_stored` count as success.
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
- **Boot**: both start before WiFi or the card is available and survive their
  absence. `Restart=on-failure` with a start-rate limit keeps a misconfiguration
  from hot-looping.

### Manual hardware verification (PRD criteria 3–6)

Record the results of each.

3. **Offline queueing** — with WiFi off, insert the card holding sample
   root-level CSVs.
   - `findmnt /mnt/sdcard` shows the card mounted `ro,nodev,nosuid,noexec`.
   - Only new in-scope files appear complete in
     `/var/lib/piuploader/queue/pending/`, and `queue/tmp/` is empty.
   - The ledger shows them `pending`.
   - The server logs no requests, and the card's contents and timestamps are
     unchanged.

4. **Online transfer** — connect WiFi.
   - The uploader attempts the pending files within one 30-second poll.
   - Valid matching acknowledgements make them `uploaded` and clear
     `queue/pending/`.
   - Volume and SQLite inspection shows one blob and one row each, with matching
     bytes and sizes.
   - Then force each failure and confirm the file stays pending and is retried:
     point `SERVER_URL` at an unreachable host (timeout), at a host that
     redirects (redirect refused), and set a wrong `API_KEY` (`401`).

5. **Uploaded-card reinsertion** — re-insert the same card. The watcher logs
   `copied=0`, no upload request is made for those filenames, and the server
   still has one blob and one row per identity.

6. **Pending-card reinsertion** — queue files while offline, then re-insert the
   card before they upload. No duplicate queued copy is created (the `pending`
   status is honoured). When connectivity returns, at-least-once requests still
   leave one server blob and row.

Also worth confirming once: a card with a **different** UUID is not mounted at
`CARD_MOUNTPOINT` and is not scanned.

## Security notes

- HTTPS with certificate verification on every network, including open and
  captive-portal WiFi. Verification is never disabled, and redirects are never
  followed, so the API key only ever reaches `SERVER_URL`.
- One shared bearer token: Railway variable on the server,
  `/etc/piuploader/config.env` (`root:piuploader 0640`) on the Pi. Compared with
  a constant-time check. Per-device auth and key rotation are post-MVP.
- Both services run as the non-root `piuploader` user with `NoNewPrivileges`,
  `ProtectSystem=strict`, and write access limited to `/var/log/piuploader` and
  `/var/lib/piuploader`. Neither has mount privileges: root provisions the
  read-only mount through the udev rule, and the watcher only reads it.
- The card is mounted `ro,nodev,nosuid,noexec` and matched by UUID, so no other
  removable drive is ever mounted or scanned, and nothing on the card can be
  executed.
- Filename validation is a whitelist of shapes, enforced independently on the Pi
  and on the server. Nothing is stripped or rewritten — a name either passes
  as-is or is rejected — and the server never joins an unchecked client value to
  the Volume path.
- A captive portal is not a security control. If a location uses one, someone
  must complete the login by hand; until then WiFi reads as connected while
  requests fail, which the daemons log as rate-limited failures and recover from
  on a later poll.
- The sensor data is not regulated, so there is no encryption at rest. Reassess
  if its sensitivity changes.
- No backups, retention, or disaster recovery. **Volume loss loses the uploaded
  CSVs**, though the never-cleared card remains the source of truth — clearing
  the Pi's ledger would let a reinsertion re-upload everything.
