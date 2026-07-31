from __future__ import annotations

import json
import os
import base64
import shlex
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from tools import railway_viewer as viewer


SCHEMA = """
CREATE TABLE pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    card_uuid TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (device_id, card_uuid, filename)
);
"""


def make_database(path: Path, count: int = 1, device_prefix: str = "pi") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        for index in range(1, count + 1):
            connection.execute(
                "INSERT INTO pings (device_id, sent_at, received_at) VALUES (?, ?, ?)",
                (
                    f"{device_prefix}-{index}",
                    f"2026-07-29T00:00:{index % 60:02d}Z",
                    f"2026-07-29T00:01:{index % 60:02d}Z",
                ),
            )
            connection.execute(
                "INSERT INTO uploads "
                "(device_id, card_uuid, filename, stored_path, size, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"{device_prefix}-{index}",
                    f"CARD-{index:04d}",
                    f"logger-{index:04d}.csv",
                    str(path.parent / f"private-{index}.csv"),
                    index,
                    f"2026-07-29T00:02:{index % 60:02d}Z",
                ),
            )
    return path


def remote(
    working_directory: Path,
    action: str,
    *arguments: object,
    env_updates: dict[str, str] | None = None,
) -> dict:
    environment = os.environ.copy()
    environment.pop("DATABASE_PATH", None)
    environment.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
    if env_updates:
        environment.update(env_updates)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            viewer.REMOTE_PYTHON,
            action,
            *(str(argument) for argument in arguments),
        ],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_remote_database_path_precedence_is_exact(tmp_path):
    fallback = make_database(tmp_path / "data" / "pings.db", device_prefix="fallback")
    volume = make_database(tmp_path / "volume" / "pings.db", device_prefix="volume")
    explicit = make_database(tmp_path / "explicit.db", device_prefix="explicit")

    payload = remote(
        tmp_path,
        "list",
        env_updates={
            "DATABASE_PATH": str(explicit),
            "RAILWAY_VOLUME_MOUNT_PATH": str(volume.parent),
        },
    )
    assert payload["pings"][0]["device_id"] == "explicit-1"

    payload = remote(
        tmp_path,
        "list",
        env_updates={"RAILWAY_VOLUME_MOUNT_PATH": str(volume.parent)},
    )
    assert payload["pings"][0]["device_id"] == "volume-1"

    payload = remote(tmp_path, "list")
    assert payload["pings"][0]["device_id"] == "fallback-1"


def test_list_returns_latest_50_newest_first_without_stored_paths(tmp_path):
    database = make_database(tmp_path / "pings.db", count=55)

    before = database.read_bytes()
    payload = remote(
        tmp_path,
        "list",
        env_updates={"DATABASE_PATH": str(database)},
    )

    assert payload["ok"] is True
    assert [row["id"] for row in payload["pings"]] == list(range(55, 5, -1))
    assert [row["id"] for row in payload["uploads"]] == list(range(55, 5, -1))
    assert payload["uploads"][0]["card_uuid"] == "CARD-0055"
    assert all("stored_path" not in row for row in payload["uploads"])
    assert database.read_bytes() == before
    assert "mode=ro" in viewer.REMOTE_PYTHON
    assert "PRAGMA query_only=ON" in viewer.REMOTE_PYTHON


def test_list_keeps_same_device_and_filename_distinct_across_cards(tmp_path):
    database = make_database(tmp_path / "pings.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO uploads "
            "(device_id, card_uuid, filename, stored_path, size, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "pi-1",
                "SECOND-CARD",
                "logger-0001.csv",
                str(tmp_path / "private-second-card.csv"),
                2,
                "2026-07-29T00:03:00Z",
            ),
        )

    payload = remote(
        tmp_path,
        "list",
        env_updates={"DATABASE_PATH": str(database)},
    )

    assert [
        (row["device_id"], row["card_uuid"], row["filename"])
        for row in payload["uploads"]
    ] == [
        ("pi-1", "SECOND-CARD", "logger-0001.csv"),
        ("pi-1", "CARD-0001", "logger-0001.csv"),
    ]
    assert all("stored_path" not in row for row in payload["uploads"])


