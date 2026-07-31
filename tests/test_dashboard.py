"""Dashboard read-API tests.

Phase 4 criteria: 1 (no viewer left behind), 2 (the authentication boundary),
3 (the connection signal), 4 (upload browsing), and 5 (the CSV preview matrix).
Nothing in this module may leave a row, a blob, or a stored file modified.
"""

import base64
import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import dashboard
import main
from dashboard import (
    LOGIN_FAILURE_LIMIT,
    LOGIN_FAILURE_WINDOW_SECONDS,
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_FILES,
    MAX_PREVIEW_CONTENT_BYTES,
    MAX_PREVIEW_FIELD_CHARACTERS,
    MAX_PREVIEW_RECORDS,
    ONLINE_WINDOW_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME_SECONDS,
    LoginRateLimiter,
    issue_session,
    validate_dashboard_settings,
    verify_session,
)
from main import Settings, create_app

API_KEY = "test-api-key"
PASSWORD = "correct horse battery staple"
SECRET = "0123456789abcdef0123456789abcdef"  # exactly 32 UTF-8 bytes

DEVICE = "raspberrypi-uploader"
CARD_A = "1234-ABCD"
CARD_B = "5678-EF01"

PROTECTED_ENDPOINTS = (
    "/dashboard/api/session",
    "/dashboard/api/status",
    "/dashboard/api/uploads",
    "/dashboard/api/uploads/summary",
    "/dashboard/api/uploads/1/preview",
    "/dashboard/api/uploads/1/download",
    "/dashboard/api/uploads/archive?ids=1",
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "pings.db"


@pytest.fixture
def uploads_path(tmp_path):
    return tmp_path / "uploads"


@pytest.fixture
def dist_path(tmp_path):
    return tmp_path / "dist"


@pytest.fixture
def settings(db_path, uploads_path, dist_path):
    return Settings(
        api_key=API_KEY,
        database_path=db_path,
        uploads_path=uploads_path,
        dashboard_password=PASSWORD,
        dashboard_session_secret=SECRET,
        dashboard_dist_path=dist_path,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    """A client that has completed a real login, cookie and all."""
    assert client.post("/dashboard/api/session", json={"password": PASSWORD}).status_code == 204
    return client


@pytest.fixture
def freeze(monkeypatch):
    """Pin `datetime.now` inside the dashboard module to an exact instant."""

    def apply(moment):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment if tz is None else moment.astimezone(tz)

        monkeypatch.setattr(dashboard, "datetime", Frozen)

    return apply


# --- Fixtures that write directly to storage ---------------------------------


def insert_ping(db_path, received_at, device_id=DEVICE, sent_at="2026-07-31T14:00:00Z"):
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO pings (device_id, sent_at, received_at) VALUES (?, ?, ?)",
            (device_id, sent_at, received_at),
        )


def insert_upload(
    db_path,
    uploads_path,
    filename,
    content=b"sensor,value\n1,2\n",
    device_id=DEVICE,
    card_uuid=CARD_A,
    received_at="2026-07-31T14:02:00Z",
    stored_path=None,
    write_blob=True,
    size=None,
):
    """Store one upload the way `POST /upload` does, returning its row id.

    `size` overrides the recorded byte count without writing a blob that large,
    so the archive's byte ceiling can be exercised without a huge fixture.
    """
    blob = Path(stored_path) if stored_path else uploads_path / device_id / card_uuid / filename
    if write_blob:
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO uploads (device_id, card_uuid, filename, stored_path, size, "
            "received_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                device_id,
                card_uuid,
                filename,
                str(blob),
                len(content) if size is None else size,
                received_at,
            ),
        )
        return int(cursor.lastrowid)


def csv_bytes(records):
    return "".join(",".join(record) + "\n" for record in records).encode("utf-8")


# === Criterion 1: the old viewer is gone =====================================


def test_repository_has_no_railway_viewer():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "tools" / "railway_viewer.py").exists()
    assert not (root / "tests" / "test_railway_viewer.py").exists()
    assert "railway_viewer" not in (root / "README.md").read_text(encoding="utf-8")


# === Criterion 2: startup configuration ======================================


def base_settings(db_path, **changes):
    values = {
        "api_key": API_KEY,
        "database_path": db_path,
        "dashboard_password": PASSWORD,
        "dashboard_session_secret": SECRET,
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"dashboard_password": ""}, "DASHBOARD_PASSWORD"),
        ({"dashboard_session_secret": ""}, "DASHBOARD_SESSION_SECRET"),
        ({"dashboard_session_secret": "a" * 31}, "at least 32 UTF-8 bytes"),
        ({"dashboard_password": API_KEY}, "must not equal API_KEY"),
    ],
)
def test_startup_rejects_unsafe_dashboard_configuration(db_path, changes, expected):
    with pytest.raises(RuntimeError, match=expected):
        with TestClient(create_app(base_settings(db_path, **changes))):
            pass


def test_multibyte_session_secret_is_measured_in_bytes_not_characters(db_path):
    # 16 characters, 32 UTF-8 bytes: long enough by the documented rule.
    validate_dashboard_settings(base_settings(db_path, dashboard_session_secret="é" * 16))
    with pytest.raises(RuntimeError, match="at least 32 UTF-8 bytes"):
        validate_dashboard_settings(
            base_settings(db_path, dashboard_session_secret="é" * 15)
        )


def test_settings_from_env_reads_dashboard_configuration(tmp_path):
    configured = Settings.from_env(
        {
            "API_KEY": " ingest-key ",
            "DATABASE_PATH": str(tmp_path / "pings.db"),
            "DASHBOARD_PASSWORD": "  spaced password  ",
            "DASHBOARD_SESSION_SECRET": SECRET,
        }
    )
    assert configured.api_key == "ingest-key"
    # Neither dashboard value is trimmed: the password is compared exactly and
    # trimming the secret would silently discard entropy.
    assert configured.dashboard_password == "  spaced password  "
    assert configured.dashboard_session_secret == SECRET
    assert configured.secure_cookies is False


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, False),
        ({"RAILWAY_VOLUME_MOUNT_PATH": "/data"}, False),
        ({"RAILWAY_ENVIRONMENT_ID": "env-123"}, True),
        ({"RAILWAY_DEPLOYMENT_ID": "deploy-123"}, True),
    ],
)
def test_secure_cookies_track_railway_deployment_not_the_volume(env, expected):
    assert main.resolve_secure_cookies(env) is expected


