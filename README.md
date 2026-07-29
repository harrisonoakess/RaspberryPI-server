# Pi → Railway Connectivity (Phase 1)

Proves the transport link in isolation: a Raspberry Pi detects that it is
connected to WiFi and, while connected, sends an authenticated heartbeat to a
FastAPI server on Railway every 30 seconds. No sensor data, SD card, or file
transfer — see `prd/phase-1-connection.md`.

The server (`/server`) and the Pi daemon (`/pi`) deploy independently and are
coupled only by the HTTP contract below.

## Layout

```text
server/   FastAPI app (POST /ping, GET /health), Railway deploy config
pi/       systemd daemon, installer, example config
tests/    test suite derived from the PRD success criteria
prd/      phase PRDs
```

## HTTP contract

| Endpoint | Auth | Success | Failures |
| --- | --- | --- | --- |
| `GET /health` | none | `200 {"status":"ok"}` | — |
| `POST /ping` | `Authorization: Bearer <API_KEY>` | `200 {"status":"acknowledged","device_id":…,"received_at":…}` | `401` bad/missing token, `422` invalid body, `503` cannot persist |

`POST /ping` body: `{"device_id": "raspberrypi-uploader", "sent_at": "2026-07-28T18:30:00Z"}`

- `device_id`: 1–63 characters, letters/numbers/internal hyphens, no leading or
  trailing hyphen. Derived from the Pi's hostname at runtime.
- `sent_at`: RFC 3339 **UTC** timestamp (`Z` or `+00:00`). Diagnostic only —
  the server's `received_at` is authoritative.
- Duplicates are accepted and stored as separate rows. Phase 1 has no
  idempotency; do not rely on this for Phase 2.

`200` is returned only after the row is committed (`synchronous=FULL`).

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

## Tests

Canonical command, from the repo root:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

`tests/` covers the API contract, auth and validation boundaries, restart
durability, WiFi detection, failure categorisation, the poll cadence, and the
log rate limiter. Hardware behaviour (real WiFi association, systemd, Railway
volumes) is verified manually below.

## Local server

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
API_KEY=local-dev-key DATABASE_PATH=./data/pings.db \
  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

The server refuses to start without `API_KEY`.

Verify (PRD success criterion 2):

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}

curl -s -X POST http://127.0.0.1:8000/ping \
  -H 'Authorization: Bearer local-dev-key' -H 'Content-Type: application/json' \
  -d '{"device_id":"raspberrypi-uploader","sent_at":"2026-07-28T18:30:00Z"}'
# {"status":"acknowledged","device_id":"raspberrypi-uploader","received_at":"..."}

# 401 — no token, then a wrong token
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/ping \
  -H 'Content-Type: application/json' -d '{"device_id":"pi","sent_at":"2026-07-28T18:30:00Z"}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/ping \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' \
  -d '{"device_id":"pi","sent_at":"2026-07-28T18:30:00Z"}'

# 422 — invalid device_id and timestamp
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/ping \
  -H 'Authorization: Bearer local-dev-key' -H 'Content-Type: application/json' \
  -d '{"device_id":"-bad","sent_at":"nope"}'
```

Inspect the rows directly:

```bash
sqlite3 server/data/pings.db 'SELECT id, device_id, sent_at, received_at FROM pings;'
```

## Railway deployment

One service, one replica — Railway volumes do not support multiple replicas.

1. **Service** → *Settings* → set **Root Directory** to `/server`.
   `railway.json` supplies the start command
   (`uvicorn main:app --host 0.0.0.0 --port $PORT`), `numReplicas: 1`, and a
   `/health` healthcheck.
2. **Volume** → attach one, mount path `/data`. Railway injects
   `RAILWAY_VOLUME_MOUNT_PATH`, and the server puts `pings.db` inside it. Set
   `DATABASE_PATH` explicitly only if you want a different filename.
3. **Variables** → `API_KEY=<long random string>`. Generate with
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
   Use the same value in the Pi's `config.env`.
4. **Networking** → generate a public domain. That HTTPS URL is `SERVER_URL`.

The database is created at application startup, not at build or pre-deploy
time, so it lands on the mounted volume.

### Durability check (PRD success criterion 3)

```bash
railway link            # select the project/service once
DOMAIN=https://<your-service>.up.railway.app

ping() { curl -s -X POST "$DOMAIN/ping" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"device_id":"raspberrypi-uploader","sent_at":"2026-07-28T18:30:00Z"}'; echo; }

rows() { railway ssh "sqlite3 \$RAILWAY_VOLUME_MOUNT_PATH/pings.db \
  'SELECT id, device_id, sent_at, received_at FROM pings;'"; }
