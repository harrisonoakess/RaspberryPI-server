"""Pi daemon tests — PRD success criteria 4 (disconnected behavior),
5 (connected soak), 6 (disconnect/reconnect), and 7 (failure log rate limiting)."""

import json
import logging
import socket
import ssl
import subprocess
import threading
import urllib.error
from datetime import datetime, timezone

import pytest

import connectivity_daemon as daemon
from connectivity_daemon import (
    CATEGORY_ACKNOWLEDGED,
    CATEGORY_DISCONNECTED,
    Config,
    ConfigError,
    PingError,
    StateLogger,
    is_wifi_connected,
    poll_once,
    resolve_device_id,
    run,
    send_ping,
)

BASE_CONFIG = Config(
    server_url="https://example.up.railway.app",
    api_key="secret-key",
    wifi_interface="wlan0",
    poll_interval_seconds=30.0,
    request_timeout_seconds=10.0,
    error_log_repeat_seconds=300.0,
)


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


def http_error(code):
    return urllib.error.HTTPError(
        "https://example.up.railway.app/ping", code, "err", {}, None
    )


@pytest.fixture
def caplog_logger(caplog):
    logger = logging.getLogger("piuploader.test")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="piuploader.test")
    return logger


# --- Configuration ------------------------------------------------------------


def test_config_from_env_applies_documented_defaults():
    config = Config.from_env({"SERVER_URL": "https://x.up.railway.app", "API_KEY": "k"})

    assert config.wifi_interface == "wlan0"
    assert config.poll_interval_seconds == 30.0
    assert config.request_timeout_seconds == 10.0
    assert config.error_log_repeat_seconds == 300.0
    assert config.ping_url == "https://x.up.railway.app/ping"


def test_config_ping_url_tolerates_trailing_slash():
    config = Config.from_env({"SERVER_URL": "https://x.up.railway.app/", "API_KEY": "k"})
    assert config.ping_url == "https://x.up.railway.app/ping"


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
    with pytest.raises(daemon.WifiCheckError):
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
def test_failures_map_to_stable_categories(outcome, category):
    opener = FakeOpener([outcome])
    with pytest.raises(PingError) as excinfo:
        send_ping(BASE_CONFIG, "pi", "2026-07-28T18:30:00Z", opener=opener)
    assert excinfo.value.category == category


def test_ping_error_messages_never_contain_the_api_key():
    for outcome in (http_error(401), urllib.error.URLError(socket.gaierror("boom"))):
        with pytest.raises(PingError) as excinfo:
            send_ping(BASE_CONFIG, "pi", "2026-07-28T18:30:00Z", opener=FakeOpener([outcome]))
        assert BASE_CONFIG.api_key not in str(excinfo.value)


# --- Criterion 7: rate-limited logging ----------------------------------------


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


# --- Criterion 4: disconnected behavior ---------------------------------------


def test_disconnected_poll_makes_no_request(caplog_logger):
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    poll_once(BASE_CONFIG, "pi", state, opener=opener, wifi_check=lambda _: False)

    assert opener.requests == []


def test_three_minutes_disconnected_logs_once_and_sends_nothing(caplog_logger, caplog):
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)
    stop_event = FakeStopEvent(clock)

    # Six 30-second polls = three minutes, inside the five-minute repeat window.
    iterations = run(
        BASE_CONFIG,
        "pi",
        state,
        stop_event,
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
        raise daemon.WifiCheckError("no such interface")

    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())
    opener = FakeOpener()

    poll_once(BASE_CONFIG, "pi", state, opener=opener, wifi_check=broken)

    assert opener.requests == []


def test_unexpected_ping_exception_does_not_crash_the_loop(caplog_logger, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("something unforeseen")

    monkeypatch.setattr(daemon, "send_ping", explode)
    state = StateLogger(caplog_logger, 300.0, clock=FakeClock())

    poll_once(BASE_CONFIG, "pi", state, wifi_check=lambda _: True)


# --- Criterion 5: connected soak ----------------------------------------------


def test_three_minute_connected_soak_pings_once_per_interval(caplog_logger, caplog):
    clock = FakeClock()
    opener = FakeOpener()
    state = StateLogger(caplog_logger, 300.0, clock=clock)

    run(
        BASE_CONFIG,
        "pi",
        state,
        FakeStopEvent(clock),
        opener=opener,
        wifi_check=lambda _: True,
        max_iterations=6,
        clock=clock,
    )

    assert len(opener.requests) == 6
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
        stop_event,
        opener=FakeOpener(),
        wifi_check=lambda _: True,
        clock=clock,
    )

    assert iterations == 0


# --- Criterion 6: disconnect and reconnect ------------------------------------


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
        poll_once(BASE_CONFIG, "pi", state, opener=opener, wifi_check=lambda _: True)
        clock.advance(30.0)

    failures = [r for r in caplog.records if "[server_error]" in r.message]
    acks = [r for r in caplog.records if "ping acknowledged" in r.message]
    assert len(failures) == 1  # second identical failure suppressed
    assert len(acks) == 1  # recovery logged immediately


# --- Timestamps ---------------------------------------------------------------


def test_sent_at_is_rfc3339_utc_and_matches_the_server_contract():
    from main import RFC3339_UTC_PATTERN

    moment = datetime(2026, 7, 28, 18, 30, 0, 987654, tzinfo=timezone.utc)
    rendered = daemon.rfc3339_utc(moment)

    assert rendered == "2026-07-28T18:30:00Z"
    assert RFC3339_UTC_PATTERN.match(rendered)