# === Criterion 2: sessions ===================================================


def test_login_sets_a_session_cookie_with_every_required_attribute(client):
    response = client.post("/dashboard/api/session", json={"password": PASSWORD})

    assert response.status_code == 204
    assert not response.content
    header = response.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in header
    assert "SameSite=Strict" in header
    assert "Path=/dashboard" in header
    assert f"Max-Age={SESSION_LIFETIME_SECONDS}" in header
    assert "Secure" not in header  # local HTTP development


def test_login_cookie_carries_secure_on_railway(settings):
    import dataclasses

    on_railway = dataclasses.replace(settings, secure_cookies=True)
    with TestClient(create_app(on_railway)) as client:
        response = client.post("/dashboard/api/session", json={"password": PASSWORD})
    assert "Secure" in response.headers["set-cookie"]


def test_login_response_never_echoes_the_password_or_a_token(client):
    response = client.post("/dashboard/api/session", json={"password": PASSWORD})
    assert PASSWORD not in response.text
    assert response.text == ""


def test_login_rejects_the_ingest_api_key(client):
    response = client.post("/dashboard/api/session", json={"password": API_KEY})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}


def test_login_rejects_an_incorrect_password_generically(client):
    response = client.post("/dashboard/api/session", json={"password": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}
    assert SESSION_COOKIE_NAME not in client.cookies


def test_login_does_not_trim_or_normalize_the_submitted_password(client):
    for candidate in (PASSWORD + " ", " " + PASSWORD, PASSWORD.upper()):
        response = client.post("/dashboard/api/session", json={"password": candidate})
        assert response.status_code == 401


def test_login_requires_a_password_field(client):
    assert client.post("/dashboard/api/session", json={}).status_code == 422
    assert client.post("/dashboard/api/session", json={"password": 5}).status_code == 422


def test_session_endpoint_reports_the_expiry(signed_in, settings):
    response = signed_in.get("/dashboard/api/session")

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    expires_at = datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    delta = expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(seconds=SESSION_LIFETIME_SECONDS - 60) < delta <= timedelta(
        seconds=SESSION_LIFETIME_SECONDS
    )


@pytest.mark.parametrize("path", PROTECTED_ENDPOINTS)
def test_protected_endpoints_reject_an_anonymous_client(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_invalid_session_clears_the_cookie(client):
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-session", path="/dashboard")
    response = client.get("/dashboard/api/status")

    assert response.status_code == 401
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "Path=/dashboard" in response.headers["set-cookie"]


def test_tampered_signature_is_rejected(client):
    token, _ = issue_session(SECRET, int(datetime.now(timezone.utc).timestamp()))
    payload, _, signature = token.partition(".")
    forged = f"{payload}.{'A' * len(signature)}"
    client.cookies.set(SESSION_COOKIE_NAME, forged, path="/dashboard")

    assert client.get("/dashboard/api/status").status_code == 401


def test_session_signed_with_another_secret_is_rejected(client):
    other, _ = issue_session("f" * 32, int(datetime.now(timezone.utc).timestamp()))
    client.cookies.set(SESSION_COOKIE_NAME, other, path="/dashboard")

    assert client.get("/dashboard/api/status").status_code == 401


def test_expired_session_is_rejected(client):
    stale = int(datetime.now(timezone.utc).timestamp()) - SESSION_LIFETIME_SECONDS - 1
    token, _ = issue_session(SECRET, stale)
    client.cookies.set(SESSION_COOKIE_NAME, token, path="/dashboard")

    assert client.get("/dashboard/api/status").status_code == 401


def test_session_lifetime_must_be_exactly_twelve_hours():
    now = 1_800_000_000
    body = json.dumps({"exp": now + SESSION_LIFETIME_SECONDS * 2, "iat": now})
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    signature = dashboard._b64url_encode(dashboard._signature(SECRET, payload))

    assert verify_session(SECRET, f"{payload}.{signature}", now) is None


def test_session_is_valid_until_the_instant_it_expires():
    now = 1_800_000_000
    token, expires_at = issue_session(SECRET, now)

    assert verify_session(SECRET, token, expires_at - 1) == expires_at
    assert verify_session(SECRET, token, expires_at) is None


@pytest.mark.parametrize("token", ["", "no-separator", "a.b", ".", "x.", "!!!.???"])
def test_malformed_session_tokens_are_rejected(token):
    assert verify_session(SECRET, token, 1_800_000_000) is None


def test_logout_expires_the_cookie_and_is_idempotent(signed_in):
    first = signed_in.delete("/dashboard/api/session")
    assert first.status_code == 204
    assert "Max-Age=0" in first.headers["set-cookie"]

    assert signed_in.get("/dashboard/api/status").status_code == 401
    # Repeating logout without a session is harmless.
    assert signed_in.delete("/dashboard/api/session").status_code == 204


# === Criterion 2: login throttling ===========================================


def attempt(client, password, **kwargs):
    return client.post("/dashboard/api/session", json={"password": password}, **kwargs)


def test_fifth_failure_still_returns_401_and_the_sixth_returns_429(client):
    for _ in range(LOGIN_FAILURE_LIMIT):
        assert attempt(client, "wrong").status_code == 401

    throttled = attempt(client, "wrong")
    assert throttled.status_code == 429
    assert 0 < int(throttled.headers["Retry-After"]) <= LOGIN_FAILURE_WINDOW_SECONDS


def test_throttling_holds_even_for_the_correct_password(client):
    for _ in range(LOGIN_FAILURE_LIMIT):
        attempt(client, "wrong")

    response = attempt(client, PASSWORD)
    assert response.status_code == 429
    assert SESSION_COOKIE_NAME not in client.cookies


def test_a_successful_login_clears_that_sources_failures(client):
    for _ in range(LOGIN_FAILURE_LIMIT - 1):
        attempt(client, "wrong")
    assert attempt(client, PASSWORD).status_code == 204

    for _ in range(LOGIN_FAILURE_LIMIT):
        assert attempt(client, "wrong").status_code == 401


def test_a_spoofed_leftmost_forwarded_address_cannot_dodge_the_limit(client):
    # Only the rightmost hop is trusted; the left values are attacker-supplied.
    for index in range(LOGIN_FAILURE_LIMIT):
        response = attempt(
            client, "wrong", headers={"X-Forwarded-For": f"10.0.0.{index}, 203.0.113.7"}
        )
        assert response.status_code == 401

    throttled = attempt(
        client, "wrong", headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.7"}
    )
    assert throttled.status_code == 429


def test_distinct_rightmost_addresses_are_throttled_independently(client):
    for _ in range(LOGIN_FAILURE_LIMIT):
        attempt(client, "wrong", headers={"X-Forwarded-For": "203.0.113.7"})
    assert attempt(client, "wrong", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 429

    other = attempt(client, "wrong", headers={"X-Forwarded-For": "198.51.100.4"})
    assert other.status_code == 401


def test_an_unparseable_forwarded_value_falls_back_to_the_direct_address(client):
    for _ in range(LOGIN_FAILURE_LIMIT):
        attempt(client, "wrong", headers={"X-Forwarded-For": "not-an-ip"})

    assert attempt(client, "wrong").status_code == 429


def test_limiter_window_rolls_and_prunes_expired_sources():
    limiter = LoginRateLimiter()
    for offset in range(LOGIN_FAILURE_LIMIT):
        limiter.record_failure("203.0.113.7", 1000 + offset)

    assert limiter.retry_after("203.0.113.7", 1005) == LOGIN_FAILURE_WINDOW_SECONDS - 5
    # `Retry-After` counts to the moment the oldest retained failure leaves the
    # window, and at that moment the source may try again.
    assert limiter.retry_after("203.0.113.7", 1000 + LOGIN_FAILURE_WINDOW_SECONDS) is None
    # Once every failure has aged out the address itself is no longer retained.
    assert limiter.retry_after("203.0.113.7", 2000 + LOGIN_FAILURE_WINDOW_SECONDS) is None
    assert limiter._failures == {}


# === Criterion 3: connection status ==========================================


def test_status_reports_never_seen_without_any_ping(signed_in):
    body = signed_in.get("/dashboard/api/status").json()

    assert body["status"] == "never_seen"
    assert body["device_id"] is None
    assert body["last_seen_at"] is None
    assert body["online_window_seconds"] == ONLINE_WINDOW_SECONDS


@pytest.mark.parametrize(
    "age_seconds, expected",
    [(0, "online"), (ONLINE_WINDOW_SECONDS, "online"), (ONLINE_WINDOW_SECONDS + 1, "offline")],
)
def test_status_boundary_is_inclusive_at_ten_minutes(
    signed_in, db_path, freeze, age_seconds, expected
):
    now = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
    insert_ping(db_path, main.rfc3339_utc(now - timedelta(seconds=age_seconds)))
    freeze(now)

    body = signed_in.get("/dashboard/api/status").json()
    assert body["status"] == expected
    assert body["device_id"] == DEVICE
    assert body["server_time"] == "2026-07-31T14:00:00Z"


def test_status_uses_the_newest_ping(signed_in, db_path, freeze):
    now = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
    insert_ping(db_path, main.rfc3339_utc(now - timedelta(hours=3)))
    insert_ping(db_path, main.rfc3339_utc(now - timedelta(seconds=30)))
    freeze(now)

    body = signed_in.get("/dashboard/api/status").json()
    assert body["status"] == "online"
    assert body["last_seen_at"] == "2026-07-31T13:59:30Z"


def test_a_future_ping_reads_as_online_and_keeps_its_exact_timestamp(
    signed_in, db_path, freeze, caplog
):
    now = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
    insert_ping(db_path, "2026-07-31T15:00:00Z")
    freeze(now)

    with caplog.at_level("WARNING"):
        body = signed_in.get("/dashboard/api/status").json()

    assert body["status"] == "online"
    assert body["last_seen_at"] == "2026-07-31T15:00:00Z"
    assert any("future" in record.message for record in caplog.records)


def test_status_is_unavailable_rather_than_offline_when_the_database_fails(
    signed_in, monkeypatch
):
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dashboard, "read_connection", explode)
    response = signed_in.get("/dashboard/api/status")

    assert response.status_code == 503
    assert response.json()["detail"] == "Could not read the connection status"


def test_an_unparseable_stored_timestamp_is_unavailable_not_offline(signed_in, db_path):
    insert_ping(db_path, "not a timestamp")
    response = signed_in.get("/dashboard/api/status")

    assert response.status_code == 503
    assert "detail" in response.json()


# === Criterion 4: upload browsing ============================================


def test_uploads_are_returned_newest_first_with_card_identity(signed_in, db_path, uploads_path):
    insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A,
        received_at="2026-07-31T14:01:00Z",
    )
    second = insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_B,
        received_at="2026-07-31T14:02:00Z",
    )

    body = signed_in.get("/dashboard/api/uploads").json()

    assert [item["id"] for item in body["items"]] == [second, second - 1]
    assert [item["card_uuid"] for item in body["items"]] == [CARD_B, CARD_A]
    assert body["items"][0]["device_id"] == DEVICE
    assert body["items"][0]["filename"] == "logger-0001.csv"
    assert body["items"][0]["received_at"] == "2026-07-31T14:02:00Z"
    assert (body["total"], body["offset"], body["sort"], body["order"]) == (
        2,
        0,
        "received_at",
        "desc",
    )


