# PRD: Phase 3 — Automatic Multi-Card Ingestion

## 1. Problem / Background

Phase 2 (`phase-2-data-sync.md`) proved the ingestion pipeline end to end, but only against a single SD card whose filesystem UUID is detected once, at setup time, and baked into `config.env` and a udev rule. Testing so far has used a mock card with placeholder data. The Pi is now ready to receive real cards — and, going forward, different physical cards over its lifetime, swapped in one at a time. Today, accepting a different card requires an operator to SSH in and run `sudo ./setup.sh --redetect-card`, which overwrites the previously pinned UUID. There is no automatic path: inserting an unrecognized card just leaves the watcher parked in `card_absent`/`card_not_mounted` indefinitely.

Beyond the workflow gap, Phase 2's dedup logic — both the Pi's local ledger and the server's uniqueness constraint — keys strictly on `filename`, with no notion of which physical card a file came from. Phase 2 explicitly relied on "one configured logger/card namespace" to make that safe (§4). Once a Pi can see multiple distinct cards over time, that assumption no longer holds: if two different cards ever produce a file with the same name (e.g. a logger that restarts its counter each time it's redeployed onto a new card), the second card's file would be silently treated as an already-known duplicate and never delivered.

This phase makes card swapping fully automatic and makes both the local and server dedup logic correctly scoped per card filesystem, while preserving every delivery guarantee Phase 2 already established.

## 2. Goals

- A newly inserted, qualifying SD card is automatically detected, mounted read-only, and ingested — no SSH session or manual command required per swap.
- A qualifying card that is already inserted when the Pi boots is detected and ingested automatically.
- Cards are swapped one at a time; the Pi always has at most one card mounted at its single fixed mountpoint.
- Local and server-side dedup are both scoped by card identity, `(card_uuid, filename)` and `(device_id, card_uuid, filename)` respectively, so two different cards with distinct UUIDs that produce the same filename are treated as distinct files and both are delivered.
- Every Phase 2 guarantee still holds per card: at-least-once delivery, no re-upload on re-insertion of the same card, no duplicate blob/row server-side, safe failure on an interrupted copy or upload.

## 3. Non-Goals

- Content validation or allowlisting of which cards are acceptable. Any card that mounts successfully with a supported filesystem is trusted to contain valid data (see §8 for future work).
- Multiple cards mounted simultaneously (e.g. via a USB hub or multi-card reader). This phase is strictly one card at a time, sequential swaps.
- Multiple qualifying filesystems on one card. A card must expose exactly one qualifying filesystem-bearing block device; an ambiguous card is rejected with a clear log message.
- Rapid swapping. The operator removes one card, waits at least five seconds, and then inserts the next card.
- Distinguishing cloned filesystems that share a UUID. Duplicate UUIDs across cards are unsupported and must be corrected before both cards are used.
- Treating a reformatted card as the same logical card. If formatting changes its UUID, its retained CSV files are ingested again under the new identity.
- A manual UUID override. The trusted, single-reader workflow does not need one in this phase.
- Any change to the data format itself — real cards produce `.csv` files with the same columns as the mock data used in Phase 2 testing. (Corrected 2026-07-31: the first real Garmin card kept its logs under `data_log/`, not at the card root as assumed here, and carried macOS AppleDouble sidecars. The watcher gained `CARD_SCAN_SUBDIR` and a dotfile filter; the columns themselves were unchanged.)
- Everything already declared out of scope in Phase 2 §3 and not explicitly revisited above (content-hash verification, LTE/cellular, WiFi roaming, high-throughput optimization, fleet management, horizontal scaling).
- Migrating existing test-era data. Local ledger, local queue, and server-side upload records from mock-card testing are wiped, not migrated (see §7.1).
- Updating the local Railway viewer or implementing Phase 4 UI behavior. UI work remains in Phase 4.

## 4. Constraints

