import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App } from "../App";
import { installApi, ok, previewFixture, uploadFixture } from "../test/server";

function twoUploads() {
  return ok({
    items: [uploadFixture(2, { filename: "second.csv" }), uploadFixture(1, { filename: "first.csv" })],
    next_before_id: null,
  });
}

async function openPreview(name: RegExp) {
  const buttons = await screen.findAllByRole("button", { name });
  await userEvent.click(buttons[0] as HTMLElement);
  return screen.findByRole("dialog");
}

describe("csv preview", () => {
  it("shows the filename, row numbers, and raw cells", async () => {
    installApi({ uploads: twoUploads, preview: () => ok(previewFixture()) });
    render(<App />);

    const dialog = await openPreview(/preview second\.csv/i);

    expect(within(dialog).getByRole("heading", { name: "second.csv" })).toBeInTheDocument();
    expect(within(dialog).getByText("temperature")).toBeInTheDocument();
    expect(within(dialog).getByRole("rowheader", { name: "1" })).toBeInTheDocument();
    expect(within(dialog).getByRole("rowheader", { name: "2" })).toBeInTheDocument();
  });

  it("reports an empty file explicitly", async () => {
    installApi({ uploads: twoUploads, preview: () => ok(previewFixture({ records: [] })) });
    render(<App />);

    const dialog = await openPreview(/preview second\.csv/i);
    expect(within(dialog).getByText(/this file is empty/i)).toBeInTheDocument();
  });

  it("reports truncated output explicitly", async () => {
    installApi({
      uploads: twoUploads,
      preview: () => ok(previewFixture({ truncated: true })),
    });
    render(<App />);

    const dialog = await openPreview(/preview second\.csv/i);
    expect(within(dialog).getByText(/preview truncated/i)).toBeInTheDocument();
  });

  it("shows a safe reason when the file cannot be previewed", async () => {
    installApi({
      uploads: twoUploads,
      preview: () => ({ status: 422, body: { detail: "The stored file is not valid CSV." } }),
    });
    render(<App />);

    const dialog = await openPreview(/preview second\.csv/i);
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/not valid CSV/i);
  });

  it("never shows the previous file's records after switching", async () => {
    installApi({
      uploads: twoUploads,
      preview: (url) =>
        url.includes("/uploads/2/")
          ? ok(previewFixture({ upload_id: 2, records: [["from-second"]] }))
          : ok(previewFixture({ upload_id: 1, records: [["from-first"]] })),
    });
    render(<App />);

    const first = await openPreview(/preview second\.csv/i);
    expect(within(first).getByText("from-second")).toBeInTheDocument();

    await userEvent.click(within(first).getByRole("button", { name: /^close$/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const second = await openPreview(/preview first\.csv/i);
    expect(await within(second).findByText("from-first")).toBeInTheDocument();
    expect(within(second).queryByText("from-second")).not.toBeInTheDocument();
  });

  it("closes with the Escape key and restores focus to the trigger", async () => {
    installApi({ uploads: twoUploads, preview: () => ok(previewFixture()) });
    render(<App />);

    const trigger = (await screen.findAllByRole("button", { name: /preview second\.csv/i }))[0];
    await userEvent.click(trigger as HTMLElement);
    await screen.findByRole("dialog");

    await userEvent.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // Radix restores focus to the control that opened the dialog.
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