def test_upload_rows_never_expose_the_stored_path(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    response = signed_in.get("/dashboard/api/uploads")

    assert "stored_path" not in response.text
    assert str(uploads_path) not in response.text


def test_an_empty_database_returns_an_empty_page(signed_in):
    assert signed_in.get("/dashboard/api/uploads").json() == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "sort": "received_at",
        "order": "desc",
    }


# --- Sorting -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("sort", "order", "expected"),
    [
        ("filename", "asc", ["a.csv", "B.csv", "c.csv"]),
        ("filename", "desc", ["c.csv", "B.csv", "a.csv"]),
        ("size", "asc", ["c.csv", "a.csv", "B.csv"]),
        ("size", "desc", ["B.csv", "a.csv", "c.csv"]),
        ("received_at", "asc", ["B.csv", "c.csv", "a.csv"]),
        ("received_at", "desc", ["a.csv", "c.csv", "B.csv"]),
    ],
)
def test_every_sortable_column_orders_both_ways(
    signed_in, db_path, uploads_path, sort, order, expected
):
    # Deliberately mixed case: filename sorting must not put every capital
    # ahead of every lowercase name the way a byte-wise sort would.
    insert_upload(
        db_path, uploads_path, "a.csv", content=b"xx", received_at="2026-07-31T14:03:00Z"
    )
    insert_upload(
        db_path, uploads_path, "B.csv", content=b"xxx", received_at="2026-07-31T14:01:00Z"
    )
    insert_upload(
        db_path, uploads_path, "c.csv", content=b"x", received_at="2026-07-31T14:02:00Z"
    )

    body = signed_in.get(
        "/dashboard/api/uploads", params={"sort": sort, "order": order}
    ).json()

    assert [item["filename"] for item in body["items"]] == expected
    assert body["sort"] == sort and body["order"] == order