- This is a trusted internal-test deployment operated by one person. Any qualifying card inserted into the dedicated reader is trusted; card allowlisting, hostile-media defenses beyond safe mounting, file-count quotas, and total-storage quotas are not required.
- There are initially two physical data cards. The second card is not yet available, so its filesystem and UUID are verified during the hardware verification in §7 rather than blocking implementation.
- One card at a time, with exactly one qualifying filesystem, mounted at the same fixed mountpoint used in Phase 2 (`CARD_MOUNTPOINT`, default `/mnt/sdcard`). The filesystem may be on a normal partition such as `/dev/sda1` or directly on the removable whole-card device such as `/dev/sda`. No concurrent multi-card support.
- Card identity is the filesystem UUID exposed by `lsblk`/`blkid` and `/dev/disk/by-uuid`. It is a logical filesystem identity, not a guaranteed hardware identity. Qualifying cards must have nonempty, distinct UUIDs matching `^[A-Za-z0-9-]{1,64}$`.
- `setup.sh` does not require a card physically inserted and does not detect or pin one card during installation.
- Only removable or hotplug block devices with one of the existing supported filesystems (`vfat`, `exfat`, `ext2`, `ext3`, `ext4`) are eligible. The Pi's own root/boot disk is always excluded.
- Mounts use `ro,nodev,nosuid,noexec`. `ext3` and `ext4` additionally use `noload` so mounting does not replay a journal onto the card.
- The operator removes a card, waits at least five seconds, and then inserts the next card. Direct rapid A→B replacement and simultaneous insertion are unsupported.
- This phase changes the shape of the local ledger, local queue directory, and server `uploads` table. All three are reset (not migrated) as part of rollout — see §7 for what a clean rollout looks like.
- Server and Pi remain separate deployables, coupled through the `/upload` contract. The required `card_uuid` field is a breaking Phase 3 change, so the one-time rollout uses a coordinated maintenance window described in §7. Normal deployments are independent again after both sides run Phase 3.

## 5. Proposed Architecture

```text
[any qualifying SD card] --> [root sdcard-mounter: poll + safety filter + read-only mount]
                                                                                       |
                                                            [Pi: sdcard-watcher] --derives current card_uuid from mounted device-->
                                                                                       |
                                                        [Pi: queue/pending/<card_uuid>/ + SQLite ledger keyed by (card_uuid, filename)]
                                                                                       |
                                                                        [Pi: uploader daemon, unchanged cadence]
                                                                                       |  HTTPS POST /upload (device_id, card_uuid, filename, file)
                                                                                       v
                                                          [Railway: FastAPI server] --checks (device_id, card_uuid, filename)--> [stores if new]
                                                                                       |
                                                                                       v
                                                    [Railway Volume: uploads_root/device_id/card_uuid/filename + SQLite]
```

**Core design principle:** dedup extends from `(device_id, filename)` to `(device_id, card_uuid, filename)` everywhere it is checked — locally in the SQLite ledger, on-disk in the queue and Volume storage paths, and in the server's uniqueness constraint. Card identity is derived live from the currently mounted filesystem and is never cached in static configuration.

### 5.1 Card mount mechanism (`pi/sdcard_mounter.py`, `pi/setup.sh`)

- `setup.sh` installs a third systemd service, `sdcard-mounter.service`, alongside the watcher and uploader. It runs as root because mounting requires privileges; the existing watcher and uploader remain unprivileged.
- The mounter is a small, single-process polling daemon. It reconciles block-device state at boot and every `CARD_MOUNT_INTERVAL_SECONDS` (default `2`). This deliberately avoids udev add/remove ordering and concurrency concerns; no udev rule is installed for card mounting.
- Each reconciliation uses `findmnt` and `lsblk` to enumerate filesystem-bearing block devices and applies these filters:
  1. `TYPE` is `part`, or `TYPE` is `disk` with a filesystem directly on it and no filesystem-bearing child partition.
  2. `RM=1` or `HOTPLUG=1`.
  3. Resolving `PKNAME` to the top-level disk shows that the candidate is not on the same physical disk as `/`.
  4. Filesystem type is one of `vfat`, `exfat`, `ext2`, `ext3`, or `ext4`.
  5. Filesystem UUID is nonempty and matches the same safe-token rule used by the server.
