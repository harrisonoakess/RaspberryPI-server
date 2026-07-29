# PRD: Phase 2 — SD Card Ingestion & Data Sync (MVP)

## 1. Problem / Background

Phase 1 (`phase-1-connection.md`) passed and proved Pi↔Railway connectivity. This phase adds the smallest useful data path: data arrives on the Pi via a removable SD card, written by one known sensor/logger, and that card is physically inserted into the Pi periodically. The logger writes a new, distinct root-level CSV filename each time it writes — it does not append to or modify existing files, and it never reuses a filename. The SD card is **never cleared**, so every insertion contains the full history of files ever written to it, including files already synced previously. Whenever the Pi has internet access, new CSV files need to reach the Railway server without being lost during connectivity gaps.

Data volume is small and infrequent, and each file is at most 10 MiB. The priority for this MVP is simply to **get the files**: every in-scope CSV that lands on the SD card reaches the Railway Volume, and re-insertion does not duplicate the stored file or database row. Content-level integrity verification and exhaustive recovery engineering are explicitly deferred; see §8.

## 2. Goals

- Every in-scope CSV file written to the SD card eventually reaches the Railway server, despite ordinary WiFi outages.
- The Pi uses at-least-once delivery: a request may be sent again when an acknowledgement is lost. The server stores one blob and one database row per logical identity, `(device_id, filename)`.
- Re-inserting the same SD card (with old + new files) does not re-copy or intentionally re-upload a file already known locally as `pending` or `uploaded`.
- Builds directly on Phase 1's proven connection, auth, and deployment pattern.

## 3. Non-Goals

