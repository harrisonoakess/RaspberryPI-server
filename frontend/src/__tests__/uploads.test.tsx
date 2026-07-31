import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { App } from "../App";
import {
  installApi,
  ok,
  pageFixture,
  paramOf,
  summaryFixture,
  uploadFixture,
} from "../test/server";

/** The list request the component most recently made. */
function lastUploadCall(calls: string[]): string {
  const listCalls = calls.filter(
    (call) => call.includes("/uploads?") && !call.includes("/uploads/summary"),
  );
  return listCalls[listCalls.length - 1] ?? "";
}

/** The sort control in a column header, which "Files"/"By card" would also match by name. */
function sortHeader(label: RegExp): HTMLElement {
  return within(screen.getByRole("columnheader", { name: label })).getByRole("button");
}

describe("uploads table", () => {
  it("lists uploads newest first with the card that produced them", async () => {
    installApi({
      uploads: () =>
        ok(
          pageFixture([
            uploadFixture(2, { card_uuid: "5678-EF01", filename: "logger-0001.csv" }),
            uploadFixture(1, { card_uuid: "1234-ABCD", filename: "logger-0001.csv" }),
          ]),
        ),
    });
    render(<App />);

    const rows = await screen.findAllByRole("row");
    // Header row plus two data rows.
    expect(rows).toHaveLength(3);
    expect(within(rows[1] as HTMLElement).getByText("5678-EF01")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("1234-ABCD")).toBeInTheDocument();
  });

  it("formats sizes and keeps the exact received timestamp available", async () => {
    installApi({ uploads: () => ok(pageFixture([uploadFixture(1, { size: 2048 })])) });
    render(<App />);

    expect(await screen.findByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getAllByTitle("2026-07-31T14:02:00Z").length).toBeGreaterThan(0);
  });

  it("distinguishes the empty state from an error", async () => {
    installApi({ uploads: () => ok(pageFixture([])) });
    const { unmount } = render(<App />);
    expect(await screen.findByText(/no uploads have been stored yet/i)).toBeInTheDocument();
    unmount();

    installApi({ uploads: () => ({ status: 503, body: { detail: "unavailable" } }) });
    render(<App />);
    expect(await screen.findByText(/could not load uploads/i)).toBeInTheDocument();
    expect(screen.queryByText(/no uploads have been stored yet/i)).not.toBeInTheDocument();
  });

  it("uses semantic table markup with a row header per file", async () => {
    installApi({ uploads: () => ok(pageFixture([uploadFixture(1)])) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    expect(screen.getByRole("columnheader", { name: /card uuid/i })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "logger-0001.csv" })).toBeInTheDocument();
  });
});

describe("summary tiles", () => {
  it("shows the totals for the whole stored set", async () => {
    installApi({
      summary: () =>
        ok(summaryFixture({ total_files: 812, total_bytes: 1024, card_count: 3, device_count: 2 })),
      uploads: () => ok(pageFixture([uploadFixture(1)], { total: 812 })),
    });
    render(<App />);

    const label = await screen.findByText("Files", { selector: "dt" });
    expect(label.parentElement).toHaveTextContent("812");
    expect(screen.getByText("1.0 KB")).toBeInTheDocument();
  });

  it("reports a failing summary without hiding the table", async () => {
    installApi({
      summary: () => ({ status: 503, body: { detail: "unavailable" } }),
      uploads: () => ok(pageFixture([uploadFixture(1)])),
    });
    render(<App />);

    expect(await screen.findByText(/could not load upload totals/i)).toBeInTheDocument();
    expect(screen.getByText("logger-0001.csv")).toBeInTheDocument();
  });
});

