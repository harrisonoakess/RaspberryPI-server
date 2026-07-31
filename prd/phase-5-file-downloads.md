# PRD: Phase 5 — File Downloads

## 1. Problem / Background

Phase 4 delivered a private dashboard that answers two questions: is the Pi
still checking in, and what has been stored? It can list every upload and
preview the first 100 records of any stored CSV.

It cannot get a file out. `GET /dashboard/api/uploads/{id}/preview` returns
parsed cells, capped at 100 records, 1 MiB of content, and 262,144 characters
per field. That is a reading aid, not retrieval: an administrator who wants the
actual file has to go to the Railway volume with the Railway CLI, which is the
credential exposure Phase 4 existed to remove.

Phase 4 named "Downloading complete CSV files" an explicit Non-Goal (§3) and
deferred "Full-file downloads" to its §12 Post-Phase 4 list. Phase 5 implements
that deferred work. Phase 4's document is left as the record of what was decided
then; this document is the current contract for downloads.

Phase 5 depends on `phase-4-frontend-dashboard.md` and assumes it is implemented
and deployed. It changes no ingest contract, no storage layout, and no existing
dashboard response shape.

**The dashboard remains read-only.** A download is a read. `PRAGMA query_only=1`
stays set on every dashboard connection, and there is still no upload, edit,
delete, or retention path. What changes is that a read may now return the whole
file rather than a bounded interpretation of it.

## 2. Goals

- Let an administrator download any single stored CSV exactly as it was
  received, byte for byte.
- Let an administrator select several files from the upload list and download
  them together as one ZIP archive.
- Keep both behind the existing dashboard session, with the same failure
  responses and the same refusal to leak `stored_path` or any other server
  internal.
- Keep the archive bounded, so one request cannot exhaust memory, CPU, or the
  connection.
- Keep the interface keyboard-operable and usable at phone and desktop widths,
  as §7.3 of Phase 4 requires of the table it extends.

## 3. Non-Goals

- Uploading, editing, deleting, or expiring anything. Phase 4 §3 still holds for
  every mutation.
- Downloading the *metadata* table (the list of uploads) as a CSV or any other
  export. The files that are downloaded are the stored files.
- Transforming, re-encoding, filtering, or concatenating file contents. What is
  stored is what is returned.
- Selecting rows across pages, or a "download every file matching these filters"
  action that is not bounded by what the table is showing.
- Bulk selection inside the grouped "By card" view. Per-file download works
  there; a "download this whole card" action is deliberately left to a later
  phase, where the count it would cover can be made explicit.
- Resumable downloads, progress reporting, or a download history.
- Per-download audit logging or named administrator identities.

## 4. Constraints and Security

- Both endpoints require a valid, unexpired `dashboard_session`. An invalid,
  expired, or tampered session returns `401` and clears the cookie, exactly as
  every other protected dashboard endpoint does.
- The path of a file to be read comes only from a database row selected by
  integer upload ID. The resolved path is re-checked against the configured
  uploads root before the file is opened. `Path.resolve()` follows symlinks, so
  a link inside the root that points outside it resolves outside and is refused
  on the same footing as a stored path that was never in the root.
- For the archive, `device_id`, `card_uuid`, and `filename` are re-validated
  against the same whitelists ingest applied (`DEVICE_ID_PATTERN`,
  `validate_card_uuid`, `validate_filename`) before they become path segments
  inside the archive. These values come from a database row, not from the
  request, and a row that fails is corrupt in the same way an out-of-root
  `stored_path` is corrupt.
- Responses never contain `stored_path`, database paths, environment values, or
  filesystem error details — in the body or in any header.
- Downloads are served with `Cache-Control: no-store` and the same security
  headers as every other `/dashboard` response. The Content Security Policy is
  unchanged: a `blob:` download is initiated by the `download` attribute rather
  than fetched or navigated to, so no CSP fetch directive governs it, and
  `script-src` is not relaxed.
