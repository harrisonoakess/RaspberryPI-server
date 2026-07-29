# PRD: Phase 1 — Pi ↔ Railway Connectivity Signal (MVP)

## 1. Problem / Background

Before building any data pipeline, the core unknown to de-risk is whether a Raspberry Pi — carried between different physical locations and manually connected to available WiFi — can reliably recognize that it is connected to WiFi and signal a server hosted on Railway.

This phase proves that transport link in isolation, with no sensor data, SD card, or file transfer involved.

## 2. Definitions

- **Online / WiFi connected:** the Pi's operating system reports that its configured wireless interface is associated with a WiFi access point. This does not by itself guarantee that the public internet or Railway is reachable.
- **Railway reachable:** an authenticated `POST /ping` request receives a successful acknowledgement from the Railway server.
- **Ping:** one heartbeat request sent while the Pi is connected to WiFi. A ping is attempted every 30 seconds while WiFi remains connected.
- **Durable ping record:** a database row that survives FastAPI process restarts, server/container restarts, and Railway redeployments. Protection from accidental volume deletion, database corruption, or infrastructure loss through backups and disaster recovery is outside Phase 1 because this phase is only validating connectivity.

## 3. Target Environment

- Raspberry Pi 3 Model B+.
- Raspberry Pi OS 64-bit using systemd.
- The Pi's built-in WiFi adapter is the expected network device. Its interface name defaults to `wlan0` but remains configurable.
- The exact operating-system distribution and release, installed Python 3 version, and detected WiFi interface name are recorded during setup and included in the verification notes.

## 4. Goals

- The Pi detects whether it is connected to WiFi.
- While connected to WiFi, the Pi sends a ping to the Railway server every 30 seconds and records whether the server acknowledged it.
- While disconnected from WiFi, the Pi does not attempt a network request.
- The Pi and server remain connected and exchange pings successfully during a three-minute connected soak test.
- Accepted ping records survive FastAPI process restarts, server/container restarts, and Railway redeployments, and the server resumes accepting new pings afterward.
- The signal loop is safe to run continuously and unattended: it does not crash while disconnected or when requests fail, it does not start overlapping requests, and it rate-limits repeated identical error logs.
- The server and Pi are deployed completely independently, coupled only by the HTTP contract in this document.

## 5. Non-Goals

- No SD card reading, file transfer, or real sensor data.
- No LTE — WiFi only, connected manually at each location.
- No automatic WiFi discovery, credential entry, or captive-portal login.
- No separate test of generic internet connectivity. Phase 1 observes WiFi association and whether Railway acknowledges the ping.
- No exactly-once or idempotent ping processing. Duplicate ping records are acceptable in Phase 1.
- No full data pipeline or historical analytics on the server.
- No automated database backups or disaster-recovery process.
- No multi-device fleet management or horizontal server scaling. Phase 1 supports one Pi and one Railway server instance.

## 6. Proposed Design

```text
[Pi: connectivity daemon] --poll every 30s--> WiFi connected?
                                                   |
                              +--------------------+--------------------+
                              | no                                      | yes
                              v                                         v
                  rate-limited local log                    HTTPS POST /ping
                  and wait for next tick                              |
                                                                       v
                                                        [Railway: FastAPI server]
                                                                       |
                                                        commit row, then respond 200
```

### 6.1 Pi side (Python, one systemd service)

`connectivity_daemon.py` runs a single, non-overlapping poll loop every 30 seconds:

1. Ask the operating system whether the configured WiFi interface is associated with an access point.
2. If WiFi is disconnected, do not make an HTTP request. Log the disconnected state according to the rate-limit rules below, then wait for the next poll.
3. If WiFi is connected, send an HTTPS `POST /ping` request using the contract in §6.2. The request has a 10-second timeout.
4. If the server acknowledges the ping, log the acknowledgement to both stdout and the rotating log file.
5. If DNS resolution, connection, TLS, timeout, authentication, validation, or server processing fails, keep the daemon running and retry on the next poll while WiFi remains connected. Log a useful failure category without logging the API key.