describe("sorting", () => {
  it("asks the server to sort, so the whole set is ordered and not just this page", async () => {
    const api = installApi({ uploads: () => ok(pageFixture([uploadFixture(1)], { total: 200 })) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    await userEvent.click(sortHeader(/size/i));

    const call = lastUploadCall(api.calls);
    expect(paramOf(call, "sort")).toBe("size");
    // Sizes read largest-first by default.
    expect(paramOf(call, "order")).toBe("desc");
  });

  it("reverses the direction when the active column is clicked again", async () => {
    const api = installApi({ uploads: () => ok(pageFixture([uploadFixture(1)])) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    // Filenames start A to Z, unlike sizes and timestamps.
    await userEvent.click(sortHeader(/^file/i));
    expect(paramOf(lastUploadCall(api.calls), "order")).toBe("asc");

    await userEvent.click(sortHeader(/^file/i));
    expect(paramOf(lastUploadCall(api.calls), "order")).toBe("desc");
  });

  it("marks the sorted column for assistive technology", async () => {
    installApi({ uploads: () => ok(pageFixture([uploadFixture(1)])) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    expect(screen.getByRole("columnheader", { name: /received/i })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(screen.getByRole("columnheader", { name: /^file/i })).toHaveAttribute(
      "aria-sort",
      "none",
    );
  });

  it("returns to the first page when the sort changes", async () => {
    const api = installApi({
      uploads: (url) =>
        ok(
          pageFixture([uploadFixture(1)], {
            total: 200,
            offset: Number(paramOf(url, "offset")),
          }),
        ),
    });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(paramOf(lastUploadCall(api.calls), "offset")).toBe("50");

    await userEvent.click(sortHeader(/size/i));
    await vi.waitFor(() => {
      expect(paramOf(lastUploadCall(api.calls), "offset")).toBe("0");
    });
  });
});

describe("filtering", () => {
  it("sends a debounced filename search and resets to the first page", async () => {
    const api = installApi({ uploads: () => ok(pageFixture([uploadFixture(1)], { total: 200 })) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    await userEvent.type(screen.getByLabelText(/search filenames/i), "logger");

    await vi.waitFor(() => {
      expect(paramOf(lastUploadCall(api.calls), "q")).toBe("logger");
    });
    // Typing six characters must not have sent six requests.
    const searched = api.calls.filter((call) => call.includes("q=")).length;
    expect(searched).toBeLessThan(6);
  });

  it("filters by card and by device, and applies the same filter to the totals", async () => {
    const api = installApi({
      summary: () =>
        ok(summaryFixture({ all_card_uuids: ["1234-ABCD", "5678-EF01"] })),
      uploads: () => ok(pageFixture([uploadFixture(1)])),
    });
    render(<App />);

    await screen.findByRole("option", { name: "5678-EF01" });
    await userEvent.selectOptions(screen.getByLabelText(/^card$/i), "5678-EF01");

    await vi.waitFor(() => {
      expect(paramOf(lastUploadCall(api.calls), "card_uuid")).toBe("5678-EF01");
    });
    const summaryCall = api.calls.filter((call) => call.includes("/uploads/summary")).pop() ?? "";
    expect(paramOf(summaryCall, "card_uuid")).toBe("5678-EF01");
  });

  it("keeps offering every card even while one is selected", async () => {
    installApi({
      summary: () => ok(summaryFixture({ all_card_uuids: ["1234-ABCD", "5678-EF01"] })),
      uploads: () => ok(pageFixture([uploadFixture(1)])),
    });
    render(<App />);

    // The control renders before the summary that fills it in.
    await screen.findByRole("option", { name: "5678-EF01" });
    const select = screen.getByLabelText(/^card$/i);
    await userEvent.selectOptions(select, "5678-EF01");

    expect(within(select).getAllByRole("option")).toHaveLength(3);
  });

  it("says a filter matched nothing rather than that nothing is stored", async () => {
    installApi({ uploads: () => ok(pageFixture([])) });
    render(<App />);

    await screen.findByText(/no uploads have been stored yet/i);
    await userEvent.type(screen.getByLabelText(/search filenames/i), "nope");

    expect(await screen.findByText(/no uploads match the current filters/i)).toBeInTheDocument();
  });

  it("clears every filter at once", async () => {
    const api = installApi({
      summary: () => ok(summaryFixture({ all_card_uuids: ["1234-ABCD", "5678-EF01"] })),
      uploads: () => ok(pageFixture([uploadFixture(1)])),
    });
    render(<App />);

    await screen.findByRole("option", { name: "5678-EF01" });
    await userEvent.type(screen.getByLabelText(/search filenames/i), "logger");
    await userEvent.selectOptions(screen.getByLabelText(/^card$/i), "5678-EF01");

    await userEvent.click(await screen.findByRole("button", { name: /clear filters/i }));

    await vi.waitFor(() => {
      const call = lastUploadCall(api.calls);
      expect(paramOf(call, "q")).toBeNull();
      expect(paramOf(call, "card_uuid")).toBeNull();
    });
    expect(screen.getByLabelText(/search filenames/i)).toHaveValue("");
  });
});

describe("paging", () => {
  it("steps through pages by offset and reports the position", async () => {
    let offset = 0;
    const api = installApi({
      uploads: (url) => {
        offset = Number(paramOf(url, "offset"));
        return ok(pageFixture([uploadFixture(offset + 1)], { total: 120, offset }));
      },
    });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    expect(screen.getByText(/page 1 of 3/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /previous/i })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(await screen.findByText(/page 2 of 3/i)).toBeInTheDocument();
    expect(paramOf(lastUploadCall(api.calls), "offset")).toBe("50");

    await userEvent.click(screen.getByRole("button", { name: /previous/i }));
    await vi.waitFor(() => {
      expect(paramOf(lastUploadCall(api.calls), "offset")).toBe("0");
    });
  });

  it("hides the pager when everything fits on one page", async () => {
    installApi({ uploads: () => ok(pageFixture([uploadFixture(1)])) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    expect(screen.queryByRole("button", { name: /next/i })).not.toBeInTheDocument();
  });
});

describe("grouped by card", () => {
  it("summarises each card and loads its files only when expanded", async () => {
    const api = installApi({
      summary: () =>
        ok(
          summaryFixture({
            cards: [
              {
                device_id: "raspberrypi-uploader",
                card_uuid: "1234-ABCD",
                file_count: 2,
                total_bytes: 2048,
                oldest_received_at: "2026-07-31T14:01:00Z",
                newest_received_at: "2026-07-31T14:02:00Z",
              },
              {
                device_id: "raspberrypi-uploader",
                card_uuid: "5678-EF01",
                file_count: 1,
                total_bytes: 512,
                oldest_received_at: "2026-07-31T13:00:00Z",
                newest_received_at: "2026-07-31T13:00:00Z",
              },
            ],
          }),
        ),
      uploads: () => ok(pageFixture([uploadFixture(7)])),
    });
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /by card/i }));

    const group = await screen.findByRole("button", { name: /1234-ABCD/ });
    expect(group).toHaveAttribute("aria-expanded", "false");
    expect(group).toHaveTextContent("2 files");
    expect(group).toHaveTextContent("2.0 KB");
    expect(screen.queryByText("logger-0007.csv")).not.toBeInTheDocument();

    await userEvent.click(group);

    expect(await screen.findByText("logger-0007.csv")).toBeInTheDocument();
    expect(group).toHaveAttribute("aria-expanded", "true");
    const call = lastUploadCall(api.calls);
    expect(paramOf(call, "card_uuid")).toBe("1234-ABCD");
    expect(paramOf(call, "device_id")).toBe("raspberrypi-uploader");
  });

  it("drops the columns an expanded group already fixes", async () => {
    installApi({ uploads: () => ok(pageFixture([uploadFixture(7)])) });
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /by card/i }));
    await userEvent.click(await screen.findByRole("button", { name: /A1B2-C3D4/ }));

    await screen.findByText("logger-0007.csv");
    expect(screen.queryByRole("columnheader", { name: /card uuid/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /device/i })).not.toBeInTheDocument();
  });

  it("says so when there are more cards than it will show", async () => {
    installApi({ summary: () => ok(summaryFixture({ cards_truncated: true })) });
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /by card/i }));
    expect(await screen.findByText(/most recently active cards are shown/i)).toBeInTheDocument();
  });
});