- Rejected devices are logged with a safe reason, device path, filesystem type, and UUID when available. Repeated unchanged states use the existing five-minute rate limiting.
- If there are no eligible filesystem-bearing devices, the mounter unmounts a stale mount at `CARD_MOUNTPOINT`, if any, and waits.
- If more than one eligible filesystem-bearing device exists, the mounter changes no mount state and logs that the card/device set is ambiguous.
- If exactly one eligible filesystem-bearing device exists and it is not already mounted at `CARD_MOUNTPOINT`, the mounter unmounts any stale mount and mounts the eligible device there. It uses `ro,nodev,nosuid,noexec`; `ext3` and `ext4` also use `noload`.
- The mounter never writes to the card, never formats it, and never changes its UUID. A mount/unmount failure is logged and retried on the next reconciliation tick.
- `setup.sh` succeeds with no card present, installs/enables all three services, and removes the old UUID-pinned udev rule and generated mount helper if present.

### 5.2 Watcher card discovery (`pi/sdcard_watcher.py`)

- The watcher no longer reads a static `CARD_UUID` or `CARD_FILESYSTEM` from config. Each scan tick it derives the UUID and device number of whatever is currently mounted at `CARD_MOUNTPOINT` by comparing `os.stat(CARD_MOUNTPOINT).st_dev` against entries under `/dev/disk/by-uuid`, generalizing the exact `st_dev`/`st_rdev` comparison Phase 2's `card_is_mounted()` already performs for one UUID.
- A guard confirms `CARD_MOUNTPOINT` is actually a distinct mount boundary (`st_dev` differs from its parent directory) before trusting any UUID match — without this, "nothing mounted" would incorrectly fall through to matching the root filesystem's own UUID.
- `card_absent` / `card_not_mounted` mean that no safe UUID currently resolves at the mountpoint. The existing `pyudev` fast-path may wake the watcher early, but correctness depends only on polling.
- The watcher scans the currently mounted card on every tick rather than only on a present/absent transition. Ledger dedup makes this harmless at the expected small test volume and removes insertion-transition state.
- Before copying each file and again before publishing the completed temporary copy, the watcher re-resolves the mounted `(card_uuid, device_number)`. If either changed, it deletes the temporary copy, writes no ledger row, and retries on a later tick.
- Filename/size validation and copy-then-rename behavior otherwise remain as in Phase 2, with every ledger and queue operation scoped by the discovered `card_uuid`.

### 5.3 Local ledger and queue (`pi/state.py`)

```sql
CREATE TABLE IF NOT EXISTS files (
  card_uuid TEXT NOT NULL,
  filename TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
  discovered_at TEXT NOT NULL,
  PRIMARY KEY (card_uuid, filename)
);
```

`known_filenames`, `status_of`, `record_pending`, `mark_uploaded`, and `filenames_with_status` all gain a `card_uuid` parameter and scope their queries to the composite key. Aggregate `counts()` remains across all cards for logging.

The on-disk queue directory is namespaced the same way: `queue/pending/<card_uuid>/<filename>` instead of Phase 2's flat `queue/pending/<filename>`. Without this, two different cards' same-named files could overwrite each other in the queue even though the ledger correctly treats them as distinct rows — this is the concrete failure mode that motivates the change (§5.5). `queue/tmp/` stays flat, since `tempfile.mkstemp` already generates randomized names independent of the source filename.

The uploader enumerates only two-level regular-file entries under `pending/` and treats each as `(card_uuid, filename)`. It validates both path segments, never follows symlinks, and uploads entries in stable `(card_uuid, filename)` order. Invalid directories or entries are logged and left untouched. Empty per-card directories may be removed after their final queued file is successfully deleted.

### 5.4 Server side (`server/main.py`)