def test_offset_pagination_returns_every_row_exactly_once(signed_in, db_path, uploads_path):
    expected = [
        insert_upload(db_path, uploads_path, f"logger-{index:04d}.csv") for index in range(7)
    ]

    seen = []
    for offset in range(0, 9, 3):
        body = signed_in.get(
            "/dashboard/api/uploads", params={"limit": "3", "offset": str(offset)}
        ).json()
        assert body["total"] == len(expected)
        seen.extend(item["id"] for item in body["items"])

    assert sorted(seen) == sorted(expected)
    assert len(seen) == len(set(seen))


def test_tied_sort_values_are_broken_by_id_so_pages_never_overlap(
    signed_in, db_path, uploads_path
):
    """Every row here has the same size and timestamp; only `id` separates them."""
    for index in range(6):
        insert_upload(db_path, uploads_path, f"logger-{index:04d}.csv")

    seen = []
    for offset in (0, 2, 4):
        body = signed_in.get(
            "/dashboard/api/uploads",
            params={"sort": "size", "order": "asc", "limit": "2", "offset": str(offset)},
        ).json()
        seen.extend(item["id"] for item in body["items"])

    assert len(seen) == len(set(seen)) == 6


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(
    signed_in, db_path, uploads_path
):
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    body = signed_in.get("/dashboard/api/uploads", params={"offset": "100"}).json()

    assert body["items"] == []
    assert body["total"] == 1


# --- Filtering ---------------------------------------------------------------


def test_filename_search_narrows_the_page_and_the_total(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    insert_upload(db_path, uploads_path, "logger-0002.csv")
    insert_upload(db_path, uploads_path, "notes.txt")

    body = signed_in.get("/dashboard/api/uploads", params={"q": "logger"}).json()

    assert body["total"] == 2
    assert {item["filename"] for item in body["items"]} == {
        "logger-0001.csv",
        "logger-0002.csv",
    }


def test_search_is_case_insensitive_and_matches_anywhere_in_the_name(
    signed_in, db_path, uploads_path
):
    insert_upload(db_path, uploads_path, "Logger-0001.csv")
    assert signed_in.get("/dashboard/api/uploads", params={"q": "LOG"}).json()["total"] == 1
    assert signed_in.get("/dashboard/api/uploads", params={"q": "0001"}).json()["total"] == 1


@pytest.mark.parametrize("needle", ["log_er", "%", "_", "logg%r"])
def test_like_wildcards_in_a_search_are_matched_literally(
    signed_in, db_path, uploads_path, needle
):
    """An unescaped `%` or `_` would turn a narrow search into "everything"."""
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    assert signed_in.get("/dashboard/api/uploads", params={"q": needle}).json()["total"] == 0


def test_a_whitespace_only_search_is_no_search_at_all(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    assert signed_in.get("/dashboard/api/uploads", params={"q": "   "}).json()["total"] == 1


def test_filters_select_a_single_card_and_device(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A)
    insert_upload(db_path, uploads_path, "logger-0002.csv", card_uuid=CARD_A)
    insert_upload(db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_B)
    insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A, device_id="other-pi"
    )

    by_card = signed_in.get("/dashboard/api/uploads", params={"card_uuid": CARD_A}).json()
    assert by_card["total"] == 3
    assert {item["card_uuid"] for item in by_card["items"]} == {CARD_A}

    by_device = signed_in.get("/dashboard/api/uploads", params={"device_id": DEVICE}).json()
    assert by_device["total"] == 3

    # The two filters intersect rather than widening each other.
    both = signed_in.get(
        "/dashboard/api/uploads", params={"card_uuid": CARD_A, "device_id": "other-pi"}
    ).json()
    assert both["total"] == 1


# --- Parameter validation ----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=-1",
        "limit=abc",
        "limit=05",
        "limit=+5",
        "limit=",
        "limit=5&limit=6",
        "offset=-1",
        "offset=abc",
        "offset=01",
        "offset=",
        "offset=1&offset=2",
        "sort=stored_path",
        "sort=id",
        "sort=size%3B+DROP+TABLE+uploads",
        "sort=",
        "sort=size&sort=filename",
        "order=ascending",
        "order=ASC",
        "order=",
        "order=asc&order=desc",
        "card_uuid=not+a+uuid",
        "card_uuid=1234-ABCD&card_uuid=5678-EF01",
        "device_id=not+a+device",
        "q=a&q=b",
        "before_id=1",
        "unknown=1",
    ],
)
def test_malformed_list_parameters_are_rejected(signed_in, query):
    assert signed_in.get(f"/dashboard/api/uploads?{query}").status_code == 422


def test_an_overlong_search_is_rejected(signed_in):
    long_needle = "a" * (dashboard.MAX_SEARCH_CHARACTERS + 1)
    response = signed_in.get("/dashboard/api/uploads", params={"q": long_needle})
    assert response.status_code == 422


def test_limit_boundaries_are_accepted(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    assert signed_in.get("/dashboard/api/uploads", params={"limit": "1"}).status_code == 200
    assert signed_in.get("/dashboard/api/uploads", params={"limit": "100"}).status_code == 200
    assert signed_in.get("/dashboard/api/uploads", params={"offset": "0"}).status_code == 200


def test_upload_list_reports_a_database_failure_as_unavailable(signed_in, monkeypatch):
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dashboard, "read_connection", explode)
    response = signed_in.get("/dashboard/api/uploads")

    assert response.status_code == 503
    assert response.json()["detail"] == "Could not read stored uploads"