def test_preview_parses_quoted_unicode_csv(tmp_path):
    database = make_database(tmp_path / "pings.db")
    csv_path = tmp_path / "private-1.csv"
    csv_path.write_text('name,value\n"sensor, one",café\n', encoding="utf-8")

    payload = remote(
        tmp_path,
        "preview",
        1,
        env_updates={"DATABASE_PATH": str(database)},
    )

    assert payload == {
        "ok": True,
        "upload_id": 1,
        "card_uuid": "CARD-0001",
        "filename": "logger-0001.csv",
        "records": [["name", "value"], ["sensor, one", "café"]],
        "truncated": False,
    }
    assert "stored_path" not in payload


def test_preview_is_capped_at_first_100_csv_records(tmp_path):
    database = make_database(tmp_path / "pings.db")
    csv_path = tmp_path / "private-1.csv"
    csv_path.write_text(
        "".join(f"{index},value-{index}\n" for index in range(105)),
        encoding="utf-8",
    )

    payload = remote(
        tmp_path,
        "preview",
        1,
        env_updates={"DATABASE_PATH": str(database)},
    )

    assert len(payload["records"]) == 100
    assert payload["records"][0] == ["0", "value-0"]
    assert payload["records"][-1] == ["99", "value-99"]
    assert payload["truncated"] is True


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", "Stored CSV file is empty."),
        (b"\xff,value\n", "Stored CSV file is not valid UTF-8."),
        (b'"unterminated,value\n', "Stored CSV file is malformed."),
    ],
)
def test_preview_reports_unreadable_csv_clearly(tmp_path, content, expected):
    database = make_database(tmp_path / "pings.db")
    (tmp_path / "private-1.csv").write_bytes(content)

    payload = remote(
        tmp_path,
        "preview",
        1,
        env_updates={"DATABASE_PATH": str(database)},
    )

    assert payload == {"ok": False, "error": expected}


def test_preview_reports_missing_file_and_unknown_upload(tmp_path):
    database = make_database(tmp_path / "pings.db")

    missing = remote(
        tmp_path,
        "preview",
        1,
        env_updates={"DATABASE_PATH": str(database)},
    )
    unknown = remote(
        tmp_path,
        "preview",
        999,
        env_updates={"DATABASE_PATH": str(database)},
    )

    assert missing == {"ok": False, "error": "Stored CSV file is missing."}
    assert unknown == {"ok": False, "error": "Upload was not found."}


def test_remote_reports_missing_and_invalid_database_without_traceback(tmp_path):
    missing = remote(
        tmp_path,
        "list",
        env_updates={"DATABASE_PATH": str(tmp_path / "missing.db")},
    )
    invalid_path = tmp_path / "invalid.db"
    invalid_path.write_text("not sqlite", encoding="utf-8")
    invalid = remote(
        tmp_path,
        "list",
        env_updates={"DATABASE_PATH": str(invalid_path)},
    )

    assert missing == {
        "ok": False,
        "error": "Railway database file was not found.",
    }
    assert invalid == {
        "ok": False,
        "error": "Railway database could not be read.",
    }


def completed(stdout: object = b'{"ok":true}', stderr: object = b"", code: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def test_fetch_remote_uses_subprocess_argv_without_a_shell(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(
            json.dumps(
                {
                    "ok": True,
                    "upload_id": 7,
                    "filename": "x.csv",
                    "records": [["x"]],
                    "truncated": False,
                }
            ).encode()
        )

    monkeypatch.setattr(viewer.subprocess, "run", fake_run)

    payload = viewer.fetch_remote("preview", 7)

    command, kwargs = calls[0]
    assert command[:2] == ["railway", "ssh"]
    assert len(command) == 3
    remote_parts = shlex.split(command[2])
    assert remote_parts[:2] == ["python3", "-c"]
    assert remote_parts[3:] == ["preview", "7"]
    prefix = 'import base64;exec(base64.b64decode("'
    suffix = '"))'
    assert remote_parts[2].startswith(prefix)
    assert remote_parts[2].endswith(suffix)
    encoded_script = remote_parts[2][len(prefix) : -len(suffix)]
    assert base64.b64decode(encoded_script).decode("utf-8") == viewer.REMOTE_PYTHON
    assert "shell" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == viewer.REMOTE_TIMEOUT_SECONDS
    assert payload["upload_id"] == 7


@pytest.mark.parametrize(
    ("action", "upload_id"),
    [
        ("delete", None),
        ("list; rm -rf data", None),
        ("list", 7),
        ("preview", None),
        ("preview", 0),
        ("preview", -1),
        ("preview", "7"),
        ("preview", True),
    ],
)
def test_fetch_remote_rejects_every_non_allowlisted_input(
    monkeypatch, action, upload_id
):
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return completed()

    monkeypatch.setattr(viewer.subprocess, "run", fake_run)

    with pytest.raises(ValueError):
        viewer.fetch_remote(action, upload_id)

    assert called is False


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FileNotFoundError(), "Railway CLI was not found"),
        (
            subprocess.TimeoutExpired(["railway"], 30),
            "Railway did not respond within 30 seconds",
        ),
    ],
)
def test_fetch_remote_reports_cli_start_failures(monkeypatch, failure, expected):
    def fake_run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(viewer.subprocess, "run", fake_run)

    with pytest.raises(viewer.ViewerError, match=expected):
        viewer.fetch_remote("list")


