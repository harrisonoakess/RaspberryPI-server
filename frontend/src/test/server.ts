/** A tiny stand-in for the dashboard API, driven per test.
 *
 * Tests declare what each endpoint should answer; the fake records the calls it
 * received so cursor and refresh behaviour can be asserted directly.
 */

import { vi } from "vitest";

import type { PiStatus, UploadItem, UploadPage, UploadPreview, UploadSummary } from "../api";

export interface Reply {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export const ok = (body: unknown): Reply => ({ status: 200, body });
export const unauthorized = (): Reply => ({ status: 401, body: { detail: "Not authenticated" } });

type Handler = (url: string, init?: RequestInit) => Reply;

export interface FakeApi {
  calls: string[];
  session: Handler;
  login: Handler;
  status: Handler;
  uploads: Handler;
  summary: Handler;
  preview: Handler;
}

export function installApi(overrides: Partial<FakeApi> = {}): FakeApi {
  const api: FakeApi = {
    calls: [],
    session: () => ok({ authenticated: true, expires_at: "2026-08-01T03:30:00Z" }),
    login: () => ({ status: 204 }),
    status: () => ok(statusFixture()),
    uploads: () => ok(pageFixture([])),
    summary: () => ok(summaryFixture()),
    preview: () => ok(previewFixture()),
    ...overrides,
  };

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    api.calls.push(`${init?.method ?? "GET"} ${url}`);

    const reply = route(api, url, init);
    const body = reply.body === undefined ? "" : JSON.stringify(reply.body);
    return new Response(reply.status === 204 ? null : body, {
      status: reply.status,
      headers: { "Content-Type": "application/json", ...reply.headers },
    });
  });

  return api;
}

function route(api: FakeApi, url: string, init?: RequestInit): Reply {
  if (url.includes("/session")) {
    if (init?.method === "POST") return api.login(url, init);
    if (init?.method === "DELETE") return { status: 204 };
    return api.session(url, init);
  }
  if (url.includes("/status")) return api.status(url, init);
  if (url.includes("/preview")) return api.preview(url, init);
  // Checked before the list: the summary path starts with the list path.
  if (url.includes("/uploads/summary")) return api.summary(url, init);
  if (url.includes("/uploads")) return api.uploads(url, init);
  return { status: 404, body: { detail: "Not Found" } };
}

/** Read a query parameter back off a recorded or handled request URL. */
export function paramOf(url: string, name: string): string | null {
  return new URL(url, "http://localhost").searchParams.get(name);
}

export function statusFixture(overrides: Partial<PiStatus> = {}): PiStatus {
  return {
    status: "online",
    device_id: "raspberrypi-uploader",
    last_seen_at: "2026-07-31T14:00:00Z",
    online_window_seconds: 600,
    server_time: "2026-07-31T14:04:00Z",
    ...overrides,
  };
}

export function uploadFixture(id: number, overrides: Partial<UploadItem> = {}): UploadItem {
  return {
    id,
    device_id: "raspberrypi-uploader",
    card_uuid: "A1B2-C3D4",
    filename: `logger-${String(id).padStart(4, "0")}.csv`,
    size: 1234,
    received_at: "2026-07-31T14:02:00Z",
    ...overrides,
  };
}

/** A page whose `total` defaults to "these items are all of them". */
export function pageFixture(items: UploadItem[], overrides: Partial<UploadPage> = {}): UploadPage {
  return {
    items,
    total: items.length,
    limit: 50,
    offset: 0,
    sort: "received_at",
    order: "desc",
    ...overrides,
  };
}

export function summaryFixture(overrides: Partial<UploadSummary> = {}): UploadSummary {
  return {
    total_files: 2,
    total_bytes: 2468,
    card_count: 1,
    device_count: 1,
    oldest_received_at: "2026-07-31T14:01:00Z",
    newest_received_at: "2026-07-31T14:02:00Z",
    cards: [
      {
        device_id: "raspberrypi-uploader",
        card_uuid: "A1B2-C3D4",
        file_count: 2,
        total_bytes: 2468,
        oldest_received_at: "2026-07-31T14:01:00Z",
        newest_received_at: "2026-07-31T14:02:00Z",
      },
    ],
    cards_truncated: false,
    all_card_uuids: ["A1B2-C3D4"],
    all_device_ids: ["raspberrypi-uploader"],
    ...overrides,
  };
}

export function previewFixture(overrides: Partial<UploadPreview> = {}): UploadPreview {
  return {
    upload_id: 42,
    filename: "logger-0042.csv",
    card_uuid: "A1B2-C3D4",
    records: [
      ["sensor", "value"],
      ["temperature", "21.4"],
    ],
    truncated: false,
    ...overrides,
  };
}