- `POST /upload`'s multipart form gains a required `card_uuid` text field alongside the existing `device_id`, `filename`, and `file`. Validated as an opaque safe token (`^[A-Za-z0-9-]{1,64}$`) rather than a strict UUID shape, since `vfat`/`exfat` cards produce short `blkid`-style IDs (e.g. `A1B2-C3D4`) while `ext2`/`ext3`/`ext4` produce full RFC4122 UUIDs.
- Validation uses a full-string match. UUID comparison is case-sensitive because the Pi sends the exact canonical token exposed under `/dev/disk/by-uuid`.
- The `uploads` table gains a `card_uuid` column; the uniqueness constraint changes from `UNIQUE(device_id, filename)` to `UNIQUE(device_id, card_uuid, filename)`.
- The stored blob path gains the same card segment as the local queue, for the identical collision reason: `uploads_root/device_id/card_uuid/filename` instead of Phase 2's `uploads_root/device_id/filename`.
- The `200` response echoes `card_uuid` back alongside the existing fields; `uploader.py`'s acknowledgement-matching check adds `parsed.get("card_uuid") == card_uuid` to the existing `device_id`/`filename`/`size` checks before marking a file uploaded.
- Everything else in Phase 2's contract (auth, 20 MiB limit, `already_stored` semantics, error codes) is unchanged.

Example successful response:

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

A missing or invalid `card_uuid` returns `422` and creates no row or blob.

### 5.5 Edge cases

- **Card yanked mid-scan or mid-copy**: unchanged from Phase 2 — an interrupted copy is never published as pending and the ledger is never touched, so the file is retried whenever that card (or the mountpoint) is next available. Now simply UUID-scoped rather than UUID-pinned.
- **Card yanked mid-upload**: no interaction with this phase's changes — once a file is in `queue/pending/<card_uuid>/`, delivery is fully decoupled from whether that card is still physically inserted.
- **Same card re-inserted later**: works correctly by construction. The ledger is append-only per `(card_uuid, filename)`; re-deriving the same UUID on re-insertion returns the same `known_filenames(card_uuid)` set as before, so only genuinely new files are copied — the same re-insertion guarantee Phase 2 §7 established, now scoped per card.
- **Two different cards sharing a filename**: the motivating scenario for this whole phase's schema changes. Card A's `logger-0001.csv` (still `pending`, not yet uploaded) is not overwritten or confused with Card B's differently-content `logger-0001.csv` once both the queue path and server storage path include the card segment.
- **Card already present at boot**: the mounter's first reconciliation mounts it; the watcher's repeated scan discovers it without requiring an insertion event.
- **Card UUID changes after formatting**: the card is a new logical identity and its retained CSV files are queued and uploaded again. This is accepted for the internal test workflow.
- **Two cards share a UUID**: unsupported. They would share a dedup namespace, so hardware verification must confirm the two real cards have different UUIDs before both are used.
- **Rapid or simultaneous insertion**: unsupported by the operating procedure. If more than one eligible filesystem is visible, the mounter leaves the current mount unchanged and logs an ambiguous-device state.
- **Unsupported or disqualified device**: the mounter declines to mount it and logs the reason. The watcher remains safely in `card_absent`/`card_not_mounted`, with no queue or ledger state written.

## 6. Configuration and Repo Layout

Pi runtime configuration changes from Phase 2:

- `CARD_UUID` and `CARD_FILESYSTEM` are removed. Existing Phase 2 entries are ignored and may be removed from the deployed `config.env`; no card-specific value is required.
- `CARD_MOUNTPOINT` behaves as before.
- `CARD_MOUNT_INTERVAL_SECONDS` is added with default `2`.
- `QUEUE_PATH` now contains per-card subdirectories under `pending/`, created lazily on first file from a given card.

```text
/server/
  main.py                      # extended: card_uuid in dedup key, upload payload, storage path
/pi/
  sdcard_mounter.py            # new: root-owned polling mount reconciler
  sdcard_watcher.py            # extended: dynamic card_uuid discovery instead of static config
  uploader.py                  # extended: card_uuid threaded through queue path, payload, ack check
  state.py                     # extended: ledger keyed by (card_uuid, filename)
  setup.sh                     # extended: installs mounter and performs explicit legacy reset
  systemd/
    sdcard-mounter.service     # new: root mount service
    sdcard-watcher.service     # updated ordering/description
    uploader.service           # unchanged behavior
/tests/
  test_sdcard_mounter.py       # new: candidate filtering and reconciliation
  test_sdcard_watcher.py       # extended: dynamic UUID and per-card queueing
  test_state.py                # extended: composite ledger identity
  test_uploader.py             # extended: nested queue and card_uuid contract
  test_server.py               # extended: server card_uuid contract and reset
/README.md                     # updated Phase 3 install, rollout, and verification
/prd/
  phase-3-multi-card-ingestion.md
```