def test_fetch_remote_reports_nonzero_invalid_json_and_remote_errors(monkeypatch):
    responses = iter(
        [
            completed(stderr=b"not logged in\n", code=1),
            completed(stdout=b"not json"),
            completed(stdout=b'{"ok":false,"error":"Stored CSV file is missing."}'),
        ]
    )
    monkeypatch.setattr(viewer.subprocess, "run", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(viewer.ViewerError, match="not logged in"):
        viewer.fetch_remote("list")
    with pytest.raises(viewer.ViewerError, match="invalid response"):
        viewer.fetch_remote("list")
    with pytest.raises(viewer.ViewerError, match="Stored CSV file is missing"):
        viewer.fetch_remote("list")


@contextmanager
def running_server():
    server = viewer.build_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(base_url: str, path: str):
    try:
        response = urlopen(base_url + path, timeout=2)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read()


def test_server_binds_to_loopback_and_page_uses_safe_dom_rendering():
    with running_server() as (base_url, server):
        status, headers, body = request(base_url, "/")

    html = body.decode()
    assert server.server_address[0] == "127.0.0.1"
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
    assert "textContent" in html
    assert "innerHTML" not in html
    assert "Refresh" in html
    assert "Card UUID" in html
    assert "upload.card_uuid" in html
    assert "payload.card_uuid" in html


def test_json_routes_return_data_with_security_headers(monkeypatch):
    calls = []

    def fake_fetch(action, upload_id=None):
        calls.append((action, upload_id))
        if action == "list":
            return {"ok": True, "pings": [], "uploads": []}
        return {
            "ok": True,
            "upload_id": upload_id,
            "card_uuid": "CARD-0001",
            "filename": "x.csv",
            "records": [["x"]],
            "truncated": False,
        }

    monkeypatch.setattr(viewer, "fetch_remote", fake_fetch)
    with running_server() as (base_url, _server):
        data_status, data_headers, data_body = request(base_url, "/api/data")
        preview_status, preview_headers, preview_body = request(
            base_url, "/api/preview?id=7"
        )

    assert data_status == preview_status == 200
    assert data_headers["Content-Type"] == "application/json; charset=utf-8"
    assert preview_headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert json.loads(data_body) == {"ok": True, "pings": [], "uploads": []}
    assert json.loads(preview_body)["upload_id"] == 7
    assert calls == [("list", None), ("preview", 7)]


@pytest.mark.parametrize("query", ["", "?id=", "?id=0", "?id=-1", "?id=01", "?id=x", "?id=1&x=2"])
def test_preview_rejects_every_noncanonical_positive_id_without_railway(
    monkeypatch, query
):
    called = False

    def fake_fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(viewer, "fetch_remote", fake_fetch)
    with running_server() as (base_url, _server):
        status, _headers, body = request(base_url, "/api/preview" + query)

    assert status == 400
    assert json.loads(body) == {
        "ok": False,
        "error": "Upload id must be a positive integer.",
    }
    assert called is False


def test_remote_failure_does_not_crash_local_server(monkeypatch):
    calls = 0

    def flaky_fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise viewer.ViewerError("Railway project is not linked.")
        return {"ok": True, "pings": [], "uploads": []}

    monkeypatch.setattr(viewer, "fetch_remote", flaky_fetch)
    with running_server() as (base_url, _server):
        first_status, _headers, first_body = request(base_url, "/api/data")
        second_status, _headers, second_body = request(base_url, "/api/data")

    assert first_status == 502
    assert json.loads(first_body)["error"] == "Railway project is not linked."
    assert second_status == 200
    assert json.loads(second_body)["ok"] is True


def test_default_and_custom_ports():
    assert viewer.parse_args([]).port == 8765
    assert viewer.parse_args(["--port", "9000"]).port == 9000
    with pytest.raises(SystemExit):
        viewer.parse_args(["--port", "0"])
