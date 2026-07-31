import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { installApi, ok, statusFixture } from "../test/server";

afterEach(() => {
  vi.useRealTimers();
});

describe("connection status", () => {
  it("shows online with the exact last-seen time", async () => {
    installApi();
    render(<App />);

    expect(await screen.findByText(/^Online/)).toBeInTheDocument();
    expect(screen.getByText(/last heartbeat/i)).toBeInTheDocument();
    // The exact UTC value stays available even though local time is displayed.
    expect(screen.getByTitle("2026-07-31T14:00:00Z")).toBeInTheDocument();
    expect(screen.getByText("raspberrypi-uploader")).toBeInTheDocument();
  });

  it("shows offline as a heartbeat age, not a live socket", async () => {
    installApi({
      status: () =>
        ok(statusFixture({ status: "offline", last_seen_at: "2026-07-31T12:00:00Z" })),
    });
    render(<App />);

    expect(await screen.findByText(/^Offline/)).toBeInTheDocument();
    expect(screen.getByText(/last heartbeat/i)).toBeInTheDocument();
    expect(screen.getByTitle("2026-07-31T12:00:00Z")).toBeInTheDocument();
  });

  it("shows never seen when no ping has arrived", async () => {
    installApi({
      status: () =>
        ok(statusFixture({ status: "never_seen", device_id: null, last_seen_at: null })),
    });
    render(<App />);

    expect(await screen.findByText(/never seen/i)).toBeInTheDocument();
    expect(screen.getByText(/no heartbeat received yet/i)).toBeInTheDocument();
  });

  it("shows unavailable rather than offline when the status read fails", async () => {
    installApi({ status: () => ({ status: 503, body: { detail: "unavailable" } }) });
    render(<App />);

    expect(await screen.findByText(/status unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/^Offline/)).not.toBeInTheDocument();
  });

  it("reloads on demand", async () => {
    const api = installApi();
    render(<App />);
    await screen.findByText(/^Online/);
    const before = api.calls.filter((call) => call.includes("/status")).length;

    await userEvent.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() => {
      expect(api.calls.filter((call) => call.includes("/status")).length).toBeGreaterThan(before);
    });
  });

  it("reloads on its own every 60 seconds", async () => {
    // Installed before render so the component's own interval is the fake one.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = installApi();
    render(<App />);
    await screen.findByText(/^Online/);
    const before = api.calls.filter((call) => call.includes("/status")).length;

    await vi.advanceTimersByTimeAsync(60_000);

    await waitFor(() => {
      expect(api.calls.filter((call) => call.includes("/status")).length).toBeGreaterThan(before);
    });
  });
});
