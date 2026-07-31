import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import {
  installApi,
  installDownloads,
  ok,
  okBytes,
  pageFixture,
  paramOf,
  summaryFixture,
  unauthorized,
  uploadFixture,
} from "../test/server";

const CSV = new TextEncoder().encode("sensor,value\n1,2\n");

/** Two rows on one page, on two different cards. */
function twoRows() {
  return pageFixture([
    uploadFixture(2, { filename: "logger-0002.csv", card_uuid: "5678-EF01", size: 100 }),
    uploadFixture(1, { filename: "logger-0001.csv", card_uuid: "1234-ABCD", size: 200 }),
  ]);
}

function archiveCall(calls: string[]): string {
  return calls.find((call) => call.includes("/uploads/archive")) ?? "";
}

afterEach(() => {
  vi.useRealTimers();
});

describe("single file download", () => {
  it("saves the stored bytes under the row's filename", async () => {
    const api = installApi({
      uploads: () => ok(pageFixture([uploadFixture(42, { filename: "logger-0042.csv" })])),
      download: () => okBytes(CSV, "text/csv"),
    });
    const saved = installDownloads();
    render(<App />);

    const row = await screen.findByRole("row", { name: /logger-0042\.csv/ });
    await userEvent.click(
      within(row).getByRole("button", { name: /^Download logger-0042\.csv from card/ }),
    );

    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]?.filename).toBe("logger-0042.csv");
    expect(await saved[0]?.blob.text()).toBe("sensor,value\n1,2\n");
    expect(api.calls.some((call) => call.includes("/uploads/42/download"))).toBe(true);
  });

  it("names each row's button after its own file", async () => {
    installApi({ uploads: () => ok(twoRows()) });
    installDownloads();
    render(<App />);

    expect(
      await screen.findByRole("button", {
        name: "Download logger-0001.csv from card 1234-ABCD",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Download logger-0002.csv from card 5678-EF01" }),
    ).toBeInTheDocument();
  });

  it("returns to the login screen when the session has ended", async () => {
    installApi({
      uploads: () => ok(pageFixture([uploadFixture(1)])),
      download: () => unauthorized(),
    });
    const saved = installDownloads();
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /^Download logger-0001/ }));

    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
    expect(saved).toHaveLength(0);
  });

  it("reports a server failure without saving anything", async () => {
    installApi({
      uploads: () => ok(pageFixture([uploadFixture(1)])),
      download: () => ({
        status: 409,
        body: { detail: "The stored file for this upload is unavailable" },
      }),
    });
    const saved = installDownloads();
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /^Download logger-0001/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(saved).toHaveLength(0);
    // The table is still there: a failed download is not a failed page.
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("bulk selection", () => {
  it("offers no download action until something is selected", async () => {
    installApi({ uploads: () => ok(twoRows()) });
    installDownloads();
    render(<App />);

    expect(await screen.findByText(/no files selected/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /download selected/i })).not.toBeInTheDocument();
  });

  it("requests one archive for the selected rows", async () => {
    const api = installApi({
      uploads: () => ok(twoRows()),
      archive: () => okBytes(new Uint8Array([0x50, 0x4b, 0x03, 0x04]), "application/zip"),
    });
    const saved = installDownloads();
    vi.setSystemTime(new Date("2026-07-31T14:05:09Z"));
    render(<App />);

    await userEvent.click(
      await screen.findByRole("checkbox", { name: /^Select logger-0001\.csv/ }),
    );
    await userEvent.click(screen.getByRole("checkbox", { name: /^Select logger-0002\.csv/ }));

    await userEvent.click(screen.getByRole("button", { name: /download selected \(2\)/i }));

    await waitFor(() => expect(saved).toHaveLength(1));
    // Page order, not click order: the request is a function of the rows shown.
    expect(paramOf(archiveCall(api.calls), "ids")).toBe("2,1");
    expect(saved[0]?.filename).toBe("uploads-20260731-140509Z.zip");
  });

  it("selects and clears every row on the page from the header checkbox", async () => {
    installApi({ uploads: () => ok(twoRows()) });
    installDownloads();
    render(<App />);

    const all = await screen.findByRole("checkbox", { name: /select all files on this page/i });
    const one = screen.getByRole("checkbox", { name: /^Select logger-0001\.csv/ });

    await userEvent.click(one);
    expect(all).toBePartiallyChecked();

    await userEvent.click(all);
    expect(screen.getByRole("button", { name: /download selected \(2\)/i })).toBeInTheDocument();

    await userEvent.click(all);
    expect(screen.queryByRole("button", { name: /download selected/i })).not.toBeInTheDocument();
  });

  it("shows the selected size and clears the selection on request", async () => {
    installApi({ uploads: () => ok(twoRows()) });
    installDownloads();
    render(<App />);

    await userEvent.click(
      await screen.findByRole("checkbox", { name: /^Select logger-0001\.csv/ }),
    );
    expect(screen.getByText(/1 file selected \(200 bytes\)/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /clear selection/i }));
    expect(await screen.findByText(/no files selected/i)).toBeInTheDocument();
  });

  it("clears the selection when the listed rows change", async () => {
    installApi({
      uploads: () => ok(twoRows()),
      summary: () => ok(summaryFixture({ all_card_uuids: ["1234-ABCD", "5678-EF01"] })),
    });
    installDownloads();
    render(<App />);

    await userEvent.click(
      await screen.findByRole("checkbox", { name: /^Select logger-0001\.csv/ }),
    );
    expect(screen.getByRole("button", { name: /download selected \(1\)/i })).toBeInTheDocument();

    // Sorting asks the server for a different set of rows.
    await userEvent.click(
      within(screen.getByRole("columnheader", { name: /^size/i })).getByRole("button"),
    );

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /download selected/i })).not.toBeInTheDocument(),
    );
  });

  it("explains a selection too large to archive instead of sending it", async () => {
    const oversized = pageFixture([
      uploadFixture(1, { filename: "logger-0001.csv", size: 200_000_000 }),
      uploadFixture(2, { filename: "logger-0002.csv", size: 200_000_000 }),
    ]);
    const api = installApi({ uploads: () => ok(oversized) });
    installDownloads();
    render(<App />);

    await userEvent.click(
      await screen.findByRole("checkbox", { name: /select all files on this page/i }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/at most/i);
    expect(screen.getByRole("button", { name: /download selected \(2\)/i })).toBeDisabled();
    expect(archiveCall(api.calls)).toBe("");
  });

  it("returns to the login screen when the archive rejects the session", async () => {
    installApi({ uploads: () => ok(twoRows()), archive: () => unauthorized() });
    const saved = installDownloads();
    render(<App />);

    await userEvent.click(
      await screen.findByRole("checkbox", { name: /select all files on this page/i }),
    );
    await userEvent.click(screen.getByRole("button", { name: /download selected \(2\)/i }));

    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
    expect(saved).toHaveLength(0);
  });

  it("offers per-file download but no checkboxes in the grouped view", async () => {
    installApi({ uploads: () => ok(twoRows()) });
    installDownloads();
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /by card/i }));
    await userEvent.click(await screen.findByRole("button", { name: /A1B2-C3D4/ }));

    expect(await screen.findByRole("button", { name: /^Download logger-0001/ })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
