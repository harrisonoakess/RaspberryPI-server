import { useCallback, useEffect, useState } from "react";

import { getSession, logout } from "./api";
import { LoginForm } from "./components/LoginForm";
import { StatusCard } from "./components/StatusCard";
import { UploadsPanel } from "./components/UploadsPanel";
import styles from "./App.module.css";

type Auth = "checking" | "anonymous" | "authenticated";

export function App() {
  const [auth, setAuth] = useState<Auth>("checking");
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        await getSession();
        if (!cancelled) {
          setAuth("authenticated");
        }
      } catch {
        if (!cancelled) {
          setAuth("anonymous");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Unmounting the whole dashboard is what guarantees no stale status, upload,
  // or preview data survives a rejected session.
  const handleUnauthorized = useCallback(() => setAuth("anonymous"), []);

  const handleSignedIn = useCallback(() => {
    setReloadToken((token) => token + 1);
    setAuth("authenticated");
  }, []);

  async function handleLogout() {
    await logout();
    setAuth("anonymous");
  }

  if (auth === "checking") {
    return (
      <main className={styles.checking}>
        <p>Loading…</p>
      </main>
    );
  }

  if (auth === "anonymous") {
    return <LoginForm onSignedIn={handleSignedIn} />;
  }

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <h1 className={styles.title}>Pi Dashboard</h1>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.action}
            onClick={() => setReloadToken((token) => token + 1)}
          >
            Refresh
          </button>
          <button type="button" className={styles.action} onClick={() => void handleLogout()}>
            Sign out
          </button>
        </div>
      </header>

      <main className={styles.main}>
        <StatusCard reloadToken={reloadToken} onUnauthorized={handleUnauthorized} />
        <UploadsPanel reloadToken={reloadToken} onUnauthorized={handleUnauthorized} />
      </main>

      <footer className={styles.footer}>
        Read-only. This dashboard cannot upload, edit, or delete anything.
      </footer>
    </div>
  );
}
