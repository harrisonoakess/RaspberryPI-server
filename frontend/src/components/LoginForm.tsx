import { useState, type FormEvent } from "react";

import { ApiError, RateLimitedError, login } from "../api";
import styles from "./LoginForm.module.css";

interface LoginFormProps {
  onSignedIn: () => void;
}

export function LoginForm({ onSignedIn }: LoginFormProps) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(password);
      // The password only ever lived in this component's state.
      setPassword("");
      onSignedIn();
    } catch (caught) {
      if (caught instanceof RateLimitedError) {
        const wait =
          caught.retryAfterSeconds === null
            ? "Please wait before trying again."
            : `Please wait about ${Math.ceil(caught.retryAfterSeconds / 60)} minute(s) before trying again.`;
        setError(`Too many failed attempts. ${wait}`);
      } else if (caught instanceof ApiError) {
        setError(caught.message);
      } else {
        setError("Could not sign in.");
      }
      setSubmitting(false);
    }
  }

  return (
    <main className={styles.screen}>
      <form className={styles.card} onSubmit={handleSubmit} aria-labelledby="login-heading">
        <h1 id="login-heading" className={styles.heading}>
          Pi Dashboard
        </h1>
        <p className={styles.subheading}>Private, read-only. Sign in to continue.</p>

        <label className={styles.label} htmlFor="dashboard-password">
          Dashboard password
        </label>
        <input
          id="dashboard-password"
          className={styles.input}
          type="password"
          name="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          autoFocus
        />

        {error !== null && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}

        <button className={styles.submit} type="submit" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
