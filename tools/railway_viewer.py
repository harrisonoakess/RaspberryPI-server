#!/usr/bin/env python3
"""Local, read-only viewer for pings and CSV uploads stored on Railway.

The browser only talks to this loopback HTTP server. Each data request invokes
the Railway CLI, which runs a fixed Python program inside the linked service.
No API key or Railway credential is sent to browser code.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


DEFAULT_PORT = 8765
REMOTE_TIMEOUT_SECONDS = 30

# This fixed program is passed directly to `python3 -c` in the Railway
# container. User input is supplied as a separate argv value, never interpolated
# into code or a shell command.
REMOTE_PYTHON = r"""
import csv
import json
import os
import sqlite3
import sys
from urllib.parse import quote


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def fail(message):
    emit({"ok": False, "error": message})


def database_path():
    explicit = os.environ.get("DATABASE_PATH", "").strip()
    if explicit:
        return explicit
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return os.path.join(volume, "pings.db")
    return os.path.join("data", "pings.db")


def connect_read_only(path):
    absolute = os.path.abspath(path)
    uri = "file:" + quote(absolute, safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def list_data(connection):
    pings = [
        dict(row)
        for row in connection.execute(
            "SELECT id, device_id, sent_at, received_at "
            "FROM pings ORDER BY id DESC LIMIT 50"
        )
    ]
    uploads = [
        dict(row)
        for row in connection.execute(
            "SELECT id, device_id, card_uuid, filename, size, received_at "
            "FROM uploads ORDER BY id DESC LIMIT 50"
        )
    ]
    emit({"ok": True, "pings": pings, "uploads": uploads})


def preview(connection, raw_upload_id):
    try:
        upload_id = int(raw_upload_id)
    except (TypeError, ValueError):
        fail("Upload id must be a positive integer.")
        return
    if upload_id <= 0 or str(upload_id) != raw_upload_id:
        fail("Upload id must be a positive integer.")
        return

    row = connection.execute(
        "SELECT card_uuid, filename, stored_path FROM uploads WHERE id = ?",
        (upload_id,),
    ).fetchone()
    if row is None:
        fail("Upload was not found.")
        return

    stored_path = row["stored_path"]
    if not os.path.isfile(stored_path):
        fail("Stored CSV file is missing.")
        return

    try:
        with open(stored_path, "r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source, strict=True)
            records = []
            for _ in range(101):
                try:
                    records.append(next(reader))
                except StopIteration:
                    break
    except UnicodeDecodeError:
        fail("Stored CSV file is not valid UTF-8.")
        return
    except csv.Error:
        fail("Stored CSV file is malformed.")
        return
    except OSError:
        fail("Stored CSV file could not be read.")
        return

    if not records:
        fail("Stored CSV file is empty.")
        return

    truncated = len(records) > 100
    emit(
        {
            "ok": True,
            "upload_id": upload_id,
            "card_uuid": row["card_uuid"],
            "filename": row["filename"],
            "records": records[:100],
            "truncated": truncated,
        }
    )


path = database_path()
if not os.path.isfile(path):
    fail("Railway database file was not found.")
    raise SystemExit(0)

try:
    connection = connect_read_only(path)
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else ""
        if action == "list":
            list_data(connection)
        elif action == "preview" and len(sys.argv) == 3:
            preview(connection, sys.argv[2])
        else:
            fail("Unsupported remote viewer action.")
    finally:
        connection.close()
except sqlite3.Error:
    fail("Railway database could not be read.")
except Exception:
    fail("Railway data could not be read.")
""".strip()


PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Railway Data Check</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { max-width: 1100px; margin: 0 auto; padding: 2rem 1rem 4rem; }
    header { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
    h1 { margin: 0; flex: 1; }
    h2 { margin-top: 2rem; }
    button { padding: .45rem .8rem; cursor: pointer; }
    .status { min-height: 1.5rem; margin: 1rem 0; }
    .error { color: #c62828; }
    .table-wrap { overflow-x: auto; border: 1px solid #8885; border-radius: .4rem; }
    table { width: 100%; border-collapse: collapse; white-space: nowrap; }
    th, td { padding: .55rem .7rem; border-bottom: 1px solid #8884; text-align: left; }
    th { background: #8882; }
    tbody tr:last-child td { border-bottom: 0; }
    .empty { padding: .8rem; color: #777; }
    #preview-table td { white-space: pre-wrap; overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <header>
    <h1>Railway Data Check</h1>
    <button id="refresh" type="button">Refresh</button>
  </header>
  <p>This local viewer reads the linked Railway service. It cannot edit or delete data.</p>
  <p id="status" class="status" aria-live="polite"></p>

  <section aria-labelledby="uploads-heading">
    <h2 id="uploads-heading">Latest uploads</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Device</th><th>Card UUID</th><th>Filename</th><th>Bytes</th><th>Received</th><th></th></tr></thead>
        <tbody id="uploads"></tbody>
      </table>
      <p id="uploads-empty" class="empty" hidden>No uploads found.</p>
    </div>
  </section>

  <section aria-labelledby="pings-heading">
    <h2 id="pings-heading">Latest pings</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Device</th><th>Sent</th><th>Received</th></tr></thead>
        <tbody id="pings"></tbody>
      </table>
      <p id="pings-empty" class="empty" hidden>No pings found.</p>
    </div>
  </section>

  <section aria-labelledby="preview-heading">
    <h2 id="preview-heading">CSV preview</h2>
    <p id="preview-status" class="status" aria-live="polite">Select Preview beside an upload.</p>
    <div id="preview-wrap" class="table-wrap" hidden>
      <table id="preview-table">
        <thead id="preview-head"></thead>
        <tbody id="preview-body"></tbody>
      </table>
    </div>
  </section>

  <script>
    "use strict";
    const statusNode = document.getElementById("status");
    const refreshButton = document.getElementById("refresh");
    const uploadsBody = document.getElementById("uploads");
    const pingsBody = document.getElementById("pings");
    const uploadsEmpty = document.getElementById("uploads-empty");
    const pingsEmpty = document.getElementById("pings-empty");
    const previewStatus = document.getElementById("preview-status");
    const previewWrap = document.getElementById("preview-wrap");
    const previewHead = document.getElementById("preview-head");
    const previewBody = document.getElementById("preview-body");

    function cell(row, value) {
      const node = document.createElement("td");
      node.textContent = value === null || value === undefined ? "" : String(value);
      row.appendChild(node);
    }

    async function readJson(url) {
      const response = await fetch(url, {cache: "no-store"});
      let payload;
      try {
        payload = await response.json();
      } catch (_) {
        throw new Error("The local viewer returned an invalid response.");
      }
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "The Railway request failed.");
      }
      return payload;
    }

    function renderUploads(uploads) {
      uploadsBody.replaceChildren();
      uploadsEmpty.hidden = uploads.length !== 0;
      for (const upload of uploads) {
        const row = document.createElement("tr");
        cell(row, upload.id);
        cell(row, upload.device_id);
        cell(row, upload.card_uuid);
        cell(row, upload.filename);
        cell(row, upload.size);
        cell(row, upload.received_at);
        const actionCell = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Preview";
        button.addEventListener("click", () => loadPreview(upload.id));
        actionCell.appendChild(button);
        row.appendChild(actionCell);
        uploadsBody.appendChild(row);
      }
    }

    function renderPings(pings) {
      pingsBody.replaceChildren();
      pingsEmpty.hidden = pings.length !== 0;
      for (const ping of pings) {
        const row = document.createElement("tr");
        cell(row, ping.id);
        cell(row, ping.device_id);
        cell(row, ping.sent_at);
        cell(row, ping.received_at);
        pingsBody.appendChild(row);
      }
    }

    async function loadData() {
      refreshButton.disabled = true;
      statusNode.className = "status";
      statusNode.textContent = "Reading Railway…";
      try {
        const payload = await readJson("/api/data");
        renderUploads(payload.uploads);
        renderPings(payload.pings);
        statusNode.textContent = "Showing up to 50 newest records from each table.";
      } catch (error) {
        statusNode.className = "status error";
        statusNode.textContent = error.message;
      } finally {
        refreshButton.disabled = false;
      }
    }

    async function loadPreview(uploadId) {
      previewWrap.hidden = true;
      previewStatus.className = "status";
      previewStatus.textContent = "Reading CSV…";
      try {
        const payload = await readJson("/api/preview?id=" + encodeURIComponent(uploadId));
        previewHead.replaceChildren();
        previewBody.replaceChildren();
        const width = payload.records.reduce((maximum, row) => Math.max(maximum, row.length), 0);
        const headingRow = document.createElement("tr");
        const recordHeading = document.createElement("th");
        recordHeading.textContent = "Record";
        headingRow.appendChild(recordHeading);
        for (let index = 0; index < width; index += 1) {
          const heading = document.createElement("th");
          heading.textContent = "Column " + (index + 1);
          headingRow.appendChild(heading);
        }
        previewHead.appendChild(headingRow);
        payload.records.forEach((record, index) => {
          const row = document.createElement("tr");
          cell(row, index + 1);
          for (let column = 0; column < width; column += 1) {
            cell(row, record[column]);
          }
          previewBody.appendChild(row);
        });
        previewStatus.textContent =
          payload.card_uuid + " / " + payload.filename + " — first " +
          payload.records.length + " record(s)" +
          (payload.truncated ? " (more records not shown)" : "");
        previewWrap.hidden = false;
      } catch (error) {
        previewStatus.className = "status error";
        previewStatus.textContent = error.message;
      }
    }

    refreshButton.addEventListener("click", loadData);
    loadData();
  </script>
</body>
</html>
"""


class ViewerError(RuntimeError):
    """A Railway CLI or remote-data failure safe to show in the local UI."""


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ViewerError("Railway returned output that was not valid UTF-8.") from exc
    return value


def _brief_cli_error(stderr: bytes | str | None) -> str:
    try:
        text = _decode_output(stderr)
    except ViewerError:
        return ""
    return " ".join(text.split())[:500]


def fetch_remote(action: str, upload_id: int | None = None) -> dict[str, Any]:
    """Run one fixed, read-only operation inside the linked Railway service."""
    if action not in {"list", "preview"}:
        raise ValueError("unsupported viewer action")
    if action == "list" and upload_id is not None:
        raise ValueError("list does not accept an upload id")
    if action == "preview" and (type(upload_id) is not int or upload_id <= 0):
        raise ValueError("preview requires a positive upload id")

    encoded_script = base64.b64encode(REMOTE_PYTHON.encode("utf-8")).decode("ascii")
    bootstrap = (
        "import base64;"
        f'exec(base64.b64decode("{encoded_script}"))'
    )
    remote_command = f"python3 -c '{bootstrap}' {action}"
    if upload_id is not None:
        remote_command += f" {upload_id}"
    command = ["railway", "ssh", remote_command]

    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=REMOTE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ViewerError(
            "Railway CLI was not found. Install it, then run railway login and railway link."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ViewerError(
            f"Railway did not respond within {REMOTE_TIMEOUT_SECONDS} seconds."
        ) from exc
    except OSError as exc:
        raise ViewerError("Railway CLI could not be started.") from exc

    if completed.returncode != 0:
        detail = _brief_cli_error(completed.stderr)
        suffix = f": {detail}" if detail else "."
        raise ViewerError(
            f"Railway command failed with exit code {completed.returncode}{suffix}"
        )

    stdout = _decode_output(completed.stdout).strip()
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ViewerError("Railway returned an invalid response.") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise ViewerError("Railway returned an invalid response.")
    if not payload["ok"]:
        message = payload.get("error")
        if not isinstance(message, str) or not message:
            message = "Railway data could not be read."
        raise ViewerError(message)
    return payload


class ViewerHandler(BaseHTTPRequestHandler):
    """Serve the static page and its two read-only JSON routes."""

    server_version = "RailwayViewer/1"

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if parsed.path == "/" and not parsed.query:
            self._send(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                PAGE_HTML.encode("utf-8"),
            )
            return

        if parsed.path == "/api/data" and not parsed.query:
            self._remote_response("list")
            return

        if parsed.path == "/api/preview":
            values = parse_qs(parsed.query, keep_blank_values=True)
            raw_ids = values.get("id", [])
            if set(values) != {"id"} or len(raw_ids) != 1:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "Upload id must be a positive integer."},
                )
                return
            try:
                upload_id = int(raw_ids[0])
            except ValueError:
                upload_id = 0
            if upload_id <= 0 or str(upload_id) != raw_ids[0]:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "Upload id must be a positive integer."},
                )
                return
            self._remote_response("preview", upload_id)
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "Not found."},
        )

    def _remote_response(self, action: str, upload_id: int | None = None) -> None:
        try:
            payload = fetch_remote(action, upload_id)
        except ViewerError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": str(exc)},
            )
            return
        self._send_json(HTTPStatus.OK, payload)

    def log_message(self, format: str, *args: object) -> None:
        # Keep normal request visibility without logging response bodies,
        # credentials, remote paths, or subprocess output.
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format % args)
        )


def port_number(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="View recent data in the linked Railway service."
    )
    parser.add_argument(
        "--port",
        type=port_number,
        default=DEFAULT_PORT,
        help=f"local loopback port (default: {DEFAULT_PORT})",
    )
    return parser.parse_args(argv)


def build_server(port: int) -> ThreadingHTTPServer:
    """Create a loopback-only HTTP server."""
    return ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = build_server(args.port)
    print(f"Railway data viewer: http://127.0.0.1:{args.port}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