The `device_id` is derived from the Pi's hostname at runtime. Because Phase 1 supports one Pi, provisioning must give that Pi a stable hostname that satisfies the `device_id` validation rules. The Pi supplies `sent_at` for diagnostic context, but the server's `received_at` is the authoritative time.

The service runs through systemd as a dedicated non-root `piuploader` user, starts on boot, and restarts after an unexpected process failure. It must continue working when it starts before WiFi is available.

#### Logging and rate limiting

- Logs go to stdout, which systemd captures in the journal, and to `/var/log/piuploader/connectivity.log`.
- The file uses `RotatingFileHandler` with an explicit size limit and backup count so logs cannot grow without bound.
- The first occurrence of a disconnected state or request-error category is logged immediately.
- An unchanged, repeated error category is logged no more than once every five minutes.
- A change to a different state or error category is logged immediately.
- Recovery from an error and every successful ping acknowledgement are logged immediately.

#### Pi runtime configuration

`/etc/piuploader/config.env` supplies:

- `SERVER_URL`
- `API_KEY`
- `WIFI_INTERFACE` (default: `wlan0`)
- `POLL_INTERVAL_SECONDS` (default: `30`)
- `REQUEST_TIMEOUT_SECONDS` (default: `10`)
- `ERROR_LOG_REPEAT_SECONDS` (default: `300`)

The real configuration file is not committed to git. It is readable by the service account, not world-readable, and the API key must never appear in logs.

### 6.2 Server side (Python, FastAPI, deployed on Railway)

#### `POST /ping`

Authentication:

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

Request body:

```json
{
  "device_id": "raspberrypi-uploader",
  "sent_at": "2026-07-28T18:30:00Z"
}
```

Validation:

- `device_id` is a hostname-style identifier from 1 through 63 characters: letters, numbers, and internal hyphens, with no leading or trailing hyphen.
- `sent_at` is a valid RFC 3339 UTC timestamp.
- Duplicate requests and duplicate values are accepted and stored as separate ping rows.

Successful response:

```http
HTTP/1.1 200 OK
```

```json
{
  "status": "acknowledged",
  "device_id": "raspberrypi-uploader",
  "received_at": "2026-07-28T18:30:01Z"
}
```

The server returns `200` only after the ping row has been committed successfully. Expected error behavior:

- `400` or `422` for an invalid request body.
- `401` for a missing or invalid bearer token.
- `500` or `503` when the server cannot persist the ping.

At minimum, each SQLite row contains:

- `id`: server-generated integer primary key
- `device_id`: client-supplied identifier
- `sent_at`: client-supplied diagnostic timestamp
- `received_at`: server-generated authoritative UTC timestamp

#### `GET /health`

- Unauthenticated liveness check.
- Returns `200` with `{"status": "ok"}` when the FastAPI process is running.
- It is not proof that the Pi has WiFi or that a ping was persisted.

### 6.3 Persistence and Railway deployment

Railway's container filesystem is ephemeral. The SQLite database therefore lives on a Railway volume mounted at runtime. The server reads the volume mount location from configuration, using Railway's volume mount environment when available, and initializes the database during application startup rather than during build or pre-deploy.

Phase 1 uses exactly one Railway service instance. Railway volumes do not support multiple replicas, and a deployment with an attached volume can have a brief period of downtime during redeployment. A ping that fails during that interval is expected to retry on the next poll.

Persisted rows are verified by inspecting the SQLite database directly with documented Railway volume/CLI and SQLite commands. Phase 1 does not add a public endpoint for browsing ping history.

The Railway process must listen on `0.0.0.0` and the port supplied by Railway.

### 6.4 Auth, transport, and WiFi security

- Transport uses HTTPS through Railway's public domain.
- The client validates the server's TLS certificate. Certificate verification must not be disabled.
- The client sends credentials only to the configured `SERVER_URL`, does not follow redirects from `/ping`, and treats a redirect as a request failure.
- Phase 1 has one Pi and uses one shared bearer-token API key.
- The server key is stored as a Railway environment variable.
- The Pi key is stored in `/etc/piuploader/config.env`.

A captive portal is WiFi that requires a person to open a web page and accept terms or sign in before public internet access is granted, as is common in hotels, airports, and cafés. A captive-portal sign-in is an access mechanism, not proof that the WiFi network is encrypted or trustworthy.