- `Content-Disposition` is `attachment`. A non-ASCII filename — which
  `validate_filename` permits — is encoded in the RFC 5987 `filename*=utf-8''`
  form; no raw non-ASCII byte appears in the header.
- The single-file response carries `Last-Modified` and `ETag` derived from the
  blob's filesystem metadata. This is accepted: it exposes the container
  filesystem's clock and nothing else, and it is what makes conditional and
  ranged requests work.
- The archive is bounded by two ceilings, both checked before any file is
  opened: at most `MAX_ARCHIVE_FILES` (100) files, and at most
  `MAX_ARCHIVE_BYTES` (256 MiB) summed from the `size` column. 100 is twice the
  list's page size, so "select everything on this page" always fits.
- The archive is streamed, not buffered to disk. A temporary file would need
  cleanup that is not guaranteed to run when a client aborts mid-download, and
  on Railway it would land either on the small ephemeral layer or, worse, on the
  volume that holds the data itself.
- Because the archive streams, every check that can fail must happen before the
  first byte: that all requested IDs exist, that the byte cap holds, and that
  each blob is present, inside the root, and has a usable name. A read that
  fails after that point is logged and allowed to abort the response; the
  central directory is never written, so every extractor reports the result as
  corrupt. A finished archive silently missing a member would not be noticeable
  at all, which is why partial success is not an option.
- Archive members are ordered by their name, not by the order the IDs arrived,
  and their timestamps come from the row's `received_at` rather than the blob's
  mtime. The same selection therefore always produces the same bytes, across
  requests and across a volume restore that rewrites mtimes.
- Both endpoints reject unknown or repeated query parameters with `422`. This is
  stricter than the Phase 4 preview endpoint, which ignores them; the existing
  endpoint's behaviour is left unchanged rather than altering a shipped
  contract, and the inconsistency is recorded here deliberately.

## 5. HTTP Contract

### 5.1 `GET /dashboard/api/uploads/{upload_id}/download`

Returns the stored blob exactly as received, with no decoding or parsing.

- `upload_id` is a canonical positive integer; anything else is `422`.
- Any query parameter is `422`.
- An unknown upload is `404`.
- A stored file that is missing, or that resolves outside the uploads root, is
  `409`.
- A database or filesystem failure is `503`, with a detail that names neither.
- Otherwise `200`, `Content-Type: text/csv; charset=utf-8`,
  `Content-Disposition: attachment; filename="<the stored filename>"`.

A file that the preview endpoint refuses — invalid UTF-8, or malformed CSV —
still downloads. The download returns the bytes that were received, not an
interpretation of them.

### 5.2 `GET /dashboard/api/uploads/archive?ids=1,2,3`

Returns a ZIP containing the requested files.

`GET` rather than `POST`: this is a read, every other data route here is a GET,
and the file cap keeps the request URI far inside any server's request-line
limit.

- `ids` is required, and is a comma-separated list of canonical positive
  integers. An empty value, a blank element, a padded or signed number, a
  repeated ID, or a repeated `ids` parameter is `422`.
- More than `MAX_ARCHIVE_FILES` IDs is `422`, before any query runs.
- Any unknown parameter is `422`.
- If any requested ID has no row, the whole request is `404`. Archives are all
  or nothing.
- If the selected rows' `size` values sum above `MAX_ARCHIVE_BYTES`, `422`,
  before any file is opened.
- If any selected row's blob is missing, resolves outside the uploads root, or
  carries an identity that fails re-validation, `409`.
- A database failure is `503`.
- Otherwise `200`, `Content-Type: application/zip`,
  `Content-Disposition: attachment; filename="uploads-YYYYMMDD-HHMMSSZ.zip"`,
  chunked.

Members are named `<device_id>/<card_uuid>/<filename>`, mirroring the storage
layout. This is what keeps two same-named files from different physical cards
distinct — the case Phase 3's `UNIQUE (device_id, card_uuid, filename)`
constraint makes possible. Members are deflated.

## 6. Dashboard Experience

- Every row in the uploads table has a **Download** action beside **Preview**.
  Because each row's visible label is identical, the accessible name states the
  file and its card. This action is present in both the flat list and the
  grouped "By card" view.
