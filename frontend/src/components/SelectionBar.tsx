/** What to do with the rows already listed, as opposed to which rows to list.
 *
 * Separate from `UploadsToolbar` because that one shapes the server query and
 * renders in both views, while selection only exists in the flat file list.
 *
 * The caps are checked here as well as on the server. The server's answer is
 * the authoritative one; repeating the check locally is what lets an oversized
 * selection say *why* it is oversized instead of spending a round trip to be
 * told 422.
 */

import { MAX_ARCHIVE_BYTES, MAX_ARCHIVE_FILES } from "../api";
import { formatBytes } from "../format";
import styles from "./SelectionBar.module.css";

interface SelectionBarProps {
  count: number;
  /** Summed from the `size` column of the selected rows on this page. */
  bytes: number;
  busy: boolean;
  onDownload: () => void;
  onClear: () => void;
}

export function SelectionBar({ count, bytes, busy, onDownload, onClear }: SelectionBarProps) {
  const tooMany = count > MAX_ARCHIVE_FILES;
  const tooLarge = bytes > MAX_ARCHIVE_BYTES;

  const reason = tooMany
    ? `An archive may contain at most ${MAX_ARCHIVE_FILES} files.`
    : tooLarge
      ? `An archive may contain at most ${formatBytes(MAX_ARCHIVE_BYTES)}.`
      : null;

  return (
    // Always mounted so the live region exists before its text changes;
    // an element that appears together with its content is announced unreliably.
    <div className={styles.bar}>
      <p className={styles.count} aria-live="polite">
        {count === 0
          ? "No files selected. Selection applies to this page."
          : `${count} ${count === 1 ? "file" : "files"} selected (${formatBytes(bytes)}).`}
      </p>

      {count > 0 && (
        <div className={styles.actions}>
          {reason !== null && (
            <span className={styles.reason} role="alert">
              {reason}
            </span>
          )}
          <button
            type="button"
            className={styles.download}
            disabled={busy || reason !== null}
            onClick={onDownload}
          >
            {busy ? "Preparing…" : `Download selected (${count})`}
          </button>
          <button type="button" className={styles.clear} disabled={busy} onClick={onClear}>
            Clear selection
          </button>
        </div>
      )}
    </div>
  );
}
