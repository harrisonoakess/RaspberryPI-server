import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App } from "../App";
import { installApi, ok, statusFixture, unauthorized } from "../test/server";

describe("login", () => {
  it("shows only the password form to an unauthenticated visitor", async () => {
    installApi({ session: unauthorized });
    render(<App />);

    expect(await screen.findByLabelText(/dashboard password/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /pi connection/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("opens the dashboard after a successful sign in", async () => {
    const api = installApi({ session: unauthorized });
    render(<App />);

    await userEvent.type(
      await screen.findByLabelText(/dashboard password/i),
      "correct horse battery staple",
    );
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("heading", { name: /pi connection/i })).toBeInTheDocument();
    expect(api.calls).toContain("POST /dashboard/api/session");
  });

  it("keeps the password out of the URL and browser storage", async () => {
    installApi({ session: unauthorized });
    render(<App />);

    await userEvent.type(await screen.findByLabelText(/dashboard password/i), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByRole("heading", { name: /pi connection/i });

    expect(window.location.search).toBe("");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("reports an invalid password generically", async () => {
    installApi({
      session: unauthorized,
      login: () => ({ status: 401, body: { detail: "Invalid credentials" } }),
    });
    render(<App />);

    await userEvent.type(await screen.findByLabelText(/dashboard password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid password/i);
    expect(screen.getByLabelText(/dashboard password/i)).toBeInTheDocument();
  });

  it("reports rate limiting with the wait it was given", async () => {
    installApi({
      session: unauthorized,
      login: () => ({
        status: 429,
        body: { detail: "Too many failed attempts" },
        headers: { "Retry-After": "600" },
      }),
    });
    render(<App />);

    await userEvent.type(await screen.findByLabelText(/dashboard password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/too many failed attempts/i);
    expect(alert).toHaveTextContent(/10 minute/i);
  });

  it("signs out back to the login screen", async () => {
    const api = installApi();
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /sign out/i }));

    expect(await screen.findByLabelText(/dashboard password/i)).toBeInTheDocument();
    expect(api.calls).toContain("DELETE /dashboard/api/session");
  });

  it("returns to login and drops dashboard state when a later call is rejected", async () => {
    let authenticated = true;
    installApi({
      status: () => (authenticated ? ok(statusFixture()) : unauthorized()),
      uploads: () =>
        authenticated
          ? ok({ items: [], next_before_id: null })
          : { status: 401, body: { detail: "Not authenticated" } },
    });
    render(<App />);

    await screen.findByText(/online/i);

    authenticated = false;
    await userEvent.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() => {
      expect(screen.getByLabelText(/dashboard password/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/last heartbeat/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("is operable from the keyboard alone", async () => {
    installApi({ session: unauthorized });
    render(<App />);

    const field = await screen.findByLabelText(/dashboard password/i);
    field.focus();
    await userEvent.keyboard("secret");
    await userEvent.tab();

    expect(screen.getByRole("button", { name: /sign in/i })).toHaveFocus();
  });
});