def test_upload_list_rejects_an_unrenderable_stored_timestamp(
    signed_in, db_path, uploads_path
):
    insert_upload(db_path, uploads_path, "logger-0001.csv", received_at="yesterday")
    assert signed_in.get("/dashboard/api/uploads").status_code == 503


# --- Summary and per-card rollups --------------------------------------------


def test_summary_totals_and_per_card_rollups(signed_in, db_path, uploads_path):
    insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A, content=b"aa",
        received_at="2026-07-31T14:01:00Z",
    )
    insert_upload(
        db_path, uploads_path, "logger-0002.csv", card_uuid=CARD_A, content=b"bbb",
        received_at="2026-07-31T14:03:00Z",
    )
    insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_B, content=b"c",
        received_at="2026-07-31T14:02:00Z",
    )

    body = signed_in.get("/dashboard/api/uploads/summary").json()

    assert body["total_files"] == 3
    assert body["total_bytes"] == 6
    assert body["card_count"] == 2
    assert body["device_count"] == 1
    assert body["oldest_received_at"] == "2026-07-31T14:01:00Z"
    assert body["newest_received_at"] == "2026-07-31T14:03:00Z"
    assert body["cards_truncated"] is False

    # Most recently active card first.
    assert [card["card_uuid"] for card in body["cards"]] == [CARD_A, CARD_B]
    first = body["cards"][0]
    assert (first["file_count"], first["total_bytes"]) == (2, 5)
    assert first["oldest_received_at"] == "2026-07-31T14:01:00Z"
    assert first["newest_received_at"] == "2026-07-31T14:03:00Z"
    assert first["device_id"] == DEVICE


def test_the_same_card_on_two_devices_is_two_rollups(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A)
    insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A, device_id="other-pi"
    )

    body = signed_in.get("/dashboard/api/uploads/summary").json()

    assert body["card_count"] == 2
    assert body["device_count"] == 2
    assert len(body["cards"]) == 2
    assert {card["device_id"] for card in body["cards"]} == {DEVICE, "other-pi"}


def test_summary_totals_follow_the_active_filters(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A, content=b"aa")
    insert_upload(db_path, uploads_path, "notes.txt", card_uuid=CARD_B, content=b"bbbb")

    body = signed_in.get("/dashboard/api/uploads/summary", params={"q": "logger"}).json()

    assert body["total_files"] == 1
    assert body["total_bytes"] == 2
    assert [card["card_uuid"] for card in body["cards"]] == [CARD_A]


def test_filter_choices_are_never_narrowed_by_the_active_filter(
    signed_in, db_path, uploads_path
):
    """Otherwise the control would only ever offer the value already chosen."""
    insert_upload(db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_A)
    insert_upload(
        db_path, uploads_path, "logger-0001.csv", card_uuid=CARD_B, device_id="other-pi"
    )

    body = signed_in.get(
        "/dashboard/api/uploads/summary", params={"card_uuid": CARD_A}
    ).json()

    assert body["total_files"] == 1
    assert body["all_card_uuids"] == [CARD_A, CARD_B]
    assert body["all_device_ids"] == ["other-pi", DEVICE]


def test_summary_of_an_empty_database_reports_zeroes_not_null_totals(signed_in):
    assert signed_in.get("/dashboard/api/uploads/summary").json() == {
        "total_files": 0,
        "total_bytes": 0,
        "card_count": 0,
        "device_count": 0,
        "oldest_received_at": None,
        "newest_received_at": None,
        "cards": [],
        "cards_truncated": False,
        "all_card_uuids": [],
        "all_device_ids": [],
    }


def test_summary_caps_the_number_of_card_groups(signed_in, db_path, uploads_path, monkeypatch):
    monkeypatch.setattr(dashboard, "MAX_CARD_GROUPS", 2)
    for index in range(3):
        insert_upload(
            db_path, uploads_path, "logger-0001.csv", card_uuid=f"CARD-{index:04d}"
        )

    body = signed_in.get("/dashboard/api/uploads/summary").json()

    assert len(body["cards"]) == 2
    assert body["cards_truncated"] is True
    # The uncapped totals still describe every row.
    assert body["total_files"] == 3
    assert body["card_count"] == 3


@pytest.mark.parametrize("query", ["limit=5", "offset=5", "sort=size", "unknown=1"])
def test_summary_rejects_parameters_that_only_apply_to_the_list(signed_in, query):
    assert signed_in.get(f"/dashboard/api/uploads/summary?{query}").status_code == 422


def test_summary_reports_a_database_failure_as_unavailable(signed_in, monkeypatch):
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dashboard, "read_connection", explode)
    response = signed_in.get("/dashboard/api/uploads/summary")

    assert response.status_code == 503
    assert response.json()["detail"] == "Could not read stored uploads"


def test_summary_never_exposes_the_stored_path(signed_in, db_path, uploads_path):
    insert_upload(db_path, uploads_path, "logger-0001.csv")
    response = signed_in.get("/dashboard/api/uploads/summary")

    assert "stored_path" not in response.text
    assert str(uploads_path) not in response.text


# === Criterion 5: CSV preview ================================================


def test_preview_returns_raw_records(signed_in, db_path, uploads_path):
    upload_id = insert_upload(
        db_path, uploads_path, "logger-0042.csv", content=b"sensor,value\ntemperature,21.4\n"
    )

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert body == {
        "upload_id": upload_id,
        "filename": "logger-0042.csv",
        "card_uuid": CARD_A,
        "records": [["sensor", "value"], ["temperature", "21.4"]],
        "truncated": False,
    }


def test_preview_handles_quoted_multiline_and_unicode_content(signed_in, db_path, uploads_path):
    content = 'label,note\n"a,b","line one\nline two"\n"séñsor","21,4 °C"\n'.encode("utf-8")
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", content=content)

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert body["records"] == [
        ["label", "note"],
        ["a,b", "line one\nline two"],
        ["séñsor", "21,4 °C"],
    ]
    assert body["truncated"] is False