## 7. Success Criteria / Verification

### 7.1 One-time clean rollout

The Phase 3 implementation includes explicit guards so legacy Phase 2 state is never interpreted using the new schemas:

- **Server guard:** on startup, the server inspects `PRAGMA table_info(uploads)`. If a legacy `uploads` table exists without `card_uuid`, startup fails clearly unless `PHASE3_RESET_UPLOADS=1` is set. With that flag, startup deletes only the contents of `uploads_root`, drops and recreates only the `uploads` table with the Phase 3 schema, and preserves the `pings` table. A filesystem or database failure aborts startup rather than serving with mixed state. Once the new schema exists, the flag is a no-op.
- **Pi guard:** `setup.sh` detects a legacy `files` table or flat files directly under `queue/pending/`. It fails clearly unless invoked with `--reset-phase2-state`. With that flag, and only after stopping the three Pi services, it deletes `state.db`, `state.db-wal`, `state.db-shm`, and the contents of `queue/pending/` and `queue/tmp/`, then recreates the empty directories. It does not delete `config.env`, API credentials, or logs. Once Phase 3 state exists, the flag is a no-op.

The operator uses this maintenance order:

1. Remove the data card and stop `uploader`, `sdcard-watcher`, and `sdcard-mounter` if already installed.
2. Set `PHASE3_RESET_UPLOADS=1` on Railway and deploy the Phase 3 server.
3. Confirm `/health` succeeds, `uploads` has the composite uniqueness constraint, upload rows/blobs are empty, and existing ping rows remain.
4. Remove `PHASE3_RESET_UPLOADS` from Railway; subsequent server restarts must retain new Phase 3 uploads.
5. On the Pi, deploy the Phase 3 `pi/` directory and run `sudo ./setup.sh --reset-phase2-state` with no data card inserted.
6. Confirm all three units are enabled, the old UUID-pinned udev rule/helper is gone, and no card-specific UUID or filesystem setting is required.
7. Start the three Pi services, then perform §7.3.

The mock upload data and local Phase 2 queue/ledger are intentionally unrecoverable after this reset. The never-cleared physical cards remain the source of truth.

### 7.2 Automated verification

From the repository root, the canonical command must pass:

```bash
.venv/bin/python -m pytest tests -q
```

Tests derived from this PRD must cover at least:

1. **Mounter filtering:** root/boot devices, non-removable devices, unsupported filesystems, missing/unsafe UUIDs, and multiple eligible filesystems are rejected without changing the mount; rejection reasons are logged. A removable whole-disk filesystem with no filesystem-bearing child partition is accepted.
2. **Mounter reconciliation:** zero candidates clears a stale mount, one candidate mounts with the correct filesystem-specific read-only options, an already-correct mount is unchanged, and failures are retried without crashing the daemon.
3. **Boot behavior:** an eligible card visible on the first reconciliation is mounted without an add event.
4. **Watcher discovery:** no mount, a non-boundary mountpoint, and a device without a safe UUID produce no ingestion; a valid mounted card returns its UUID dynamically.
5. **Mount-change safety:** a UUID/device change during a copy publishes no pending file and writes no ledger row.
6. **Per-card local identity:** the same filename under two UUIDs creates two ledger rows and two distinct queued files. Pending and uploaded reinsertion dedup remains scoped to the correct UUID.
7. **Nested uploader queue:** only safe `pending/<card_uuid>/<filename>` regular files are sent, symlinks and malformed entries are not followed, and ordering is stable.
8. **HTTP contract:** `card_uuid` is required; empty, unsafe, 65-character, separator, control-character, and final-newline values return `422` with no row/blob. Valid 1- and 64-character tokens are accepted.
9. **Server dedup matrix:** the same `(device_id, card_uuid, filename)` returns `already_stored`, while changing either `device_id` or `card_uuid` stores a distinct row and blob.
10. **Acknowledgement safety:** a missing or mismatched `card_uuid` leaves the queued file pending; matching `stored` and `already_stored` responses mark exactly that composite identity uploaded.
11. **Legacy reset guards:** both server and Pi refuse legacy state without the explicit reset control, reset only the documented targets when authorized, preserve pings/configuration, and do not reset valid Phase 3 state on later restarts or setup runs.
12. **Phase 2 delivery failures:** timeout, redirect, malformed acknowledgement, representative non-`200`, interrupted copy, and restart with pending nested queues retain data for retry.

