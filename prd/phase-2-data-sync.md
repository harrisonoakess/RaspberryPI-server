# PRD: Phase 2 — SD Card Ingestion & Data Sync

## 1. Problem / Background

With Pi↔Railway connectivity proven in Phase 1 (`phase-1-connection.md`), this phase adds the real data path: data arrives on the Pi via a removable SD card, written by a separate sensor/logger device, that gets physically inserted into the Pi periodically. The SD card is **never cleared**, so every insertion contains the full history of data ever written to it, including data already synced previously. Whenever the Pi has internet access, any data collected since the last successful sync needs to reach the Railway server — without losing data during connectivity gaps, and without re-sending data already delivered.

Data volume is small and infrequent. The priority is **reliability and simplicity**, not throughput.

## 2. Goals

- Data written to the SD card eventually reaches the Railway server, exactly once recorded, regardless of how many times the Pi goes offline in between.
- Re-inserting the same SD card (with old + new data) never re-copies or re-uploads already-synced data.
- Builds directly on Phase 1's proven connection, auth, and deployment pattern.

## 3. Non-Goals

- LTE / cellular connectivity (later phase).
- Automatic WiFi network discovery/roaming.
- High-throughput or large-file transfer optimization.
- Multi-device fleet management, horizontal server scaling.

## 4. Constraints

- **SD card is append-only from the Pi's perspective** — the Pi must identify *new* data by content hash, not by "what's currently on the card."
- **Server and Pi remain fully independent deployables**, coupled only by the HTTP API contract established in Phase 1 and extended here.

## 5. Proposed Architecture

```
[SD card] --insert--> [Pi: sdcard-watcher] --copies new files--> [Pi: local queue + SQLite state]
                                                                        |
                                                    [Pi: uploader daemon (evolves Phase 1's connectivity daemon)]
                                                                        |  HTTPS POST /upload (when online)
                                                                        v
                                                        [Railway: FastAPI server] --stores--> [Railway Volume + SQLite]
```

**Core design principle:** a file is only ever deleted/marked-done on the Pi after the server explicitly acknowledges it (2xx response) — at-least-once delivery. Every file is identified by a SHA-256 content hash, so duplicate uploads are deduplicated server-side instead of stored/recorded twice.

### 5.1 Pi side (Python, two systemd services)

**`sdcard-watcher`** — watches for SD card insertion via `pyudev`. On insert:
1. Mounts the card read-only to a known mountpoint.
2. Walks its files, computes a SHA-256 hash for each.
3. Copies any file whose hash isn't already recorded in the local SQLite state DB into `queue/pending/`.
4. Records `{hash, original_filename, source_path, status: pending}` in SQLite.
5. Unmounts the card.

**`uploader`** — evolves Phase 1's `connectivity_daemon.py`: same poll loop, but now when online it walks `queue/pending/` and `POST`s each file as multipart form data to `/upload` with its hash and metadata. On 2xx, marks it `uploaded` and clears it from `pending/`. On failure, leaves it `pending` and retries next tick.

Both services share one SQLite DB (`/var/lib/piuploader/state.db`).

### 5.2 Server side (Python, FastAPI, deployed on Railway)

- `POST /upload` — auth'd via the same shared bearer-token API key from Phase 1. Accepts file + metadata, checks the hash against what's already stored (dedupe), writes the file to a Railway **persistent Volume**, records `{hash, device_id, filename, timestamp, size}` in a SQLite DB on the same volume. Returns 200 with an ack.
- `GET /health` and `POST /ping` carry over from Phase 1 unchanged.

Railway Volume + SQLite is the simplest storage choice for MVP; swappable later for S3/R2 + Postgres if storage needs or server instance count grow.

## 6. Repo Layout (additions on top of Phase 1)

```
/server/
  main.py              # extended with POST /upload
/pi/
  sdcard_watcher.py           # new
  uploader.py                 # evolves connectivity_daemon.py
  state.py                    # new: shared SQLite helper
  setup.sh
  systemd/
    sdcard-watcher.service    # new
    uploader.service          # replaces connectivity-daemon.service
/prd/
  phase-2-data-sync.md
```

## 7. Success Criteria / Verification

1. Local FastAPI server: `/upload` works end-to-end via curl, including hash-based dedupe (same file uploaded twice stores/records it once).
2. Deployed to Railway with a Volume attached; same curl test passes against the public URL.
3. On the Pi: inserting a USB SD card reader with sample files while **offline** queues files in `queue/pending/` without uploading.
4. Connecting to WiFi causes the uploader to pick up pending files within one poll interval; they appear on the server, and local files are cleared from `pending/`.
5. Re-inserting the same SD card queues and uploads zero duplicate files.

## 8. Open Questions / Future Work

- Migrate storage to S3/R2 + Postgres if data volume or reliability needs grow.
- Add LTE modem support once WiFi-based MVP is validated.
- Consider encrypting data at rest on the Pi/server if the sensor data is sensitive.