- A download failure is reported next to the row that failed and leaves the
  table rendered. A `401` returns to the login screen and clears protected UI
  state, as Phase 4 §7.1 requires of any protected call.
- The flat file list has a checkbox in each row and a select-all checkbox in the
  header. The header checkbox is indeterminate when some but not all rows on the
  page are selected. Checkboxes sit in the leading cell, which is a plain cell:
  the row's header remains its filename.
- A selection bar above the table states how many files are selected and their
  total size, and offers **Download selected (N)** and **Clear selection**.
- **Selection covers the current page only** and is cleared whenever the listed
  rows change — a filter, a sort, a page, or a manual refresh. The panel holds
  one page at a time, so a selection that outlived a page change would name
  files whose size and filename it could no longer show, and the count it
  displayed would be a claim it could not substantiate. The bar says so.
- A selection that exceeds either cap disables the download and states which
  ceiling it crossed, rather than sending a request that can only be refused.
  The server's check remains the authoritative one.
- The download is fetched by the application rather than followed as a link. A
  plain link is followed by the browser, not the application: an expired session
  would be written to disk as a file full of `{"detail":"Not authenticated"}`
  under the CSV's name, and the dashboard would carry on rendering state the
  server had already rejected.

## 7. Deployment and Configuration

Nothing changes. No new environment variable, no new secret, no new dependency
on either side — `zipfile` and `zlib` are Python standard library, and the
frontend adds no package. All server code lives in `server/dashboard.py`, which
the repository-root `Dockerfile` already copies into the runtime image.

## 8. Success Criteria / Verification

1. **Authentication boundary:** neither endpoint returns data without a valid
   session; both are covered by the parametrized anonymous-client test.
2. **Fidelity:** a downloaded file is byte-identical to the stored blob,
   including CRLF line endings, a UTF-8 BOM, and a missing trailing newline. A
   file that the preview endpoint rejects as invalid UTF-8 still downloads
   whole.
3. **Naming:** the response is an attachment named for the stored file, and a
   non-ASCII filename is percent-encoded in the RFC 5987 form with no raw
   non-ASCII byte in the header.
4. **Failure symmetry:** a non-canonical ID, an unknown upload, a missing blob,
   and an out-of-root row produce the same status codes as the preview endpoint
   does for the same conditions, and none of them names a path.
5. **Archive correctness:** the members are exactly the requested rows, named
   `<device>/<card>/<file>`, each byte-identical to its source; two same-named
   files from different cards both survive; `testzip()` passes.
6. **Archive determinism:** the same selection requested twice, in two different
   ID orders, produces identical bytes.
7. **Archive bounds:** a selection over either cap is refused before a file is
   opened, and an unknown ID refuses the whole request.
8. **Streaming honesty:** a read failure after the preflight leaves a
   structurally invalid ZIP rather than a valid one with a member missing.
9. **Frontend behaviour:** per-row download, its 401 and error states, selection
   and select-all including the indeterminate state, selection clearing on every
   query change, the cap explanation, the archive request and its filename, and
   the absence of checkboxes in the grouped view are all covered by frontend
   tests.
10. **No regression:** the canonical pytest suite and the frontend's typecheck,
    tests, and production build all pass. No Phase 1 through Phase 4 response
    shape changes.
11. **Manual verification:** the browser smoke test in the README covers both
    downloads against the production build and its real Content Security Policy,
    which is the one claim here that no unit test can make.

## 9. Required Automated Checks

From the repository root:

```bash
.venv/bin/python -m pytest tests -q
```

From `frontend/`:

```bash
npm run typecheck && npm test && npm run build
```

## 10. Post-Phase 5

- "Download this card" as a bounded bulk action in the grouped view.
- Selection that survives paging, once the interface can honestly describe what
  it holds.
- Exporting the upload metadata table.
- Retention, deletion, and restore workflows.
- Per-download audit logging, once there is more than one administrator identity
  to attribute a download to.