### 7.3 Pi and Railway hardware verification

Record the commands and results for each step:

1. **Setup without a card:** `setup.sh` completes successfully with no data card inserted, and all three services stay running in an absent-card state.
2. **First-card facts:** with Card A inserted, record:

   ```bash
   lsblk -o PATH,TYPE,RM,HOTPLUG,FSTYPE,UUID,MOUNTPOINTS
   ```

   Confirm it has exactly one qualifying filesystem and a nonempty safe UUID. Record whether that filesystem is on a partition such as `/dev/sda1` or directly on the whole-card device such as `/dev/sda`; both are supported.
3. **Card present at boot:** boot the Pi with Card A already inserted. Within the mounter plus watcher polling intervals, it is mounted at `CARD_MOUNTPOINT` and scanned without an operator command.
4. **Read-only mount:** `findmnt CARD_MOUNTPOINT` shows `ro,nodev,nosuid,noexec`, plus `noload`/`norecovery` if the filesystem is `ext3` or `ext4`.
5. **Offline queueing:** with WiFi disconnected, Card A's new root-level CSVs appear completely under `queue/pending/<card-a-uuid>/`, its ledger rows are pending, `queue/tmp/` is empty after the scan, no HTTP request occurs, and card contents are unchanged.
6. **Online transfer:** reconnect WiFi. Matching acknowledgements mark the rows uploaded, remove the queued copies, and create one server row/blob per `(device_id, card_uuid, filename)`.
7. **Unchanged reinsertion:** remove Card A, wait at least five seconds, and reinsert it unchanged. It causes zero copies and zero upload requests for already-known filenames.
8. **New file on a known card:** add one new CSV to Card A and reinsert it. Only the new filename is queued and uploaded.
9. **Second-card facts:** when Card B is available, record the same `lsblk` output and confirm its UUID is nonempty and different from Card A. A duplicate UUID must be corrected before continuing.
10. **Colliding filename:** while offline, queue `logger-0001.csv` from Card A, remove it, wait at least five seconds, and queue a different `logger-0001.csv` from Card B. Both composite identities must coexist locally. Reconnect WiFi and confirm two distinct server rows/blobs and no false `already_stored`.
11. **Clear rejection:** an unsupported or ambiguous removable device is not mounted or ingested, and its reason is visible in `sdcard-mounter` logs.

## 8. Post-MVP / Look at Later

Carried forward from Phase 2 §8, still unaddressed and still not required for this phase:

- Content hashes, end-to-end checksum verification, and detection of content changes or filename reuse *within* a single card's lifetime.
- Encryption at rest.
- Public list/download endpoints, a UI, CSV parsing, and analytics.
- Per-device authentication, key rotation, and fleet credential management.
- Concurrent/horizontal server scaling and migration to object storage plus a managed database.

New to this phase:

- Content validation or allowlisting of which cards are acceptable (§3) — currently any mountable, filesystem-qualifying card is trusted.
- Multiple cards mounted and ingested simultaneously (e.g. via a USB hub or multi-reader) — this phase is strictly sequential, one card at a time.
- Multiple qualifying filesystems on one card and automatic selection among them.
- Rapid card swapping and event-driven mounting; the polling intervals are sufficient for the internal workflow.
- Hardware-backed or assigned card identity, duplicate-filesystem-UUID handling, and treating a reformatted card as its prior identity.
- Manual card UUID overrides.
- File-count, total-queue-size, and Railway Volume quotas.
- A registry of known/expected card UUIDs with friendly names, for operator visibility into which physical card is currently inserted.
- Automatic provisioning workflows beyond what's in scope here (e.g. a first-boot wizard, remote/fleet card management).
