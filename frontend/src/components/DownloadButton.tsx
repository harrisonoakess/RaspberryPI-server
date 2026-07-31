/** One row's download action.
 *
 * The file is fetched rather than linked so an expired session lands on the
 * login screen like every other call, instead of being saved to disk as a file
 * full of JSON under the CSV's name.
 */

import { useState } from "react";

import { UnauthorizedError, downloadUpload, type UploadItem } from "../api";
import { saveBlob } from "../download";
import styles from "./DownloadButton.module.css";

interface DownloadButtonProps {
  upload: UploadItem;
  onUnauthorized: () => void;
}

export function DownloadButton({ upload, onUnauthorized }: DownloadButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      saveBlob(await downloadUpload(upload.id, upload.filename));
    } catch (caught) {
      if (caught instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      setError(caught instanceof Error ? caught.message : "Could not download this file.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className={styles.trigger}
        disabled={busy}
        // Every row's button reads "Download" on screen, so the accessible name
        // has to say which file it saves.
        aria-label={`Download ${upload.filename} from card ${upload.card_uuid}`}
        onClick={() => void run()}
      >
        {busy ? "Downloading…" : "Download"}
      </button>
      {error !== null && (
        <span className={styles.error} role="alert">
          {error}
        </span>
      )}
    </>
  );
}
