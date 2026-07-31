import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App } from "../App";
import { installApi, ok, uploadFixture } from "../test/server";

function pageOf(ids: number[], nextBeforeId: number | null) {
  return ok({ items: ids.map((id) => uploadFixture(id)), next_before_id: nextBeforeId });
}

describe("uploads table", () => {
  it("lists uploads newest first with the card that produced them", async () => {
    installApi({
      uploads: () =>
        ok({
          items: [
            uploadFixture(2, { card_uuid: "5678-EF01", filename: "logger-0001.csv" }),
            uploadFixture(1, { card_uuid: "1234-ABCD", filename: "logger-0001.csv" }),
          ],
          next_before_id: null,
        }),
    });
    render(<App />);

    const rows = await screen.findAllByRole("row");
    // Header row plus two data rows.
    expect(rows).toHaveLength(3);
    expect(within(rows[1] as HTMLElement).getByText("5678-EF01")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("1234-ABCD")).toBeInTheDocument();
  });

  it("formats sizes and keeps the exact received timestamp available", async () => {
    installApi({ uploads: () => ok({ items: [uploadFixture(1, { size: 2048 })], next_before_id: null }) });
    render(<App />);

    expect(await screen.findByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByTitle("2026-07-31T14:02:00Z")).toBeInTheDocument();
  });

  it("appends the next page without duplicating rows and then hides the button", async () => {
    let call = 0;
    const api = installApi({
      uploads: () => {
        call += 1;
        return call === 1 ? pageOf([5, 4, 3], 3) : pageOf([2, 1], null);
      },
    });
    render(<App />);

    await screen.findByText("logger-0005.csv");
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    expect(await screen.findByText("logger-0001.csv")).toBeInTheDocument();
    expect(screen.getAllByText("logger-0003.csv")).toHaveLength(1);
    expect(screen.getAllByRole("row")).toHaveLength(6);
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
    expect(api.calls).toContain("GET /dashboard/api/uploads?before_id=3");
  });

  it("hides load more when the first page is the only page", async () => {
    installApi({ uploads: () => pageOf([1], null) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("distinguishes the empty state from an error", async () => {
    installApi({ uploads: () => ok({ items: [], next_before_id: null }) });
    const { unmount } = render(<App />);
    expect(await screen.findByText(/no uploads have been stored yet/i)).toBeInTheDocument();
    unmount();

    installApi({ uploads: () => ({ status: 503, body: { detail: "unavailable" } }) });
    render(<App />);
    expect(await screen.findByText(/could not load uploads/i)).toBeInTheDocument();
    expect(screen.queryByText(/no uploads have been stored yet/i)).not.toBeInTheDocument();
  });

  it("uses semantic table markup with a row header per file", async () => {
    installApi({ uploads: () => pageOf([1], null) });
    render(<App />);

    await screen.findByText("logger-0001.csv");
    expect(screen.getByRole("columnheader", { name: /card uuid/i })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "logger-0001.csv" })).toBeInTheDocument();
  });
});
