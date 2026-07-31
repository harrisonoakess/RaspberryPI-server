/** Typed client for the dashboard read APIs.
 *
 * Every URL is relative, so the compiled application talks to whatever origin
 * served it and no API base address is baked into the bundle. No secret is ever
 * read from, or written to, browser storage: the session lives entirely in the
 * `HttpOnly` cookie the server sets.
 */

export type ConnectionStatus = "online" | "offline" | "never_seen";

export interface SessionInfo {
  authenticated: boolean;
  expires_at: string;
}

export interface PiStatus {
  status: ConnectionStatus;
  device_id: string | null;
  last_seen_at: string | null;
  online_window_seconds: number;
  server_time: string;
}

export interface UploadItem {
  id: number;
  device_id: string;
  card_uuid: string;
  filename: string;
  size: number;
  received_at: string;
}

/** The columns the server will order by. Anything else is a 422. */
export const SORT_KEYS = ["received_at", "filename", "card_uuid", "device_id", "size"] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export type SortOrder = "asc" | "desc";

export interface UploadPage {
  items: UploadItem[];
  /** Rows matching the filters, not rows on this page. */
  total: number;
  limit: number;
  offset: number;
  sort: SortKey;
  order: SortOrder;
}

/** One card's rollup, as the grouped view renders it. */
export interface CardSummary {
  device_id: string;
  card_uuid: string;
  file_count: number;
  total_bytes: number;
  oldest_received_at: string;
  newest_received_at: string;
}

export interface UploadSummary {
  total_files: number;
  total_bytes: number;
  card_count: number;
  device_count: number;
  oldest_received_at: string | null;
  newest_received_at: string | null;
  cards: CardSummary[];
  cards_truncated: boolean;
  /** Every stored value, ignoring the active filters, so a control can be changed back. */
  all_card_uuids: string[];
  all_device_ids: string[];
}

/** Which rows a request is about. Empty strings mean "not filtered". */
export interface UploadFilters {
  q: string;
  cardUuid: string;
  deviceId: string;
}

export const NO_FILTERS: UploadFilters = { q: "", cardUuid: "", deviceId: "" };

export interface UploadQuery extends UploadFilters {
  sort: SortKey;
  order: SortOrder;
  limit: number;
  offset: number;
}

export interface UploadPreview {
  upload_id: number;
  filename: string;
  card_uuid: string;
  records: string[][];
  truncated: boolean;
}

/** A downloaded payload and the name it should be saved under. */
export interface DownloadedFile {
  blob: Blob;
  filename: string;
}

/** The server's archive limits, mirrored so a doomed request is never sent.
 *
 * The server check is the authoritative one; these exist only so the reason a
 * selection is too large can be shown before a round trip. They must match
 * `MAX_ARCHIVE_FILES` and `MAX_ARCHIVE_BYTES` in `server/dashboard.py`.
 */
export const MAX_ARCHIVE_FILES = 100;
export const MAX_ARCHIVE_BYTES = 268_435_456;

const BASE = "/dashboard/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The session is missing, expired, or was rejected: return to the login screen. */
export class UnauthorizedError extends ApiError {
  constructor() {
    super("Your session has ended. Please sign in again.", 401);
    this.name = "UnauthorizedError";
  }
}

export class RateLimitedError extends ApiError {
  readonly retryAfterSeconds: number | null;

  constructor(retryAfterSeconds: number | null) {
    super("Too many failed attempts. Please wait and try again.", 429);
    this.name = "RateLimitedError";
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

async function detailOf(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string" && detail.length > 0) {
        return detail;
      }
    }
  } catch {
    // A non-JSON error body is not worth surfacing verbatim.
  }
  return fallback;
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(`${BASE}${path}`, {
      credentials: "same-origin",
      ...init,
    });
  } catch {
    throw new ApiError("Could not reach the server.", 0);
  }
}

async function readJson<T>(path: string): Promise<T> {
  const response = await send(path);
  if (response.status === 401) {
    throw new UnauthorizedError();
  }
  if (!response.ok) {
    throw new ApiError(
      await detailOf(response, "The server could not complete that request."),
      response.status,
    );
  }
  return (await response.json()) as T;
}

export async function getSession(): Promise<SessionInfo> {
  return readJson<SessionInfo>("/session");
}

/** Sign in. A wrong password is a `401` here and must not end the session flow. */
export async function login(password: string): Promise<void> {
  const response = await send("/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });

  if (response.status === 429) {
    const header = response.headers.get("Retry-After");
    const seconds = header === null ? Number.NaN : Number.parseInt(header, 10);
    throw new RateLimitedError(Number.isFinite(seconds) ? seconds : null);
  }
  if (response.status === 401) {
    throw new ApiError("Invalid password.", 401);
  }
  if (!response.ok) {
    throw new ApiError(await detailOf(response, "Could not sign in."), response.status);
  }
}

export async function logout(): Promise<void> {
  await send("/session", { method: "DELETE" });
}

export async function getStatus(): Promise<PiStatus> {
  return readJson<PiStatus>("/status");
}

/** The filter parameters, omitting the ones that are not set.
 *
 * An empty value is left out rather than sent as `""`: the server validates
 * every parameter it receives, and "no filter" is the absence of one.
 */
function filterParams(filters: UploadFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.q.trim() !== "") {
    params.set("q", filters.q.trim());
  }
  if (filters.cardUuid !== "") {
    params.set("card_uuid", filters.cardUuid);
  }
  if (filters.deviceId !== "") {
    params.set("device_id", filters.deviceId);
  }
  return params;
}

function suffix(params: URLSearchParams): string {
  const query = params.toString();
  return query === "" ? "" : `?${query}`;
}

export async function listUploads(query: UploadQuery): Promise<UploadPage> {
  const params = filterParams(query);
  params.set("sort", query.sort);
  params.set("order", query.order);
  params.set("limit", String(query.limit));
  params.set("offset", String(query.offset));
  return readJson<UploadPage>(`/uploads${suffix(params)}`);
}

export async function getUploadSummary(filters: UploadFilters): Promise<UploadSummary> {
  return readJson<UploadSummary>(`/uploads/summary${suffix(filterParams(filters))}`);
}

export async function getPreview(uploadId: number): Promise<UploadPreview> {
  return readJson<UploadPreview>(`/uploads/${encodeURIComponent(String(uploadId))}/preview`);
}

/** Fetch a binary payload with the same failure mapping as every JSON read.
 *
 * A plain `<a download>` would be simpler, but it is the browser that follows
 * it, not the application: an expired session would be saved to disk as a file
 * full of `{"detail":"Not authenticated"}` under the CSV's name, and the
 * dashboard would carry on rendering state the server has already rejected.
 */
async function readBlob(path: string): Promise<Blob> {
  const response = await send(path);
  if (response.status === 401) {
    throw new UnauthorizedError();
  }
  if (!response.ok) {
    throw new ApiError(
      await detailOf(response, "The server could not complete that request."),
      response.status,
    );
  }
  return response.blob();
}

export async function downloadUpload(
  uploadId: number,
  filename: string,
): Promise<DownloadedFile> {
  const blob = await readBlob(`/uploads/${encodeURIComponent(String(uploadId))}/download`);
  return { blob, filename };
}

export async function downloadArchive(
  ids: readonly number[],
  filename: string,
): Promise<DownloadedFile> {
  const params = new URLSearchParams({ ids: ids.join(",") });
  const blob = await readBlob(`/uploads/archive?${params.toString()}`);
  return { blob, filename };
}
