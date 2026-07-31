import { useCallback, useEffect, useState } from "react";

import { UnauthorizedError, getStatus, type PiStatus } from "../api";
import { Timestamp } from "./Timestamp";
import styles from "./StatusCard.module.css";

const REFRESH_INTERVAL_MS = 60_000;

interface StatusCardProps {
  reloadToken: number;
  onUnauthorized: () => void;
}

type Load =
  | { kind: "loading" }
  | { kind: "ready"; status: PiStatus }
  | { kind: "error"; message: string };

/** Shape and wording carry the state as well as colour does. */
const PRESENTATION = {
  online: { glyph: "●", word: "Online", className: styles.online },
  offline: { glyph: "▲", word: "Offline", className: styles.offline },
  never_seen: { glyph: "○", word: "Never seen", className: styles.neverSeen },
} as const;

export function StatusCard({ reloadToken, onUnauthorized }: StatusCardProps) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });

  const refresh = useCallback(async () => {
    try {
      setLoad({ kind: "ready", status: await getStatus() });
    } catch (caught) {
      if (caught instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      // A failed read is unknown, not offline: saying "offline" here would
      // blame the Pi for a server or network fault.
      setLoad({
        kind: "error",
        message: "Connection status is unavailable. This does not mean the Pi is offline.",
      });
    }
  }, [onUnauthorized]);

  useEffect(() => {
    void refresh();
  }, [refresh, reloadToken]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return (
    <section className={styles.card} aria-labelledby="status-heading">
      <h2 id="status-heading" className={styles.heading}>
        Pi connection
      </h2>
      <div className={styles.body} role="status" aria-live="polite">
        {load.kind === "loading" && <p className={styles.loading}>Checking the last heartbeat…</p>}

        {load.kind === "error" && (
          <p className={`${styles.line} ${styles.unavailable}`}>
            <span className={styles.glyph} aria-hidden="true">
              ?
            </span>
            <span>Status unavailable — {load.message}</span>
          </p>
        )}

        {load.kind === "ready" && <StatusLine status={load.status} />}
      </div>
    </section>
  );
}

function StatusLine({ status }: { status: PiStatus }) {
  const presentation = PRESENTATION[status.status];
  const minutes = Math.round(status.online_window_seconds / 60);

  return (
    <>
      <p className={`${styles.line} ${presentation.className}`}>
        <span className={styles.glyph} aria-hidden="true">
          {presentation.glyph}
        </span>
        <span className={styles.headline}>
          {presentation.word}
          {status.last_seen_at === null ? (
            " — no heartbeat received yet"
          ) : (
            <>
              {" — last heartbeat "}
              <Timestamp utc={status.last_seen_at} />
            </>
          )}
        </span>
      </p>
      <dl className={styles.details}>
        <div className={styles.detail}>
          <dt>Device</dt>
          <dd>{status.device_id ?? "—"}</dd>
        </div>
        <div className={styles.detail}>
          <dt>Server time</dt>
          <dd>
            <Timestamp utc={status.server_time} />
          </dd>
        </div>
      </dl>
      <p className={styles.note}>
        Inferred from heartbeats received in the last {minutes} minutes, not a live connection.
      </p>
    </>
  );
}
