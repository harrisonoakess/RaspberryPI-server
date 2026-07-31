"""Uploader tests.

Phase 1 criteria that carry over: disconnected behavior, connected soak,
disconnect/reconnect, and failure log rate limiting.
Phase 3 criteria: 7 (nested per-card queue), 10 (acknowledgement safety,
including the card_uuid check that gates deleting a local copy), and 12 (the
Phase 2 delivery failures, now per card).
"""

import json
import logging
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import uploader
from state import STATUS_PENDING, STATUS_UPLOADED, Ledger
from uploader import (
    CATEGORY_ACKNOWLEDGED,
    Config,
    ConfigError,
    IntervalTimer,
    PingError,
    StateLogger,
    UploadError,
    build_multipart,
    is_safe_card_uuid,
    is_safe_filename,
    is_wifi_connected,
    poll_once,
    queued_entries,
    resolve_device_id,
    run,
    scan_pending,
    send_ping,
    send_upload,
    upload_pending,
)

# A queue path that does not exist: the ping-focused tests must not depend on
# any real queue directory being present on the machine running them.
NO_QUEUE = Path("/nonexistent-piuploader-queue")

CARD_A = "1234-ABCD"
CARD_B = "5678-EF01"

BASE_CONFIG = Config(
    server_url="https://example.up.railway.app",
    api_key="secret-key",
    wifi_interface="wlan0",
    poll_interval_seconds=30.0,
    ping_interval_seconds=300.0,
    request_timeout_seconds=10.0,
    upload_timeout_seconds=120.0,
    error_log_repeat_seconds=300.0,
    queue_path=NO_QUEUE,
    state_db_path=NO_QUEUE / "state.db",
)

# Phase 1 sent a heartbeat on every 30-second poll. Phase 2 moves it to its own
# five-minute interval, so tests about per-tick pinging say so explicitly.
PING_EVERY_TICK = replace(BASE_CONFIG, ping_interval_seconds=30.0)

# Never opened: these tests queue nothing, so no ledger row is ever written.
IDLE_LEDGER = Ledger(NO_QUEUE / "state.db")


