"""Server contract tests — PRD success criteria 2 (local API contract) and 3
(durability across restarts)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from main import Settings, create_app, resolve_database_path

API_KEY = "test-api-key"
VALID_BODY = {"device_id": "raspberrypi-uploader", "sent_at": "2026-07-28T18:30:00Z"}
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "pings.db"


@pytest.fixture
def client(db_path):
    app = create_app(Settings(api_key=API_KEY, database_path=db_path))
    with TestClient(app) as test_client:
        yield test_client


def rows(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT id, device_id, sent_at, received_at FROM pings ORDER BY id"
        ).fetchall()


# --- Criterion 2: local API contract -----------------------------------------


def test_health_returns_documented_response(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_requires_no_authentication(client):
    assert client.get("/health").status_code == 200


def test_valid_ping_returns_documented_response(client):
    response = client.post("/ping", json=VALID_BODY, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["device_id"] == "raspberrypi-uploader"
    # Server-generated authoritative timestamp, second-precision RFC 3339 UTC.
    assert body["received_at"].endswith("Z")
    assert len(body["received_at"]) == 20


def test_accepted_ping_is_persisted_with_all_columns(client, db_path):
    received_at = client.post("/ping", json=VALID_BODY, headers=AUTH).json()["received_at"]

    persisted = rows(db_path)
    assert len(persisted) == 1
    row_id, device_id, sent_at, stored_received_at = persisted[0]
    assert row_id == 1
    assert device_id == "raspberrypi-uploader"
    assert sent_at == "2026-07-28T18:30:00Z"
    assert stored_received_at == received_at


def test_duplicate_pings_are_stored_as_separate_rows(client, db_path):
    for _ in range(3):
        assert client.post("/ping", json=VALID_BODY, headers=AUTH).status_code == 200

    assert [row[0] for row in rows(db_path)] == [1, 2, 3]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": f"Basic {API_KEY}"},
        {"Authorization": API_KEY},
        # Prefix of the real key must not be accepted.
        {"Authorization": f"Bearer {API_KEY[:-1]}"},
    ],
)
def test_bad_authentication_returns_401(client, db_path, headers):
    response = client.post("/ping", json=VALID_BODY, headers=headers)

    assert response.status_code == 401
    assert rows(db_path) == []


def test_unauthenticated_request_advertises_bearer_scheme(client):
    response = client.post("/ping", json=VALID_BODY)
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"device_id": "raspberrypi-uploader"},
        {"sent_at": "2026-07-28T18:30:00Z"},
        # device_id boundaries
        {"device_id": "", "sent_at": "2026-07-28T18:30:00Z"},
        {"device_id": "-leading", "sent_at": "2026-07-28T18:30:00Z"},
        {"device_id": "trailing-", "sent_at": "2026-07-28T18:30:00Z"},
        {"device_id": "under_score", "sent_at": "2026-07-28T18:30:00Z"},
        {"device_id": "has space", "sent_at": "2026-07-28T18:30:00Z"},
        {"device_id": "a" * 64, "sent_at": "2026-07-28T18:30:00Z"},
        # sent_at must be a valid RFC 3339 UTC timestamp
        {"device_id": "pi", "sent_at": "not-a-timestamp"},
        {"device_id": "pi", "sent_at": "2026-07-28"},
        {"device_id": "pi", "sent_at": "2026-07-28 18:30:00Z"},
        {"device_id": "pi", "sent_at": "2026-07-28T18:30:00"},
        {"device_id": "pi", "sent_at": "2026-07-28T18:30:00+02:00"},
        {"device_id": "pi", "sent_at": "2026-13-28T18:30:00Z"},
        {"device_id": "pi", "sent_at": 1234567890},
    ],
)
def test_invalid_body_returns_422_and_persists_nothing(client, db_path, body):
    response = client.post("/ping", json=body, headers=AUTH)

    assert response.status_code in (400, 422)
    assert rows(db_path) == []


@pytest.mark.parametrize(
    "device_id",
    ["a", "a" * 63, "raspberrypi-uploader", "pi-3b-plus-01", "PI01"],
)
def test_device_id_boundaries_are_accepted(client, device_id):
    body = {"device_id": device_id, "sent_at": "2026-07-28T18:30:00Z"}
    assert client.post("/ping", json=body, headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "sent_at,stored",
    [
        ("2026-07-28T18:30:00Z", "2026-07-28T18:30:00Z"),
        ("2026-07-28T18:30:00+00:00", "2026-07-28T18:30:00Z"),
        ("2026-07-28T18:30:00.123456Z", "2026-07-28T18:30:00.123456Z"),
        ("2026-07-28t18:30:00z", "2026-07-28T18:30:00Z"),
    ],
)
def test_sent_at_accepts_utc_forms_and_normalizes(client, db_path, sent_at, stored):
    body = {"device_id": "pi", "sent_at": sent_at}
    assert client.post("/ping", json=body, headers=AUTH).status_code == 200
    assert rows(db_path)[0][2] == stored


def test_persistence_failure_returns_503(client, db_path, monkeypatch):
    def broken_insert(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(main, "insert_ping", broken_insert)
    response = client.post("/ping", json=VALID_BODY, headers=AUTH)

    assert response.status_code == 503


# --- Criterion 3: durability across restarts ---------------------------------


def test_rows_survive_process_restart_and_new_pings_still_succeed(db_path):
    """Stands in for the FastAPI/container restart: a fresh app over the same
    volume-backed database file must see the old row and accept new ones."""
    settings = Settings(api_key=API_KEY, database_path=db_path)

    with TestClient(create_app(settings)) as first:
        assert first.post("/ping", json=VALID_BODY, headers=AUTH).status_code == 200

    # Second process, same database file on the volume.
    with TestClient(create_app(settings)) as second:
        assert len(rows(db_path)) == 1
        assert second.post("/ping", json=VALID_BODY, headers=AUTH).status_code == 200

    # Third process, standing in for a redeploy.
    with TestClient(create_app(settings)) as third:
        assert len(rows(db_path)) == 2
        assert third.post("/ping", json=VALID_BODY, headers=AUTH).status_code == 200

    assert [row[0] for row in rows(db_path)] == [1, 2, 3]


def test_startup_creates_missing_database_directory(tmp_path):
    nested = tmp_path / "volume" / "nested" / "pings.db"
    with TestClient(create_app(Settings(api_key=API_KEY, database_path=nested))):
        assert nested.exists()


def test_startup_fails_without_api_key(db_path):
    app = create_app(Settings(api_key="", database_path=db_path))
    with pytest.raises(RuntimeError, match="API_KEY"):
        with TestClient(app):
            pass


# --- Configuration ------------------------------------------------------------


def test_database_path_prefers_explicit_setting():
    env = {"DATABASE_PATH": "/custom/pings.db", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    assert str(resolve_database_path(env)) == "/custom/pings.db"


def test_database_path_uses_railway_volume_mount():
    assert str(resolve_database_path({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})) == "/data/pings.db"


def test_database_path_falls_back_to_local_directory():
    assert str(resolve_database_path({})) == "data/pings.db"
