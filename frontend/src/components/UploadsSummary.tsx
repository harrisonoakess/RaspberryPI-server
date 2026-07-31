/** Totals for the rows currently in view, above the table that lists them. */

import type { UploadSummary } from "../api";
import { formatBytes } from "../format";
import { Timestamp } from "./Timestamp";
import styles from "./UploadsSummary.module.css";

interface UploadsSummaryProps {
  summary: UploadSummary | null;
  filtered: boolean;
  error: string | null;
}

export function UploadsSummaryBar({ summary, filtered, error }: UploadsSummaryProps) {
  if (error !== null) {
    return (
      <p className={styles.error} role="alert">
        {error}
      </p>
    );
  }

  if (summary === null) {
    // No skeleton: the tiles are small, and a placeholder number that is later
    // replaced is worse than a moment with nothing to read.
    return <p className={styles.state}>Loading totals…</p>;
  }

  const tiles = [
    { label: filtered ? "Files matched" : "Files", value: String(summary.total_files) },
    { label: "Total size", value: formatBytes(summary.total_bytes) },
    { label: "Cards", value: String(summary.card_count) },
    { label: "Devices", value: String(summary.device_count) },
  ];

  return (
    <div className={styles.summary}>
      <dl className={styles.tiles}>
        {tiles.map((tile) => (
          <div key={tile.label} className={styles.tile}>
            <dt className={styles.tileLabel}>{tile.label}</dt>
            <dd className={styles.tileValue}>{tile.value}</dd>
          </div>
        ))}
      </dl>

      {summary.newest_received_at !== null && summary.oldest_received_at !== null && (
        <p className={styles.range}>
          Newest <Timestamp utc={summary.newest_received_at} /> · Oldest{" "}
          <Timestamp utc={summary.oldest_received_at} />
        </p>
      )}
    </div>
  );
}