class FakeClock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeStopEvent(threading.Event):
    """A stop event whose wait() advances the fake clock instead of sleeping."""

    def __init__(self, clock):
        super().__init__()
        self._clock = clock

    def wait(self, timeout=None):
        self._clock.advance(timeout or 0.0)
        return self.is_set()


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """Stands in for the urllib opener; records every request it is given."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        outcome = self.responses.pop(0) if self.responses else ok_response()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def upload_requests(self):
        return [entry for entry in self.requests if entry[0].full_url.endswith("/upload")]

    @property
    def ping_requests(self):
        return [entry for entry in self.requests if entry[0].full_url.endswith("/ping")]


def ok_response(device_id="raspberrypi-uploader", received_at="2026-07-28T18:30:01Z"):
    return FakeResponse(
        200,
        json.dumps(
            {
                "status": "acknowledged",
                "device_id": device_id,
                "received_at": received_at,
            }
        ).encode(),
    )


def upload_ack(
    filename,
    size,
    device_id="pi",
    card_uuid=CARD_A,
    status="stored",
    received_at="2026-07-29T18:30:01Z",
    code=200,
):
    return FakeResponse(
        code,
        json.dumps(
            {
                "status": status,
                "device_id": device_id,
                "card_uuid": card_uuid,
                "filename": filename,
                "size": size,
                "received_at": received_at,
            }
        ).encode(),
    )


def http_error(code, url="https://example.up.railway.app/ping"):
    return urllib.error.HTTPError(url, code, "err", {}, None)


UPLOAD_URL = "https://example.up.railway.app/upload"


def parse_multipart(body, content_type):
    """Minimal multipart reader, enough to assert the request shape."""
    boundary = content_type.split("boundary=", 1)[1]
    fields = {}
    for chunk in body.split(f"--{boundary}".encode()):
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        match = re.search(rb'name="([^"]+)"', head)
        if match is None:  # pragma: no cover - defensive
            continue
        fields[match.group(1).decode()] = data[:-2] if data.endswith(b"\r\n") else data
    return fields


def always_ping(clock=time.monotonic):
    """A heartbeat timer that is due on every tick."""
    return IntervalTimer(0.0, clock=clock)


@pytest.fixture
def caplog_logger(caplog):
    logger = logging.getLogger("piuploader.test")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="piuploader.test")
    return logger


@pytest.fixture
def queue_config(tmp_path):
    """A config with a real, empty queue directory and ledger path."""
    config = replace(
        BASE_CONFIG,
        queue_path=tmp_path / "queue",
        state_db_path=tmp_path / "state.db",
    )
    config.pending_dir.mkdir(parents=True)
    return config


@pytest.fixture
def ledger(queue_config):
    instance = Ledger(queue_config.state_db_path)
    instance.initialize()
    return instance


def enqueue(config, ledger, name, content=b"a,b\n1,2\n", card_uuid=CARD_A):
    """Queue one file exactly the way the watcher does: pending/<card>/<name>."""
    directory = config.pending_dir_for(card_uuid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
    ledger.record_pending(card_uuid, name)
    return content


# --- Configuration ------------------------------------------------------------


def test_config_from_env_applies_documented_defaults():
    config = Config.from_env({"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k"})

    assert config.wifi_interface == "wlan0"
    assert config.poll_interval_seconds == 30.0
    assert config.ping_interval_seconds == 300.0
    assert config.request_timeout_seconds == 10.0
    assert config.upload_timeout_seconds == 120.0
    assert config.error_log_repeat_seconds == 300.0
    assert config.max_upload_bytes == 20_971_520
    assert config.queue_path == Path("/var/lib/piuploader/queue")
    assert config.state_db_path == Path("/var/lib/piuploader/state.db")
    assert config.ping_url == "https://x.up.railway.app/ping"
    assert config.upload_url == "https://x.up.railway.app/upload"


def test_config_urls_tolerate_a_trailing_slash():
    config = Config.from_env({"SERVER_URL": "https://x.up.railway.app/", "API_KEY": "k"})
    assert config.ping_url == "https://x.up.railway.app/ping"
    assert config.upload_url == "https://x.up.railway.app/upload"


def test_pending_dir_is_a_subdirectory_of_the_queue():
    assert BASE_CONFIG.pending_dir == NO_QUEUE / "pending"


@pytest.mark.parametrize(
    "env",
    [
        {"API_KEY": "k"},
        {"SERVER_URL": "https://x.up.railway.app"},
        {"SERVER_URL": "", "API_KEY": "k"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "  "},
        # Credentials must never travel over plaintext to a remote host.
        {"SERVER_URL": "http://example.com", "API_KEY": "k"},
        {"SERVER_URL": "ftp://example.com", "API_KEY": "k"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k", "POLL_INTERVAL_SECONDS": "0"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k", "POLL_INTERVAL_SECONDS": "-5"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k", "REQUEST_TIMEOUT_SECONDS": "abc"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k", "PING_INTERVAL_SECONDS": "0"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k", "UPLOAD_TIMEOUT_SECONDS": "0"},
        {"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k", "MAX_UPLOAD_BYTES": "-1"},
    ],
)
def test_invalid_config_is_rejected(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_http_localhost_is_allowed_for_local_development():
    config = Config.from_env({"SERVER_URL": "http://localhost:8000", "API_KEY": "k"})
    assert config.ping_url == "http://localhost:8000/ping"


@pytest.mark.parametrize(
    "hostname,expected",
    [
        ("raspberrypi-uploader", "raspberrypi-uploader"),
        ("pi01.local", "pi01"),
        ("  pi-3b-plus  ", "pi-3b-plus"),
    ],
)
def test_device_id_derived_from_hostname(hostname, expected):
    assert resolve_device_id(hostname) == expected


@pytest.mark.parametrize("hostname", ["", "-bad", "bad-", "under_score", "a" * 64, "."])
def test_invalid_hostname_is_a_config_error(hostname):
    with pytest.raises(ConfigError):
        resolve_device_id(hostname)


# --- Filename rules (must match the server) -----------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "logger-0001.csv",
        "a.csv",
        ".csv",
        "UPPER.CSV",
        "mixed.Csv",
        "with space.csv",
        "with-dash_and.dots.csv",
        "unicode-ü.csv",
        "a" * 251 + ".csv",
    ],
)
def test_safe_filenames_are_accepted(name):
    assert is_safe_filename(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "no-extension",
        "data.txt",
        "data.csv.gz",
        "sub/dir.csv",
        "sub\\dir.csv",
        "../escape.csv",
        "/absolute.csv",
        "trailing/",
        "nul\x00.csv",
        "newline\n.csv",
        "carriage\r.csv",
        "tab\t.csv",
        "bell\x07.csv",
        "a" * 252 + ".csv",
        # 255 characters but more than 255 UTF-8 bytes.
        "ü" * 251 + ".csv",
    ],
)
def test_unsafe_or_out_of_scope_filenames_are_rejected(name):
    assert is_safe_filename(name) is False


def test_pi_and_server_filename_rules_agree():
    """The Pi must never queue or send a name the server would reject."""
    from main import validate_filename

    accepted = ["logger-0001.csv", ".csv", "UPPER.CSV", "with space.csv", "unicode-ü.csv"]
    rejected = ["..", "data.txt", "sub/dir.csv", "nul\x00.csv", "newline\n.csv", "a" * 252 + ".csv"]

    for name in accepted:
        assert is_safe_filename(name) is True
        assert validate_filename(name) == name
    for name in rejected:
        assert is_safe_filename(name) is False
        with pytest.raises(ValueError):
            validate_filename(name)


# --- WiFi detection -----------------------------------------------------------


def _runner(stdout="", returncode=0, raises=None):
    def run_command(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args[0], returncode, stdout, "")

    return run_command


def test_wifi_connected_when_iw_reports_association():
    runner = _runner("Connected to b8:27:eb:00:00:00 (on wlan0)\n\tSSID: cafe\n")
    assert is_wifi_connected("wlan0", runner=runner) is True


def test_wifi_disconnected_when_iw_reports_not_connected():
    assert is_wifi_connected("wlan0", runner=_runner("Not connected.\n")) is False


def test_wifi_falls_back_to_operstate_when_iw_is_missing(tmp_path):
    (tmp_path / "wlan0").mkdir()
    (tmp_path / "wlan0" / "operstate").write_text("up\n")

    connected = is_wifi_connected(
        "wlan0", runner=_runner(raises=FileNotFoundError("iw")), sysfs_root=tmp_path
    )
    assert connected is True


def test_wifi_operstate_down_reads_as_disconnected(tmp_path):
    (tmp_path / "wlan0").mkdir()
    (tmp_path / "wlan0" / "operstate").write_text("down\n")

    connected = is_wifi_connected(
        "wlan0", runner=_runner(returncode=237), sysfs_root=tmp_path
    )
    assert connected is False


def test_unknown_interface_raises_wifi_check_error(tmp_path):
    with pytest.raises(uploader.WifiCheckError):
        is_wifi_connected(
            "wlan9", runner=_runner(raises=FileNotFoundError("iw")), sysfs_root=tmp_path
        )


# --- Ping request shape and failure categories --------------------------------


def test_ping_sends_documented_request():
    opener = FakeOpener()
    send_ping(BASE_CONFIG, "raspberrypi-uploader", "2026-07-28T18:30:00Z", opener=opener)

    request, timeout = opener.requests[0]
    assert request.full_url == "https://example.up.railway.app/ping"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret-key"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {
        "device_id": "raspberrypi-uploader",
        "sent_at": "2026-07-28T18:30:00Z",
    }
    assert timeout == 10.0


@pytest.mark.parametrize(
    "outcome,category",
    [
        (http_error(301), "redirect_rejected"),
        (http_error(302), "redirect_rejected"),
        (http_error(401), "auth_rejected"),
        (http_error(422), "request_rejected"),
        (http_error(400), "request_rejected"),
        (http_error(500), "server_error"),
        (http_error(503), "server_error"),
        (http_error(418), "unexpected_status_418"),
        (urllib.error.URLError(socket.gaierror("Name or service not known")), "dns_error"),
        (urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), "tls_error"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError("refused")), "connection_error"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionResetError("reset"), "connection_error"),
        (FakeResponse(200, b"not json"), "invalid_ack"),
        (FakeResponse(200, json.dumps({"status": "nope"}).encode()), "invalid_ack"),
        (FakeResponse(204, b""), "unexpected_status_204"),
    ],
)
def test_ping_failures_map_to_stable_categories(outcome, category):
    opener = FakeOpener([outcome])
    with pytest.raises(PingError) as excinfo:
        send_ping(BASE_CONFIG, "pi", "2026-07-28T18:30:00Z", opener=opener)
    assert excinfo.value.category == category


def test_ping_error_messages_never_contain_the_api_key():
    for outcome in (http_error(401), urllib.error.URLError(socket.gaierror("boom"))):
        with pytest.raises(PingError) as excinfo:
            send_ping(BASE_CONFIG, "pi", "2026-07-28T18:30:00Z", opener=FakeOpener([outcome]))
        assert BASE_CONFIG.api_key not in str(excinfo.value)


# --- Criterion 4: the POST /upload request ------------------------------------


def test_multipart_body_carries_the_four_documented_fields():
    body, content_type = build_multipart("pi", CARD_A, "logger-0001.csv", b"a,b\n1,2\n")

    assert content_type.startswith("multipart/form-data; boundary=")
    assert parse_multipart(body, content_type) == {
        "device_id": b"pi",
        "card_uuid": CARD_A.encode(),
        "filename": b"logger-0001.csv",
        "file": b"a,b\n1,2\n",
    }


def test_multipart_boundary_is_redrawn_when_it_occurs_in_the_content(monkeypatch):
    """A CSV that happens to contain the boundary must not terminate the part."""
    real_token_hex = uploader.secrets.token_hex
    drawn = []

    def scripted_token_hex(size):
        value = "00" * size if not drawn else real_token_hex(size)
        drawn.append(value)
        return value

    monkeypatch.setattr(uploader.secrets, "token_hex", scripted_token_hex)
    collision = b"x----piuploader" + b"00" * 16 + b"y"
    body, content_type = build_multipart("pi", CARD_A, "a.csv", collision)

    assert len(drawn) == 2  # the first boundary collided, so a second was drawn
    assert parse_multipart(body, content_type)["file"] == collision


def test_multipart_file_part_uses_a_fixed_informational_filename():
    """The authoritative name is the `filename` field, so no real name is ever
    interpolated into a header where it could forge one."""
    body, _ = build_multipart("pi", CARD_A, 'quote".csv', b"payload")
    headers = body.split(b"\r\n\r\n")[0]

    assert b'filename="upload.csv"' in body
    assert b'quote".csv' not in headers


def test_upload_sends_the_documented_request():
    opener = FakeOpener([upload_ack("logger-0001.csv", 8)])
    send_upload(BASE_CONFIG, "pi", CARD_A, "logger-0001.csv", b"a,b\n1,2\n", opener=opener)

    request, timeout = opener.requests[0]
    assert request.full_url == UPLOAD_URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret-key"
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert request.get_header("Accept") == "application/json"
    # Uploads get the longer timeout, not the ping timeout.
    assert timeout == 120.0


def test_upload_accepts_an_empty_file():
    opener = FakeOpener([upload_ack("empty.csv", 0)])
    assert send_upload(BASE_CONFIG, "pi", CARD_A, "empty.csv", b"", opener=opener)["status"] == "stored"


def test_upload_accepts_a_file_exactly_at_the_limit():
    content = b"x" * BASE_CONFIG.max_upload_bytes
    opener = FakeOpener([upload_ack("big.csv", len(content))])
    assert send_upload(BASE_CONFIG, "pi", CARD_A, "big.csv", content, opener=opener)["status"] == "stored"


def test_upload_accepts_already_stored_as_success():
    opener = FakeOpener([upload_ack("a.csv", 3, status="already_stored")])
    acknowledgement = send_upload(BASE_CONFIG, "pi", CARD_A, "a.csv", b"a,b", opener=opener)
    assert acknowledgement["status"] == "already_stored"


@pytest.mark.parametrize(
    "outcome,category",
    [
        (http_error(301, UPLOAD_URL), "redirect_rejected"),
        (http_error(401, UPLOAD_URL), "auth_rejected"),
        (http_error(413, UPLOAD_URL), "too_large"),
        (http_error(422, UPLOAD_URL), "request_rejected"),
        (http_error(400, UPLOAD_URL), "request_rejected"),
        (http_error(500, UPLOAD_URL), "server_error"),
        (http_error(503, UPLOAD_URL), "server_error"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError(socket.gaierror("nope")), "dns_error"),
        (urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), "tls_error"),
        (TimeoutError("timed out"), "timeout"),
        # A 200 is not enough: the acknowledgement itself must be valid.
        (FakeResponse(200, b"not json"), "invalid_ack"),
        (FakeResponse(200, json.dumps([]).encode()), "invalid_ack"),
        (FakeResponse(204, b""), "unexpected_status_204"),
    ],
)
def test_upload_failures_map_to_stable_categories(outcome, category):
    opener = FakeOpener([outcome])
    with pytest.raises(UploadError) as excinfo:
        send_upload(BASE_CONFIG, "pi", CARD_A, "a.csv", b"a,b", opener=opener)
    assert excinfo.value.category == category


def ack_payload(**overrides):
    payload = {
        "status": "stored",
        "device_id": "pi",
        "card_uuid": CARD_A,
        "filename": "a.csv",
        "size": 3,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


@pytest.mark.parametrize(
    "payload,category",
    [
        (ack_payload(status="queued"), "invalid_ack"),
        (ack_payload(device_id="other"), "mismatched_ack"),
        (ack_payload(filename="b.csv"), "mismatched_ack"),
        (ack_payload(size=4), "mismatched_ack"),
        (ack_payload(size=None), "mismatched_ack"),
        # Criterion 10: the acknowledgement has to name the card that was sent.
        (ack_payload(card_uuid=None), "mismatched_ack"),
        (ack_payload(card_uuid=CARD_B), "mismatched_ack"),
        (ack_payload(card_uuid=CARD_A.lower()), "mismatched_ack"),
    ],
)
def test_upload_rejects_an_acknowledgement_that_does_not_match_the_request(payload, category):
    opener = FakeOpener([FakeResponse(200, json.dumps(payload).encode())])
    with pytest.raises(UploadError) as excinfo:
        send_upload(BASE_CONFIG, "pi", CARD_A, "a.csv", b"a,b", opener=opener)
    assert excinfo.value.category == category


def test_upload_refuses_an_unsafe_filename_without_sending_anything():
    opener = FakeOpener()
    with pytest.raises(UploadError) as excinfo:
        send_upload(BASE_CONFIG, "pi", CARD_A, "../escape.csv", b"a", opener=opener)

    assert excinfo.value.category == "unsafe_filename"
    assert opener.requests == []


def test_upload_refuses_an_oversized_file_without_sending_anything():
    opener = FakeOpener()
    content = b"x" * (BASE_CONFIG.max_upload_bytes + 1)
    with pytest.raises(UploadError) as excinfo:
        send_upload(BASE_CONFIG, "pi", CARD_A, "big.csv", content, opener=opener)

    assert excinfo.value.category == "too_large"
    assert opener.requests == []


def test_upload_error_messages_never_contain_the_api_key():
    with pytest.raises(UploadError) as excinfo:
        send_upload(
            BASE_CONFIG,
            "pi",
            CARD_A,
            "a.csv",
            b"a",
            opener=FakeOpener([http_error(401, UPLOAD_URL)]),
        )
    assert BASE_CONFIG.api_key not in str(excinfo.value)


# --- Criterion 4: queue draining ----------------------------------------------


def test_queued_filenames_is_empty_when_the_queue_does_not_exist():
    assert queued_entries(BASE_CONFIG) == []


def test_successful_upload_marks_the_ledger_and_clears_the_queued_copy(
    queue_config, ledger, caplog_logger
):
    content = enqueue(queue_config, ledger, "logger-0001.csv")
    opener = FakeOpener([upload_ack("logger-0001.csv", len(content))])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.attempted, batch.uploaded, batch.failed) == (1, 1, 0)
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_UPLOADED
    assert not (queue_config.pending_dir_for(CARD_A) / "logger-0001.csv").exists()


def test_already_stored_also_clears_the_queued_copy(queue_config, ledger, caplog_logger):
    """A retry after a lost acknowledgement must still finish the file locally."""
    content = enqueue(queue_config, ledger, "a.csv")
    opener = FakeOpener([upload_ack("a.csv", len(content), status="already_stored")])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.uploaded, batch.already_stored) == (0, 1)
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_UPLOADED
    assert not (queue_config.pending_dir_for(CARD_A) / "a.csv").exists()


def test_files_are_sent_with_their_exact_bytes_and_names(queue_config, ledger, caplog_logger):
    enqueue(queue_config, ledger, "logger-0002.csv", b"sensor,value\r\n1,2\r\n")
    opener = FakeOpener([upload_ack("logger-0002.csv", 19)])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    upload_pending(queue_config, "pi", ledger, state, opener=opener)

    request, _ = opener.requests[0]
    fields = parse_multipart(request.data, request.get_header("Content-type"))
    assert fields == {
        "device_id": b"pi",
        "card_uuid": CARD_A.encode(),
        "filename": b"logger-0002.csv",
        "file": b"sensor,value\r\n1,2\r\n",
    }


def test_an_empty_queued_file_is_uploaded(queue_config, ledger, caplog_logger):
    enqueue(queue_config, ledger, "empty.csv", b"")
    opener = FakeOpener([upload_ack("empty.csv", 0)])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert batch.uploaded == 1
    assert ledger.status_of(CARD_A, "empty.csv") == STATUS_UPLOADED


@pytest.mark.parametrize(
    "outcome",
    [
        http_error(503, UPLOAD_URL),
        http_error(500, UPLOAD_URL),
        http_error(301, UPLOAD_URL),
        http_error(401, UPLOAD_URL),
        urllib.error.URLError(TimeoutError("timed out")),
        # Malformed and mismatched acknowledgements are failures, not successes.
        FakeResponse(200, b"not json"),
        FakeResponse(
            200,
            json.dumps(
                {"status": "stored", "device_id": "pi", "filename": "a.csv", "size": 99}
            ).encode(),
        ),
        FakeResponse(
            200,
            json.dumps(
                {"status": "stored", "device_id": "other", "filename": "a.csv", "size": 8}
            ).encode(),
        ),
    ],
)
def test_a_failed_upload_leaves_the_file_pending_for_retry(
    queue_config, ledger, caplog_logger, outcome
):
    enqueue(queue_config, ledger, "a.csv")
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=FakeOpener([outcome]))

    assert batch.uploaded == 0
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_PENDING
    assert (queue_config.pending_dir_for(CARD_A) / "a.csv").exists()


def test_one_failure_does_not_stop_the_files_after_it(queue_config, ledger, caplog_logger):
    for name in ("a.csv", "b.csv", "c.csv"):
        enqueue(queue_config, ledger, name, b"12345")

    opener = FakeOpener(
        [upload_ack("a.csv", 5), http_error(503, UPLOAD_URL), upload_ack("c.csv", 5)]
    )
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.attempted, batch.uploaded, batch.failed) == (3, 2, 1)
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_UPLOADED
    assert ledger.status_of(CARD_A, "b.csv") == STATUS_PENDING
    assert ledger.status_of(CARD_A, "c.csv") == STATUS_UPLOADED
    assert (queue_config.pending_dir_for(CARD_A) / "b.csv").exists()


def test_a_retry_on_a_later_tick_delivers_the_file_that_failed(
    queue_config, ledger, caplog_logger
):
    enqueue(queue_config, ledger, "a.csv", b"12345")
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    upload_pending(
        queue_config, "pi", ledger, state, opener=FakeOpener([http_error(503, UPLOAD_URL)])
    )
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_PENDING

    upload_pending(queue_config, "pi", ledger, state, opener=FakeOpener([upload_ack("a.csv", 5)]))

    assert ledger.status_of(CARD_A, "a.csv") == STATUS_UPLOADED
    assert queued_entries(queue_config) == []


def test_an_unsafe_queued_name_is_skipped_and_never_sent(queue_config, ledger, caplog_logger):
    queue_config.pending_dir_for(CARD_A).mkdir(parents=True)
    (queue_config.pending_dir_for(CARD_A) / "notes.txt").write_bytes(b"x")
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.attempted, batch.rejected) == (0, 1)
    assert opener.requests == []
    # Left in place: deleting data is worse than a repeated log line.
    assert (queue_config.pending_dir_for(CARD_A) / "notes.txt").exists()


def test_an_empty_queue_makes_no_requests(queue_config, ledger, caplog_logger):
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert batch.attempted == 0
    assert opener.requests == []


def test_uploads_are_attempted_in_a_stable_name_order(queue_config, ledger, caplog_logger):
    for name in ("c.csv", "a.csv", "b.csv"):
        enqueue(queue_config, ledger, name, b"1")

    opener = FakeOpener([upload_ack(name, 1) for name in ("a.csv", "b.csv", "c.csv")])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    upload_pending(queue_config, "pi", ledger, state, opener=opener)

    sent = [
        parse_multipart(request.data, request.get_header("Content-type"))["filename"]
        for request, _ in opener.requests
    ]
    assert sent == [b"a.csv", b"b.csv", b"c.csv"]


# --- Rate-limited logging -----------------------------------------------------


def test_first_occurrence_logs_immediately(caplog_logger, caplog):
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    assert state.record("dns_error", "first failure") is True
    assert "first failure" in caplog.text


def test_same_category_is_suppressed_for_the_repeat_window(caplog_logger):
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    state.record("dns_error", "failure")

    # Ten more polls across just under five minutes -> none logged.
    for _ in range(9):
        clock.advance(30.0)
        assert state.record("dns_error", "failure") is False

    clock.advance(30.0)  # now at 300s
    assert state.record("dns_error", "failure") is True


def test_different_category_logs_immediately(caplog_logger):
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    state.record("dns_error", "failure")

    clock.advance(30.0)
    assert state.record("server_error", "different failure") is True


def test_recovery_logs_immediately(caplog_logger):
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    state.record("dns_error", "failure")

    clock.advance(30.0)
    assert state.record(CATEGORY_ACKNOWLEDGED, "recovered", always=True) is True


def test_every_acknowledgement_is_logged_even_when_unchanged(caplog_logger):
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    for _ in range(6):
        assert state.record(CATEGORY_ACKNOWLEDGED, "ack", always=True) is True
        clock.advance(30.0)


def test_suppressed_count_is_reported_on_the_next_emitted_line(caplog_logger, caplog):
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    state.record("dns_error", "failure")
    for _ in range(9):
        clock.advance(30.0)
        state.record("dns_error", "failure")

    caplog.clear()
    clock.advance(30.0)
    state.record("dns_error", "failure")
    assert "suppressed 9 identical entries" in caplog.text


def test_upload_and_ping_failures_use_separate_categories(
    queue_config, ledger, caplog_logger, caplog
):
    """A failing upload must not suppress the ping failure line, or vice versa."""
    enqueue(queue_config, ledger, "a.csv")
    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    opener = FakeOpener([http_error(503), http_error(503, UPLOAD_URL)])

    poll_once(
        queue_config,
        "pi",
        state,
        ledger,
        always_ping(clock),
        opener=opener,
        wifi_check=lambda _: True,
    )

    assert any("ping failed [server_error]" in r.message for r in caplog.records)
    assert any(
        f"upload of '{CARD_A}/a.csv' failed [server_error]" in r.message
        for r in caplog.records
    )


# --- Heartbeat cadence --------------------------------------------------------


def test_interval_timer_fires_immediately_then_once_per_interval():
    clock = FakeClock()
    timer = IntervalTimer(300.0, clock=clock)

    assert timer.due() is True
    timer.record()

    clock.advance(299.0)
    assert timer.due() is False
    clock.advance(1.0)
    assert timer.due() is True


def test_heartbeat_uses_its_own_five_minute_interval_inside_the_poll_loop(caplog_logger):
    """Six 30-second polls is three minutes, so only the first ping is due."""
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    run(
        BASE_CONFIG,
        "pi",
        state,
        IDLE_LEDGER,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _: True,
        max_iterations=6,
        clock=clock,
    )

    assert len(opener.ping_requests) == 1


def test_heartbeat_repeats_once_the_interval_elapses(caplog_logger):
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    # 21 polls spans 600 seconds, so pings are due at t=0, t=300, and t=600.
    run(
        BASE_CONFIG,
        "pi",
        state,
        IDLE_LEDGER,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _: True,
        max_iterations=21,
        clock=clock,
    )

    assert len(opener.ping_requests) == 3


def test_a_failed_heartbeat_is_not_retried_before_its_interval(caplog_logger):
    clock = FakeClock()
    opener = FakeOpener([http_error(503)])
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    run(
        BASE_CONFIG,
        "pi",
        state,
        IDLE_LEDGER,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _: True,
        max_iterations=6,
        clock=clock,
    )

    assert len(opener.ping_requests) == 1


# --- Disconnected behavior ----------------------------------------------------


def test_disconnected_poll_makes_no_request_of_either_kind(queue_config, ledger, caplog_logger):
    enqueue(queue_config, ledger, "a.csv")
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    poll_once(
        queue_config,
        "pi",
        state,
        ledger,
        always_ping(),
        opener=opener,
        wifi_check=lambda _: False,
    )

    assert opener.requests == []
    # Criterion 3: queued while offline, still queued, still pending.
    assert ledger.status_of(CARD_A, "a.csv") == STATUS_PENDING
    assert (queue_config.pending_dir_for(CARD_A) / "a.csv").exists()


def test_three_minutes_disconnected_logs_once_and_sends_nothing(caplog_logger, caplog):
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    # Six 30-second polls = three minutes, inside the five-minute repeat window.
    iterations = run(
        BASE_CONFIG,
        "pi",
        state,
        IDLE_LEDGER,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _: False,
        max_iterations=6,
        clock=clock,
    )

    assert iterations == 6
    assert opener.requests == []
    disconnected_lines = [r for r in caplog.records if "not connected" in r.message]
    assert len(disconnected_lines) == 1


def test_wifi_check_failure_does_not_crash_the_loop(caplog_logger):
    def broken(_interface):
        raise uploader.WifiCheckError("no such interface")

    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())
    opener = FakeOpener()

    poll_once(
        BASE_CONFIG, "pi", state, IDLE_LEDGER, always_ping(), opener=opener, wifi_check=broken
    )

    assert opener.requests == []


def test_unexpected_ping_exception_does_not_crash_the_loop(caplog_logger, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("something unforeseen")

    monkeypatch.setattr(uploader, "send_ping", explode)
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    poll_once(BASE_CONFIG, "pi", state, IDLE_LEDGER, always_ping(), wifi_check=lambda _: True)


def test_unexpected_upload_exception_does_not_crash_the_loop(
    queue_config, ledger, caplog_logger, monkeypatch
):
    def explode(*args, **kwargs):
        raise ValueError("something unforeseen")

    enqueue(queue_config, ledger, "a.csv")
    monkeypatch.setattr(uploader, "send_upload", explode)
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    poll_once(
        queue_config,
        "pi",
        state,
        ledger,
        always_ping(),
        opener=FakeOpener(),
        wifi_check=lambda _: True,
    )

    assert ledger.status_of(CARD_A, "a.csv") == STATUS_PENDING
    assert (queue_config.pending_dir_for(CARD_A) / "a.csv").exists()


# --- Criterion 4: reconnect drains the queue within one poll interval ---------


def test_reconnecting_uploads_the_queue_on_the_next_poll(queue_config, ledger, caplog_logger):
    for name in ("a.csv", "b.csv"):
        enqueue(queue_config, ledger, name, b"12345")

    clock = FakeClock()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    opener = FakeOpener([ok_response(), upload_ack("a.csv", 5), upload_ack("b.csv", 5)])
    online = [False, True]

    iterations = run(
        queue_config,
        "pi",
        state,
        ledger,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _iface: online.pop(0),
        max_iterations=2,
        clock=clock,
    )

    assert iterations == 2
    # Nothing was sent on the offline tick; both files went out on the next one.
    assert len(opener.upload_requests) == 2
    assert queued_entries(queue_config) == []
    assert ledger.filenames_with_status(CARD_A, STATUS_UPLOADED) == ["a.csv", "b.csv"]


def test_connected_soak_pings_every_tick_when_the_intervals_match(caplog_logger, caplog):
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    run(
        PING_EVERY_TICK,
        "pi",
        state,
        IDLE_LEDGER,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _: True,
        max_iterations=6,
        clock=clock,
    )

    assert len(opener.ping_requests) == 6
    acks = [r for r in caplog.records if "ping acknowledged" in r.message]
    assert len(acks) == 6


def test_polls_do_not_overlap_and_hold_the_configured_cadence():
    """The loop is sequential, so a slow tick delays the next one instead of
    running two requests at once."""
    clock = FakeClock()
    tick_starts = []

    def slow_wifi_check(_interface):
        tick_starts.append(clock.now)
        clock.advance(5.0)  # request takes 5 seconds
        return True

    state = StateLogger(logging.getLogger("piuploader.test"), 300.0, clock=clock)

    run(
        BASE_CONFIG,
        "pi",
        state,
        IDLE_LEDGER,
        FakeStopEvent(clock),
        opener=FakeOpener(),
        wifi_check=slow_wifi_check,
        max_iterations=4,
        clock=clock,
    )

    assert tick_starts == [0.0, 30.0, 60.0, 90.0]


def test_stop_event_ends_the_loop_between_polls():
    clock = FakeClock()
    stop_event = threading.Event()
    stop_event.set()

    iterations = run(
        BASE_CONFIG,
        "pi",
        StateLogger(logging.getLogger("piuploader.test"), 300.0, clock=clock),
        IDLE_LEDGER,
        stop_event,
        opener=FakeOpener(),
        wifi_check=lambda _: True,
        clock=clock,
    )

    assert iterations == 0


# --- Disconnect and reconnect -------------------------------------------------


def test_disconnect_reconnect_cycles_produce_expected_states(caplog_logger, caplog):
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    # Three cycles of connected -> disconnected -> connected.
    schedule = [True, False, True, False, True, False, True]
    remaining = list(schedule)

    for _ in schedule:
        poll_once(
            BASE_CONFIG,
            "pi",
            state,
            IDLE_LEDGER,
            always_ping(clock),
            opener=opener,
            wifi_check=lambda _iface: remaining.pop(0),
        )
        clock.advance(30.0)

    # A request is attempted on every connected poll and none while disconnected.
    assert len(opener.requests) == schedule.count(True)

    acks = [r for r in caplog.records if "ping acknowledged" in r.message]
    disconnects = [r for r in caplog.records if "not connected" in r.message]
    # Every acknowledgement is logged; each disconnect is a state change, so it
    # is logged immediately too.
    assert len(acks) == 4
    assert len(disconnects) == 3


def test_ping_resumes_after_a_failure_and_logs_the_recovery(caplog_logger, caplog):
    clock = FakeClock()
    opener = FakeOpener([http_error(503), http_error(503), ok_response()])
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    for _ in range(3):
        poll_once(
            BASE_CONFIG,
            "pi",
            state,
            IDLE_LEDGER,
            always_ping(clock),
            opener=opener,
            wifi_check=lambda _: True,
        )
        clock.advance(30.0)

    failures = [r for r in caplog.records if "[server_error]" in r.message]
    acks = [r for r in caplog.records if "ping acknowledged" in r.message]
    assert len(failures) == 1  # second identical failure suppressed
    assert len(acks) == 1  # recovery logged immediately


# --- Timestamps ---------------------------------------------------------------


def test_sent_at_is_rfc3339_utc_and_matches_the_server_contract():
    from main import RFC3339_UTC_PATTERN

    moment = datetime(2026, 7, 28, 18, 30, 0, 987654, tzinfo=timezone.utc)
    rendered = uploader.rfc3339_utc(moment)

    assert rendered == "2026-07-28T18:30:00Z"
    assert RFC3339_UTC_PATTERN.match(rendered)


# --- card_uuid rules (must match the server) ----------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "A",
        "1234-ABCD",
        "a" * 64,
        "11111111-2222-3333-4444-555555555555",
        "ABCD1234",
        "-leading-hyphen",
    ],
)
def test_safe_card_uuids_are_accepted(value):
    assert is_safe_card_uuid(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 65,
        "has space",
        "has/slash",
        "has\\backslash",
        "under_score",
        "dot.separated",
        "nul\x00",
        "newline\n",
        # `$` would match before a final newline; the rule must not.
        "1234-ABCD\n",
        "tab\t",
        "unicode-ü",
        "..",
        ".",
        7,
        None,
    ],
)
def test_unsafe_card_uuids_are_rejected(value):
    assert is_safe_card_uuid(value) is False


def test_pi_and_server_card_uuid_rules_agree():
    """The Pi must never send a card_uuid the server would reject."""
    from main import validate_card_uuid

    accepted = ["A", "1234-ABCD", "a" * 64, "11111111-2222-3333-4444-555555555555"]
    rejected = ["", "a" * 65, "has/slash", "under_score", "1234-ABCD\n", ".."]

    for value in accepted:
        assert is_safe_card_uuid(value) is True
        assert validate_card_uuid(value) == value
    for value in rejected:
        assert is_safe_card_uuid(value) is False
        with pytest.raises(ValueError):
            validate_card_uuid(value)


def test_upload_refuses_an_unsafe_card_uuid_without_sending_anything():
    opener = FakeOpener()
    with pytest.raises(UploadError) as excinfo:
        send_upload(BASE_CONFIG, "pi", "../escape", "a.csv", b"a", opener=opener)

    assert excinfo.value.category == "unsafe_card_uuid"
    assert opener.requests == []


# --- Criterion 7: the nested per-card queue -----------------------------------


def test_only_two_level_regular_files_are_queued(queue_config, ledger):
    enqueue(queue_config, ledger, "b.csv", card_uuid=CARD_B)
    enqueue(queue_config, ledger, "a.csv", card_uuid=CARD_A)

    assert queued_entries(queue_config) == [(CARD_A, "a.csv"), (CARD_B, "b.csv")]


def test_entries_are_ordered_by_card_then_filename(queue_config, ledger):
    for card, name in ((CARD_B, "z.csv"), (CARD_A, "c.csv"), (CARD_B, "a.csv"), (CARD_A, "a.csv")):
        enqueue(queue_config, ledger, name, card_uuid=card)

    assert queued_entries(queue_config) == [
        (CARD_A, "a.csv"),
        (CARD_A, "c.csv"),
        (CARD_B, "a.csv"),
        (CARD_B, "z.csv"),
    ]


def test_a_flat_file_at_the_top_of_the_queue_is_reported_not_uploaded(queue_config):
    """Phase 2 queue debris has no card identity, so it cannot be sent."""
    (queue_config.pending_dir / "logger-0001.csv").write_bytes(b"orphan")

    scan = scan_pending(queue_config)

    assert scan.entries == []
    assert scan.skipped == ["logger-0001.csv"]


def test_a_symlinked_card_directory_is_never_followed(queue_config, ledger, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.csv").write_bytes(b"not ours\n")
    (queue_config.pending_dir / CARD_B).symlink_to(outside)
    enqueue(queue_config, ledger, "a.csv", card_uuid=CARD_A)

    scan = scan_pending(queue_config)

    assert scan.entries == [(CARD_A, "a.csv")]
    assert CARD_B in scan.skipped


def test_a_symlinked_queue_entry_is_never_followed(queue_config, ledger, tmp_path):
    secret = tmp_path / "outside.csv"
    secret.write_bytes(b"not ours\n")
    enqueue(queue_config, ledger, "a.csv")
    (queue_config.pending_dir_for(CARD_A) / "link.csv").symlink_to(secret)

    scan = scan_pending(queue_config)

    assert scan.entries == [(CARD_A, "a.csv")]
    assert f"{CARD_A}/link.csv" in scan.skipped


def test_a_directory_nested_below_a_card_directory_is_not_descended_into(queue_config, ledger):
    enqueue(queue_config, ledger, "a.csv")
    (queue_config.pending_dir_for(CARD_A) / "deeper").mkdir()
    (queue_config.pending_dir_for(CARD_A) / "deeper" / "b.csv").write_bytes(b"x")

    scan = scan_pending(queue_config)

    assert scan.entries == [(CARD_A, "a.csv")]
    assert f"{CARD_A}/deeper" in scan.skipped


def test_a_malformed_queue_entry_is_logged_and_left_in_place(
    queue_config, ledger, caplog_logger, caplog
):
    (queue_config.pending_dir / "stray.csv").write_bytes(b"x")
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.attempted, batch.rejected) == (0, 1)
    assert opener.requests == []
    assert (queue_config.pending_dir / "stray.csv").exists()
    assert "not a pending/<card_uuid>/<filename> file" in caplog.text


def test_an_unsafe_card_directory_name_is_skipped_and_never_sent(
    queue_config, ledger, caplog_logger
):
    unsafe = queue_config.pending_dir / "not_a_uuid"
    unsafe.mkdir(parents=True)
    (unsafe / "a.csv").write_bytes(b"x")
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.attempted, batch.rejected) == (0, 1)
    assert opener.requests == []
    assert (unsafe / "a.csv").exists()


def test_files_from_two_cards_are_each_sent_with_their_own_card_uuid(
    queue_config, ledger, caplog_logger
):
    """Criterion 6/7: the same filename on two cards is two uploads."""
    enqueue(queue_config, ledger, "logger-0001.csv", b"card a\n", card_uuid=CARD_A)
    enqueue(queue_config, ledger, "logger-0001.csv", b"card b\n", card_uuid=CARD_B)
    opener = FakeOpener(
        [
            upload_ack("logger-0001.csv", 7, card_uuid=CARD_A),
            upload_ack("logger-0001.csv", 7, card_uuid=CARD_B),
        ]
    )
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.attempted, batch.uploaded) == (2, 2)
    sent = [
        (
            parse_multipart(request.data, request.get_header("Content-type"))["card_uuid"],
            parse_multipart(request.data, request.get_header("Content-type"))["file"],
        )
        for request, _ in opener.requests
    ]
    assert sent == [(CARD_A.encode(), b"card a\n"), (CARD_B.encode(), b"card b\n")]
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_UPLOADED
    assert ledger.status_of(CARD_B, "logger-0001.csv") == STATUS_UPLOADED


def test_an_emptied_card_directory_is_cleaned_up(queue_config, ledger, caplog_logger):
    enqueue(queue_config, ledger, "a.csv")
    enqueue(queue_config, ledger, "b.csv", card_uuid=CARD_B)
    opener = FakeOpener([upload_ack("a.csv", 8), upload_ack("b.csv", 8, card_uuid=CARD_B)])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert sorted(entry.name for entry in queue_config.pending_dir.iterdir()) == []


def test_a_card_directory_with_files_left_is_not_removed(queue_config, ledger, caplog_logger):
    enqueue(queue_config, ledger, "a.csv")
    enqueue(queue_config, ledger, "b.csv")
    opener = FakeOpener([upload_ack("a.csv", 8), http_error(503, UPLOAD_URL)])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert queued_entries(queue_config) == [(CARD_A, "b.csv")]


# --- Criterion 10: acknowledgement safety -------------------------------------


def test_a_mismatched_card_uuid_leaves_the_file_pending(queue_config, ledger, caplog_logger):
    """The acknowledgement is the only evidence used to delete a local copy."""
    enqueue(queue_config, ledger, "logger-0001.csv")
    opener = FakeOpener([upload_ack("logger-0001.csv", 8, card_uuid=CARD_B)])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert (batch.uploaded, batch.failed) == (0, 1)
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_PENDING
    assert (queue_config.pending_dir_for(CARD_A) / "logger-0001.csv").exists()


def test_a_missing_card_uuid_in_the_acknowledgement_leaves_the_file_pending(
    queue_config, ledger, caplog_logger
):
    enqueue(queue_config, ledger, "logger-0001.csv")
    payload = ack_payload(filename="logger-0001.csv", size=8, card_uuid=None)
    opener = FakeOpener([FakeResponse(200, json.dumps(payload).encode())])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    batch = upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert batch.uploaded == 0
    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_PENDING


@pytest.mark.parametrize("status", ["stored", "already_stored"])
def test_a_matching_acknowledgement_marks_exactly_that_identity_uploaded(
    queue_config, ledger, caplog_logger, status
):
    enqueue(queue_config, ledger, "logger-0001.csv", card_uuid=CARD_A)
    enqueue(queue_config, ledger, "logger-0001.csv", card_uuid=CARD_B)
    opener = FakeOpener(
        [
            upload_ack("logger-0001.csv", 8, card_uuid=CARD_A, status=status),
            # Card B's identical filename fails, so only card A can be closed out.
            http_error(503, UPLOAD_URL),
        ]
    )
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    upload_pending(queue_config, "pi", ledger, state, opener=opener)

    assert ledger.status_of(CARD_A, "logger-0001.csv") == STATUS_UPLOADED
    assert ledger.status_of(CARD_B, "logger-0001.csv") == STATUS_PENDING
    assert not (queue_config.pending_dir_for(CARD_A) / "logger-0001.csv").exists()
    assert (queue_config.pending_dir_for(CARD_B) / "logger-0001.csv").exists()


# --- Criterion 12: a restart keeps nested queues deliverable ------------------


def test_a_restart_finds_the_nested_queue_and_delivers_it(queue_config, ledger, caplog_logger):
    """Stands in for a reboot between queueing and uploading."""
    enqueue(queue_config, ledger, "a.csv", b"12345", card_uuid=CARD_A)
    enqueue(queue_config, ledger, "b.csv", b"12345", card_uuid=CARD_B)

    # A fresh ledger handle over the same files, as a restarted process sees it.
    restarted = Ledger(queue_config.state_db_path)
    assert queued_entries(queue_config) == [(CARD_A, "a.csv"), (CARD_B, "b.csv")]

    opener = FakeOpener([upload_ack("a.csv", 5), upload_ack("b.csv", 5, card_uuid=CARD_B)])
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())
    batch = upload_pending(queue_config, "pi", restarted, state, opener=opener)

    assert batch.uploaded == 2
    assert queued_entries(queue_config) == []
