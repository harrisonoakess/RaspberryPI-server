import { useCallback, useEffect, useState } from "react";

import { UnauthorizedError, listUploads, type UploadItem } from "../api";
import { formatBytes } from "../format";
import { PreviewDialog } from "./PreviewDialog";
import { Timestamp } from "./Timestamp";
import styles from "./UploadsPanel.module.css";

interface UploadsPanelProps {
  reloadToken: number;
  onUnauthorized: () => void;
}

export function UploadsPanel({ reloadToken, onUnauthorized }: UploadsPanelProps) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(
    async (beforeId: number | null) => {
      setLoading(true);
      setError(null);
      try {
        const page = await listUploads(beforeId);
        setItems((current) => {
          if (beforeId === null) {
            return page.items;
          }
          // Appending by cursor cannot duplicate, but a repeated click while a
          // request is in flight could; filtering by id makes that impossible.
          const known = new Set(current.map((item) => item.id));
          return [...current, ...page.items.filter((item) => !known.has(item.id))];
        });
        setCursor(page.next_before_id);
        setLoaded(true);
      } catch (caught) {
        if (caught instanceof UnauthorizedError) {
          onUnauthorized();
          return;
        }
        setError("Could not load uploads.");
      } finally {
        setLoading(false);
      }
    },
    [onUnauthorized],
  );

  useEffect(() => {
    setItems([]);
    setCursor(null);
    setLoaded(false);
    void load(null);
  }, [load, reloadToken]);

  const empty = loaded && items.length === 0;

  return (
    <section className={styles.panel} aria-labelledby="uploads-heading">
      <h2 id="uploads-heading" className={styles.heading}>
        Uploads
      </h2>

      {error !== null && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      {loading && items.length === 0 && error === null && (
        <p className={styles.state}>Loading uploads…</p>
      )}

      {empty && error === null && (
        <p className={styles.state}>No uploads have been stored yet.</p>
      )}

      {items.length > 0 && (
        <div className={styles.tableScroll}>
          <table className={styles.table}>
            <caption className={styles.caption}>
              Stored uploads, newest first. {items.length} shown.
            </caption>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Card UUID</th>
                <th scope="col">Device</th>
                <th scope="col">Size</th>
                <th scope="col">Received</th>
                <th scope="col">
                  <span className={styles.visuallyHidden}>Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <th scope="row" className={styles.filename}>
                    {item.filename}
                  </th>
                  <td className={styles.mono}>{item.card_uuid}</td>
                  <td>{item.device_id}</td>
                  <td className={styles.numeric}>{formatBytes(item.size)}</td>
                  <td>
                    <Timestamp utc={item.received_at} />
                  </td>
                  <td>
                    <PreviewDialog upload={item} onUnauthorized={onUnauthorized} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {cursor !== null && (
        <button
          type="button"
          className={styles.loadMore}
          onClick={() => void load(cursor)}
          disabled={loading}
        >
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
    </section>
  );
}