def test_preview_of_an_empty_file_succeeds(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "empty.csv", content=b"")

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert body["records"] == []
    assert body["truncated"] is False


def test_exactly_one_hundred_records_are_not_truncated(signed_in, db_path, uploads_path):
    content = csv_bytes([[str(index), "x"] for index in range(MAX_PREVIEW_RECORDS)])
    upload_id = insert_upload(db_path, uploads_path, "hundred.csv", content=content)

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert len(body["records"]) == MAX_PREVIEW_RECORDS
    assert body["truncated"] is False


def test_one_hundred_and_one_records_are_truncated_to_one_hundred(
    signed_in, db_path, uploads_path
):
    content = csv_bytes([[str(index), "x"] for index in range(MAX_PREVIEW_RECORDS + 1)])
    upload_id = insert_upload(db_path, uploads_path, "hundred-one.csv", content=content)

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert len(body["records"]) == MAX_PREVIEW_RECORDS
    assert body["records"][-1] == [str(MAX_PREVIEW_RECORDS - 1), "x"]
    assert body["truncated"] is True


def aggregate_records(count):
    """`count` single-cell records that together fill the aggregate limit."""
    cell = "a" * (MAX_PREVIEW_CONTENT_BYTES // 8)
    return [[cell] for _ in range(count)]


def test_content_exactly_at_the_aggregate_limit_is_returned_whole(
    signed_in, db_path, uploads_path
):
    content = csv_bytes(aggregate_records(8))
    upload_id = insert_upload(db_path, uploads_path, "exact.csv", content=content)

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert len(body["records"]) == 8
    assert body["truncated"] is False


def test_a_record_past_the_aggregate_limit_is_omitted_whole(signed_in, db_path, uploads_path):
    content = csv_bytes(aggregate_records(9))
    upload_id = insert_upload(db_path, uploads_path, "over.csv", content=content)

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert len(body["records"]) == 8
    assert all(len(record) == 1 for record in body["records"])
    assert body["truncated"] is True


def test_a_field_at_the_character_limit_is_allowed(signed_in, db_path, uploads_path):
    content = ("x" * MAX_PREVIEW_FIELD_CHARACTERS + "\n").encode("utf-8")
    upload_id = insert_upload(db_path, uploads_path, "wide.csv", content=content)

    body = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").json()

    assert len(body["records"][0][0]) == MAX_PREVIEW_FIELD_CHARACTERS


def test_a_field_past_the_character_limit_is_rejected(signed_in, db_path, uploads_path):
    content = ("x" * (MAX_PREVIEW_FIELD_CHARACTERS + 1) + "\n").encode("utf-8")
    upload_id = insert_upload(db_path, uploads_path, "too-wide.csv", content=content)

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview")

    assert response.status_code == 422
    assert "field" in response.json()["detail"].lower()


def test_malformed_csv_is_rejected(signed_in, db_path, uploads_path):
    upload_id = insert_upload(
        db_path, uploads_path, "broken.csv", content=b'sensor,"value"x\n1,2\n'
    )

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview")

    assert response.status_code == 422
    assert "CSV" in response.json()["detail"]


def test_non_utf8_content_is_rejected(signed_in, db_path, uploads_path):
    upload_id = insert_upload(
        db_path, uploads_path, "latin.csv", content=b"sensor,value\n\xff\xfe,2\n"
    )

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview")

    assert response.status_code == 422
    assert "UTF-8" in response.json()["detail"]


def test_unknown_upload_returns_not_found(signed_in):
    assert signed_in.get("/dashboard/api/uploads/999/preview").status_code == 404


@pytest.mark.parametrize("identifier", ["0", "-1", "abc", "01", "1.5", "%20"])
def test_a_non_canonical_upload_id_is_rejected(signed_in, identifier):
    response = signed_in.get(f"/dashboard/api/uploads/{identifier}/preview")
    assert response.status_code in (404, 422)
    assert response.status_code != 200


def test_a_missing_stored_file_is_a_conflict(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "gone.csv", write_blob=False)

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview")

    assert response.status_code == 409
    assert str(uploads_path) not in response.text


def test_a_stored_path_outside_the_uploads_root_is_a_conflict(
    signed_in, db_path, uploads_path, tmp_path
):
    escaped = tmp_path / "elsewhere" / "secret.csv"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("secret,data\n", encoding="utf-8")
    upload_id = insert_upload(
        db_path, uploads_path, "secret.csv", stored_path=escaped, write_blob=False
    )

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview")

    assert response.status_code == 409
    assert "secret" not in response.text


def test_a_filesystem_failure_is_unavailable(signed_in, db_path, uploads_path, monkeypatch):
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    def explode(self):
        raise OSError("input/output error")

    monkeypatch.setattr(Path, "read_bytes", explode)
    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview")

    assert response.status_code == 503
    assert str(uploads_path) not in response.text


def test_preview_does_not_modify_the_stored_file_or_row(signed_in, db_path, uploads_path):
    content = b"sensor,value\n1,2\n"
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", content=content)
    blob = uploads_path / DEVICE / CARD_A / "logger-0001.csv"
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM uploads").fetchall()

    assert signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").status_code == 200

    assert blob.read_bytes() == content
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM uploads").fetchall() == before


# === Criterion 6: file downloads =============================================


def test_download_returns_the_stored_bytes_exactly(signed_in, db_path, uploads_path):
    # CRLF, a BOM, and no trailing newline: everything a helpful reader would
    # be tempted to normalize away.
    content = b"\xef\xbb\xbfsensor,value\r\ntemperature,21.4"
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", content=content)

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    assert response.status_code == 200
    assert response.content == content


def test_download_returns_a_file_that_preview_refuses(signed_in, db_path, uploads_path):
    """A blob that is not valid UTF-8 is still the bytes that were received."""
    content = b"sensor,value\n\xff\xfe,2\n"
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", content=content)

    assert signed_in.get(f"/dashboard/api/uploads/{upload_id}/preview").status_code == 422

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")
    assert response.status_code == 200
    assert response.content == content


def test_download_is_an_attachment_named_for_the_stored_file(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "logger-0042.csv")

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "logger-0042.csv" in disposition
    assert response.headers["content-type"].startswith("text/csv")


def test_download_encodes_a_non_ascii_filename(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "données-café.csv")

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    # RFC 5987, and no raw non-ASCII byte smuggled into the header.
    assert "filename*=utf-8''" in disposition
    assert "donn%C3%A9es-caf%C3%A9.csv" in disposition
    disposition.encode("ascii")


@pytest.mark.parametrize("upload_id", ["0", "-1", "abc", "01", "1.5", " 1"])
def test_a_non_canonical_download_id_is_rejected(signed_in, upload_id):
    assert signed_in.get(f"/dashboard/api/uploads/{upload_id}/download").status_code == 422


def test_download_of_an_unknown_upload_is_not_found(signed_in):
    assert signed_in.get("/dashboard/api/uploads/999/download").status_code == 404


def test_download_of_a_missing_stored_file_is_a_conflict(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", write_blob=False)

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    assert response.status_code == 409
    assert str(uploads_path) not in response.text


def test_download_of_a_path_outside_the_uploads_root_is_a_conflict(
    signed_in, db_path, uploads_path, tmp_path
):
    escaped = tmp_path / "elsewhere" / "secret.csv"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("secret,data\n", encoding="utf-8")
    upload_id = insert_upload(
        db_path, uploads_path, "secret.csv", stored_path=escaped, write_blob=False
    )

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    assert response.status_code == 409
    assert "secret" not in response.text


def test_a_download_filesystem_failure_is_unavailable(
    signed_in, db_path, uploads_path, monkeypatch
):
    """The route reads the blob's metadata itself, so this is a 503, not a late 500."""
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")
    real_stat = Path.stat

    def explode(self, *args, **kwargs):
        if self.name == "logger-0001.csv":
            raise OSError("input/output error")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", explode)
    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    assert response.status_code == 503
    assert str(uploads_path) not in response.text


def test_download_rejects_unknown_query_parameters(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    assert signed_in.get(f"/dashboard/api/uploads/{upload_id}/download?foo=1").status_code == 422


def test_download_never_exposes_the_stored_path(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    response = signed_in.get(f"/dashboard/api/uploads/{upload_id}/download")

    assert all(str(uploads_path) not in value for value in response.headers.values())


def test_download_does_not_modify_the_stored_file_or_row(signed_in, db_path, uploads_path):
    content = b"sensor,value\n1,2\n"
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", content=content)
    blob = uploads_path / DEVICE / CARD_A / "logger-0001.csv"
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM uploads").fetchall()

    assert signed_in.get(f"/dashboard/api/uploads/{upload_id}/download").status_code == 200

    assert blob.read_bytes() == content
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM uploads").fetchall() == before


# --- Multi-file archives ---


def zip_of(response):
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_archive_members_match_the_stored_bytes(signed_in, db_path, uploads_path):
    first = b"sensor,value\n1,2\n"
    second = b"sensor,value\n3,4\n"
    third = b"sensor,value\n5,6\n"
    one = insert_upload(db_path, uploads_path, "logger-0001.csv", content=first)
    two = insert_upload(db_path, uploads_path, "logger-0002.csv", content=second)
    three = insert_upload(
        db_path, uploads_path, "logger-0003.csv", content=third, card_uuid=CARD_B
    )

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={one},{two},{three}")

    assert response.status_code == 200
    archive = zip_of(response)
    assert archive.testzip() is None
    assert archive.namelist() == [
        f"{DEVICE}/{CARD_A}/logger-0001.csv",
        f"{DEVICE}/{CARD_A}/logger-0002.csv",
        f"{DEVICE}/{CARD_B}/logger-0003.csv",
    ]
    assert archive.read(f"{DEVICE}/{CARD_A}/logger-0001.csv") == first
    assert archive.read(f"{DEVICE}/{CARD_A}/logger-0002.csv") == second
    assert archive.read(f"{DEVICE}/{CARD_B}/logger-0003.csv") == third


def test_archive_disambiguates_same_named_files_from_different_cards(
    signed_in, db_path, uploads_path
):
    """The point of the device/card/file layout: neither file overwrites the other."""
    from_a = b"card,a\n"
    from_b = b"card,b\n"
    one = insert_upload(db_path, uploads_path, "logger-0001.csv", content=from_a, card_uuid=CARD_A)
    two = insert_upload(db_path, uploads_path, "logger-0001.csv", content=from_b, card_uuid=CARD_B)

    archive = zip_of(signed_in.get(f"/dashboard/api/uploads/archive?ids={one},{two}"))

    assert len(archive.namelist()) == 2
    assert archive.read(f"{DEVICE}/{CARD_A}/logger-0001.csv") == from_a
    assert archive.read(f"{DEVICE}/{CARD_B}/logger-0001.csv") == from_b


def test_archive_is_an_attachment_named_as_a_zip(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={upload_id}")

    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert re.search(r'filename="uploads-\d{8}-\d{6}Z\.zip"', disposition)


def test_archive_is_byte_identical_for_the_same_selection(signed_in, db_path, uploads_path):
    """Ordering and timestamps come from the rows, not from the request or the disk."""
    one = insert_upload(db_path, uploads_path, "logger-0002.csv")
    two = insert_upload(db_path, uploads_path, "logger-0001.csv")

    first = signed_in.get(f"/dashboard/api/uploads/archive?ids={one},{two}")
    second = signed_in.get(f"/dashboard/api/uploads/archive?ids={two},{one}")

    assert first.content == second.content


@pytest.mark.parametrize("ids", ["", "1,", ",1", "1,,2", "01", "-1", "abc", " 1", "1 ,2", "1,1"])
def test_archive_rejects_a_malformed_ids_parameter(signed_in, ids):
    assert signed_in.get(f"/dashboard/api/uploads/archive?ids={ids}").status_code == 422


def test_archive_requires_ids(signed_in):
    assert signed_in.get("/dashboard/api/uploads/archive").status_code == 422


def test_archive_rejects_unknown_or_repeated_query_parameters(signed_in):
    assert signed_in.get("/dashboard/api/uploads/archive?ids=1&ids=2").status_code == 422
    assert signed_in.get("/dashboard/api/uploads/archive?ids=1&foo=2").status_code == 422


def test_archive_rejects_more_ids_than_the_file_cap(signed_in):
    ids = ",".join(str(n) for n in range(1, MAX_ARCHIVE_FILES + 2))

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={ids}")

    assert response.status_code == 422
    assert str(MAX_ARCHIVE_FILES) in response.json()["detail"]


def test_archive_rejects_a_selection_over_the_byte_cap(
    signed_in, db_path, uploads_path, monkeypatch
):
    """The `size` column is enough to refuse: no blob is opened."""
    upload_id = insert_upload(
        db_path, uploads_path, "logger-0001.csv", size=MAX_ARCHIVE_BYTES + 1
    )

    def explode(self, *args, **kwargs):
        raise AssertionError("no stored file should be opened")

    monkeypatch.setattr(Path, "open", explode)
    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={upload_id}")

    assert response.status_code == 422
    assert str(MAX_ARCHIVE_BYTES) in response.json()["detail"]


def test_archive_with_an_unknown_id_is_not_found(signed_in, db_path, uploads_path):
    """All or nothing: a partial archive would drop a file with nothing to notice it."""
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    assert signed_in.get(f"/dashboard/api/uploads/archive?ids={upload_id},999").status_code == 404


def test_archive_rejects_a_missing_member_before_streaming(signed_in, db_path, uploads_path):
    present = insert_upload(db_path, uploads_path, "logger-0001.csv")
    absent = insert_upload(db_path, uploads_path, "logger-0002.csv", write_blob=False)

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={present},{absent}")

    # A JSON body at all proves the check ran before any header was committed.
    assert response.status_code == 409
    assert response.json()["detail"]
    assert str(uploads_path) not in response.text


def test_archive_rejects_an_outside_root_member_before_streaming(
    signed_in, db_path, uploads_path, tmp_path
):
    escaped = tmp_path / "elsewhere" / "secret.csv"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("secret,data\n", encoding="utf-8")
    present = insert_upload(db_path, uploads_path, "logger-0001.csv")
    outside = insert_upload(
        db_path, uploads_path, "secret.csv", stored_path=escaped, write_blob=False
    )

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={present},{outside}")

    assert response.status_code == 409
    assert "secret" not in response.text


def test_archive_response_carries_the_documented_headers(signed_in, db_path, uploads_path):
    """The security middleware still applies to a streamed response."""
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={upload_id}")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


def test_archive_never_exposes_the_stored_path(signed_in, db_path, uploads_path):
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")

    response = signed_in.get(f"/dashboard/api/uploads/archive?ids={upload_id}")

    assert all(str(uploads_path) not in value for value in response.headers.values())


def test_archive_does_not_modify_rows_or_blobs(signed_in, db_path, uploads_path):
    content = b"sensor,value\n1,2\n"
    upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv", content=content)
    blob = uploads_path / DEVICE / CARD_A / "logger-0001.csv"
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM uploads").fetchall()

    assert signed_in.get(f"/dashboard/api/uploads/archive?ids={upload_id}").status_code == 200

    assert blob.read_bytes() == content
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM uploads").fetchall() == before


def test_a_read_failure_mid_archive_does_not_yield_a_valid_zip(settings, db_path, uploads_path):
    """Past the preflight there is no way to report an error, so the ZIP is left
    structurally invalid rather than finished with a member silently missing."""
    def explode(self, *args, **kwargs):
        raise OSError("input/output error")

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        # Created inside the client so the schema exists before the row is written.
        upload_id = insert_upload(db_path, uploads_path, "logger-0001.csv")
        assert client.post("/dashboard/api/session", json={"password": PASSWORD}).status_code == 204
        with mock.patch.object(Path, "open", explode):
            response = client.get(f"/dashboard/api/uploads/archive?ids={upload_id}")

    # 200 because the preflight passed and the headers were already sent: this
    # is the case that cannot be reported, not one caught before streaming.
    assert response.status_code == 200
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(response.content)).testzip()


# === Response headers and static hosting =====================================


def test_dashboard_api_responses_carry_the_documented_headers(signed_in):
    response = signed_in.get("/dashboard/api/status")

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy.split("script-src 'self'")[1].split(";")[0]
    assert "object-src 'none'" in policy
    assert "base-uri 'none'" in policy
    assert "frame-ancestors 'none'" in policy


def test_ingest_routes_are_untouched_by_the_dashboard(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert "Content-Security-Policy" not in health.headers

    assert client.post("/ping", json={"device_id": DEVICE, "sent_at": "2026-07-31T14:00:00Z"}).status_code == 401


def test_unknown_dashboard_api_routes_do_not_fall_through_to_the_spa(client):
    response = client.get("/dashboard/api/nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_unrelated_unknown_routes_do_not_fall_through_to_the_spa(client):
    assert client.get("/nope").status_code == 404


def test_missing_build_reports_a_clear_error(client):
    response = client.get("/dashboard")
    assert response.status_code == 503
    assert "built" in response.json()["detail"]


def test_spa_shell_is_served_for_unknown_dashboard_paths(client, dist_path):
    (dist_path / "assets").mkdir(parents=True)
    (dist_path / "index.html").write_text("<main>shell</main>", encoding="utf-8")

    for path in ("/dashboard", "/dashboard/", "/dashboard/uploads/42"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "shell" in response.text
        assert response.headers["Cache-Control"] == "no-store"


def test_hashed_assets_are_cached_immutably(client, dist_path):
    (dist_path / "assets").mkdir(parents=True)
    (dist_path / "index.html").write_text("<main>shell</main>", encoding="utf-8")
    (dist_path / "assets" / "index-abc123.js").write_text("export {}", encoding="utf-8")

    response = client.get("/dashboard/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_asset_paths_cannot_escape_the_build_directory(client, dist_path, tmp_path):
    dist_path.mkdir(parents=True)
    (dist_path / "index.html").write_text("<main>shell</main>", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")

    response = client.get("/dashboard/../outside.txt")

    assert "private" not in response.text


def test_the_login_shell_is_reachable_without_a_session(client, dist_path):
    dist_path.mkdir(parents=True)
    (dist_path / "index.html").write_text("<main>shell</main>", encoding="utf-8")

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert PASSWORD not in response.text
    assert API_KEY not in response.text
