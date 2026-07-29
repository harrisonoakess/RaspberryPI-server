"""Server contract tests.

Phase 1 criteria: the local `/ping` contract and durability across restarts.
Phase 2 criterion 1: the local `POST /upload` contract — one blob and one row
per `(device_id, filename)`, and no blob or row created by a rejected request.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from main import (
    DEFAULT_MAX_UPLOAD_BYTES,
    Settings,
    create_app,
    resolve_database_path,
    resolve_max_upload_bytes,
    resolve_uploads_path,
    validate_filename,
)

API_KEY = "test-api-key"
VALID_BODY = {"device_id": "raspberrypi-uploader", "sent_at": "2026-07-28T18:30:00Z"}
AUTH = {"Authorization": f"Bearer {API_KEY}"}

DEVICE = "raspberrypi-uploader"
CSV = b"sensor,value\n1,2\n"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "pings.db"


@pytest.fixture
def uploads_path(tmp_path):
    return tmp_path / "uploads"


@pytest.fixture
def settings(db_path, uploads_path):
    return Settings(api_key=API_KEY, database_path=db_path, uploads_path=uploads_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def rows(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT id, device_id, sent_at, received_at FROM pings ORDER BY id"
        ).fetchall()


def upload_rows(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT id, device_id, filename, stored_path, size, received_at "
            "FROM uploads ORDER BY id"
        ).fetchall()


def blobs(uploads_path):
    """Every stored blob, as paths relative to the uploads root."""
    if not uploads_path.exists():
        return []
    return sorted(
        str(path.relative_to(uploads_path))
        for path in uploads_path.rglob("*")
        if path.is_file()
    )


def replace_limit(settings, max_upload_bytes):
    return Settings(
        api_key=settings.api_key,
        database_path=settings.database_path,
        uploads_path=settings.uploads_path,
        max_upload_bytes=max_upload_bytes,
    )


def post_upload(client, filename=None, content=CSV, device_id=DEVICE, headers=AUTH, **extra):
    """Send a `POST /upload` with the three documented multipart fields."""
    data = {}
    if device_id is not None:
        data["device_id"] = device_id
    if filename is not None:
        data["filename"] = filename
    data.update(extra)
    files = {"file": ("upload.csv", content, "text/csv")} if content is not None else None
    return client.post("/upload", data=data, files=files, headers=headers)


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


# --- Criterion 1: the POST /upload contract ----------------------------------


def test_valid_upload_returns_the_documented_acknowledgement(client):
    response = post_upload(client, "logger-0001.csv")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stored"
    assert body["device_id"] == DEVICE
    assert body["filename"] == "logger-0001.csv"
    assert body["size"] == len(CSV)
    assert body["received_at"].endswith("Z")
    assert len(body["received_at"]) == 20


def test_stored_upload_writes_one_blob_and_one_row(client, db_path, uploads_path):
    received_at = post_upload(client, "logger-0001.csv").json()["received_at"]

    assert blobs(uploads_path) == [f"{DEVICE}/logger-0001.csv"]
    assert (uploads_path / DEVICE / "logger-0001.csv").read_bytes() == CSV

    persisted = upload_rows(db_path)
    assert len(persisted) == 1
    row_id, device_id, filename, stored_path, size, stored_received_at = persisted[0]
    assert (row_id, device_id, filename) == (1, DEVICE, "logger-0001.csv")
    assert stored_path == str(uploads_path / DEVICE / "logger-0001.csv")
    assert size == len(CSV)
    assert stored_received_at == received_at


def test_an_empty_csv_is_stored(client, db_path, uploads_path):
    response = post_upload(client, "empty.csv", content=b"")

    assert response.status_code == 200
    assert response.json()["size"] == 0
    assert (uploads_path / DEVICE / "empty.csv").read_bytes() == b""
    assert upload_rows(db_path)[0][4] == 0


def test_a_csv_exactly_at_the_limit_is_stored(client, uploads_path):
    content = b"x" * DEFAULT_MAX_UPLOAD_BYTES
    response = post_upload(client, "big.csv", content=content)

    assert response.status_code == 200
    assert response.json()["size"] == DEFAULT_MAX_UPLOAD_BYTES
    assert (uploads_path / DEVICE / "big.csv").stat().st_size == DEFAULT_MAX_UPLOAD_BYTES


def test_one_byte_over_the_limit_returns_413_and_stores_nothing(
    client, db_path, uploads_path
):
    content = b"x" * (DEFAULT_MAX_UPLOAD_BYTES + 1)
    response = post_upload(client, "toobig.csv", content=content)

    assert response.status_code == 413
    assert upload_rows(db_path) == []
    assert blobs(uploads_path) == []


def test_the_limit_is_enforced_while_reading_not_just_from_content_length(settings):
    """A client that under-declares Content-Length must still be capped."""
    app = create_app(replace_limit(settings, 64))
    with TestClient(app) as client:
        response = post_upload(client, "toobig.csv", content=b"x" * 65)

    assert response.status_code == 413


def test_re_uploading_the_same_identity_returns_already_stored(client, db_path, uploads_path):
    first = post_upload(client, "logger-0001.csv").json()
    second = post_upload(client, "logger-0001.csv")

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "already_stored"
    # The existing row's metadata, including its original received_at.
    assert body["received_at"] == first["received_at"]
    assert body["size"] == first["size"]
    assert body["filename"] == "logger-0001.csv"


def test_repeated_uploads_leave_exactly_one_blob_and_one_row(client, db_path, uploads_path):
    """At-least-once delivery from the Pi must converge on one stored object."""
    for _ in range(4):
        assert post_upload(client, "logger-0001.csv").status_code == 200

    assert len(upload_rows(db_path)) == 1
    assert blobs(uploads_path) == [f"{DEVICE}/logger-0001.csv"]


def test_a_duplicate_never_overwrites_the_stored_bytes(client, uploads_path):
    post_upload(client, "logger-0001.csv", content=b"original\n")
    response = post_upload(client, "logger-0001.csv", content=b"different bytes\n")

    assert response.json()["status"] == "already_stored"
    # Under the logger's never-reused-filename assumption, later bytes for the
    # same identity are not authoritative.
    assert (uploads_path / DEVICE / "logger-0001.csv").read_bytes() == b"original\n"
    assert response.json()["size"] == len(b"original\n")


def test_the_same_filename_from_another_device_is_a_separate_identity(
    client, db_path, uploads_path
):
    assert post_upload(client, "logger-0001.csv", device_id="pi-a").status_code == 200
    second = post_upload(client, "logger-0001.csv", device_id="pi-b")

    assert second.json()["status"] == "stored"
    assert len(upload_rows(db_path)) == 2
    assert blobs(uploads_path) == ["pi-a/logger-0001.csv", "pi-b/logger-0001.csv"]


def test_the_unique_constraint_is_enforced_by_the_database(client, db_path):
    post_upload(client, "logger-0001.csv")

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO uploads (device_id, filename, stored_path, size, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (DEVICE, "logger-0001.csv", "/tmp/x", 1, "2026-07-29T18:30:00Z"),
            )


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
        {"Authorization": f"Bearer {API_KEY[:-1]}"},
    ],
)
def test_upload_without_valid_auth_returns_401_and_stores_nothing(
    client, db_path, uploads_path, headers
):
    response = post_upload(client, "logger-0001.csv", headers=headers)

    assert response.status_code == 401
    assert upload_rows(db_path) == []
    assert blobs(uploads_path) == []


def test_unauthenticated_upload_advertises_bearer_scheme(client):
    response = post_upload(client, "logger-0001.csv", headers={})
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "filename",
    [
        "..",
        ".",
        "../escape.csv",
        "/absolute.csv",
        "sub/dir.csv",
        "sub\\dir.csv",
        "nul\x00.csv",
        "newline\n.csv",
        "carriage\r.csv",
        "tab\t.csv",
        "no-extension",
        "data.txt",
        "data.csv.gz",
        "",
        "a" * 252 + ".csv",
        # 255 characters but over 255 UTF-8 bytes, which the filesystem rejects.
        "ü" * 251 + ".csv",
    ],
)
def test_an_unsafe_or_out_of_scope_filename_is_rejected_and_stores_nothing(
    client, db_path, uploads_path, filename
):
    response = post_upload(client, filename)

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []
    assert blobs(uploads_path) == []


@pytest.mark.parametrize(
    "filename", ["logger-0001.csv", "a.csv", ".csv", "UPPER.CSV", "with space.csv", "unicode-ü.csv"]
)
def test_safe_filename_boundaries_are_accepted(client, filename):
    assert post_upload(client, filename).status_code == 200


@pytest.mark.parametrize(
    "device_id", ["", "-leading", "trailing-", "under_score", "has space", "a" * 64, "../pi"]
)
def test_an_invalid_device_id_is_rejected_and_stores_nothing(
    client, db_path, uploads_path, device_id
):
    response = post_upload(client, "logger-0001.csv", device_id=device_id)

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []
    assert blobs(uploads_path) == []


def test_a_missing_filename_field_is_rejected(client, db_path):
    response = post_upload(client, filename=None)

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []


def test_a_missing_device_id_field_is_rejected(client, db_path):
    response = post_upload(client, "logger-0001.csv", device_id=None)

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []


def test_a_missing_file_part_is_rejected(client, db_path):
    response = client.post(
        "/upload", data={"device_id": DEVICE, "filename": "a.csv"}, headers=AUTH
    )

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []


def test_a_file_part_sent_as_a_text_field_is_rejected(client, db_path):
    response = client.post(
        "/upload",
        data={"device_id": DEVICE, "filename": "a.csv", "file": "not-a-file"},
        headers=AUTH,
    )

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []


def test_a_body_that_is_not_multipart_is_rejected(client, db_path):
    response = client.post("/upload", json={"device_id": DEVICE}, headers=AUTH)

    assert response.status_code in (400, 422)
    assert upload_rows(db_path) == []


def test_the_filename_field_is_authoritative_over_the_file_parts_own_name(
    client, uploads_path
):
    """PRD §5.3: the file part's filename attribute is informational only."""
    response = client.post(
        "/upload",
        data={"device_id": DEVICE, "filename": "authoritative.csv"},
        files={"file": ("../ignored.csv", CSV, "text/csv")},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["filename"] == "authoritative.csv"
    assert blobs(uploads_path) == [f"{DEVICE}/authoritative.csv"]


def test_a_failed_row_insert_returns_503_and_leaves_no_blob(
    client, uploads_path, monkeypatch
):
    def broken_insert(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(main, "insert_upload", broken_insert)
    response = post_upload(client, "logger-0001.csv")

    assert response.status_code == 503
    assert blobs(uploads_path) == []


def test_a_failed_blob_publish_returns_500_and_removes_the_row(
    client, db_path, uploads_path, monkeypatch
):
    """A row must never point at a blob that was not written."""

    def broken_replace(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(main.os, "replace", broken_replace)
    response = post_upload(client, "logger-0001.csv")

    assert response.status_code == 500
    assert upload_rows(db_path) == []
    assert blobs(uploads_path) == []


def test_a_failed_write_returns_500_and_stores_nothing(client, db_path, monkeypatch):
    def broken_mkstemp(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(main.tempfile, "mkstemp", broken_mkstemp)
    response = post_upload(client, "logger-0001.csv")

    assert response.status_code == 500
    assert upload_rows(db_path) == []


def test_uploads_survive_a_process_restart(settings, db_path, uploads_path):
    """Stands in for a redeploy: a fresh app over the same volume sees the row
    and answers a retry with already_stored."""
    with TestClient(create_app(settings)) as first:
        assert post_upload(first, "logger-0001.csv").status_code == 200

    with TestClient(create_app(settings)) as second:
        response = post_upload(second, "logger-0001.csv")
        assert response.json()["status"] == "already_stored"
        assert post_upload(second, "logger-0002.csv").json()["status"] == "stored"

    assert len(upload_rows(db_path)) == 2
    assert blobs(uploads_path) == [
        f"{DEVICE}/logger-0001.csv",
        f"{DEVICE}/logger-0002.csv",
    ]


def test_startup_creates_the_uploads_directory(settings, uploads_path):
    with TestClient(create_app(settings)):
        assert uploads_path.is_dir()


# --- Filename validation in isolation ----------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["a.csv", ".csv", "UPPER.CSV", "mixed.Csv", "with space.csv", "a" * 251 + ".csv"],
)
def test_validate_filename_returns_accepted_names_unchanged(filename):
    assert validate_filename(filename) == filename


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "a/b.csv", "a\\b.csv", "x.txt", "nul\x00.csv", "a" * 252 + ".csv", 7],
)
def test_validate_filename_rejects_unsafe_values(filename):
    with pytest.raises(ValueError):
        validate_filename(filename)


# --- Criterion 3: durability across restarts ---------------------------------


def test_rows_survive_process_restart_and_new_pings_still_succeed(settings, db_path):
    """Stands in for the FastAPI/container restart: a fresh app over the same
    volume-backed database file must see the old row and accept new ones."""
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


def test_uploads_path_prefers_explicit_setting():
    env = {"UPLOADS_PATH": "/custom/blobs", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    assert str(resolve_uploads_path(env)) == "/custom/blobs"


def test_uploads_path_uses_railway_volume_mount():
    assert str(resolve_uploads_path({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})) == "/data/uploads"


def test_uploads_root_is_derived_from_the_database_when_unconfigured(tmp_path):
    """One configured volume location has to cover the database and the blobs."""
    assert resolve_uploads_path({}) is None
    settings = Settings(api_key=API_KEY, database_path=tmp_path / "vol" / "pings.db")
    assert settings.uploads_root == tmp_path / "vol" / "uploads"


def test_max_upload_bytes_defaults_to_ten_mebibytes():
    assert resolve_max_upload_bytes({}) == 10_485_760
    assert DEFAULT_MAX_UPLOAD_BYTES == 10_485_760


def test_max_upload_bytes_can_be_overridden():
    assert resolve_max_upload_bytes({"MAX_UPLOAD_BYTES": "1024"}) == 1024


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5"])
def test_an_invalid_max_upload_bytes_is_rejected(raw):
    with pytest.raises(ValueError):
        resolve_max_upload_bytes({"MAX_UPLOAD_BYTES": raw})
