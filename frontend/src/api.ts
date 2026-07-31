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

export interface UploadPage {
  items: UploadItem[];
  next_before_id: number | null;
}

export interface UploadPreview {
  upload_id: number;
  filename: string;
  card_uuid: string;
  records: string[][];
  truncated: boolean;
}

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

export async function listUploads(beforeId: number | null = null): Promise<UploadPage> {
  const query = beforeId === null ? "" : `?before_id=${encodeURIComponent(String(beforeId))}`;
  return readJson<UploadPage>(`/uploads${query}`);
}

export async function getPreview(uploadId: number): Promise<UploadPreview> {
  return readJson<UploadPreview>(`/uploads/${encodeURIComponent(String(uploadId))}/preview`);
}