- Content-hash verification of file contents (deferred — see §8; relies on the logger's current behavior of always writing new, distinct filenames).
- Listing, downloading, previewing, parsing, or analyzing uploaded files through an API or UI. Receipt is verified by direct Railway Volume and SQLite inspection.
- Nested directories, non-CSV files, multiple cards, and multiple logger namespaces.
- LTE / cellular connectivity (later phase).
- Automatic WiFi network discovery/roaming.
- High-throughput or large-file transfer optimization.
- Multi-device fleet management, horizontal server scaling.

## 4. Constraints

- There is one configured logger/card namespace. The logger always creates a new, distinct root-level `.csv` filename, never appends to or modifies an existing file, and never reuses a filename during the logger's lifetime. This is what makes filename-based tracking sufficient for MVP dedup.
- Only regular files in the card root whose names end in `.csv` (case-insensitive) are in scope. Directories, symlinks, and filesystem metadata are ignored.
- Files may be empty and must be no larger than **10 MiB (10,485,760 bytes)**.
- The card's filesystem format is currently unknown. Pi setup detects and records its filesystem type and UUID, configures that UUID as the only accepted card, and provisions a read-only mount with `nodev,nosuid,noexec`. Unsupported formats fail setup clearly rather than falling back to an arbitrary removable drive.
- The sensor data is not regulated. Encryption at rest is not required for this MVP; HTTPS remains required in transit.
- **Server and Pi remain fully independent deployables**, coupled only by the HTTP API contract established in Phase 1 and extended here.

## 5. Proposed Architecture

```text
[SD card] --insert--> [Pi: sdcard-watcher] --copies new-only files--> [Pi: queue/pending/ + SQLite ledger]
                                                                              |
                                                          [Pi: uploader daemon (evolves Phase 1's connectivity daemon)]
                                                                              |  HTTPS POST /upload (when online)
                                                                              v
                                                              [Railway: FastAPI server] --checks (device_id, filename)--> [stores if new]
                                                                              |
                                                                              v
                                                                  [Railway Volume + SQLite]
```

**Core design principle:** dedup is by `(device_id, filename)`, not content hash. Delivery over HTTP is at least once, so a lost response may cause a retry. The server's uniqueness constraint makes those attempts one stored blob and one database row. A queued file is only marked uploaded and deleted on the Pi after the server returns the exact valid acknowledgement in §5.3.

### 5.1 Pi side (Python, two systemd services)

**`sdcard-watcher`** — watches for the configured card via `pyudev`. Pi setup, rather than the unprivileged watcher, provisions the root-owned mount behavior. On insertion:

1. Confirms the inserted filesystem UUID equals configured `CARD_UUID` and accesses it only through the known read-only mountpoint.
2. Scans only root-level regular `.csv` files.
3. Rejects an unsafe name or file larger than 10 MiB, logs the reason, and continues scanning later files.
4. Skips any filename already recorded in the local SQLite ledger with status `pending` or `uploaded`.
5. Copies each remaining file to a temporary queue name, closes the completed copy, then atomically renames it into `queue/pending/` and records `{filename, status: pending, discovered_at}` in the ledger. An interrupted or failed copy is never published as pending, and the original card file is never changed or deleted.

**`uploader`** — evolves Phase 1's `connectivity_daemon.py`. It keeps the existing non-overlapping 30-second poll cadence. When WiFi is connected, it attempts pending uploads promptly and retries failures on a later tick. It also keeps the Phase 1 `/ping` heartbeat, but sends it on a separate recommended five-minute interval rather than every poll. Upload work does not wait for the next heartbeat.

For each pending file, the uploader calls `POST /upload` using §5.3. It marks the ledger row `uploaded` and removes the queued copy only after a `200` response whose JSON has an allowed status and exactly matches the requested `device_id`, `filename`, and byte size. A timeout, redirect, malformed or mismatched acknowledgement, or any non-`200` response leaves the file `pending`. One failed file does not prevent later pending files from being attempted.

Both services share one SQLite ledger at `/var/lib/piuploader/state.db`, a single table:

```sql
CREATE TABLE files (
  filename TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
  discovered_at TEXT NOT NULL
);
```

### 5.2 Server side (Python, FastAPI, deployed on Railway)

- `POST /upload` follows the exact contract in §5.3 and authenticates with the Phase 1 bearer token.
- The server validates the request before deriving a storage path. A valid filename is a single basename from 1 through 255 UTF-8 characters, has no `/`, `\`, NUL, control character, `.` or `..` path component, and ends in `.csv` case-insensitively. The server never joins an unchecked client value to the Volume path.
- The SQLite `uploads` table has a database-enforced `UNIQUE(device_id, filename)` constraint and records at least `{device_id, filename, stored_path, received_at, size}`. The persistent blob and this SQLite database both live on the Railway Volume.
- For a new identity, the server writes a temporary file, verifies the 10 MiB limit while reading, moves the completed file to its final safe path, inserts the row, and returns `200` only after both operations succeed. Handled failures remove temporary output and return non-`200`.
- For an existing identity, the server does not overwrite the blob or add a row; it returns `already_stored` with the existing row's metadata. Under the logger assumptions, later bytes for the same identity are never authoritative.
- `GET /health` and the `POST /ping` HTTP contract carry over from Phase 1. Only the Pi heartbeat interval changes to the recommended five minutes.

Railway Volume + SQLite remains the simplest storage choice for MVP; swappable later for S3/R2 + Postgres if storage needs or server instance count grow.

### 5.3 `POST /upload` HTTP contract

Request:

```http
POST /upload
Authorization: Bearer <API_KEY>
Accept: application/json
Content-Type: multipart/form-data; boundary=...
```

The multipart form has exactly these required fields:

- `device_id`: text using Phase 1's hostname-style validation.
- `filename`: text containing the exact source basename. This field is authoritative; the client-provided filename attribute on the `file` part is informational only.
- `file`: the CSV bytes, from 0 through 10,485,760 bytes.

Successful new-file response:

```json
{
  "status": "stored",
  "device_id": "raspberrypi-uploader",
  "filename": "logger-0001.csv",
  "size": 1234,
  "received_at": "2026-07-29T18:30:01Z"
}
```

Successful duplicate response has the same shape and the metadata of the existing stored object, with `"status": "already_stored"`. Both success cases return `200`. Expected errors are:

- `401` for a missing or invalid bearer token.
- `413` when `file` is larger than 10 MiB.
- `400` or `422` for a missing field, invalid `device_id`, or unsafe/out-of-scope `filename`.
- `500` or `503` when the blob or row cannot be persisted.

Redirects and every status other than `200` are upload failures on the Pi. As in Phase 1, the client validates TLS, does not follow redirects, never logs the API key, and uses the configured request timeout.

## 6. Configuration and Repo Layout

Pi runtime configuration extends Phase 1 with:

- `CARD_UUID`: required filesystem UUID detected during setup.
- `CARD_FILESYSTEM`: filesystem type detected and recorded during setup.
- `CARD_MOUNTPOINT`: known read-only mountpoint.
- `QUEUE_PATH` and `STATE_DB_PATH`.
- `POLL_INTERVAL_SECONDS` (default `30`) for upload attempts.
- `PING_INTERVAL_SECONDS` (recommended default `300`) for `/ping`.
- `MAX_UPLOAD_BYTES` fixed/defaulted to `10485760` on both Pi and server.

`setup.sh` detects the inserted intended card with `lsblk --fs` and/or `blkid`, records its UUID and filesystem type, and provisions the least-privileged root-owned read-only mount behavior. The `piuploader` services remain non-root and receive write access only to their queue, ledger, and logs. The setup/upgrade is idempotent and replaces the Phase 1 connectivity service with the watcher and uploader without deleting an existing config, queue, or ledger.

```text
/server/
  main.py              # extended with POST /upload
/pi/
  sdcard_watcher.py           # new
  uploader.py                 # evolves connectivity_daemon.py
  state.py                    # new: shared SQLite ledger helper
  setup.sh
  systemd/
    sdcard-watcher.service    # new
    uploader.service          # replaces connectivity-daemon.service
/prd/
  phase-2-data-sync.md
```

## 7. Success Criteria / Verification

1. **Local contract:** authenticated curl uploads of a 0-byte CSV and a 10 MiB CSV receive matching `stored` acknowledgements. Uploading the same `(device_id, filename)` again receives `already_stored`; direct Volume and SQLite inspection shows one blob and one row. Missing/incorrect auth returns `401`, an unsafe filename returns `400`/`422`, and a file one byte over the limit returns `413`, with no blob or row created.
2. **Railway receipt:** with a persistent Volume attached, the same valid curl upload succeeds against the public URL. Direct Volume and SQLite inspection confirms the blob bytes, filename, device ID, byte size, and row. Retrieval/listing endpoints are not required.
3. **Offline queueing:** on the Pi, setup records the intended card's UUID and filesystem. Inserting that card with sample root-level CSV files while offline copies only new in-scope files completely into `queue/pending/`; it does not attempt HTTP and does not change the card.
4. **Online transfer:** connecting to WiFi causes the uploader to attempt pending files within one 30-second poll interval. Valid matching acknowledgements make the files `uploaded` and clear their queued copies. A timeout, malformed/mismatched acknowledgement, redirect, or representative non-`200` response leaves the file pending for retry. Direct Volume and SQLite inspection verifies receipt.
5. **Uploaded-card reinsertion:** re-inserting the same card after upload copies zero files and causes zero upload requests for those filenames; the server still has one blob and one row per identity.
6. **Pending-card reinsertion:** re-inserting the card before a prior upload completes creates no duplicate queued copy because the ledger's `pending` status is honored. When connectivity succeeds, at-least-once requests still result in one server blob and row.

## 8. Post-MVP / Look at Later

None of the following is required to ship Phase 2's file-transfer MVP:

- Exhaustive crash/power-loss reconciliation across every queue, filesystem, and SQLite transition, including `fsync` guarantees and fault-injection tests.
- Card-present-at-boot and hot-unplug behavior beyond basic safe failure (do not publish a partial copy; retain the original on the card; log the error).
- Multiple cards, multiple loggers/device namespaces, nested directories, and non-CSV files.
- Content hashes, end-to-end checksum verification, and detection of content changes or filename reuse. Revisit this if the logger's immutable, never-reused filename assumption changes.
- Encryption at rest. The current data is not regulated; reassess if its sensitivity changes.
- Automated retention/deletion, Volume capacity alerts, backups, restore tests, and disaster recovery.
- Public list/download endpoints, a UI, CSV parsing, and analytics. For MVP, direct Volume and database inspection proves receipt.
- Advanced metrics, tracing, dashboards, and alerting beyond useful systemd/server logs.
- Per-device authentication, key rotation workflows, and fleet credential management.
- A quarantine/retry-limit/operator workflow for a permanently rejected or otherwise poisonous file.
- Concurrent/horizontal server scaling and migration to object storage plus a managed database.
- Broader filesystem support, automatic card discovery, and selecting cards without a provisioned UUID.
- LTE/cellular connectivity and automatic WiFi discovery/roaming.