```

Run in order and record the output at each step:

1. `ping` → `200`; `rows` shows row 1.
2. Restart the service (Railway dashboard → *Restart*). `rows` still shows row 1.
3. `ping` → `200`; `rows` shows rows 1–2.
4. Redeploy (`railway up`, or push to the connected branch). `rows` still shows
   rows 1–2.
5. `ping` → `200`; `rows` shows rows 1–3.

If `sqlite3` is absent from the runtime image, read the rows with Python
instead:

```bash
railway ssh "python3 -c \"import os,sqlite3;print(sqlite3.connect(os.environ['RAILWAY_VOLUME_MOUNT_PATH']+'/pings.db').execute('SELECT * FROM pings').fetchall())\""
```

A deployment with an attached volume has a brief downtime window during
redeploy. A ping that fails then is expected to succeed on the next poll.

## Pi installation

Requirements: Raspberry Pi 3 Model B+, Raspberry Pi OS 64-bit, systemd. The
daemon uses **only the Python 3 standard library** — nothing to `pip install`,
so setup works on a Pi with no internet access yet.

1. Give the Pi a stable hostname that is a valid `device_id`. The daemon exits
   with a configuration error if the hostname cannot produce one:

   ```bash
   sudo hostnamectl set-hostname raspberrypi-uploader
   ```

2. Copy `pi/` to the Pi and run the installer (idempotent — safe to re-run):

   ```bash
   sudo ./setup.sh
   ```

   It verifies the model, architecture, Python 3, systemd, and hostname;
   detects the WiFi interface; creates the `piuploader` system user;
   creates `/etc/piuploader` (`0750`), `/var/log/piuploader` (`0750`), and
   `/opt/piuploader`; installs the daemon and the systemd unit; enables the
   service; and writes the environment report. It never overwrites an existing
   `config.env`. Pass `--skip-hardware-check` to install on non-Pi hardware for
   development.

3. Fill in the real values:

   ```bash
   sudo nano /etc/piuploader/config.env   # SERVER_URL and API_KEY
   sudo systemctl restart connectivity-daemon
   ```

   `config.env` is `root:piuploader 0640` — readable by the service, not
   world-readable, never committed. See `pi/config.env.example` for every
   setting and its default (`WIFI_INTERFACE=wlan0`,
   `POLL_INTERVAL_SECONDS=30`, `REQUEST_TIMEOUT_SECONDS=10`,
   `ERROR_LOG_REPEAT_SECONDS=300`).

4. Watch it:

   ```bash
   journalctl -u connectivity-daemon -f
   sudo tail -f /var/log/piuploader/connectivity.log
   ```

Logs go to stdout (captured by the journal) and to
`/var/log/piuploader/connectivity.log`, which rotates at 1 MB with 5 backups so
it cannot grow without bound. The API key is never logged.

### Behaviour

- **Disconnected**: no HTTP request is attempted at all. The daemon keeps
  running.
- **Connected**: `POST /ping` over HTTPS with a 10-second timeout, TLS
  certificate verification always on, redirects refused (a redirect is treated
  as a request failure so credentials only ever reach `SERVER_URL`).
- **Failures**: DNS, connection, TLS, timeout, auth, validation, and server
  errors each keep the daemon alive and retry on the next poll.
- **Rate limiting**: the first occurrence of a state or error category is
  logged immediately; an unchanged category repeats at most once every 5
  minutes (and reports how many entries it suppressed); a change of category,
  a recovery, and every successful acknowledgement are logged immediately.
- **Cadence**: one sequential loop, so requests can never overlap — a slow
  request delays the next tick rather than running alongside it.
- **Boot**: starts before WiFi is available and survives it. `Restart=on-failure`
  with a start-rate limit keeps a misconfiguration from hot-looping.

### Manual hardware verification (PRD success criteria 4–7)

Record the results of each:

4. **Disconnected boot** — boot with WiFi off, `systemctl status
   connectivity-daemon` stays `active (running)` for 3 minutes, the journal has
   exactly one "not connected" line (the 5-minute repeat window has not
   elapsed), and the server logs no requests.
5. **3-minute connected soak** — connect WiFi; the first ping lands within one
   30-second interval; acknowledgements continue roughly every 30 seconds for
   3 minutes with no restarts; the journal and the rotating file both show
   them; the SQLite rows show the matching `received_at` values.
6. **Disconnect/reconnect ×3** — each disconnect stops HTTP attempts without
   stopping the daemon; each reconnect resumes within one poll interval;
   duplicate rows are fine and nothing gets stuck.
7. **Rate limiting** — point `SERVER_URL` at an unreachable host: the first
   failure logs immediately, the same category stays quiet for 5 minutes, and
   restoring the correct URL logs the recovery immediately.

## Security notes

- HTTPS with certificate verification on every network, including open and
  captive-portal WiFi. Verification is never disabled.
- One shared bearer token: Railway variable on the server,
  `/etc/piuploader/config.env` on the Pi. Compared with a constant-time check.
- The daemon runs as the non-root `piuploader` user with `NoNewPrivileges`,
  `ProtectSystem=strict`, and write access limited to its log directory.
- A captive portal is not a security control. If a location uses one, someone
  must complete the login by hand; until then WiFi reads as connected while
  `/ping` fails, which the daemon logs as a rate-limited failure and recovers
  from on a later poll.
- Phase 1 has no backups or disaster recovery. Volume loss loses the pings.
