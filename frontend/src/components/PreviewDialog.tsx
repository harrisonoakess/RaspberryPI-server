import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useState } from "react";

import { UnauthorizedError, getPreview, type UploadItem, type UploadPreview } from "../api";
import styles from "./PreviewDialog.module.css";

interface PreviewDialogProps {
  upload: UploadItem;
  onUnauthorized: () => void;
}

type Load =
  | { kind: "loading" }
  | { kind: "ready"; preview: UploadPreview }
  | { kind: "error"; message: string };

/**
 * One dialog per row, owning its own trigger and its own records.
 *
 * Radix handles focus trapping, focus restore to the trigger, Escape, and the
 * modal ARIA roles — the parts of a dialog that are easy to get subtly wrong by
 * hand. Keeping the fetched records inside the dialog is what makes stale
 * records from a previously opened file impossible rather than merely unlikely.
 */
export function PreviewDialog({ upload, onUnauthorized }: PreviewDialogProps) {
  const [open, setOpen] = useState(false);
  const [load, setLoad] = useState<Load>({ kind: "loading" });

  useEffect(() => {
    if (!open) {
      return;
    }
    setLoad({ kind: "loading" });
    let cancelled = false;

    void (async () => {
      try {
        const preview = await getPreview(upload.id);
        if (!cancelled) {
          setLoad({ kind: "ready", preview });
        }
      } catch (caught) {
        if (caught instanceof UnauthorizedError) {
          onUnauthorized();
          return;
        }
        if (!cancelled) {
          setLoad({
            kind: "error",
            message:
              caught instanceof Error && caught.message
                ? caught.message
                : "This file could not be previewed.",
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, upload.id, onUnauthorized]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          className={styles.trigger}
          // Every row's button reads "Preview" on screen, so the accessible
          // name has to say which file it opens.
          aria-label={`Preview ${upload.filename} from card ${upload.card_uuid}`}
        >
          Preview
        </button>
      </Dialog.Trigger>

      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.content}>
          <Dialog.Title className={styles.title}>{upload.filename}</Dialog.Title>
          <Dialog.Description className={styles.description}>
            {`First records of this file, from card ${upload.card_uuid}. Read-only.`}
          </Dialog.Description>

          {load.kind === "loading" && <p className={styles.state}>Loading preview…</p>}

          {load.kind === "error" && (
            <p className={styles.error} role="alert">
              {load.message}
            </p>
          )}

          {load.kind === "ready" && <PreviewBody preview={load.preview} />}

          <Dialog.Close asChild>
            <button type="button" className={styles.close}>
              Close
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function PreviewBody({ preview }: { preview: UploadPreview }) {
  if (preview.records.length === 0) {
    return <p className={styles.state}>This file is empty — it contains no CSV records.</p>;
  }

  const columns = Math.max(...preview.records.map((record) => record.length));

  return (
    <>
      <div className={styles.gridScroll}>
        <table className={styles.grid}>
          <caption className={styles.visuallyHidden}>
            Raw CSV cells. The first record is not assumed to be a header.
          </caption>
          <thead>
            <tr>
              <th scope="col" className={styles.rowNumberHeading}>
                Row
              </th>
              {Array.from({ length: columns }, (_, column) => (
                <th scope="col" key={column}>
                  Column {column + 1}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.records.map((record, index) => (
              // Records have no identity of their own; position is the key.
              <tr key={index}>
                <th scope="row" className={styles.rowNumber}>
                  {index + 1}
                </th>
                {Array.from({ length: columns }, (_, column) => (
                  <td key={column} className={styles.cell}>
                    {record[column] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {preview.truncated && (
        <p className={styles.truncated}>
          Preview truncated. Later records in this file are not shown.
        </p>
      )}
    </>
  );
}