WPA2- or WPA3-protected WiFi is preferred when available. Phase 1 does not require a captive portal and does not treat captive-portal login as a security control. If a test location uses one, a person must complete its login manually. Until then, the Pi may report that WiFi is connected while `/ping` fails. The daemon records a rate-limited request failure and succeeds on a later poll after access is granted.

The HTTPS and bearer-token requirements apply on every network, including open or captive-portal WiFi.

## 7. Repo Layout (this phase's slice)

```text
/server/
  main.py                         # FastAPI app: /ping and /health
  requirements.txt
  railway.json                    # Railway start/deploy configuration
/pi/
  connectivity_daemon.py
  setup.sh                        # idempotent service-user and systemd installation
  systemd/
    connectivity-daemon.service
  config.env.example
/prd/
  phase-1-connection.md
README.md                         # local, Railway, Pi, and DB-inspection instructions
```

The README must state the detected operating-system distribution and release, Python version, and WiFi interface name and document:

- Local server setup and verification.
- Railway environment variables, public domain, volume mount, and `/server` root directory.
- Pi hostname, configuration, service-user, log-directory, and systemd installation.
- Direct SQLite inspection before and after a Railway redeploy.

## 8. Success Criteria / Verification

1. **Pi environment**
   - Setup confirms that the device is a Raspberry Pi 3 Model B+ running Raspberry Pi OS 64-bit.
   - Setup records the operating-system distribution and release, Python 3 version, systemd availability, and detected WiFi interface name.
   - The configured WiFi interface matches the detected built-in adapter.

2. **Local API contract**
   - The server runs locally and `/health` returns the documented `200` response.
   - A valid authenticated `/ping` returns the documented `200` response.
   - Direct SQLite inspection shows `device_id`, client `sent_at`, and server `received_at`.
   - Missing/incorrect authentication and invalid bodies return their documented non-2xx responses.

3. **Railway deployment and durability**
   - The single-instance server deploys with its SQLite database on the attached volume.
   - An authenticated `/ping` returns `200`, and direct inspection confirms the row exists.
   - After a server/container restart, direct inspection confirms that the same row still exists.
   - After the restart, a new authenticated `/ping` succeeds and creates a new row.
   - After a Railway redeploy, direct inspection confirms that both rows still exist.
   - After the redeploy, another authenticated `/ping` succeeds and creates another row.

4. **Disconnected Pi behavior**
   - The Pi boots and the daemon starts while WiFi is disconnected.
   - For at least three minutes, the daemon stays active, makes no `/ping` requests, and does not crash.
   - The first disconnected message is logged immediately; no identical repeat appears during that three-minute interval because the repeat limit is five minutes.

5. **Three-minute connected soak test**
   - After the Pi connects to WiFi, the first `/ping` is attempted within one 30-second poll interval.
   - The Pi remains connected for three continuous minutes.
   - Successful acknowledgements continue throughout the three minutes, approximately once per poll interval, without overlapping requests or daemon restarts.
   - The Pi's journal and rotating file contain the acknowledgements.
   - Direct SQLite inspection shows the corresponding rows with authoritative `received_at` values.

6. **Disconnect and reconnect behavior**
   - Disconnecting WiFi stops HTTP attempts without stopping the daemon.
   - Reconnecting WiFi resumes ping attempts within one poll interval.
   - Three disconnect/reconnect cycles produce the expected disconnected, recovery, and acknowledged states.
   - Duplicate ping rows are acceptable; no request or state becomes stuck.

7. **Failure log rate limiting**
   - A repeatable request failure is logged immediately on its first occurrence.
   - The same failure category is not logged again for five minutes.
   - Recovery or a different error category is logged immediately.

## 9. Hand-off to Phase 2

Once Phase 1 is validated, Phase 2 reuses the Railway deployment, volume-backed SQLite pattern, API-key authentication, authoritative server timestamps, and systemd daemon pattern. Phase 2 adds idempotent file delivery and server-side deduplication; Phase 1 ping duplicates do not establish exactly-once delivery semantics for Phase 2.
